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
