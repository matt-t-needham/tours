"""SQLite schema and connection helpers.

Events are never deleted — a row absent from a full pull gets `removed_at` set.
`raw_json` is kept on every event so new fields can be backfilled without
spending API calls.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS plex_artist (
    rating_key   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    norm_name    TEXT NOT NULL,
    mbid         TEXT,
    genres       TEXT,
    ignored      INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plex_artist_norm ON plex_artist(norm_name);
CREATE INDEX IF NOT EXISTS idx_plex_artist_mbid ON plex_artist(mbid);

CREATE TABLE IF NOT EXISTS jb_artist (
    jambase_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    norm_name    TEXT NOT NULL,
    mbid         TEXT,
    genres       TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jb_artist_norm ON jb_artist(norm_name);

-- Keyed on the JamBase artist id, so a confirmed decision survives forever and
-- a name collision only ever costs one review.
CREATE TABLE IF NOT EXISTS artist_link (
    plex_rating_key TEXT NOT NULL,
    jambase_id      TEXT NOT NULL,
    status          TEXT NOT NULL,   -- auto | pending | confirmed | rejected
    method          TEXT NOT NULL,   -- mbid | name
    decided_at      TEXT,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (plex_rating_key, jambase_id)
);
CREATE INDEX IF NOT EXISTS idx_link_jb ON artist_link(jambase_id);
CREATE INDEX IF NOT EXISTS idx_link_status ON artist_link(status);

CREATE TABLE IF NOT EXISTS event (
    jambase_id       TEXT PRIMARY KEY,
    name             TEXT,
    start_local      TEXT,           -- venue-local, NO offset. Never UTC-convert.
    door_local       TEXT,
    tz               TEXT,           -- from location.address.x-timezone
    venue            TEXT,
    city             TEXT,
    city_lat         REAL,
    city_lon         REAL,
    metro_id         TEXT,           -- derived from the venue city, not the query
    status           TEXT,           -- scheduled | postponed | rescheduled | cancelled
    status_changed_at TEXT,
    previous_start   TEXT,
    event_type       TEXT,           -- Concert | Festival
    date_published   TEXT,           -- when JamBase first announced it
    date_modified    TEXT,
    event_url        TEXT,
    ticket_url       TEXT,
    ticket_seller    TEXT,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    removed_at       TEXT,
    raw_json         TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_start ON event(start_local);
CREATE INDEX IF NOT EXISTS idx_event_metro ON event(metro_id);

CREATE TABLE IF NOT EXISTS event_performer (
    event_id          TEXT NOT NULL,
    jambase_artist_id TEXT NOT NULL,
    is_headliner      INTEGER NOT NULL DEFAULT 0,
    rank              INTEGER,
    PRIMARY KEY (event_id, jambase_artist_id)
);
CREATE INDEX IF NOT EXISTS idx_perf_artist ON event_performer(jambase_artist_id);

-- Every HTTP request actually sent, including retries and failures, because
-- that is what JamBase bills. `params` is scrubbed of anything secret.
CREATE TABLE IF NOT EXISTS api_call (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    params          TEXT,
    http_status     INTEGER,
    items_returned  INTEGER,
    rate_remaining  INTEGER,         -- from the RateLimit header (hour window)
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_call_ts ON api_call(ts);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Saved trip searches (trips.py): a place + date range, searched at will and
-- kept apart from the west-coast tables so they never reach the main list,
-- the digests or the removal logic. Only matched shows are stored, in
-- `results`, and the row is deleted once date_to passes or the results are
-- older than JAMBASE_RETENTION_DAYS.
CREATE TABLE IF NOT EXISTS trip_search (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    region      TEXT NOT NULL,   -- tab label: the metro, or the city if none
    query       TEXT NOT NULL,   -- what was typed, re-resolved on update
    place       TEXT NOT NULL,   -- what it resolved to, e.g. "Cambridge, US-MA"
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    radius_mi   INTEGER NOT NULL,
    date_from   TEXT NOT NULL,
    date_to     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    events_seen INTEGER NOT NULL DEFAULT 0,
    calls       INTEGER NOT NULL DEFAULT 0,
    results     TEXT NOT NULL DEFAULT '[]'
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Columns added after a table first shipped. SQLite has no "ADD COLUMN IF NOT
# EXISTS", and the DB carries review decisions we do not want to rebuild, so
# migrate in place rather than recreating.
_ADDED_COLUMNS = {
    "plex_artist": {"genres": "TEXT"},
    "event": {"event_type": "TEXT", "date_published": "TEXT",
              "date_modified": "TEXT", "ticket_seller": "TEXT"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    if get_state(conn, "schema_version") is None:
        set_state(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_state(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value) if isinstance(value, (dict, list)) else str(value)),
    )
    conn.commit()


# Tables holding data obtained from JamBase. The trial terms require deleting
# JamBase data when the trial ends; `wipe_jambase_data` makes that one command
# rather than a day-14 scramble. plex_artist is OUR data and survives.
JAMBASE_TABLES = ("event_performer", "event", "jb_artist", "artist_link", "trip_search")


def wipe_jambase_data(conn: sqlite3.Connection) -> dict[str, int]:
    """Delete every row derived from JamBase. Returns per-table counts."""
    counts = {}
    for table in JAMBASE_TABLES:
        counts[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        conn.execute(f"DELETE FROM {table}")
    # Metro IDs and event counts learned from JamBase are config-shaped facts,
    # but clear the cached geography anyway so nothing survives by accident.
    conn.execute("DELETE FROM sync_state WHERE key LIKE 'metro:%'")
    conn.commit()
    return counts
