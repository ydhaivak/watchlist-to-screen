"""The tools the harness can run, and the JSON that describes them to the model."""

import datetime
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load .env when running locally; on Cloud Run the vars are already in the environment.
load_dotenv()


# ---------------------------------------------------------------------------
# Letterboxd shared infrastructure
# ---------------------------------------------------------------------------

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    # Sec-Fetch-* headers signal a real browser navigation; required to pass Cloudflare on /films/page/N/
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}

CACHE_TTL = 60 * 60  # seconds — watchlists and ratings cached for 1 hour

# username.lower() → (fetched_at_timestamp, data)
_watchlist_store: dict[str, tuple[float, list]] = {}
_ratings_store:  dict[str, tuple[float, dict]] = {}


def _lb_fetch(
    url: str,
    session: requests.Session | None = None,
) -> tuple[BeautifulSoup | None, str | None]:
    """Fetch one Letterboxd page.  Retries on 429/503 with backoff; logs to stderr.

    Returns:
      (soup, None)        on HTTP 200
      (None, None)        on HTTP 404  — caller decides how to handle
      (None, error_json)  on any other error (after retries exhausted)
    """
    getter = session or requests
    backoffs = [2, 5, 10]

    for attempt in range(len(backoffs) + 1):
        if attempt > 0:
            delay = backoffs[attempt - 1]
            print(f"[lb] retry {attempt} for {url} after {delay}s", file=sys.stderr, flush=True)
            time.sleep(delay)

        try:
            resp = getter.get(url, headers=_BROWSER_HEADERS, timeout=15)
        except requests.Timeout:
            return None, json.dumps({"error": "Request timed out. Try again."})
        except requests.RequestException as e:
            return None, json.dumps({"error": f"Network error: {e}"})

        print(f"[lb] HTTP {resp.status_code}  {url}", file=sys.stderr, flush=True)

        if resp.status_code == 200:
            return BeautifulSoup(resp.text, "html.parser"), None
        if resp.status_code == 404:
            return None, None
        if resp.status_code in (429, 503) and attempt < len(backoffs):
            continue  # retry with backoff

        # 403 (bot protection) or exhausted retries
        if resp.status_code in (403, 429, 503):
            return None, json.dumps({
                "error": (
                    f"Letterboxd blocked the request (HTTP {resp.status_code}). "
                    "Ask the user to export their data from letterboxd.com/settings/data/ "
                    "and upload the CSV instead."
                ),
                "_status": resp.status_code,
            })
        return None, json.dumps({"error": f"Unexpected HTTP {resp.status_code} from Letterboxd."})

    return None, json.dumps({"error": "Failed after retries."})  # unreachable but satisfies type checker


def _parse_film_name(name: str) -> tuple[str, int | None]:
    """'Some Title (2024)' → ('Some Title', 2024). No year → (name, None)."""
    m = re.match(r"^(.*?)\s+\((\d{4})\)$", name)
    return (m.group(1), int(m.group(2))) if m else (name, None)


# ---------------------------------------------------------------------------
# get_letterboxd_watchlist
# ---------------------------------------------------------------------------

MAX_WATCHLIST_PAGES = 20  # ~560 films max


def _do_fetch_watchlist(username: str) -> tuple[list | None, str | None]:
    """Scrape all watchlist pages sequentially using a session.

    A persistent session with Referer threading lets us paginate through
    Letterboxd without triggering Cloudflare bot protection.
    """
    films = []
    session = requests.Session()
    base_url = f"https://letterboxd.com/{username}/watchlist/"

    for page in range(1, MAX_WATCHLIST_PAGES + 1):
        url = base_url if page == 1 else f"{base_url}page/{page}/"
        if page > 1:
            session.headers["Referer"] = f"{base_url}page/{page - 1}/" if page > 2 else base_url
            time.sleep(1.0)

        soup, err = _lb_fetch(url, session=session)
        if err:
            return None, err
        if soup is None:  # 404
            if page == 1:
                return None, json.dumps({
                    "error": (
                        f"User '{username}' was not found on Letterboxd. "
                        "The username is the part after letterboxd.com/ "
                        "(e.g. letterboxd.com/yegandk → username is 'yegandk')."
                    )
                })
            break  # went past the last page

        if page == 1 and not soup.select_one("ul.filmlist, ul.grid"):
            return None, json.dumps({
                "error": (
                    f"Could not find a film grid for '{username}'. "
                    "The watchlist may be private or the username may be wrong."
                )
            })

        for div in soup.select("li.griditem div[data-item-slug]"):
            title, year = _parse_film_name(div.get("data-item-name", ""))
            slug = div.get("data-item-slug", "")
            films.append({"title": title, "slug": slug, "year": year})

        if not soup.select_one("a.next"):
            break

    return films, None


def _cached_watchlist(username: str) -> tuple[list | None, str | None]:
    """Return watchlist films from cache (30 min TTL) or fetch fresh."""
    key = username.lower()
    entry = _watchlist_store.get(key)
    if entry and time.time() - entry[0] < CACHE_TTL:
        return entry[1], None

    films, err = _do_fetch_watchlist(username)
    if err:
        return None, err

    _watchlist_store[key] = (time.time(), films)
    return films, None


def get_letterboxd_watchlist(username: str) -> str:
    """Fetch every film from a Letterboxd user's public watchlist."""
    films, err = _cached_watchlist(username)
    if err:
        return err
    return json.dumps({"username": username, "total": len(films), "films": films})


