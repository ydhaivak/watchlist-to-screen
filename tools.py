"""The tools the harness can run, and the JSON that describes them to the model."""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load .env when running locally; on Cloud Run the vars are already in the environment.
load_dotenv()

# ---------------------------------------------------------------------------
# Weather tool (Open-Meteo, no API key needed)
# ---------------------------------------------------------------------------

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def get_weather(location: str) -> str:
    """Get the current weather for a location."""
    try:
        places = requests.get(GEOCODE_URL, params={"name": location, "count": 1}, timeout=10).json()
        if not places.get("results"):
            return json.dumps({"error": f"City '{location}' was not found."})
        place = places["results"][0]

        current = requests.get(
            FORECAST_URL,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
            },
            timeout=10,
        ).json()["current"]
    except requests.RequestException as e:
        return json.dumps({"error": f"Weather service failed: {e}"})

    return json.dumps({
        "location": place["name"],
        "temp_f": current["temperature_2m"],
        "humidity": current["relative_humidity_2m"],
        "wind_mph": current["wind_speed_10m"],
    })


# ---------------------------------------------------------------------------
# Letterboxd watchlist tool
# ---------------------------------------------------------------------------

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}
MAX_WATCHLIST_PAGES = 20  # ~560 films; prevents hanging on very large watchlists


def get_letterboxd_watchlist(username: str) -> str:
    """Fetch every film from a Letterboxd user's public watchlist."""
    films = []

    for page in range(1, MAX_WATCHLIST_PAGES + 1):
        if page == 1:
            url = f"https://letterboxd.com/{username}/watchlist/"
        else:
            url = f"https://letterboxd.com/{username}/watchlist/page/{page}/"

        try:
            resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=15)
        except requests.Timeout:
            return json.dumps({"error": f"Request timed out on page {page}. Try again."})
        except requests.RequestException as e:
            return json.dumps({"error": f"Network error: {e}"})

        if resp.status_code == 404:
            return json.dumps({
                "error": (
                    f"User '{username}' was not found on Letterboxd. "
                    "The username is the part of the URL after letterboxd.com/ "
                    "(e.g. for letterboxd.com/yegandk the username is 'yegandk')."
                )
            })
        if resp.status_code in (403, 429, 503):
            return json.dumps({
                "error": (
                    f"Letterboxd blocked the request (HTTP {resp.status_code}). "
                    "Ask the user to export their watchlist as a CSV from "
                    "letterboxd.com/settings/data/ and upload it instead."
                )
            })
        if resp.status_code != 200:
            return json.dumps({"error": f"Unexpected HTTP {resp.status_code} from Letterboxd."})

        soup = BeautifulSoup(resp.text, "html.parser")

        if page == 1 and not soup.select_one("ul.filmlist, ul.grid"):
            return json.dumps({
                "error": (
                    f"Could not find a film grid for '{username}'. "
                    "The watchlist may be private or the username may be wrong."
                )
            })

        # Each film is a <li class="griditem"> containing a div with data-item-slug.
        for div in soup.select("li.griditem div[data-item-slug]"):
            name = div.get("data-item-name", "")
            slug = div.get("data-item-slug", "")

            # data-item-name is "Some Title (2024)" or just "Some Title" when year is unknown.
            match = re.match(r"^(.*?)\s+\((\d{4})\)$", name)
            if match:
                title, year = match.group(1), int(match.group(2))
            else:
                title, year = name, None

            films.append({"title": title, "slug": slug, "year": year})

        if not soup.select_one("a.next"):
            break

    return json.dumps({"username": username, "total": len(films), "films": films})


# ---------------------------------------------------------------------------
# TMDB shared helpers: search, caches, and fetch functions
# ---------------------------------------------------------------------------

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG  = "https://image.tmdb.org/t/p"

# _details_cache[tmdb_id] = {title, year, poster_url, runtime, genres}
# Keyed by tmdb_id alone — no region needed for movie metadata.
_details_cache: dict[int, dict] = {}

# _providers_cache[(tmdb_id, region)] = {stream, free, ads, rent, buy, link}
# Each provider list is [{name, logo_url}] — logo_urls kept here for show_films display.
_providers_cache: dict[tuple, dict] = {}


