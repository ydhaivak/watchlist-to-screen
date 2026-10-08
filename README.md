# 🎬 Watchlist to Screen

**Your Letterboxd watchlist, matched to a screen tonight.**

**Live app:** https://watchlist-to-screen-git-858340578834.us-east1.run.app/
*(Sign in with your Columbia Google account. The app is behind Identity-Aware Proxy.)*

---

## The problem

Every Letterboxd user has the same problem: a watchlist that keeps growing and a movie night that starts with twenty minutes of scrolling. Which of these films is actually streaming on a service I pay for? Is anything I've been meaning to see in theaters right now? And when I'm watching with a friend, which films do we *both* want to see?

Letterboxd knows what you want to watch. It doesn't know how you can watch it tonight. **Watchlist to Screen** connects the two.

## What it does

Watchlist to Screen is a chat agent built on Gemini. Give it any **public Letterboxd username** and ask in plain English. It will:

- **Read a watchlist.** It pulls every film on a user's Letterboxd watchlist, across all pages.
- **Find where to stream it.** It checks each film's streaming, free-with-ads, rental, and purchase options in the US, and can filter to the services you have ("only Max and Netflix").
- **Check theaters.** It finds which watchlist films are in theaters now or opening soon, with a showtimes link for your zip code.
- **Match two people.** It finds the films that are on *both* people's watchlists, the natural "watch it together" candidates, and then tells you where to watch them.
- **Show, don't just tell.** It displays poster cards (with runtime and streaming-service logos) for exactly the films it's talking about.

The agent keeps the conversation in memory, so follow-ups work naturally: *"Which of those is shortest?"*, *"What about Netflix instead?"*, *"Any of them in theaters?"*

## Sample queries for grading

Paste these into the chat, or click them on the intro curtain:

1. **Streaming filter:**
   > What movies on yegandk's watchlist can I stream on HBO Max?

2. **Two-person match + where to watch:**
   > What's on both yegandk's and aa3345's watchlists, and where can we stream them?

3. **Theaters near you:**
   > Is anything on yegandk's watchlist in theaters near me? My zip code is 10027.

Good follow-ups to test memory: *"Which of those is the shortest?"* or *"Show me only the ones from before 2000."*

Other public usernames work too; try your own.

> **Note:** The first request for a username takes a few seconds because the agent reads each Letterboxd page politely, one at a time. Results are cached, so follow-ups are fast.

## The tools

| Tool | What it does | Data source |
|---|---|---|
| `get_letterboxd_watchlist` | Fetches every film on a user's public watchlist (title, year, Letterboxd slug), across all pages. | Letterboxd (public pages) |
| `get_letterboxd_ratings` | Fetches a user's watched films and star ratings and returns a compact summary (counts, average rating, top-rated films). | Letterboxd (public pages) |
| `compare_letterboxd_users` | **Original tool.** Compares two Letterboxd users: films on both watchlists, plus (when ratings are available) a taste-compatibility score and the films they've both seen. Films are matched by Letterboxd slug, so matching is exact and needs no extra API calls. | Letterboxd |
| `find_where_to_watch` | For a batch of films, finds streaming, free/ads, rent, and buy options in a region, and flags films on the user's own services. | TMDB API (JustWatch data) |
| `find_in_theaters` | Finds which films are in theaters now or opening soon in a region, and builds a showtimes link for a zip code. | TMDB API |
| `show_films` | **Original tool.** A display tool: the model calls it last, with only the films its answer mentions, and the frontend renders them as poster cards. | TMDB API (posters, runtimes) |