# ---------------------------------------------------------------------------
# get_letterboxd_ratings
# ---------------------------------------------------------------------------

MAX_RATINGS_PAGES = 50   # up to ~3600 films
FILMS_PER_PAGE    = 72   # Letterboxd shows 72 films per page on /films/
RATINGS_WORKERS   = 2    # low concurrency so Letterboxd doesn't rate-limit us


def _parse_ratings_page(soup: BeautifulSoup) -> list[dict]:
    """Extract film entries from one /films/ page.

    Each entry: {slug, title, year, rating (0.5–5 or None), liked (bool)}
    Rating comes from <span class="rating rated-N"> where N/2 = stars.
    Liked comes from presence of <span class="icon-liked">.
    """
    films = []
    for li in soup.select("li.griditem"):
        div = li.select_one("div[data-item-slug]")
        if not div:
            continue

        slug = div.get("data-item-slug", "")
        if not slug:
            continue

        title, year = _parse_film_name(div.get("data-item-name", ""))

        rating = None
        liked = False
        p = li.select_one("p.poster-viewingdata")
        if p:
            # Find a span whose class list contains something like "rated-6"
            for span in p.find_all("span", class_="rating"):
                for cls in span.get("class", []):
                    if cls.startswith("rated-"):
                        try:
                            rating = int(cls[6:]) / 2.0  # rated-6 → 3.0 stars
                        except ValueError:
                            pass
                        break
            liked = p.select_one("span.icon-liked") is not None

        films.append({"slug": slug, "title": title, "year": year, "rating": rating, "liked": liked})

    return films


def _fetch_all_ratings(username: str) -> tuple[list | None, int | None, str | None]:
    """Fetch all /films/ pages sequentially using a session.

    A persistent session with Referer threading bypasses the Cloudflare bot
    protection that returns HTTP 403 on /films/page/N/ for stateless requests.

    Returns (all_films, total_count, error_json).
    On any page failure after retries, returns an error so callers never
    silently compute on partial data.
    """
    base_url = f"https://letterboxd.com/{username}/films/"
    session = requests.Session()

    # Page 1 — also gives us the total and max page count.
    soup1, err = _lb_fetch(base_url, session=session)
    if err:
        return None, None, err
    if soup1 is None:
        return None, None, json.dumps({
            "error": (
                f"User '{username}' was not found on Letterboxd. "
                "The username is the part after letterboxd.com/."
            )
        })

    if not soup1.select("li.griditem"):
        return None, None, json.dumps({
            "error": (
                f"No films found for '{username}'. "
                "The profile may be private or no films have been logged."
            )
        })

    # Total watched count from the heading tooltip: title="522\xa0films"
    total = None
    tooltip = soup1.select_one("h1 span.tooltip[title]")
    if tooltip:
        m = re.search(r"([\d,]+)", tooltip["title"].replace("\xa0", " "))
        if m:
            total = int(m.group(1).replace(",", ""))

    if total:
        max_page = min(math.ceil(total / FILMS_PER_PAGE), MAX_RATINGS_PAGES)
    else:
        max_page = 1
        for a in soup1.select("div.paginate-pages a, ul.paginate-pages a"):
            try:
                max_page = max(max_page, int(a.text.strip()))
            except ValueError:
                pass
        max_page = min(max_page, MAX_RATINGS_PAGES)

    all_films = _parse_ratings_page(soup1)

    for page_num in range(2, max_page + 1):
        url = f"{base_url}page/{page_num}/"
        prev_url = f"{base_url}page/{page_num - 1}/" if page_num > 2 else base_url
        session.headers["Referer"] = prev_url
        time.sleep(1.0)

        soup, err = _lb_fetch(url, session=session)
        if err:
            return None, None, err  # propagate — never compute on partial data
        if soup is None:  # 404 past the last page
            break
        all_films.extend(_parse_ratings_page(soup))

    return all_films, total, None


def _build_ratings_summary(username: str, data: dict) -> dict:
    """Build the compact model-facing summary from full ratings data."""
    films  = data["films"]
    rated  = [f for f in films if f["rating"] is not None]
    liked  = [f for f in films if f["liked"]]

    dist: dict[str, int] = {}
    for f in rated:
        k = str(f["rating"])
        dist[k] = dist.get(k, 0) + 1

    top_rated = sorted(rated, key=lambda f: (f["rating"], f["liked"]), reverse=True)[:15]

    return {
        "username": username,
        "total_watched": data["total"],
        "num_rated": len(rated),
        "avg_rating": round(sum(f["rating"] for f in rated) / len(rated), 2) if rated else None,
        "rating_distribution": dist,
        "top_rated": [
            {"title": f["title"], "year": f["year"], "slug": f["slug"], "rating": f["rating"]}
            for f in top_rated
        ],
        "recent_liked": [
            {"title": f["title"], "year": f["year"], "slug": f["slug"]}
            for f in liked[:15]
        ],
    }


def _cached_ratings(username: str) -> tuple[dict | None, str | None]:
    """Return full ratings data from cache (30 min TTL) or fetch fresh."""
    key = username.lower()
    entry = _ratings_store.get(key)
    if entry and time.time() - entry[0] < CACHE_TTL:
        return entry[1], None

    films, total, err = _fetch_all_ratings(username)
    if err:
        return None, err

    data = {"total": total or len(films), "films": films}
    _ratings_store[key] = (time.time(), data)
    return data, None


