"""SQLite database layer — mirrors Room DB from the Android app."""

import json
import sqlite3
import os
import logging
from contextlib import contextmanager

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "nostalgiavision.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS content (
            rating_key      TEXT PRIMARY KEY,
            key             TEXT,
            media_part_key  TEXT,
            type            TEXT,          -- 'movie', 'episode', 'show', 'track'
            title           TEXT,
            summary         TEXT,
            year            INTEGER,
            studio          TEXT,
            content_rating  TEXT,
            duration        INTEGER,       -- ms
            show_rating_key TEXT,
            show_title      TEXT,
            season_number   INTEGER,
            episode_number  INTEGER,
            episode_title   TEXT,
            episode_summary TEXT,
            container       TEXT,
            audio_codec     TEXT,
            video_codec     TEXT,
            video_resolution TEXT,
            has_subtitles   INTEGER DEFAULT 0,
            thumb_url       TEXT,
            art_url         TEXT,
            clear_logo_url  TEXT,
            added_at        INTEGER,       -- unix timestamp
            library_id      TEXT
        );

        CREATE TABLE IF NOT EXISTS content_genres (
            rating_key TEXT,
            genre      TEXT,
            PRIMARY KEY (rating_key, genre)
        );

        CREATE TABLE IF NOT EXISTS content_collections (
            rating_key      TEXT,
            collection_id   TEXT,
            collection_name TEXT,
            PRIMARY KEY (rating_key, collection_id)
        );

        CREATE TABLE IF NOT EXISTS cache_metadata (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            cached_at   INTEGER,           -- unix timestamp
            library_ids TEXT               -- JSON list
        );

        CREATE TABLE IF NOT EXISTS programs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id      TEXT NOT NULL,
            rating_key      TEXT,
            media_part_key  TEXT,
            type            TEXT,
            title           TEXT,
            show_title      TEXT,
            summary         TEXT,
            season_number   INTEGER,
            episode_number  INTEGER,
            episode_title   TEXT,
            content_rating  TEXT,
            video_resolution TEXT,
            audio_codec     TEXT,
            video_codec     TEXT,
            has_subtitles   INTEGER DEFAULT 0,
            thumb_url       TEXT,
            art_url         TEXT,
            clear_logo_url  TEXT,
            duration_ms     INTEGER,           -- actual Plex duration (pre-alignment)
            start_time      INTEGER NOT NULL,  -- ms since epoch
            end_time        INTEGER NOT NULL,
            is_new          INTEGER DEFAULT 0,
            commercial_media_key TEXT,
            commercial_duration  INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_programs_channel_time
            ON programs (channel_id, start_time, end_time);
        """)
        # Migration: add duration_ms if it doesn't exist yet (for existing DBs)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(programs)").fetchall()]
        if "duration_ms" not in cols:
            conn.execute("ALTER TABLE programs ADD COLUMN duration_ms INTEGER")
        conn.executescript("""

        CREATE TABLE IF NOT EXISTS custom_channels (
            number     INTEGER PRIMARY KEY,
            config_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS channel_overrides (
            number        INTEGER PRIMARY KEY,
            override_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hidden_channels (
            number INTEGER PRIMARY KEY
        );
        """)
    log.info("Database initialised at %s", DB_PATH)


# ── Content cache ──────────────────────────────────────────────────────────────

def upsert_content_batch(rows: list):
    """Bulk insert/replace content rows."""
    if not rows:
        return
    with get_db() as conn:
        conn.executemany("""
            INSERT OR REPLACE INTO content
            (rating_key, key, media_part_key, type, title, summary, year,
             studio, content_rating, duration, show_rating_key, show_title,
             season_number, episode_number, episode_title, episode_summary,
             container, audio_codec, video_codec, video_resolution,
             has_subtitles, thumb_url, art_url, clear_logo_url, added_at,
             library_id)
            VALUES
            (:rating_key,:key,:media_part_key,:type,:title,:summary,:year,
             :studio,:content_rating,:duration,:show_rating_key,:show_title,
             :season_number,:episode_number,:episode_title,:episode_summary,
             :container,:audio_codec,:video_codec,:video_resolution,
             :has_subtitles,:thumb_url,:art_url,:clear_logo_url,:added_at,
             :library_id)
        """, rows)


def upsert_genres_batch(rows: list):
    if not rows:
        return
    with get_db() as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO content_genres (rating_key, genre)
            VALUES (:rating_key, :genre)
        """, rows)


def upsert_collections_batch(rows: list):
    if not rows:
        return
    with get_db() as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO content_collections
            (rating_key, collection_id, collection_name)
            VALUES (:rating_key, :collection_id, :collection_name)
        """, rows)


def set_cache_metadata(cached_at: int, library_ids: str):
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO cache_metadata (id, cached_at, library_ids)
            VALUES (1, ?, ?)
        """, (cached_at, library_ids))


def get_cache_metadata() -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM cache_metadata WHERE id = 1").fetchone()
        return dict(row) if row else None


def get_all_content(type_filter: str | None = None) -> list:
    with get_db() as conn:
        if type_filter:
            rows = conn.execute(
                "SELECT * FROM content WHERE type = ?", (type_filter,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM content").fetchall()
        return [dict(r) for r in rows]


def get_content_by_key(rating_key: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM content WHERE rating_key = ?", (rating_key,)
        ).fetchone()
        return dict(row) if row else None


def get_content_by_keys(keys: list) -> list:
    if not keys:
        return []
    placeholders = ",".join("?" * len(keys))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM content WHERE rating_key IN ({placeholders})", keys
        ).fetchall()
        return [dict(r) for r in rows]


def get_genres_for_keys(keys: list) -> dict:
    """Returns {rating_key: [genre, ...]}"""
    if not keys:
        return {}
    placeholders = ",".join("?" * len(keys))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT rating_key, genre FROM content_genres WHERE rating_key IN ({placeholders})",
            keys
        ).fetchall()
    result: dict = {}
    for r in rows:
        result.setdefault(r["rating_key"], []).append(r["genre"])
    return result