def _tmdb_headers() -> dict | None:
    token = os.environ.get("TMDB_READ_TOKEN")
    if not token:
        return None
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _search_movie(title: str, year: int | None, headers: dict) -> tuple[int | None, str | None]:
    """Search TMDB for a movie. Returns (tmdb_id, note_or_error). tmdb_id is None on failure."""
    params = {"query": title, "include_adult": False}
    if year:
        params["year"] = year

    try:
        resp = requests.get(f"{TMDB_BASE}/search/movie", params=params, headers=headers, timeout=10)
    except requests.Timeout:
        return None, "TMDB search timed out."
    except requests.RequestException as e:
        return None, f"Network error during TMDB search: {e}"

    if resp.status_code == 401:
        return None, "TMDB auth failed — set TMDB_READ_TOKEN in .env."
    if resp.status_code == 429:
        return None, "TMDB rate limit hit. Try again in a moment."
    if resp.status_code != 200:
        return None, f"TMDB search returned HTTP {resp.status_code}."

    results = resp.json().get("results", [])
    if not results:
        hint = f" (year: {year})" if year else ""
        return None, f"'{title}'{hint} not found on TMDB. Check the spelling."

    top = results[0]
    note = None
    if not year and len(results) > 1:
        years_found = {r["release_date"][:4] for r in results if r.get("release_date")}
        if len(years_found) > 1:
            resolved_year = top["release_date"][:4] if top.get("release_date") else "?"
            note = (
                f"Multiple matches; showing top result '{top.get('title')} ({resolved_year})'. "
                "Pass year to disambiguate."
            )

    return top["id"], note


def _fetch_details(tmdb_id: int, headers: dict) -> dict:
    """Fetch movie metadata (title, year, poster, runtime, genres) with caching.

    Uses /movie/{id} only — no providers. Called by show_films.
    Already populated for any film that went through find_where_to_watch.
    """
    if tmdb_id in _details_cache:
        return _details_cache[tmdb_id]

    try:
        resp = requests.get(f"{TMDB_BASE}/movie/{tmdb_id}", headers=headers, timeout=10)
    except requests.Timeout:
        return {"error": "TMDB request timed out."}
    except requests.RequestException as e:
        return {"error": f"Network error: {e}"}

    if resp.status_code == 401:
        return {"error": "TMDB auth failed — set TMDB_READ_TOKEN in .env."}
    if resp.status_code == 429:
        return {"error": "TMDB rate limit hit. Try again in a moment."}
    if resp.status_code == 404:
        return {"error": f"TMDB ID {tmdb_id} not found."}
    if resp.status_code != 200:
        return {"error": f"TMDB returned HTTP {resp.status_code}."}

    data = resp.json()
    result = _parse_details(data)
    _details_cache[tmdb_id] = result
    return result


def _fetch_combined(tmdb_id: int, region: str, headers: dict) -> dict:
    """Fetch movie metadata + providers in one request with caching.

    Uses /movie/{id}?append_to_response=watch/providers.
    Populates both _details_cache and _providers_cache.
    Called by find_where_to_watch.
    """
    prov_key = (tmdb_id, region)

    # Full cache hit
    if tmdb_id in _details_cache and prov_key in _providers_cache:
        details = _details_cache[tmdb_id]
        if "error" in details:
            return details
        return {**details, **_providers_cache[prov_key]}

    try:
        resp = requests.get(
            f"{TMDB_BASE}/movie/{tmdb_id}",
            params={"append_to_response": "watch/providers", "language": "en-US"},
            headers=headers,
            timeout=10,
        )
    except requests.Timeout:
        return {"error": "TMDB request timed out."}
    except requests.RequestException as e:
        return {"error": f"Network error: {e}"}

    if resp.status_code == 401:
        return {"error": "TMDB auth failed — set TMDB_READ_TOKEN in .env."}
    if resp.status_code == 429:
        return {"error": "TMDB rate limit hit. Try again in a moment."}
    if resp.status_code == 404:
        return {"error": f"TMDB ID {tmdb_id} not found."}
    if resp.status_code != 200:
        return {"error": f"TMDB returned HTTP {resp.status_code}."}

    data = resp.json()
    details = _parse_details(data)
    _details_cache[tmdb_id] = details

    region_data = data.get("watch/providers", {}).get("results", {}).get(region)
    providers = _parse_providers(region_data)
    _providers_cache[prov_key] = providers

    return {**details, **providers}