def get_letterboxd_ratings(username: str) -> str:
    """Fetch a user's full Letterboxd film log and return a compact summary."""
    data, err = _cached_ratings(username)
    if err:
        return err
    return json.dumps(_build_ratings_summary(username, data))


# ---------------------------------------------------------------------------
# compare_letterboxd_users
# ---------------------------------------------------------------------------

def _fetch_profile(username: str) -> dict:
    """Fetch display name and avatar URL for a Letterboxd user.

    Strategy:
    - Display name: RSS feed channel title ("Letterboxd - <name>") — accessible even when
      the profile page returns 403.
    - Avatar URL: /films/ page, first img with a.ltrbxd.com/resized/avatar in its src.

    Returns a dict with keys display_name (str) and avatar_url (str | None).
    Falls back to username / None on any error so the compare never fails.
    """
    display_name = username
    avatar_url = None

    # --- Display name from RSS ---
    try:
        resp = requests.get(
            f"https://letterboxd.com/{username}/rss/",
            headers=_BROWSER_HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            import warnings
            from bs4 import XMLParsedAsHTMLWarning
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
                soup_rss = BeautifulSoup(resp.text, "html.parser")
            title_el = soup_rss.find("title")
            if title_el:
                m = re.match(r"^Letterboxd\s*-\s*(.+)$", title_el.get_text(strip=True), re.IGNORECASE)
                if m:
                    display_name = m.group(1).strip()
    except Exception:
        pass

    # --- Avatar from /films/ page (which returns 200 unlike the profile page) ---
    soup_films, _ = _lb_fetch(f"https://letterboxd.com/{username}/films/")
    if soup_films:
        for img in soup_films.find_all("img"):
            src = img.get("src", "")
            if "a.ltrbxd.com/resized/avatar" in src:
                avatar_url = src
                break

    return {"display_name": display_name, "avatar_url": avatar_url}


def _compatibility(films_a: list[dict], films_b: list[dict]) -> dict:
    """Score 0–100 based on how closely two users' ratings agree.

    Formula: score = max(0, round(100 × (1 – mean_abs_diff / 1.5)))
    A mean difference of 0 → 100 (perfect agreement).
    A mean difference of ≥ 1.5 stars → 0 (clamped).
    Low-confidence flag when fewer than 10 shared ratings.
    """
    ra = {f["slug"]: f["rating"] for f in films_a if f["rating"] is not None}
    rb = {f["slug"]: f["rating"] for f in films_b if f["rating"] is not None}
    common = set(ra) & set(rb)

    if not common:
        return {
            "score": None, "based_on": 0, "mean_abs_diff": None,
            "a_higher": 0, "exact_agree": 0, "b_higher": 0,
            "confidence": "none — no films rated by both users",
        }

    diffs      = [abs(ra[s] - rb[s]) for s in common]
    mean_diff  = sum(diffs) / len(diffs)
    score      = max(0, round(100 * (1 - mean_diff / 1.5)))
    a_higher   = sum(1 for s in common if ra[s] > rb[s])
    exact_agree= sum(1 for s in common if ra[s] == rb[s])
    b_higher   = sum(1 for s in common if ra[s] < rb[s])
    confidence = "high" if len(common) >= 10 else f"low — only {len(common)} shared ratings"

    return {
        "score":        score,
        "based_on":     len(common),
        "mean_abs_diff": round(mean_diff, 2),
        "a_higher":     a_higher,
        "exact_agree":  exact_agree,
        "b_higher":     b_higher,
        "confidence":   confidence,
    }


def _search_and_get_poster(title: str, year: int | None, headers: dict) -> str | None:
    """Search TMDB by title/year and return the poster URL. Returns None on any failure."""
    tmdb_id, _ = _search_movie(title, year, headers)
    if not tmdb_id:
        return None
    details = _fetch_details(tmdb_id, headers)
    return details.get("poster_url")


def compare_letterboxd_users(user_a: str, user_b: str) -> str:
    """Compare two Letterboxd users' watchlists and ratings."""
    # --- Fetch watchlists (sequential) ---
    wl_a, err = _cached_watchlist(user_a)
    if err:
        return err
    wl_b, err = _cached_watchlist(user_b)
    if err:
        return err

    # --- Fetch ratings + profiles concurrently ---
    with ThreadPoolExecutor(max_workers=4) as pool:
        fut_ra = pool.submit(_cached_ratings, user_a)
        fut_rb = pool.submit(_cached_ratings, user_b)
        fut_pa = pool.submit(_fetch_profile,  user_a)
        fut_pb = pool.submit(_fetch_profile,  user_b)
        rd_a, err_a  = fut_ra.result()
        rd_b, err_b  = fut_rb.result()
        prof_meta_a  = fut_pa.result()
        prof_meta_b  = fut_pb.result()

    if err_a:
        return err_a
    if err_b:
        return err_b

    # --- Slug-keyed lookups ---
    slugs_wl_a = {f["slug"]: f for f in wl_a}
    slugs_wl_b = {f["slug"]: f for f in wl_b}
    slugs_rd_a = {f["slug"]: f for f in rd_a["films"]}
    slugs_rd_b = {f["slug"]: f for f in rd_b["films"]}

    def _profile_stats(rd: dict, meta: dict) -> dict:
        films = rd["films"]
        rated = [f for f in films if f["rating"] is not None]
        avg   = round(sum(f["rating"] for f in rated) / len(rated), 2) if rated else None
        return {
            "display_name": meta["display_name"],
            "avatar_url":   meta["avatar_url"],
            "watched":      rd["total"],
            "rated":        len(rated),
            "avg_rating":   avg,
        }

    profile_a = _profile_stats(rd_a, prof_meta_a)
    profile_b = _profile_stats(rd_b, prof_meta_b)

    # --- Venn (all logged films, rated or not) ---
    set_a = set(slugs_rd_a)
    set_b = set(slugs_rd_b)
    both_seen_count   = len(set_a & set_b)
    venn_both_seen    = both_seen_count  # alias kept for clarity in output

    # --- Compatibility ---
    compat = _compatibility(rd_a["films"], rd_b["films"])
    compatibility = {
        "score":         compat["score"],
        "based_on":      compat["based_on"],
        "mean_abs_diff": compat["mean_abs_diff"],
        "confidence":    compat["confidence"],
    }

    # --- Both-seen films: top 12 sorted by combined rating (prefer both rated highly) ---
    BOTH_SEEN_DISPLAY = 12
    both_seen_all = []
    for slug in set_a & set_b:
        fa = slugs_rd_a[slug]
        fb = slugs_rd_b[slug]
        ra_val = fa.get("rating")
        rb_val = fb.get("rating")
        both_seen_all.append({
            "title":    fa["title"],
            "year":     fa["year"],
            "slug":     slug,
            "rating_a": ra_val,
            "rating_b": rb_val,
            # sort key: prefer films both rated, then by combined rating desc
            "_sort": (ra_val is not None and rb_val is not None, (ra_val or 0) + (rb_val or 0)),
        })
    both_seen_all.sort(key=lambda x: x["_sort"], reverse=True)
    both_seen_films = [
        {k: v for k, v in f.items() if not k.startswith("_")}
        for f in both_seen_all[:BOTH_SEEN_DISPLAY]
    ]

    # --- Shared watchlist (cap at 20) ---
    shared_wl = [
        {"title": slugs_wl_a[s]["title"], "year": slugs_wl_a[s]["year"], "slug": s}
        for s in set(slugs_wl_a) & set(slugs_wl_b)
    ][:20]

    # --- Resolve poster URLs for both_seen_films + shared_wl (cap at 32 TMDB lookups) ---
    tmdb_headers = _tmdb_headers()
    if tmdb_headers:
        to_resolve: dict[str, tuple] = {}
        for film in both_seen_films + shared_wl:
            slug = film.get("slug", "")
            if slug and slug not in to_resolve and len(to_resolve) < 32:
                to_resolve[slug] = (film.get("title", ""), film.get("year"))

        poster_map: dict[str, str | None] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(_search_and_get_poster, t, y, tmdb_headers): slug
                for slug, (t, y) in to_resolve.items()
            }
            for future in as_completed(futures):
                poster_map[futures[future]] = future.result()

        for f in both_seen_films + shared_wl:
            f["poster_url"] = poster_map.get(f.get("slug", ""))

    return json.dumps({
        "user_a": user_a,
        "user_b": user_b,
        "profile_a":      profile_a,
        "profile_b":      profile_b,
        "both_seen":      venn_both_seen,
        "compatibility":  compatibility,
        "both_seen_films": both_seen_films,
        "both_seen_total": both_seen_count,
        "shared_watchlist": shared_wl,
    })


