# 🎬 Film Forge
### Movie Library Manager — v3.7.6
**Developed by Oddbit Studios**

> *catalog. search. discover.*

---

## Overview

Film Forge is a desktop application for cataloging, searching, and managing your personal movie library. It scans your local drives for video files, automatically fetches metadata from the OMDb API (title, runtime, genre, cast, plot, poster), and stores everything in a local SQLite database. A built-in Flask web server lets you browse and rate your collection from any device on your network — or publicly via a custom domain.

---

## Features

- **Automatic scanning** — Point Film Forge at any folder and it finds all video files (.mp4, .mkv, .avi, .mov, and more)
- **OMDb metadata fetch** — Pulls title, year, runtime, MPAA rating, genre, cast, plot summary, and poster art automatically
- **SQLite database** — All data stored locally in `film_forge.db`; no cloud dependency
- **5-star personal ratings** — Rate any movie 1–5 stars from the Database tab or the web interface
- **Watched tracking** — Mark movies as watched/unwatched
- **Duplicate detection** — Find duplicate files by SHA-256 hash or by IMDb ID
- **Excel export** — Export your full library to a formatted `.xlsx` spreadsheet
- **NDJSON export** — Export to newline-delimited JSON for use with other tools
- **Web interface** — Browse, search, filter, rate, and mark watched from any browser
- **REST JSON API** — Query your library programmatically
- **Railway sync** — Push your database to GitHub and auto-redeploy to Railway with one click
- **Pull ratings from web** — Sync watched status and ratings made on the web back to your local database

---

## Requirements

### Desktop App
- Python 3.10+
- See `requirements.txt` for full dependency list
- Key packages: `tkinter`, `requests`, `Pillow`, `pandas`, `openpyxl`, `flask`, `flask-cors`

### Web Server (Railway)
- Python 3.10+
- `flask`, `flask-cors`, `requests`, `gunicorn`

---

## Installation

```bash
git clone https://github.com/oddbitstudios/Film-Forge.git
cd Film-Forge
pip install -r requirements.txt
python Film_Forge_3_7_6.py
```

---

## Usage

### Desktop App

1. **Add Folders** — Click *Add Folders* in the toolbar and select directories containing your video files
2. **Start Scan** — Click *Start Scan* to find all video files in the selected folders
3. **Fetch Details** — Click *Fetch Details* to pull metadata from OMDb for every movie found
4. **Database tab** — Browse your full library, mark watched, set star ratings (1–5), play files
5. **Export** — Export to Excel or NDJSON from the Export tab
6. **Settings** — Enter your OMDb API key, configure Railway sync, set your GitHub repo and web URL

### Web Interface

The web interface is served by `film_forge_web.py` and can be accessed locally or deployed to Railway.

**Quick views:** All · Unrated · Watched · Unwatched · Recently Rated · Recently Watched

**Filters:** Watched status · Rated status · Sort order

**Rating:** Click ★ 1–5 to rate any movie directly in the browser

**Watched:** Click *Watch* / *Unwatch* on any movie card

---

## Web Deployment (Railway)

1. Push `film_forge_web.py`, `film_forge.db`, `Procfile`, and `requirements.txt` to GitHub
2. Connect the repo to Railway
3. Set environment variables in Railway if needed:
   - `MDC_DB_PATH` — path to the database (default: `/app/film_forge.db`)
   - `MDC_EDIT_KEY` — optional key to protect rating/watched edits
   - `OMDB_API_KEY` — OMDb API key for poster self-healing on startup
4. Point your custom domain (e.g. `filmforge.oddbitstudios.com`) to the Railway deployment

---

## REST API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/movies` | Paginated movie list with filters |
| `GET` | `/api/movies/<imdb_id>` | Single movie detail |
| `POST` | `/api/set_watched` | Mark watched/unwatched |
| `POST` | `/api/set_rating` | Set star rating (1–5) or clear |
| `GET` | `/api/docs` | API documentation |

**Query parameters for `/api/movies`:**
- `q` — search title, actor, genre, or IMDb ID
- `page`, `per_page` (10 / 25 / 50 / 100)
- `watched` — `all` / `yes` / `no`
- `rated` — `all` / `yes` / `no`
- `sort` — `title` / `year_desc` / `year_asc` / `rating_desc` / `rating_asc` / `recent_rated` / `recent_watched`

---

## File Structure

```
Film-Forge/
├── Film_Forge_3_7_6.py     # Desktop application
├── film_forge_web.py        # Flask web server (Railway entry point)
├── film_forge_db.py         # SQLite helper (used by desktop app)
├── film_forge.db            # SQLite database (your movie library)
├── Procfile                 # Railway startup command
├── requirements.txt         # Python dependencies
└── README.md
```

---

## OMDb API Key

Film Forge uses the [OMDb API](https://www.omdbapi.com/) for movie metadata. A free API key is available at [omdbapi.com](https://www.omdbapi.com/apikey.aspx). Enter your key in **Settings → OMDb API → API Key** and click *Apply Settings*.

---

## License

© 2026 Oddbit Studios. All rights reserved.