def _parse_details(data: dict) -> dict:
    """Extract movie metadata fields from a raw TMDB /movie/{id} response."""
    poster_path = data.get("poster_path")
    return {
        "title": data.get("title", ""),
        "year": int(data["release_date"][:4]) if data.get("release_date") else None,
        "runtime": data.get("runtime") or None,
        "genres": [g["name"] for g in data.get("genres", [])],
        "poster_url": f"{TMDB_IMG}/w342{poster_path}" if poster_path else None,
    }


def _parse_providers(region_data: dict | None) -> dict:
    """Extract provider lists from a TMDB watch/providers region block.

    Keeps logo_url so show_films can display logos; find_where_to_watch
    strips them when building its output.
    """
    if not region_data:
        return {
            "stream": [], "free": [], "ads": [], "rent": [], "buy": [],
            "link": None,
            "providers_note": (
                "No providers found. Film may be in theaters only, "
                "unreleased, or unavailable in this region."
            ),
        }

    def plist(key: str) -> list[dict]:
        return [
            {
                "name": p["provider_name"],
                "logo_url": f"{TMDB_IMG}/w92{p['logo_path']}" if p.get("logo_path") else None,
            }
            for p in region_data.get(key, [])
        ]

    return {
        "stream": plist("flatrate"),
        "free": plist("free"),
        "ads": plist("ads"),
        "rent": plist("rent"),
        "buy": plist("buy"),
        "link": region_data.get("link"),
    }


def _service_match(query: str, provider_name: str) -> bool:
    """Loose match: 'HBO Max' matches 'Max', 'Criterion Channel' matches 'Criterion'."""
    q = re.sub(r"[^a-z0-9]", "", query.lower())
    p = re.sub(r"[^a-z0-9]", "", provider_name.lower())
    return bool(q and p and (q in p or p in q))


# ---------------------------------------------------------------------------
# find_where_to_watch tool
# ---------------------------------------------------------------------------

MAX_BATCH = 75


def _process_one_film(film: dict, region: str, my_services: list, headers: dict) -> dict:
    """Resolve one film entry to a lean provider result (no poster/logo URLs)."""
    tmdb_id = film.get("tmdb_id")
    search_note = None

    if not tmdb_id:
        title = film.get("title", "")
        year = film.get("year")
        if not title:
            return {"error": "Each film needs either 'tmdb_id' or 'title'."}
        tmdb_id, search_note = _search_movie(title, year, headers)
        if tmdb_id is None:
            return {"query": title, "year": year, "error": search_note}

    data = _fetch_combined(tmdb_id, region, headers)
    if "error" in data:
        return {"tmdb_id": tmdb_id, "title": film.get("title", f"tmdb:{tmdb_id}"), "error": data["error"]}

    # Provider names only — no logo_url. The model doesn't need image URLs.
    def names(key: str) -> list[str]:
        return [p["name"] for p in data.get(key, [])]

    result: dict = {
        "title": data["title"],
        "year": data["year"],
        "tmdb_id": tmdb_id,
        "runtime": data["runtime"],
        "genres": data["genres"],
        "providers": {
            "stream": names("stream"),
            "free": names("free"),
            "ads": names("ads"),
            "rent": names("rent"),
            "buy": names("buy"),
        },
        "link": data["link"],
    }

    notes = [n for n in [data.get("providers_note"), search_note] if n]
    if notes:
        result["note"] = " ".join(notes)

    if my_services:
        all_avail = data.get("stream", []) + data.get("free", []) + data.get("ads", [])
        result["on_my_services"] = [
            p["name"] for p in all_avail
            if any(_service_match(s, p["name"]) for s in my_services)
        ]

    return result