# ---------------------------------------------------------------------------
# TMDB shared helpers: search, caches, and fetch functions
# ---------------------------------------------------------------------------

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG  = "https://image.tmdb.org/t/p"

# _details_cache[tmdb_id]          = {title, year, poster_url, runtime, genres}
# _providers_cache[(tmdb_id,region)] = {stream, free, ads, rent, buy, link}
_details_cache:   dict[int,   dict] = {}
_providers_cache: dict[tuple, dict] = {}


def _tmdb_headers() -> dict | None:
    token = os.environ.get("TMDB_READ_TOKEN")
    if not token:
        return None
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _search_movie(title: str, year: int | None, headers: dict) -> tuple[int | None, str | None]:
    """Search TMDB. Returns (tmdb_id, note_or_error). tmdb_id is None on failure."""
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
    """Fetch metadata for one film (/movie/{id}), with caching."""
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

    result = _parse_details(resp.json())
    _details_cache[tmdb_id] = result
    return result


def _fetch_combined(tmdb_id: int, region: str, headers: dict) -> dict:
    """One request: /movie/{id}?append_to_response=watch/providers. Populates both caches."""
    prov_key = (tmdb_id, region)
    if tmdb_id in _details_cache and prov_key in _providers_cache:
        d = _details_cache[tmdb_id]
        return d if "error" in d else {**d, **_providers_cache[prov_key]}

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
    poster_path = data.get("poster_path")
    return {
        "title":      data.get("title", ""),
        "year":       int(data["release_date"][:4]) if data.get("release_date") else None,
        "runtime":    data.get("runtime") or None,
        "genres":     [g["name"] for g in data.get("genres", [])],
        "poster_url": f"{TMDB_IMG}/w342{poster_path}" if poster_path else None,
    }


def _parse_providers(region_data: dict | None) -> dict:
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
        "free":   plist("free"),
        "ads":    plist("ads"),
        "rent":   plist("rent"),
        "buy":    plist("buy"),
        "link":   region_data.get("link"),
    }