def get_collections_for_keys(keys: list) -> dict:
    """Returns {rating_key: [{collection_id, collection_name}, ...]}"""
    if not keys:
        return {}
    placeholders = ",".join("?" * len(keys))
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT rating_key, collection_id, collection_name
                FROM content_collections
                WHERE rating_key IN ({placeholders})""",
            keys
        ).fetchall()
    result: dict = {}
    for r in rows:
        result.setdefault(r["rating_key"], []).append(
            {"collection_id": r["collection_id"], "collection_name": r["collection_name"]}
        )
    return result


def clear_content():
    with get_db() as conn:
        conn.execute("DELETE FROM content")
        conn.execute("DELETE FROM content_genres")
        conn.execute("DELETE FROM content_collections")
        conn.execute("DELETE FROM cache_metadata")


def get_content_count() -> dict:
    with get_db() as conn:
        movies = conn.execute("SELECT COUNT(*) FROM content WHERE type='movie'").fetchone()[0]
        episodes = conn.execute("SELECT COUNT(*) FROM content WHERE type='episode'").fetchone()[0]
        shows = conn.execute("SELECT COUNT(*) FROM content WHERE type='show'").fetchone()[0]
        return {"movies": movies, "episodes": episodes, "shows": shows}


# ── Programs ───────────────────────────────────────────────────────────────────

def insert_programs(rows: list):
    if not rows:
        return
    with get_db() as conn:
        conn.executemany("""
            INSERT INTO programs
            (channel_id, rating_key, media_part_key, type, title, show_title,
             summary, season_number, episode_number, episode_title,
             content_rating, video_resolution, audio_codec, video_codec,
             has_subtitles, thumb_url, art_url, clear_logo_url,
             duration_ms, start_time, end_time, is_new,
             commercial_media_key, commercial_duration)
            VALUES
            (:channel_id,:rating_key,:media_part_key,:type,:title,:show_title,
             :summary,:season_number,:episode_number,:episode_title,
             :content_rating,:video_resolution,:audio_codec,:video_codec,
             :has_subtitles,:thumb_url,:art_url,:clear_logo_url,
             :duration_ms,:start_time,:end_time,:is_new,
             :commercial_media_key,:commercial_duration)
        """, rows)


def delete_channel_programs_from(channel_id: str, from_time: int):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM programs WHERE channel_id = ? AND start_time >= ?",
            (channel_id, from_time)
        )


def delete_expired_programs(cutoff_time: int):
    with get_db() as conn:
        conn.execute("DELETE FROM programs WHERE end_time < ?", (cutoff_time,))


def get_programs_for_channel(channel_id: str, start: int, end: int) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM programs
            WHERE channel_id = ?
              AND end_time > ?
              AND start_time < ?
            ORDER BY start_time
        """, (channel_id, start, end)).fetchall()
        return [dict(r) for r in rows]


def get_all_programs(start: int, end: int) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM programs
            WHERE end_time > ? AND start_time < ?
            ORDER BY channel_id, start_time
        """, (start, end)).fetchall()
        return [dict(r) for r in rows]


def get_latest_program_end(channel_id: str) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(end_time) FROM programs WHERE channel_id = ?",
            (channel_id,)
        ).fetchone()
        return row[0] or 0


def clear_all_programs():
    with get_db() as conn:
        conn.execute("DELETE FROM programs")


# ── Custom channels / overrides ────────────────────────────────────────────────

def save_custom_channel(number: int, config_json: str):
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO custom_channels (number, config_json)
            VALUES (?, ?)
        """, (number, config_json))


def get_custom_channels() -> list:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM custom_channels ORDER BY number").fetchall()
        return [dict(r) for r in rows]


def delete_custom_channel(number: int):
    with get_db() as conn:
        conn.execute("DELETE FROM custom_channels WHERE number = ?", (number,))


def save_channel_override(number: int, override_json: str):
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO channel_overrides (number, override_json)
            VALUES (?, ?)
        """, (number, override_json))


def get_channel_override(number: int) -> str | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT override_json FROM channel_overrides WHERE number = ?",
            (number,)
        ).fetchone()
        return row["override_json"] if row else None


def get_all_channel_overrides() -> dict:
    """Return {number: config_dict} for all overridden predefined channels."""
    with get_db() as conn:
        rows = conn.execute("SELECT number, override_json FROM channel_overrides").fetchall()
    result = {}
    for r in rows:
        try:
            result[r["number"]] = json.loads(r["override_json"])
        except Exception:
            pass
    return result


def delete_channel_override(number: int):
    with get_db() as conn:
        conn.execute("DELETE FROM channel_overrides WHERE number = ?", (number,))


def set_channel_visibility(number: int, visible: bool):
    with get_db() as conn:
        if visible:
            conn.execute("DELETE FROM hidden_channels WHERE number = ?", (number,))
        else:
            conn.execute(
                "INSERT OR IGNORE INTO hidden_channels (number) VALUES (?)",
                (number,)
            )


def get_hidden_channels() -> set:
    with get_db() as conn:
        rows = conn.execute("SELECT number FROM hidden_channels").fetchall()
        return {r["number"] for r in rows}