def find_where_to_watch(
    films: list,
    region: str = "US",
    my_services: list | None = None,
) -> str:
    """Look up streaming, rental, and purchase options for a list of films via TMDB/JustWatch.

    Runs up to 8 requests concurrently. Caches results so show_films needs no
    extra requests for films already looked up here.
    """
    headers = _tmdb_headers()
    if not headers:
        return json.dumps({
            "error": "TMDB_READ_TOKEN is not set. Add it to .env (locally) or as an env var (Cloud Run)."
        })

    truncated = False
    if len(films) > MAX_BATCH:
        films = films[:MAX_BATCH]
        truncated = True

    results: list = [None] * len(films)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_process_one_film, film, region, my_services or [], headers): i
            for i, film in enumerate(films)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    out: dict = {"region": region, "results": results}
    if truncated:
        out["note"] = f"Batch was capped at {MAX_BATCH} films."
    return json.dumps(out)


# ---------------------------------------------------------------------------
# show_films tool
# ---------------------------------------------------------------------------

MAX_DISPLAY = 100


def _resolve_for_display(film: dict, headers: dict) -> dict:
    """Resolve one film to display data (poster, runtime, cached stream logos)."""
    tmdb_id = film.get("tmdb_id")
    title = film.get("title", "")
    year = film.get("year")

    if not tmdb_id:
        if not title:
            return {"title": "Unknown", "poster_url": None, "error": "No title or tmdb_id."}
        tmdb_id, note = _search_movie(title, year, headers)
        if tmdb_id is None:
            return {"title": title, "year": year, "poster_url": None, "error": note}

    details = _fetch_details(tmdb_id, headers)
    if "error" in details:
        # Show a placeholder card rather than dropping the film entirely.
        return {
            "title": title or f"tmdb:{tmdb_id}",
            "year": year,
            "tmdb_id": tmdb_id,
            "poster_url": None,
            "error": details["error"],
        }

    result: dict = {
        "title": details["title"],
        "year": details["year"],
        "tmdb_id": tmdb_id,
        "poster_url": details["poster_url"],
        "runtime": details["runtime"],
    }

    if film.get("slug"):
        result["slug"] = film["slug"]

    # Include stream provider logos if already in cache (from find_where_to_watch).
    # Default to US region; no extra requests are made.
    cached = _providers_cache.get((tmdb_id, "US"))
    if cached and not cached.get("providers_note"):
        result["stream"] = cached["stream"]  # [{name, logo_url}]

    return result


def show_films(films: list) -> str:
    """Resolve a list of films to display data and return it for the UI to render as poster cards."""
    headers = _tmdb_headers()
    if not headers:
        return json.dumps({
            "error": "TMDB_READ_TOKEN is not set. Add it to .env (locally) or as an env var (Cloud Run)."
        })

    if len(films) > MAX_DISPLAY:
        films = films[:MAX_DISPLAY]

    resolved: list = [None] * len(films)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_resolve_for_display, film, headers): i
            for i, film in enumerate(films)
        }
        for future in as_completed(futures):
            resolved[futures[future]] = future.result()

    displayed = sum(1 for r in resolved if not r.get("error"))
    return json.dumps({"displayed": displayed, "films": resolved})


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather (temperature, humidity, wind) for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name, e.g. 'New York'"},
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_letterboxd_watchlist",
            "description": (
                "Fetch all films on a Letterboxd user's public watchlist. "
                "Call this whenever the user asks about someone's Letterboxd watchlist, "
                "what films they want to watch, or to compare two watchlists. "
                "Returns the username, total film count, and a list of films with "
                "title, Letterboxd slug, and release year. "
                "The username is the segment of the Letterboxd URL directly after "
                "letterboxd.com/ — for example, for letterboxd.com/yegandk the "
                "username is 'yegandk'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": (
                            "Letterboxd username (the part after letterboxd.com/). "
                            "Do not include slashes or the full URL."
                        ),
                    },
                },
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_where_to_watch",
            "description": (
                "Look up where to stream, rent, or buy a list of movies using TMDB/JustWatch data. "
                "Call this after getting a watchlist, or whenever the user asks where to watch specific films. "
                "Pass tmdb_ids whenever you have them (from a prior call) — it skips a search round-trip. "
                "Always batch: pass all films in one call rather than one film at a time. "
                "Returns per film: title, year, tmdb_id, runtime, genres, "
                "providers grouped as stream/free/ads/rent/buy (provider names), and the JustWatch link. "
                "If my_services is given, also returns on_my_services per film."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "films": {
                        "type": "array",
                        "description": (
                            "List of films to look up. Each item is an object with: "
                            "tmdb_id (int, preferred when available), "
                            "title (string, required if no tmdb_id), "
                            "year (int, optional but helps disambiguate searches)."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "tmdb_id": {"type": "integer"},
                                "title": {"type": "string"},
                                "year": {"type": "integer"},
                            },
                        },
                    },
                    "region": {
                        "type": "string",
                        "description": "ISO 3166-1 alpha-2 country code for provider availability. Default 'US'.",
                        "default": "US",
                    },
                    "my_services": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of streaming services the user subscribes to "
                            "(e.g. ['Netflix', 'Max', 'Criterion Channel']). "
                            "When provided, each film result includes an 'on_my_services' field. "
                            "Names are matched loosely."
                        ),
                    },
                },
                "required": ["films"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_films",
            "description": (
                "Display poster cards for a list of films in the UI. "
                "Call this LAST, once per response, with only the films your answer actually mentions "
                "or recommends — not every film you checked. "
                "For watchlist queries, pass ALL films if the list has 100 or fewer; "
                "for longer lists pass the first 100. "
                "For other questions (e.g. recommendations, where-to-watch), pass only the films you mention. "
                "If you already called find_where_to_watch, pass the same tmdb_ids — no extra requests needed. "
                "Returns a short confirmation ('Displayed N films') for you to acknowledge."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "films": {
                        "type": "array",
                        "description": (
                            "Films to display. Each item: "
                            "tmdb_id (int, preferred), "
                            "title (string, required if no tmdb_id), "
                            "year (int, optional), "
                            "slug (string, optional Letterboxd slug)."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "tmdb_id": {"type": "integer"},
                                "title": {"type": "string"},
                                "year": {"type": "integer"},
                                "slug": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["films"],
            },
        },
    },
]

