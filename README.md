# Watchlist to Screen

**Live demo:** https://watchlist-to-screen-git-858340578834.us-east1.run.app/

A conversational movie-discovery agent that reads public Letterboxd profiles and
answers questions like "what should we watch tonight, and where can we stream it?"
Built with FastAPI + LiteLLM + Gemini Flash Lite on Vertex AI, with all tool calls
surfaced in the UI.

---

## What it does

- **Explore a watchlist** — scrapes any public Letterboxd user's watchlist and
  lets you ask questions about it in natural language.
- **Understand taste** — fetches a user's full film log (ratings, likes) to
  answer questions about their preferences and viewing history.
- **Compare two friends** — finds the films two users both want to see, scores
  their taste compatibility, and surfaces their shared watchlist.
- **Find where to stream** — looks up streaming, rental, and purchase options
  via TMDB/JustWatch for any list of films.
- **Check theaters** — checks which films from a watchlist are currently playing
  or opening soon, with optional Google showtimes links by zip code.
- **Show posters** — resolves TMDB poster art for every film it recommends,
  rendered as a scrollable card row in the UI.

The agent remembers the full conversation within a session, so you can ask
follow-up questions without repeating context.

---

## Tools

All six tools are purpose-built; none came from the starter template.

### `get_letterboxd_watchlist`
Scrapes every film from a public Letterboxd watchlist (handles pagination via a
persistent session to bypass Cloudflare). Returns title, year, and Letterboxd
slug for every film. Error messages tell the model when a user doesn't exist or
their profile is private.

### `get_letterboxd_ratings`
Scrapes a user's complete film log — up to ~3 600 films across 50 pages —
including star ratings (0.5–5) and liked flags. Returns a compact summary:
total watched, number rated, average rating, rating distribution, top 15
highest-rated films, and 15 recently liked films. The full per-film list is
cached and reused by `compare_letterboxd_users`.

### `compare_letterboxd_users`
Compares two Letterboxd users side by side. Fetches both watchlists and full
film logs, then computes:
- **Profile cards** — display name, avatar, watched/rated counts, average rating
- **Both-seen count** — all films both users have logged, rated or not
- **Compatibility score** — `max(0, round(100 × (1 – mean_abs_diff / 1.5)))`;
  0 means ≥ 1.5★ average disagreement, 100 means perfect agreement
- **Both-seen films** — up to 12 films sorted by combined rating, with posters
- **Shared watchlist** — films on both watchlists, with posters

Rendered in the UI as a rich compatibility card with an SVG score ring and
horizontal poster rows.

### `find_in_theaters`
Pulls current and upcoming releases from TMDB (`/movie/now_playing` and
`/movie/upcoming`) and checks a film list against them. Falls back to per-film
`/release_dates` for limited releases. When a zip code is provided, each
matched film gets a Google showtimes search link. Theater data is cached for
3 hours.

### `find_where_to_watch`
Batch-looks up streaming, rental, and purchase options for up to 75 films via
TMDB's JustWatch-backed provider data. Accepts an optional `my_services` list
and returns which films are available on the user's own subscriptions.

### `show_films`
A UI-rendering tool: resolves TMDB poster art, runtime, and (when already
cached) stream provider logos for a list of films. The model calls this as its
final step before answering so the user sees a poster grid alongside the
response. Returns `{ displayed, films[] }`.

---

## Architecture

```
browser ──POST /chat──► FastAPI (app.py)
                            │
                      run_agent()  ← LiteLLM → Gemini Flash Lite (Vertex AI)
                            │
                       tool harness  ← tools.py
                       (≤6 rounds)        │
                                     Letterboxd (scraping)
                                     TMDB REST API
```

- **Session store** — `sessions: dict[str, list]` in memory; full message
  history preserved across turns.
- **`/chat` response shape** — always `{ response, session_id, tool_calls[] }`
  where each call has `{ name, args, result }`.
- **Caching** — Letterboxd watchlists and ratings: 1-hour TTL; TMDB theater
  data: 3-hour TTL; TMDB film details and providers: process-lifetime cache.
- **Pagination** — uses `requests.Session` with `Referer` and `Sec-Fetch-*`
  headers to thread through Letterboxd pages without triggering Cloudflare
  (403 on stateless paginated requests).

---

## Three sample queries for the grader

### 1 — Watchlist + streaming lookup
```
What movies are on yegandk's watchlist, and which ones can I stream on Netflix?
```
Expected flow: `get_letterboxd_watchlist` → `find_where_to_watch` → `show_films`.
The response lists Netflix-available films from the watchlist with streaming
confirmation.

### 2 — Theater check with zip code
```
Is anything on yegandk's watchlist playing in theaters near zip code 10027?
```
Expected flow: `get_letterboxd_watchlist` → `find_in_theaters` → `show_films`.
Returns matched films with status ("in theaters" / "opens YYYY-MM-DD") and
clickable Google showtimes links.

### 3 — Two-friend comparison
```
Compare yegandk and aa3345. What should we watch together tonight, and where can we stream it?
```
Expected flow: `compare_letterboxd_users` → `find_where_to_watch` → `show_films`.
Renders the compatibility card (score ring, both profiles, both-seen poster row,
shared watchlist) and recommends specific films with streaming options.

---

## Running locally

**Prerequisites:** Python 3.10+, a GCP project with Vertex AI enabled, and a
TMDB API read token.

```bash
# 1. Authenticate with Google Cloud
gcloud auth application-default login

# 2. Set your TMDB token
echo "TMDB_READ_TOKEN=your_token_here" > .env

# 3. Install and run
uv run app.py
```

Open http://localhost:8000. The intro curtain explains what the agent can do
and provides example queries to click.

> **TMDB token** — free at https://www.themoviedb.org/settings/api (read access
> token, not the API key).

---

## Stack

| Layer | Technology |
|---|---|
| LLM | Gemini 3.5 Flash Lite via Vertex AI (LiteLLM) |
| Web server | FastAPI + Uvicorn |
| Letterboxd data | HTTP scraping (requests + BeautifulSoup4) |
| Movie metadata | TMDB REST API v3 |
| Frontend | Vanilla JS + marked.js + DOMPurify |
