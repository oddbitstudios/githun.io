# film_forge_web.py — standalone Railway web server
# Oddbit Studios
# Todd Jarmiolowski

__version__ = "3.7.6"

import os
import re
import json
import math
import sqlite3
import hashlib
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
from flask import Flask, request as flask_request, render_template_string, jsonify
from flask_cors import CORS as FlaskCORS
# --- film_forge_db inlined ---
import sqlite3
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone


def utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    p = Path(path)
    with p.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class MovieDB:
    """SQLite helper with auto-migrations + pagination."""

    def __init__(self, db_path: str, schema_path: str | None = None):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.row_factory = sqlite3.Row

        self._ensure_tables()
        self._migrate_files_last_seen()
        self._migrate_user_last_rated()
        self._ensure_indexes()
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    # --- OMDb cache helpers (used by process_files_and_fetch_data) ---
    def get_cached_omdb_by_imdb(self, imdb_id: str) -> dict | None:
        """Return the stored omdb_json dict for a movie by IMDb ID, or None."""
        imdb_id = (imdb_id or "").strip()
        if not imdb_id:
            return None
        row = self.conn.execute(
            "SELECT omdb_json FROM movies WHERE imdb_id=?", (imdb_id,)
        ).fetchone()
        if row and row["omdb_json"]:
            try:
                return json.loads(row["omdb_json"])
            except Exception:
                return None
        return None

    def get_cached_omdb_by_title_year(self, title: str, year: str) -> dict | None:
        """Return the stored omdb_json dict for a movie by title+year, or None."""
        title = (title or "").strip()
        year  = (year  or "").strip()
        if not title:
            return None
        row = self.conn.execute(
            "SELECT omdb_json FROM movies WHERE UPPER(title)=UPPER(?) AND CAST(year AS TEXT)=?",
            (title, year)
        ).fetchone()
        if row and row["omdb_json"]:
            try:
                return json.loads(row["omdb_json"])
            except Exception:
                return None
        return None

    # --- convenience search (used by the desktop DB-viewer dialog) ---
    def search(self, q: str, limit: int = 400) -> list:
        """Simple title/actor/genre search returning rows with a path field."""
        q = (q or "").strip()
        like = f"%{q}%"
        sql = """
            SELECT
              m.movie_id, m.title, m.year, m.imdb_id, m.genre, m.mpaa_rating,
              m.actors, m.plot,
              COALESCE(um.watched, 0)   AS watched,
              um.personal_rating,
              f.path
            FROM movies m
            LEFT JOIN user_movie um ON um.movie_id = m.movie_id
            LEFT JOIN files      f  ON f.movie_id  = m.movie_id
            WHERE (?='' OR m.title LIKE ? OR m.actors LIKE ?
                        OR m.genre LIKE ? OR m.imdb_id LIKE ?)
            ORDER BY m.title ASC
            LIMIT ?
        """
        return self.conn.execute(sql, [q, like, like, like, like, int(limit)]).fetchall()

    def _ensure_tables(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS movies (
          movie_id INTEGER PRIMARY KEY,
          imdb_id TEXT UNIQUE,
          title TEXT NOT NULL,
          year INTEGER,
          runtime TEXT,
          genre TEXT,
          mpaa_rating TEXT,
          actors TEXT,
          plot TEXT,
          poster TEXT,
          omdb_json TEXT,
          updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS files (
          file_id INTEGER PRIMARY KEY,
          movie_id INTEGER,
          path TEXT UNIQUE NOT NULL,
          size_bytes INTEGER,
          mtime_utc TEXT,
          sha256 TEXT,
          ext TEXT,
          FOREIGN KEY(movie_id) REFERENCES movies(movie_id)
        );

        CREATE TABLE IF NOT EXISTS user_movie (
          movie_id INTEGER PRIMARY KEY,
          watched INTEGER DEFAULT 0,
          personal_rating REAL,
          notes TEXT,
          last_watched_at TEXT,
          FOREIGN KEY(movie_id) REFERENCES movies(movie_id)
        );
        """)

    def _table_cols(self, table: str) -> set[str]:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] for r in rows}

    def _migrate_files_last_seen(self):
        cols = self._table_cols("files")
        if "last_seen_utc" not in cols:
            self.conn.execute("ALTER TABLE files ADD COLUMN last_seen_utc TEXT;")

    def _migrate_user_last_rated(self):
        cols = self._table_cols("user_movie")
        if "last_rated_at" not in cols:
            self.conn.execute("ALTER TABLE user_movie ADD COLUMN last_rated_at TEXT;")

    def _ensure_indexes(self):
        self.conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
        CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
        CREATE INDEX IF NOT EXISTS idx_movies_title ON movies(title);
        """)
        if "last_seen_utc" in self._table_cols("files"):
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_files_last_seen ON files(last_seen_utc);")
        if "last_rated_at" in self._table_cols("user_movie"):
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_user_last_rated ON user_movie(last_rated_at);")

    # --- upserts ---
    def upsert_movie(self, omdb: dict) -> int:
        imdb_id = (omdb.get("imdbID") or "").strip()
        title = (omdb.get("Title") or "").strip()
        year = (omdb.get("Year") or "").strip()
        year_i = int(year) if year.isdigit() else None

        payload = json.dumps(omdb, ensure_ascii=False)
        updated_at = now_utc_iso()

        row = None
        if imdb_id:
            row = self.conn.execute("SELECT movie_id FROM movies WHERE imdb_id=?", (imdb_id,)).fetchone()

        if row:
            movie_id = row["movie_id"]
            self.conn.execute("""
                UPDATE movies SET
                  title=?, year=?, runtime=?, genre=?, mpaa_rating=?, actors=?, plot=?, poster=?,
                  omdb_json=?, updated_at=?
                WHERE movie_id=?
            """, (
                title,
                year_i,
                omdb.get("Runtime", "") or "",
                omdb.get("Genre", "") or "",
                omdb.get("Rated", "") or "",
                omdb.get("Actors", "") or "",
                omdb.get("Plot", "") or "",
                omdb.get("Poster", "") or "",
                payload,
                updated_at,
                movie_id
            ))
        else:
            cur = self.conn.execute("""
                INSERT INTO movies (
                  imdb_id, title, year, runtime, genre, mpaa_rating, actors, plot, poster, omdb_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                imdb_id,
                title,
                year_i,
                omdb.get("Runtime", "") or "",
                omdb.get("Genre", "") or "",
                omdb.get("Rated", "") or "",
                omdb.get("Actors", "") or "",
                omdb.get("Plot", "") or "",
                omdb.get("Poster", "") or "",
                payload,
                updated_at
            ))
            movie_id = cur.lastrowid

        self.conn.execute(
            "INSERT INTO user_movie (movie_id) VALUES (?) ON CONFLICT(movie_id) DO NOTHING",
            (movie_id,)
        )
        self.conn.commit()
        return movie_id

    def upsert_file(self, movie_id: int | None, path: str, do_hash: bool = False):
        p = Path(path)
        st = p.stat()
        sha = sha256_file(path) if do_hash else None
        seen = now_utc_iso()

        self.conn.execute("""
            INSERT INTO files (movie_id, path, size_bytes, mtime_utc, sha256, ext, last_seen_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
              movie_id=excluded.movie_id,
              size_bytes=excluded.size_bytes,
              mtime_utc=excluded.mtime_utc,
              sha256=excluded.sha256,
              ext=excluded.ext,
              last_seen_utc=excluded.last_seen_utc
        """, (
            movie_id,
            str(p),
            int(st.st_size),
            utc_iso(st.st_mtime),
            sha,
            p.suffix.lower(),
            seen
        ))
        self.conn.commit()

    # --- user CRUD ---
    def set_watched(self, imdb_id: str, watched: int):
        imdb_id = (imdb_id or "").strip()
        row = self.conn.execute("SELECT movie_id FROM movies WHERE imdb_id=?", (imdb_id,)).fetchone()
        if not row:
            return
        mid = row["movie_id"]
        self.conn.execute("""
            UPDATE user_movie
            SET watched=?,
                last_watched_at=CASE WHEN ?=1 THEN datetime('now') ELSE last_watched_at END
            WHERE movie_id=?
        """, (watched, watched, mid))
        self.conn.commit()

    def set_rating(self, imdb_id: str, rating: float | None):
        imdb_id = (imdb_id or "").strip()
        row = self.conn.execute("SELECT movie_id FROM movies WHERE imdb_id=?", (imdb_id,)).fetchone()
        if not row:
            return
        mid = row["movie_id"]
        self.conn.execute("""
            UPDATE user_movie
            SET personal_rating=?,
                last_rated_at=CASE WHEN ? IS NULL THEN last_rated_at ELSE datetime('now') END
            WHERE movie_id=?
        """, (rating, rating, mid))
        self.conn.commit()

    # --- pagination search ---
    def search_page(self, q: str, offset: int, limit: int, watched_filter: str = "all", rated_filter: str = "all",
                    sort: str = "title"):
        q = (q or "").strip()
        like = f"%{q}%"
        where_parts = ["(?='' OR m.title LIKE ? OR m.actors LIKE ? OR m.genre LIKE ? OR m.imdb_id LIKE ?)"]
        params = [q, like, like, like, like]
        if watched_filter == "yes":
            where_parts.append("COALESCE(um.watched,0)=1")
        elif watched_filter == "no":
            where_parts.append("COALESCE(um.watched,0)=0")
        if rated_filter == "yes":
            where_parts.append("um.personal_rating IS NOT NULL")
        elif rated_filter == "no":
            where_parts.append("um.personal_rating IS NULL")
        where_sql = " AND ".join(where_parts)

        order_sql = "m.title ASC, m.year ASC"
        if sort == "year_desc":
            order_sql = "m.year DESC, m.title ASC"
        elif sort == "year_asc":
            order_sql = "m.year ASC, m.title ASC"
        elif sort == "rating_desc":
            order_sql = "CASE WHEN um.personal_rating IS NULL THEN 1 ELSE 0 END, um.personal_rating DESC, m.title ASC"
        elif sort == "rating_asc":
            order_sql = "CASE WHEN um.personal_rating IS NULL THEN 1 ELSE 0 END, um.personal_rating ASC, m.title ASC"
        elif sort == "recent_rated":
            order_sql = "CASE WHEN um.last_rated_at IS NULL THEN 1 ELSE 0 END, um.last_rated_at DESC, m.title ASC"
        elif sort == "recent_watched":
            order_sql = "CASE WHEN um.last_watched_at IS NULL THEN 1 ELSE 0 END, um.last_watched_at DESC, m.title ASC"

        sql = f"""
            SELECT
              m.movie_id,
              m.title as title,
              m.year as year,
              m.imdb_id as imdb_id,
              m.genre as genre,
              m.mpaa_rating as mpaa_rating,
              m.actors as actors,
              m.plot as plot,
              COALESCE(um.watched,0) as watched,
              um.personal_rating as personal_rating,
              um.last_rated_at as last_rated_at,
              um.last_watched_at as last_watched_at
            FROM movies m
            LEFT JOIN user_movie um ON um.movie_id=m.movie_id
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """
        return self.conn.execute(sql, params + [int(limit), int(offset)]).fetchall()

    def count_filtered(self, q: str, watched_filter: str = "all", rated_filter: str = "all") -> int:
        q = (q or "").strip()
        like = f"%{q}%"
        where_parts = ["(?='' OR m.title LIKE ? OR m.actors LIKE ? OR m.genre LIKE ? OR m.imdb_id LIKE ?)"]
        params = [q, like, like, like, like]
        if watched_filter == "yes":
            where_parts.append("COALESCE(um.watched,0)=1")
        elif watched_filter == "no":
            where_parts.append("COALESCE(um.watched,0)=0")
        if rated_filter == "yes":
            where_parts.append("um.personal_rating IS NOT NULL")
        elif rated_filter == "no":
            where_parts.append("um.personal_rating IS NULL")
        where_sql = " AND ".join(where_parts)
        row = self.conn.execute(f"""
            SELECT COUNT(*) as c
            FROM movies m
            LEFT JOIN user_movie um ON um.movie_id=m.movie_id
            WHERE {where_sql}
        """, params).fetchone()
        return int(row["c"]) if row else 0

    def prune_missing_files(self, roots: list[str], scan_started_utc: str):
        if not roots:
            return
        for root in roots:
            prefix = str(Path(root))
            if not prefix.endswith("\\") and not prefix.endswith("/"):
                prefix = prefix + "\\"
            like = prefix.replace("%", "\\%").replace("_", "\\_") + "%"
            self.conn.execute("""
                DELETE FROM files
                WHERE path LIKE ? ESCAPE '\\'
                  AND (last_seen_utc IS NULL OR last_seen_utc < ?)
            """, (like, scan_started_utc))
        self.conn.commit()

# --- end film_forge_db ---

# ── Paths ─────────────────────────────────────────────────────
DB_PATH          = os.environ.get("MDC_DB_PATH", "/app/film_forge.db")
OMDB_API_KEY     = os.environ.get("OMDB_API_KEY", "")
POSTER_CACHE_DIR = Path(DB_PATH).parent / "posters"
POSTER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))

# ── Flask app ──────────────────────────────────────────────────
_flask_app   = Flask(__name__)
FlaskCORS(_flask_app)
app          = _flask_app   # gunicorn entry point

WEB_DB_PATH  = DB_PATH
WEB_EDIT_KEY = os.environ.get("MDC_EDIT_KEY", "")

def _edits_enabled() -> bool:
    return bool((WEB_EDIT_KEY or "").strip())

def _check_key() -> bool:
    if not _edits_enabled():
        return True
    auth = flask_request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip() == WEB_EDIT_KEY.strip()
    return (flask_request.form.get("key") or "").strip() == WEB_EDIT_KEY.strip()

def _get_db() -> MovieDB:
    return MovieDB(WEB_DB_PATH, None)

def _poster_web_url(poster_val: str, imdb_id: str) -> str:
    """
    Return a web-accessible URL for a poster.
    The DB stores the remote OMDb URL directly, so we just return it as-is.
    Local file paths (from old versions) are ignored — we fall back to a
    fresh OMDb lookup by imdb_id if needed.
    """
    if not poster_val:
        return ""
    # If it looks like a local file path, discard it — unusable on Railway
    p = Path(poster_val)
    if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and not poster_val.startswith("http"):
        return ""
    return poster_val

@_flask_app.get("/posters/<path:filename>")
def _serve_poster(filename):
    """Serve locally cached poster images when running in local/LAN mode."""
    from flask import send_from_directory, abort
    safe_name = Path(filename).name
    for directory in [POSTER_CACHE_DIR, Path(SCRIPT_DIR) / "posters"]:
        poster_path = directory / safe_name
        if poster_path.exists():
            return send_from_directory(str(directory), safe_name)
    abort(404)

def _repair_poster_urls():
    """
    On startup: find movies whose poster column is empty or a local path,
    re-fetch from OMDb and update the DB.  Reads the OMDb key from ff_meta
    so it works on Railway without needing an environment variable.
    """
    try:
        db = _get_db()

        # Get OMDb key — prefer module global, fall back to value stored in DB
        api_key = OMDB_API_KEY or ""
        if not api_key:
            try:
                row = db.conn.execute(
                    "SELECT value FROM ff_meta WHERE key='omdb_api_key' LIMIT 1"
                ).fetchone()
                if row:
                    api_key = row[0] or ""
            except Exception:
                pass

        if not api_key:
            print("[poster-repair] no OMDb API key available — skipping")
            db.close()
            return

        rows = db.conn.execute(
            "SELECT movie_id, imdb_id, poster FROM movies "
            "WHERE imdb_id IS NOT NULL AND imdb_id != ''"
        ).fetchall()
        stale = [
            (r["movie_id"], r["imdb_id"])
            for r in rows
            if not (r["poster"] or "").startswith("http")
        ]
        if not stale:
            db.close()
            return
        print(f"[poster-repair] fixing {len(stale)} missing/stale poster URL(s)…")
        fixed = 0
        for movie_id, imdb_id in stale:
            try:
                url = f"http://www.omdbapi.com/?i={imdb_id}&apikey={api_key}"
                resp = requests.get(url, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("Response") == "True":
                        poster = data.get("Poster", "")
                        if poster and poster.lower() != "n/a":
                            db.conn.execute(
                                "UPDATE movies SET poster=? WHERE movie_id=?",
                                (poster, movie_id)
                            )
                            fixed += 1
            except Exception as e:
                print(f"[poster-repair] {imdb_id}: {e}")
        db.conn.commit()
        db.close()
        print(f"[poster-repair] done — fixed {fixed}/{len(stale)}")
    except Exception as e:
        print(f"[poster-repair] error: {e}")

# Run poster repair in background on startup so Railway self-heals
threading.Thread(target=_repair_poster_urls, daemon=True).start()

def _search_with_poster(db, q, offset, per_page, watched, rated, sort):
    q    = (q or "").strip()
    like = f"%{q}%"
    where_parts = ["(? = '' OR m.title LIKE ? OR m.actors LIKE ? OR m.genre LIKE ? OR m.imdb_id LIKE ?)"]
    params      = [q, like, like, like, like]
    if watched == "yes":  where_parts.append("COALESCE(um.watched,0)=1")
    elif watched == "no": where_parts.append("COALESCE(um.watched,0)=0")
    if rated == "yes":    where_parts.append("um.personal_rating IS NOT NULL")
    elif rated == "no":   where_parts.append("um.personal_rating IS NULL")
    order_map = {
        "year_desc":      "m.year DESC, m.title ASC",
        "year_asc":       "m.year ASC, m.title ASC",
        "rating_desc":    "CASE WHEN um.personal_rating IS NULL THEN 1 ELSE 0 END, um.personal_rating DESC, m.title ASC",
        "rating_asc":     "CASE WHEN um.personal_rating IS NULL THEN 1 ELSE 0 END, um.personal_rating ASC, m.title ASC",
        "recent_rated":   "CASE WHEN um.last_rated_at IS NULL THEN 1 ELSE 0 END, um.last_rated_at DESC, m.title ASC",
        "recent_watched": "CASE WHEN um.last_watched_at IS NULL THEN 1 ELSE 0 END, um.last_watched_at DESC, m.title ASC",
    }
    order_sql = order_map.get(sort, "m.title ASC, m.year ASC")
    sql = f"""
        SELECT m.movie_id, m.title, m.year, m.imdb_id, m.genre, m.mpaa_rating,
               m.runtime, m.actors, m.plot, m.poster,
               COALESCE(um.watched,0) AS watched,
               um.personal_rating     AS personal_rating,
               um.last_rated_at, um.last_watched_at
        FROM   movies m
        LEFT JOIN user_movie um ON um.movie_id = m.movie_id
        WHERE  {" AND ".join(where_parts)}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """
    return db.conn.execute(sql, params + [int(per_page), int(offset)]).fetchall()

def _row_to_dict(r) -> dict:
    imdb_id    = r["imdb_id"] or ""
    poster_val = r["poster"]  or ""
    return {
        "imdb_id":         imdb_id,
        "title":           r["title"]           or "",
        "year":            r["year"]            or None,
        "runtime":         r["runtime"]         or "",
        "mpaa_rating":     r["mpaa_rating"]     or "",
        "genre":           r["genre"]           or "",
        "actors":          r["actors"]          or "",
        "plot":            r["plot"]            or "",
        "poster":          _poster_web_url(poster_val, imdb_id),
        "watched":         bool(r["watched"]),
        "personal_rating": r["personal_rating"],
    }

_WEB_TEMPLATE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAABCGlDQ1BJQ0MgUHJvZmlsZQAAeJxjYGA8wQAELAYMDLl5JUVB7k4KEZFRCuwPGBiBEAwSk4sLGHADoKpv1yBqL+viUYcLcKakFicD6Q9ArFIEtBxopAiQLZIOYWuA2EkQtg2IXV5SUAJkB4DYRSFBzkB2CpCtkY7ETkJiJxcUgdT3ANk2uTmlyQh3M/Ck5oUGA2kOIJZhKGYIYnBncAL5H6IkfxEDg8VXBgbmCQixpJkMDNtbGRgkbiHEVBYwMPC3MDBsO48QQ4RJQWJRIliIBYiZ0tIYGD4tZ2DgjWRgEL7AwMAVDQsIHG5TALvNnSEfCNMZchhSgSKeDHkMyQx6QJYRgwGDIYMZAKbWPz9HbOBQAAAEg0lEQVR4nMWXy4scVRTGf+feW11VPd2dIQ8nSmLMgzzEBxIQ/wPBjYgg/gNZuFURBDfqMisXbgV3Ctko7tyIEkEUUVzEaF4aY2ISM5lkprurbt1zXFRnMpkkY2Iy4wcNRVVT33de37kl0/t6lmICAwTEC4iAGQjXYe1tVzoQ434hpKg4PNkDDqtgfCpilYFrSZfCBLB084N7EYAp2UxG/UdChwoKBG6MfgK5xdU9CxAcVhlppNjYwE+e3L8gVxaAh/HJBrMl5GsIh6Ot+RpFvBzhWvevNkQE793itJkZKSlh9alb8jpG4uW45CZ013UJqx29E6GqIrt3buPlF54lzzMGgz5Hfz3F+x98jFvt2osTYhXZuX0Lb73zOnt2bSf4wFS3RGCFEtxtZlYKRKCqa+LcHIc+/ZzvfjzCuKoQcbcXYPVdpEZAwsqKvXN4Nd5+4xXKfp+PDn3Gq28eXNYD0kYiDsJmj3PSroRJ1yJLvHAyueJAK2jm0g3vWA5VRYPnwGvv8sVX39LrdSn7xWQMaXcPalgDxSMZg8enSFcUE2uFJBAPpktIxMCEkAcuHp5DxwlTcMuzYeBDRugPEDFSk0iqrd7B3q41pxVXOHp7S7Q2Ops9aRbqvxK+50nDBl8G0jjhg0ctITgkQBoqxbYMMOJ8g9XG/C/jtiRmiAhNbJjZtIFnnn6Sr7/5not/z+GDx8xaAfE3Zd1TPeI40tQN/YdLxhcb4jkDbcWlecNPOzQqmCA6SW1U+o91aRZqRlfGlGVJWjDmT4xwHZmscSE2DfWwJu/mhAk5tEsX8UAwUmro7DKGZ2usEporidQYzRXFnOHXQbE1UP+ZSGMljZVmQdGhEWeVYp9QzzVkPd9u1WsVMCMQ6A36eOexZIulCYhgjVFdaCg2FtRHEoP9BcMTNfnGDBxIJqx/KXD64FX8lGPDcwXDnxQJQqoU33f0BiXzPwjlgxlXfx4imSySSIB8t0NNsSFYBMkceAioIZlj9PsYq3JcEBaOjSnWF1BFQi8QLzeIOsRD+YRj/Ys58XjE9YQ0Mpw5Fk4McUG4emRIM59aAdemwQkWwZWGegEP1hg+95MxNHAdR3W+JtVKMZNTrBfyzR10lJjakdN/PrJzf4nfZAw/UULPI1NGNvCkUWJ0riKNDN+RG8kFLBrV8QTeEGQxK8maJUZkrZmEzBMvN1z4cnZxoF3HwYcl0wcUn3uq2ZoLhxfwA7BmEmTm8F1pa38LH0iAVyFNRl7HTKx46Z+NRSMSL618AVHh7Hsj8q09Lh2qGB1TwrRrk9cBQdqu1puJoZ2C6SlYGEHRMfIgdCY/GezpWnPG2q2wgvuKCHhBa20PxXd4ejLajfjoloyjZ2pm1nke2uCYyo3zc9y5gOtvW3J9xxBUQZzixOFFSKqIyF0eSG7j8/8Ow7WOg5kR1RABNVuMZ01xbacJ4P57VPcH7YkoTDr+/xBgjVHuCLhMQNc+FQEMVwAdg3iLD8LVFiAI8ZJSPBKwkTA6GbFa16wiwecZOjaac4oJdDY6WpdZm0z8A2KoHwVectR/AAAAAElFTkSuQmCC">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Film Forge</title>
  <style>
body { font-family: Arial, sans-serif; margin: 18px; background:#111; color:#ddd; }
a { color:#69b4ff; }
.row { border-bottom: 1px solid #333; padding: 10px 0; }
.meta { color: #aaa; margin-top: 4px; }
.small { color: #888; font-size: 12px; }
input[type="text"] { width: 320px; padding: 6px; background:#222; color:#ddd; border:1px solid #444; border-radius:4px; }
select { padding: 6px; background:#222; color:#ddd; border:1px solid #444; border-radius:4px; }
button { padding: 6px 10px; cursor:pointer; background:#333; color:#ddd; border:1px solid #555; border-radius:4px; }
button:hover { background:#444; }
.pill { display:inline-block; padding: 2px 8px; border:1px solid #444; border-radius: 999px; font-size: 12px; margin-left: 6px; color:#aaa; }
.actions { margin-top: 8px; display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
.filters { margin-top: 10px; padding: 10px; border: 1px solid #333; border-radius: 8px; background:#1a1a1a; }
details { margin-top: 6px; }
summary { cursor: pointer; color:#69b4ff; }
.muted { color:#666; }
.toast { position: fixed; right: 16px; bottom: 16px; background: #333; color: #fff;
         padding: 10px 12px; border-radius: 8px; font-size: 13px; opacity: 0;
         transform: translateY(10px); transition: all 180ms ease; pointer-events: none; max-width: 320px; }
.toast.show { opacity: 0.95; transform: translateY(0); }
.stars { display:inline-flex; gap:4px; user-select:none; }
.star { font-size: 18px; line-height: 1; cursor:pointer; color:#555; }
.star.on { color:#f5c518; }
.pager { display:flex; gap:10px; align-items:center; margin: 12px 0; flex-wrap:wrap; }
.badge { display:inline-block; padding: 2px 8px; border:1px solid #444; border-radius: 999px; font-size: 12px; }
.quick { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:10px; }
.quick a { text-decoration:none; padding:6px 10px; border:1px solid #444; border-radius:8px; color:#aaa; }
.quick a.active { font-weight:bold; color:#fff; border-color:#888; }
h2 { color:#fff; margin-bottom:4px; }
.api-note { font-size:13px; color:#888; margin-top:6px; }
  </style>
</head>
<body>
  <h2 style="display:flex; align-items:center;"><img src="data:image/png;base64,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" style="height:40px; width:150px; vertical-align:middle; margin-right:10px;">Film Forge <span style="font-size:13px; color:#666; font-weight:normal; margin-left:8px;">v{{ version }}</span></h2>
  <div class="api-note">{{ grand_total }} movies &nbsp;·&nbsp; <span title="Last synced from Film Forge desktop app">🔄 Last synced: <b>{{ last_updated }}</b></span> &nbsp;·&nbsp; JSON API at <a href="/api/docs">/api/docs</a></div>

  <form method="get" action="/" class="filters" id="searchForm">
<div style="margin-bottom:10px;">
  <input type="text" name="q" placeholder="Search title, actor, genre, IMDb ID..." value="{{ q|e }}">
  <button type="submit">Search</button>
</div>
<div class="quick">
  <span class="badge">Quick views:</span>
  <a href="/?{{ qs_view_all }}"            class="{{ 'active' if view=='all'            else '' }}">All</a>
  <a href="/?{{ qs_view_unrated }}"         class="{{ 'active' if view=='unrated'         else '' }}">Unrated</a>
  <a href="/?{{ qs_view_watched }}"         class="{{ 'active' if view=='watched'         else '' }}">Watched</a>
  <a href="/?{{ qs_view_unwatched }}"       class="{{ 'active' if view=='unwatched'       else '' }}">Unwatched</a>
  <a href="/?{{ qs_view_recent_rated }}"    class="{{ 'active' if view=='recent_rated'    else '' }}">Recently Rated</a>
  <a href="/?{{ qs_view_recent_watched }}"  class="{{ 'active' if view=='recent_watched'  else '' }}">Recently Watched</a>
</div>
<div style="display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin-top:10px;">
  <div><label class="small"><b>Watched</b></label><br>
    <select name="watched" onchange="document.getElementById('searchForm').submit()">
      <option value="all" {{ 'selected' if watched=='all' else '' }}>All</option>
      <option value="yes" {{ 'selected' if watched=='yes' else '' }}>Watched</option>
      <option value="no"  {{ 'selected' if watched=='no'  else '' }}>Unwatched</option>
    </select></div>
  <div><label class="small"><b>Rated</b></label><br>
    <select name="rated" onchange="document.getElementById('searchForm').submit()">
      <option value="all" {{ 'selected' if rated=='all' else '' }}>All</option>
      <option value="yes" {{ 'selected' if rated=='yes' else '' }}>Rated</option>
      <option value="no"  {{ 'selected' if rated=='no'  else '' }}>Unrated</option>
    </select></div>
  <div><label class="small"><b>Sort</b></label><br>
    <select name="sort" onchange="document.getElementById('searchForm').submit()">
      <option value="title"          {{ 'selected' if sort=='title'          else '' }}>Title (A–Z)</option>
      <option value="year_desc"      {{ 'selected' if sort=='year_desc'      else '' }}>Year (new → old)</option>
      <option value="year_asc"       {{ 'selected' if sort=='year_asc'       else '' }}>Year (old → new)</option>
      <option value="rating_desc"    {{ 'selected' if sort=='rating_desc'    else '' }}>Rating (high → low)</option>
      <option value="rating_asc"     {{ 'selected' if sort=='rating_asc'     else '' }}>Rating (low → high)</option>
      <option value="recent_rated"   {{ 'selected' if sort=='recent_rated'   else '' }}>Recently rated</option>
      <option value="recent_watched" {{ 'selected' if sort=='recent_watched' else '' }}>Recently watched</option>
    </select></div>
  <div><label class="small"><b>Edit key</b></label><br>
    <input type="text" id="editKey" placeholder="(optional)" style="width:180px; padding:6px;"></div>
  <div class="small muted">
    {% if edits_enabled %}Edits require the Edit key.{% else %}Edits are open (no key set).{% endif %}
  </div>
</div>
<input type="hidden" name="page"     value="{{ page }}">
<input type="hidden" name="per_page" value="{{ per_page }}">
<input type="hidden" name="view"     value="{{ view }}">
  </form>

<div class="pager small">
<span><b>Results:</b> {{ total }} (showing {{ shown_from }}–{{ shown_to }})</span>
<span class="badge">Page {{ page }} / {{ pages }}</span>
{% if page > 1 %}<a href="/?{{ qs_first }}">First</a>{% endif %}
{% if page > 1 %}<a href="/?{{ qs_prev }}">Prev</a>{% endif %}
{% if page < pages %}<a href="/?{{ qs_next }}">Next</a>{% endif %}
{% if page < pages %}<a href="/?{{ qs_last }}">Last</a>{% endif %}
&nbsp;
<form style="display:inline" method="get" action="/">
  {% for k, v in qs_hidden.items() %}<input type="hidden" name="{{ k }}" value="{{ v }}">{% endfor %}
  Go to page: <input type="number" name="page" min="1" max="{{ pages }}" value="{{ page }}"
    style="width:55px; padding:3px; background:#222; color:#ddd; border:1px solid #444; border-radius:4px;">
  <button type="submit">Go</button>
</form>
Per page: <a href="/?{{ qs_pp_50 }}">50</a> | <a href="/?{{ qs_pp_100 }}">100</a>
</div>

  {% for r in results %}
<div class="row" data-imdb="{{ r.imdb_id }}">
  <div style="display:flex; gap:14px; align-items:flex-start;">
    {% if r.poster %}
      <img src="{{ r.poster }}" alt="poster"
           style="width:80px; min-width:80px; border-radius:4px; border:1px solid #333; object-fit:cover;">
    {% else %}
      <div style="width:80px; min-width:80px; height:120px; background:#222; border-radius:4px;
                  border:1px solid #333; display:flex; align-items:center; justify-content:center;
                  color:#555; font-size:22px;">🎬</div>
    {% endif %}
    <div style="flex:1;">
      <div>
        <b>{{ r.title }}</b>{% if r.year %} ({{ r.year }}){% endif %}
        <span class="pill">{{ r.imdb_id }}</span>
        {% if r.mpaa_rating %}<span class="pill">{{ r.mpaa_rating }}</span>{% endif %}
        {% if r.runtime %}<span class="pill">{{ r.runtime }}</span>{% endif %}
      </div>
      <div class="meta">
        {% if r.actors %}<div><b>Actors:</b> {{ r.actors }}</div>{% endif %}
        {% if r.genre  %}<div><b>Genre:</b>  {{ r.genre  }}</div>{% endif %}
        {% if r.plot %}
          <div style="margin-top:6px;color:#bbb;font-size:13px;">{{ r.plot }}</div>
        {% endif %}
      </div>
      <div class="actions">
        <button class="watchedBtn" data-target="{{ 0 if r.watched else 1 }}">{{ 'Unwatch' if r.watched else 'Watch' }}</button>
        <span class="small">Rating:</span>
        <span class="stars">
          {% for s in [1,2,3,4,5] %}
            <span class="star {{ 'on' if r.personal_rating is not none and r.personal_rating >= s else '' }}" data-star="{{ s }}">★</span>
          {% endfor %}
        </span>
        <button class="clearRatingBtn">Clear</button>
        <span class="small" style="margin-left:10px;">
          Watched: <span class="watchedText">{{ 'Yes' if r.watched else 'No' }}</span> |
          Rating:  <span class="ratingText">{{ '' if r.personal_rating is none else r.personal_rating }}</span>
        </span>
      </div>
    </div>
  </div>
</div>
  {% endfor %}

  <div class="pager small">
{% if page > 1 %}<a href="/?{{ qs_first }}">First</a>{% endif %}
{% if page > 1 %}<a href="/?{{ qs_prev }}">Prev</a>{% endif %}
{% if page < pages %}<a href="/?{{ qs_next }}">Next</a>{% endif %}
{% if page < pages %}<a href="/?{{ qs_last }}">Last</a>{% endif %}
  </div>
  <div class="toast" id="toast"></div>
<script>
(function(){
  const toastEl = document.getElementById('toast');
  function toast(msg){ if(!toastEl) return; toastEl.textContent=msg; toastEl.classList.add('show'); setTimeout(()=>toastEl.classList.remove('show'),1400); }
  const keyInput = document.getElementById('editKey');
  if(keyInput){ keyInput.value=localStorage.getItem('mdc_edit_key')||''; keyInput.addEventListener('change',()=>{ localStorage.setItem('mdc_edit_key',keyInput.value||''); }); }
  function currentKey(){ return (localStorage.getItem('mdc_edit_key')||'').trim(); }
  async function postForm(url,data){ const resp=await fetch(url,{method:'POST',headers:{'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams(data)}); const js=await resp.json().catch(()=>({ok:false,error:'Bad response'})); if(!resp.ok||!js.ok) throw new Error(js.error||('HTTP '+resp.status)); return js; }
  function rowFor(el){ return el.closest('.row'); }
  function setRowWatched(row,w){ row.querySelector('.watchedText').textContent=w?'Yes':'No'; const b=row.querySelector('.watchedBtn'); b.dataset.target=w?'0':'1'; b.textContent=w?'Unwatch':'Watch'; }
  function setRowRating(row,r){ row.querySelector('.ratingText').textContent=(r==null)?'':String(r); row.querySelectorAll('.star').forEach(s=>s.classList.toggle('on',r!=null&&r>=parseInt(s.dataset.star,10))); }
  document.addEventListener('click',async ev=>{
const t=ev.target; if(!(t instanceof HTMLElement)) return;
if(t.classList.contains('watchedBtn')){ ev.preventDefault(); const row=rowFor(t); const imdb=row?.dataset?.imdb; if(!imdb) return; const tgt=t.dataset.target||'1'; try{ await postForm('/api/set_watched',{imdb_id:imdb,watched:tgt,key:currentKey()}); setRowWatched(row,tgt==='1'); toast('Saved'); }catch(e){ toast('Not saved: '+e.message); } }
if(t.classList.contains('star')){ ev.preventDefault(); const row=rowFor(t); const imdb=row?.dataset?.imdb; if(!imdb) return; try{ const js=await postForm('/api/set_rating',{imdb_id:imdb,rating:t.dataset.star,key:currentKey()}); setRowRating(row,js.rating); toast('Saved'); }catch(e){ toast('Not saved: '+e.message); } }
if(t.classList.contains('clearRatingBtn')){ ev.preventDefault(); const row=rowFor(t); const imdb=row?.dataset?.imdb; if(!imdb) return; try{ const js=await postForm('/api/set_rating',{imdb_id:imdb,rating:'',key:currentKey()}); setRowRating(row,js.rating); toast('Cleared'); }catch(e){ toast('Not saved: '+e.message); } }
  });
})();
</script>
</body>
</html>"""

def _build_qs(**kwargs):
    d = dict(flask_request.args)
    for k, v in kwargs.items():
        if v is None: d.pop(k, None)
        else: d[k] = str(v)
    return urlencode([(k, d[k]) for k in sorted(d.keys())])

@_flask_app.get("/")
def _web_index():
    q       = (flask_request.args.get("q")       or "").strip()
    watched = (flask_request.args.get("watched") or "all").strip().lower()
    rated   = (flask_request.args.get("rated")   or "all").strip().lower()
    sort    = (flask_request.args.get("sort")    or "title").strip().lower()
    view    = (flask_request.args.get("view")    or "all").strip().lower()
    page    = max(1, int(flask_request.args.get("page")     or 1))
    per_page = int(flask_request.args.get("per_page") or 50)
    per_page = 50 if per_page not in (50, 100) else per_page

    if   view == "unrated":        rated   = "no"
    elif view == "watched":        watched = "yes"
    elif view == "unwatched":      watched = "no"
    elif view == "recent_rated":   sort    = "recent_rated"
    elif view == "recent_watched": sort    = "recent_watched"

    db = _get_db()
    grand_total = db.conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
    # Prefer explicit sync timestamp, fall back to most recent movie update
    try:
        sync_ts = db.conn.execute(
            "SELECT value FROM ff_meta WHERE key='last_synced' LIMIT 1"
        ).fetchone()
        row_ts = sync_ts[0] if sync_ts else None
    except Exception:
        row_ts = None
    if not row_ts:
        try:
            row_ts = db.conn.execute("SELECT MAX(updated_at) FROM movies").fetchone()[0]
        except Exception:
            row_ts = None
    if row_ts:
        try:
            from datetime import datetime as _dt, timezone as _tz, timedelta
            ts = _dt.fromisoformat(row_ts.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_tz.utc)
            # Determine EST (-5) vs EDT (-4) based on approximate DST rules:
            # EDT: 2nd Sunday in March 2:00am -> 1st Sunday in November 2:00am
            year = ts.year
            # 2nd Sunday in March
            mar1 = _dt(year, 3, 1, 2, 0, tzinfo=_tz.utc)
            dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
            # 1st Sunday in November
            nov1 = _dt(year, 11, 1, 2, 0, tzinfo=_tz.utc)
            dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
            if dst_start <= ts < dst_end:
                offset = timedelta(hours=-4)  # EDT
                tz_label = "EDT"
            else:
                offset = timedelta(hours=-5)  # EST
                tz_label = "EST"
            ts_eastern = ts + offset
            last_updated = ts_eastern.strftime(f"%b %d, %Y  %I:%M %p {tz_label}")
        except Exception as _ts_err:
            print(f"[timestamp] failed: {_ts_err!r}")
            last_updated = row_ts
    else:
        last_updated = "never"

    total   = db.count_filtered(q, watched_filter=watched, rated_filter=rated)
    pages   = max(1, math.ceil(total / per_page))
    page    = min(page, pages)
    raw_results = _search_with_poster(db, q, (page-1)*per_page, per_page, watched, rated, sort)
    db.close()

    # Resolve local poster paths to web-accessible URLs
    results = [_row_to_dict(r) for r in raw_results]

    return render_template_string(
        _WEB_TEMPLATE,
        version=__version__,
        q=q, watched=watched, rated=rated, sort=sort, view=view,
        page=page, pages=pages, per_page=per_page, total=total,
        grand_total=grand_total, last_updated=last_updated,
        shown_from=0 if total == 0 else (page-1)*per_page+1,
        shown_to=min(total, (page-1)*per_page+len(results)),
        results=results, edits_enabled=_edits_enabled(),
        qs_prev=_build_qs(page=page-1),    qs_next=_build_qs(page=page+1),
        qs_pp_50=_build_qs(per_page=50,page=1), qs_pp_100=_build_qs(per_page=100,page=1),
        qs_view_all=_build_qs(view="all",page=1),
        qs_view_unrated=_build_qs(view="unrated",page=1),
        qs_view_watched=_build_qs(view="watched",page=1),
        qs_view_unwatched=_build_qs(view="unwatched",page=1),
        qs_view_recent_rated=_build_qs(view="recent_rated",page=1),
        qs_view_recent_watched=_build_qs(view="recent_watched",page=1),
        qs_first=_build_qs(page=1),
        qs_last=_build_qs(page=pages),
        qs_hidden={k: v for k, v in dict(flask_request.args).items() if k !='page'},
    )

@_flask_app.get("/api/movies")
def _api_movies():
    q        = (flask_request.args.get("q")       or "").strip()
    watched  = (flask_request.args.get("watched") or "all").strip().lower()
    rated    = (flask_request.args.get("rated")   or "all").strip().lower()
    sort     = (flask_request.args.get("sort")    or "title").strip().lower()
    try:
        page     = max(1, int(flask_request.args.get("page")     or 1))
        per_page = int(flask_request.args.get("per_page") or 25)
        per_page = per_page if per_page in (10, 25, 50, 100) else 25
    except ValueError:
        return jsonify({"ok": False, "error": "page/per_page must be integers"}), 400
    db    = _get_db()
    total = db.count_filtered(q, watched_filter=watched, rated_filter=rated)
    pages = max(1, math.ceil(total / per_page))
    page  = min(page, pages)
    rows  = db.search_page(q, (page-1)*per_page, per_page,
                           watched_filter=watched, rated_filter=rated, sort=sort)
    db.close()
    return jsonify({"ok": True, "total": total, "page": page,
                    "pages": pages, "per_page": per_page,
                    "movies": [_row_to_dict(r) for r in rows]})

@_flask_app.get("/api/movies/<imdb_id>")
def _api_movie_detail(imdb_id: str):
    db  = _get_db()
    row = db.conn.execute("""
        SELECT m.imdb_id, m.title, m.year, m.runtime, m.mpaa_rating,
               m.genre, m.actors, m.plot, m.poster,
               COALESCE(um.watched,0) AS watched,
               um.personal_rating     AS personal_rating
        FROM   movies m
        LEFT JOIN user_movie um ON um.movie_id = m.movie_id
        WHERE  m.imdb_id = ? LIMIT 1
    """, (imdb_id,)).fetchone()
    db.close()
    if not row:
        return jsonify({"ok": False, "error": "Not found"}), 404
    _id  = row["imdb_id"] or ""
    return jsonify({"ok": True, "movie": {
        "imdb_id": _id, "title": row["title"] or "",
        "year": row["year"], "runtime": row["runtime"] or "",
        "mpaa_rating": row["mpaa_rating"] or "", "genre": row["genre"] or "",
        "actors": row["actors"] or "", "plot": row["plot"] or "",
        "poster": _poster_web_url(row["poster"] or "", _id),
        "watched": bool(row["watched"]),
        "personal_rating": row["personal_rating"],
    }})

@_flask_app.post("/api/set_watched")
def _api_set_watched():
    imdb_id = (flask_request.form.get("imdb_id") or "").strip()
    watched = 1 if (flask_request.form.get("watched") or "").strip().lower() in ("1","true","yes","on") else 0
    if not imdb_id: return jsonify({"ok": False, "error": "Missing imdb_id"}), 400
    if not _check_key(): return jsonify({"ok": False, "error": "Invalid edit key"}), 403
    db = _get_db(); db.set_watched(imdb_id, watched); db.close()
    return jsonify({"ok": True, "imdb_id": imdb_id, "watched": watched})

@_flask_app.post("/api/set_rating")
def _api_set_rating():
    imdb_id    = (flask_request.form.get("imdb_id") or "").strip()
    rating_raw = (flask_request.form.get("rating")  or "").strip()
    if not imdb_id: return jsonify({"ok": False, "error": "Missing imdb_id"}), 400
    if not _check_key(): return jsonify({"ok": False, "error": "Invalid edit key"}), 403
    rating = None
    if rating_raw:
        try:
            rating = float(rating_raw)
        except ValueError:
            return jsonify({"ok": False, "error": "Rating must be a number"}), 400
        if not (1.0 <= rating <= 5.0):
            return jsonify({"ok": False, "error": "Rating must be 1–5"}), 400
    db = _get_db(); db.set_rating(imdb_id, rating); db.close()
    return jsonify({"ok": True, "imdb_id": imdb_id, "rating": rating})

@_flask_app.get("/api/docs")
def _api_docs():
    host = flask_request.host_url.rstrip("/")
    return (
        f"Film Forge — JSON API\n"
        f"===================================\n\n"
        f"Base URL: {host}\n\n"
        f"GET  /api/movies\n"
        f"  Params: q, page, per_page (10|25|50|100), watched (all|yes|no),\n"
        f"          rated (all|yes|no), sort (title|year_desc|year_asc|\n"
        f"          rating_desc|rating_asc|recent_rated|recent_watched)\n"
        f"  Returns: {{ok, total, page, pages, per_page, movies:[...]}}\n\n"
        f"GET  /api/movies/<imdb_id>\n"
        f"  Returns: {{ok, movie:{{imdb_id,title,year,runtime,mpaa_rating,\n"
        f"            genre,actors,plot,poster,watched,personal_rating}}}}\n\n"
        f"POST /api/set_watched\n"
        f"  Body (form):  imdb_id=tt1234567&watched=1\n"
        f"  Auth header:  Authorization: Bearer <edit_key>\n\n"
        f"POST /api/set_rating\n"
        f"  Body (form):  imdb_id=tt1234567&rating=4   (1-5, blank=clear)\n"
        f"  Auth header:  Authorization: Bearer <edit_key>\n\n"
        f"Edit key required: {'yes' if _edits_enabled() else 'no (open to all)'}\n"
    ), 200, {"Content-Type": "text/plain"}

# end Flask block

if __name__ == "__main__":
    _flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)))