def _service_match(query: str, provider_name: str) -> bool:
    q = re.sub(r"[^a-z0-9]", "", query.lower())
    p = re.sub(r"[^a-z0-9]", "", provider_name.lower())
    return bool(q and p and (q in p or p in q))


# ---------------------------------------------------------------------------
# find_in_theaters
# ---------------------------------------------------------------------------

THEATERS_TTL = 3 * 60 * 60  # 3 hours — theater lineups change slowly

# region.upper() → (fetched_at_timestamp, {"now_playing": [...], "upcoming": [...]})
_theaters_cache: dict[str, tuple[float, dict]] = {}


def _normalize_title(title: str) -> str:
    """Lowercase, strip leading 'the', remove non-alphanumeric — for fuzzy matching."""
    t = title.lower().strip()
    t = re.sub(r"^the\s+", "", t)
    t = re.sub(r"[^a-z0-9]", "", t)
    return t


def _fetch_theater_pages(
    endpoint: str, region: str, headers: dict, max_pages: int = 3
) -> tuple[list[dict], str | None]:
    """Fetch up to max_pages from /movie/now_playing or /movie/upcoming.

    Returns (films, error_json). error_json is set only on a hard failure
    (auth error, network error); an empty result set returns ([], None).
    """
    films: list[dict] = []
    for page in range(1, max_pages + 1):
        try:
            resp = requests.get(
                f"{TMDB_BASE}/{endpoint}",
                params={"region": region, "page": page, "language": "en-US"},
                headers=headers,
                timeout=10,
            )
        except requests.Timeout:
            return films, json.dumps({"error": "TMDB request timed out fetching theater listings."})
        except requests.RequestException as e:
            return films, json.dumps({"error": f"Network error fetching theater listings: {e}"})

        if resp.status_code == 401:
            return [], json.dumps({"error": "TMDB auth failed — set TMDB_READ_TOKEN in .env."})
        if resp.status_code == 429:
            return [], json.dumps({"error": "TMDB rate limit hit. Try again in a moment."})
        if resp.status_code != 200:
            # Non-fatal: treat unexpected status as end of pages
            break

        data = resp.json()
        films.extend(data.get("results", []))
        if page >= data.get("total_pages", 1):
            break
    return films, None


def _parse_theater_film(f: dict, status_override: str | None = None) -> dict:
    """Convert a raw TMDB result to our lean theater-film dict."""
    today = datetime.date.today()
    release_str = f.get("release_date", "")
    year = int(release_str[:4]) if release_str else None

    if status_override:
        status = status_override
    elif release_str:
        rel = datetime.date.fromisoformat(release_str)
        status = "in theaters" if rel <= today else f"opens {release_str}"
    else:
        status = "in theaters"

    return {
        "tmdb_id": f["id"],
        "title":   f.get("title", ""),
        "year":    year,
        "release_date": release_str,
        "status":  status,
        "normalized": _normalize_title(f.get("title", "")),
    }


def _get_theater_data(region: str, headers: dict) -> tuple[dict | None, str | None]:
    """Return cached or fresh now_playing + upcoming for a region."""
    key = region.upper()
    entry = _theaters_cache.get(key)
    if entry and time.time() - entry[0] < THEATERS_TTL:
        return entry[1], None

    raw_np, err_np = _fetch_theater_pages("movie/now_playing", region, headers)
    if err_np:
        return None, err_np
    raw_up, err_up = _fetch_theater_pages("movie/upcoming", region, headers)
    if err_up:
        return None, err_up

    if not raw_np and not raw_up:
        return None, json.dumps({"error": "TMDB returned no theater listings for this region."})

    now_playing = [_parse_theater_film(f, "in theaters") for f in raw_np]
    upcoming    = [_parse_theater_film(f)                for f in raw_up]

    # Deduplicate upcoming vs now_playing (TMDB sometimes overlaps)
    np_ids = {f["tmdb_id"] for f in now_playing}
    upcoming = [f for f in upcoming if f["tmdb_id"] not in np_ids]

    data = {"now_playing": now_playing, "upcoming": upcoming}
    _theaters_cache[key] = (time.time(), data)
    return data, None