TOOL_MAP = {
    "get_weather": get_weather,
    "get_letterboxd_watchlist": get_letterboxd_watchlist,
    "find_where_to_watch": find_where_to_watch,
    "show_films": show_films,
}


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}"})


# ---------------------------------------------------------------------------
# Smoke tests — run with: python tools.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    def hr(label):
        print(f"\n{'='*60}\n{label}\n{'='*60}")

    # 1. show_films: Heat by tmdb_id (no prior find_where_to_watch — should fetch details)
    hr("show_films — Heat by tmdb_id, cold cache")
    r = json.loads(show_films([{"tmdb_id": 949}]))
    f = r["films"][0]
    print(f"displayed: {r['displayed']}")
    print(f"  {f['title']} ({f['year']}) {f['runtime']}min poster={bool(f['poster_url'])}")
    print(f"  stream from cache: {f.get('stream', 'not cached')}")

    # 2. find_where_to_watch + show_films: Paris, Texas — providers cached for show_films
    hr("find_where_to_watch then show_films — Paris, Texas")
    wtr = json.loads(find_where_to_watch([{"title": "Paris, Texas", "year": 1984}]))
    wf = wtr["results"][0]
    print(f"  providers (names only, no logos): stream={wf['providers']['stream']}")
    print(f"  poster_url in find output: {'poster_url' in wf}")  # should be False

    sf = json.loads(show_films([{"tmdb_id": wf["tmdb_id"]}]))
    f2 = sf["films"][0]
    print(f"  show_films poster={bool(f2['poster_url'])}")
    print(f"  show_films stream from cache: {[p['name'] for p in f2.get('stream', [])]}")
    assert f2.get("stream"), "stream should be in cache from find_where_to_watch"

    # 3. show_films: nonsense title — should return placeholder
    hr("show_films — nonsense title (placeholder)")
    r = json.loads(show_films([{"title": "xyzzy_does_not_exist_9999"}]))
    print(r["films"][0])

    # 4. show_films: Letterboxd-style films (title+year, no tmdb_id)
    hr("show_films — 5 films from yegandk watchlist")
    wl = json.loads(get_letterboxd_watchlist("yegandk"))
    sample = wl["films"][:5]
    r = json.loads(show_films(sample))
    print(f"displayed: {r['displayed']}/{len(sample)}")
    for f in r["films"]:
        print(f"  {f['title']} ({f.get('year')}) poster={bool(f.get('poster_url'))}")