def _check_release_dates(tmdb_id: int, region: str, headers: dict) -> str | None:
    """Check /movie/{id}/release_dates for a theatrical release in region.

    Returns "in theaters", "opens YYYY-MM-DD", or None if not found.
    Covers limited and international releases not in now_playing/upcoming.
    """
    today = datetime.date.today()
    cutoff_past   = today - datetime.timedelta(days=45)
    cutoff_future = today + datetime.timedelta(days=30)

    try:
        resp = requests.get(
            f"{TMDB_BASE}/movie/{tmdb_id}/release_dates",
            headers=headers,
            timeout=10,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None

    region_entry = next(
        (r for r in resp.json().get("results", []) if r.get("iso_3166_1") == region.upper()),
        None,
    )
    if not region_entry:
        return None

    for rel in region_entry.get("release_dates", []):
        if rel.get("type") != 3:  # 3 = theatrical
            continue
        date_str = rel.get("release_date", "")[:10]
        if not date_str:
            continue
        try:
            release_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            continue
        if cutoff_past <= release_date <= today:
            return "in theaters"
        if today < release_date <= cutoff_future:
            return f"opens {date_str}"

    return None


def _showtimes_link(title: str, zip_code: str) -> str:
    return "https://www.google.com/search?q=" + quote_plus(f"{title} showtimes near {zip_code}")


def find_in_theaters(
    region: str = "US",
    zip_code: str | None = None,
    films: list | None = None,
) -> str:
    """Find what's currently in theaters or opening soon, optionally filtering a list of films."""
    headers = _tmdb_headers()
    if not headers:
        return json.dumps({"error": "TMDB_READ_TOKEN is not set."})

    theater_data, err = _get_theater_data(region, headers)
    if err:
        return err

    now_playing = theater_data["now_playing"]
    upcoming    = theater_data["upcoming"]
    all_films   = now_playing + upcoming

    by_id:         dict[int,   dict] = {f["tmdb_id"]: f for f in all_films}
    by_title_year: dict[tuple, dict] = {}
    for f in all_films:
        by_title_year[(f["normalized"], f["year"])] = f

    def build_result(f: dict) -> dict:
        r: dict = {
            "title":   f["title"],
            "year":    f["year"],
            "tmdb_id": f["tmdb_id"],
            "status":  f["status"],
        }
        if zip_code:
            r["showtimes_link"] = _showtimes_link(f["title"], zip_code)
        return r

    # --- No filter: return what's playing ---
    if films is None:
        results = [build_result(f) for f in now_playing[:20]]
        out: dict = {"region": region, "now_playing_count": len(now_playing), "results": results}
        if not zip_code:
            out["note"] = (
                "No zip_code given — showtimes links omitted. "
                "Ask the user for their zip code to include them."
            )
        return json.dumps(out)

    # --- Filter mode: match provided films against theater data ---
    matched: list[dict] = []
    check_release: list[tuple[dict, int]] = []  # (film_input, tmdb_id)

    for film in films:
        tmdb_id = film.get("tmdb_id")
        title   = film.get("title", "")
        year    = film.get("year")
        hit     = None

        if tmdb_id and tmdb_id in by_id:
            hit = by_id[tmdb_id]
        elif title:
            norm = _normalize_title(title)
            hit = by_title_year.get((norm, year))
            if not hit:
                # Try without year constraint
                hit = next((tf for (n, _), tf in by_title_year.items() if n == norm), None)

        if hit:
            matched.append(build_result(hit))
        elif tmdb_id:
            check_release.append((film, tmdb_id))

    # Check release_dates for unmatched films with a tmdb_id (concurrent)
    if check_release:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(_check_release_dates, tid, region, headers): (film, tid)
                for film, tid in check_release
            }
            for future in as_completed(futures):
                film, tid = futures[future]
                status = future.result()
                if not status:
                    continue
                details = _fetch_details(tid, headers)
                r = {
                    "title":   details.get("title") or film.get("title", ""),
                    "year":    details.get("year")  or film.get("year"),
                    "tmdb_id": tid,
                    "status":  status,
                }
                if zip_code:
                    r["showtimes_link"] = _showtimes_link(r["title"], zip_code)
                matched.append(r)

    out = {"region": region, "matched_count": len(matched), "results": matched}
    if not zip_code and matched:
        out["note"] = (
            "No zip_code given — showtimes links omitted. "
            "Ask the user for their zip code to include them."
        )
    return json.dumps(out)


# ---------------------------------------------------------------------------
# find_where_to_watch
# ---------------------------------------------------------------------------

MAX_BATCH = 75


def _process_one_film(film: dict, region: str, my_services: list, headers: dict) -> dict:
    tmdb_id = film.get("tmdb_id")
    search_note = None

    if not tmdb_id:
        title = film.get("title", "")
        year  = film.get("year")
        if not title:
            return {"error": "Each film needs either 'tmdb_id' or 'title'."}
        tmdb_id, search_note = _search_movie(title, year, headers)
        if tmdb_id is None:
            return {"query": title, "year": year, "error": search_note}

    data = _fetch_combined(tmdb_id, region, headers)
    if "error" in data:
        return {"tmdb_id": tmdb_id, "title": film.get("title", f"tmdb:{tmdb_id}"), "error": data["error"]}

    def names(key: str) -> list[str]:
        return [p["name"] for p in data.get(key, [])]

    result: dict = {
        "title":   data["title"],
        "year":    data["year"],
        "tmdb_id": tmdb_id,
        "runtime": data["runtime"],
        "genres":  data["genres"],
        "providers": {
            "stream": names("stream"),
            "free":   names("free"),
            "ads":    names("ads"),
            "rent":   names("rent"),
            "buy":    names("buy"),
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
    """Look up streaming/rental options via TMDB. Runs 8 concurrent requests; caches results."""
    headers = _tmdb_headers()
    if not headers:
        return json.dumps({"error": "TMDB_READ_TOKEN is not set."})

    if len(films) > MAX_BATCH:
        films = films[:MAX_BATCH]

    results: list = [None] * len(films)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_process_one_film, film, region, my_services or [], headers): i
            for i, film in enumerate(films)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    return json.dumps({"region": region, "results": results})


# ---------------------------------------------------------------------------
# show_films
# ---------------------------------------------------------------------------

MAX_DISPLAY = 100


def _resolve_for_display(film: dict, headers: dict) -> dict:
    tmdb_id = film.get("tmdb_id")
    title   = film.get("title", "")
    year    = film.get("year")

    if not tmdb_id:
        if not title:
            return {"title": "Unknown", "poster_url": None, "error": "No title or tmdb_id."}
        tmdb_id, note = _search_movie(title, year, headers)
        if tmdb_id is None:
            return {"title": title, "year": year, "poster_url": None, "error": note}

    details = _fetch_details(tmdb_id, headers)
    if "error" in details:
        return {
            "title":      title or f"tmdb:{tmdb_id}",
            "year":       year,
            "tmdb_id":    tmdb_id,
            "poster_url": None,
            "error":      details["error"],
        }

    result: dict = {
        "title":      details["title"],
        "year":       details["year"],
        "tmdb_id":    tmdb_id,
        "poster_url": details["poster_url"],
        "runtime":    details["runtime"],
    }
    if film.get("slug"):
        result["slug"] = film["slug"]

    # Stream logos from provider cache if already populated by find_where_to_watch
    cached = _providers_cache.get((tmdb_id, "US"))
    if cached and not cached.get("providers_note"):
        result["stream"] = cached["stream"]

    return result


def show_films(films: list) -> str:
    """Resolve films to display data (posters + cached stream logos) for the UI."""
    headers = _tmdb_headers()
    if not headers:
        return json.dumps({"error": "TMDB_READ_TOKEN is not set."})

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
            "name": "get_letterboxd_watchlist",
            "description": (
                "Fetch all films on a Letterboxd user's public watchlist. "
                "Call this when the user asks what films a specific person wants to watch. "
                "Returns: username, total film count, and a list of films "
                "(title, Letterboxd slug, release year). "
                "The username is the path segment after letterboxd.com/ — "
                "e.g. letterboxd.com/yegandk → 'yegandk'. Do not pass a full URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "Letterboxd username (no slashes, no full URL).",
                    },
                },
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_letterboxd_ratings",
            "description": (
                "Fetch a Letterboxd user's film log (everything they have watched and logged). "
                "Call this when the user asks about someone's taste, top films, average rating, "
                "or viewing history for a single user. "
                "Returns: total watched, number rated, average rating, "
                "rating distribution (0.5–5 stars in 0.5 increments), "
                "top 15 highest-rated films, and 15 recently liked films. "
                "The full per-film list is cached internally and reused by compare_letterboxd_users."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "Letterboxd username (the path segment after letterboxd.com/).",
                    },
                },
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_letterboxd_users",
            "description": (
                "Compare two Letterboxd users' watchlists and film ratings side by side. "
                "Call this when two Letterboxd usernames are in play and the user wants to "
                "find something to watch together or understand taste overlap. "
                "Returns:\n"
                "  profile_a / profile_b: display name, avatar, watched count, rated count, avg rating\n"
                "  both_seen: count of films both have logged (rated or not)\n"
                "  compatibility: score 0–100 (max(0, round(100×(1–mean_abs_diff/1.5)))), "
                "based_on count, mean_abs_diff, confidence\n"
                "  both_seen_films: up to 12 films both have seen, sorted by combined rating "
                "(prefer films both rated highly), each with poster_url\n"
                "  both_seen_total: total count of films both have seen\n"
                "  shared_watchlist: up to 20 films on both watchlists, each with poster_url\n"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_a": {
                        "type": "string",
                        "description": "First Letterboxd username.",
                    },
                    "user_b": {
                        "type": "string",
                        "description": "Second Letterboxd username.",
                    },
                },
                "required": ["user_a", "user_b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_in_theaters",
            "description": (
                "Find films currently in theaters or opening soon (within ~30 days), "
                "using TMDB now_playing and upcoming data. "
                "Call this when the user asks what's playing, mentions going to the movies, "
                "or wants to know whether specific films are in theaters. "
                "Pass a list of films to filter (e.g. a watchlist) — the tool checks each against "
                "current/upcoming releases and falls back to per-film release_dates for limited releases. "
                "Omit films to get the full now-playing list. "
                "Returns per matched film: title, year, tmdb_id, "
                "status ('in theaters' or 'opens YYYY-MM-DD'), "
                "and showtimes_link if zip_code was given. "
                "showtimes_link is a Google search URL — it shows where/when the film is playing "
                "near the zip code. There is no direct showtimes API; say so if asked. "
                "If the user wants showtimes but hasn't given a zip code, ask for it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {
                        "type": "string",
                        "description": "ISO 3166-1 alpha-2 country code, e.g. 'US'. Defaults to 'US'.",
                    },
                    "zip_code": {
                        "type": "string",
                        "description": (
                            "User's zip or postal code. When provided, each result includes "
                            "a showtimes_link Google search URL."
                        ),
                    },
                    "films": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tmdb_id": {"type": "integer"},
                                "title":   {"type": "string"},
                                "year":    {"type": "integer"},
                            },
                        },
                        "description": (
                            "Films to check against current/upcoming releases. "
                            "Each entry needs tmdb_id OR title (year improves title matching). "
                            "Omit this field entirely to get the full now-playing list."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_where_to_watch",
            "description": (
                "Look up streaming, rental, and purchase options for a list of films "
                "via TMDB (backed by JustWatch data). "
                "Call this when the user asks where to stream or watch specific films at home. "
                "Pass all films in a single call. "
                "Returns per film: title, year, tmdb_id, runtime, genres, "
                "providers broken down by category "
                "(stream = subscription, free = free with ads-free, ads = ad-supported, "
                "rent, buy), and a JustWatch deep link. "
                "If a film is not found on TMDB, the result includes an error field with the reason. "
                "If my_services is given, each result also includes on_my_services: "
                "the subset of stream/free/ads providers that match (fuzzy name match)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "films": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tmdb_id": {"type": "integer"},
                                "title":   {"type": "string"},
                                "year":    {"type": "integer"},
                            },
                        },
                        "description": (
                            "Films to look up. Each needs tmdb_id OR title; "
                            "year is optional but helps disambiguate titles."
                        ),
                    },
                    "region": {
                        "type": "string",
                        "description": "ISO 3166-1 alpha-2 country code, e.g. 'US'. Defaults to 'US'.",
                    },
                    "my_services": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Streaming services the user subscribes to, e.g. ['Netflix', 'Max']. "
                            "When provided, results include on_my_services per film."
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
                "Render poster cards for a list of films in the chat UI. "
                "Always call this as the final tool call before giving your answer, "
                "with exactly the films your answer mentions or recommends — no others. "
                "For watchlist results: pass all films if ≤ 100, otherwise the first 100. "
                "Pass tmdb_id when available to avoid an extra TMDB search per film. "
                "Returns a JSON object with a 'displayed' count and per-film display data "
                "(poster URL, runtime, stream provider logos if already cached)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "films": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tmdb_id": {"type": "integer"},
                                "title":   {"type": "string"},
                                "year":    {"type": "integer"},
                                "slug":    {"type": "string"},
                            },
                        },
                        "description": (
                            "Films to display. Each needs tmdb_id OR title. "
                            "slug (Letterboxd slug) is optional — included when available."
                        ),
                    },
                },
                "required": ["films"],
            },
        },
    },
]

TOOL_MAP = {
    "get_letterboxd_watchlist": get_letterboxd_watchlist,
    "get_letterboxd_ratings":   get_letterboxd_ratings,
    "compare_letterboxd_users": compare_letterboxd_users,
    "find_in_theaters":         find_in_theaters,
    "find_where_to_watch":      find_where_to_watch,
    "show_films":               show_films,
}


def run_tool(name: str, args: dict) -> str:
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}"})


# ---------------------------------------------------------------------------
# Smoke tests — python tools.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    def hr(label: str):
        print(f"\n{'='*60}\n{label}\n{'='*60}")

    # 1. Ratings for yegandk — verify totals
    hr("get_letterboxd_ratings — yegandk")
    t0 = time.time()
    r = json.loads(get_letterboxd_ratings("yegandk"))
    elapsed = time.time() - t0
    if "error" in r:
        print("ERROR:", r["error"])
    else:
        print(f"total_watched: {r['total_watched']}  num_rated: {r['num_rated']}  avg: {r['avg_rating']}")
        print(f"fetch time: {elapsed:.1f}s")
        print(f"top 5: {[f['title'] + ' (' + str(f['rating']) + '★)' for f in r['top_rated'][:5]]}")
        print(f"recent liked: {[f['title'] for f in r['recent_liked'][:3]]}")

    # 2. Ratings for nonexistent user
    hr("get_letterboxd_ratings — nonexistent user")
    r2 = json.loads(get_letterboxd_ratings("thisuserdoesnotexist99999xyz"))
    print(r2)

    # 3. Cache hit check — second call should be instant
    hr("Cache check — second call to yegandk ratings")
    t1 = time.time()
    get_letterboxd_ratings("yegandk")
    print(f"Cached call took: {time.time() - t1:.3f}s  (should be ~0)")

    # 4. Compare — needs a real friend username
    FRIEND = "aa3345"
    if FRIEND != "REPLACE_WITH_FRIEND_USERNAME":
        hr(f"compare_letterboxd_users — yegandk vs {FRIEND}")
        t2 = time.time()
        c = json.loads(compare_letterboxd_users("yegandk", FRIEND))
        elapsed2 = time.time() - t2
        if "error" in c:
            print("ERROR:", c["error"])
        else:
            print(f"Cold compare time: {elapsed2:.1f}s")
            print(f"Compatibility: {c['compatibility']}")
            print(f"Both seen: {c['both_seen']}  (showing {len(c['both_seen_films'])} films)")
            print(f"Shared watchlist ({len(c['shared_watchlist'])}): "
                  f"{[f['title'] for f in c['shared_watchlist'][:3]]}")
    else:
        hr("compare_letterboxd_users — skipped (set FRIEND above)")
        print("Set FRIEND = 'your_friends_username' to run the comparison test.")

    # 5. What's in theaters now (no filter)
    hr("find_in_theaters — what's playing (no filter)")
    t3 = time.time()
    th = json.loads(find_in_theaters())
    print(f"Fetch time: {time.time() - t3:.1f}s")
    if "error" in th:
        print("ERROR:", th["error"])
    else:
        print(f"now_playing_count: {th['now_playing_count']}")
        for f in th["results"][:5]:
            print(f"  {f['title']} ({f['year']}) — {f['status']}")

    # 6. Filter watchlist against theaters (uses cached watchlist from test 1 indirectly)
    hr("find_in_theaters — filter yegandk's watchlist, zip 10027")
    wl_raw = json.loads(get_letterboxd_watchlist("yegandk"))
    wl_films = wl_raw.get("films", [])
    t4 = time.time()
    th2 = json.loads(find_in_theaters(zip_code="10027", films=wl_films))
    print(f"Fetch time: {time.time() - t4:.1f}s")
    if "error" in th2:
        print("ERROR:", th2["error"])
    else:
        print(f"matched {th2['matched_count']} of {len(wl_films)} watchlist films")
        for f in th2["results"]:
            link = f.get("showtimes_link", "")
            print(f"  {f['title']} ({f['year']}) — {f['status']}")
            if link:
                print(f"    {link[:80]}...")
