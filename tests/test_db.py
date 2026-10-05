from datetime import datetime, timezone

import db
import jambase
import pytest


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init(c)
    return c


def test_init_is_idempotent(conn):
    db.init(conn)
    assert db.get_state(conn, "schema_version") == str(db.SCHEMA_VERSION)


def test_quota_period_start_calendar_month(conn):
    ref = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    assert jambase.quota_period_start(conn, ref).date().isoformat() == "2026-09-01"


def test_quota_period_start_honours_anchor_day(conn):
    """We do not know whether JamBase resets on the calendar month or the
    signup date, so the anchor is configurable."""
    db.set_state(conn, "quota_anchor_day", "14")
    ref = datetime(2026, 9, 23, tzinfo=timezone.utc)
    assert jambase.quota_period_start(conn, ref).date().isoformat() == "2026-09-14"
    ref = datetime(2026, 9, 3, tzinfo=timezone.utc)
    assert jambase.quota_period_start(conn, ref).date().isoformat() == "2026-08-14"


def test_quota_anchor_wraps_year(conn):
    db.set_state(conn, "quota_anchor_day", "20")
    ref = datetime(2026, 1, 5, tzinfo=timezone.utc)
    assert jambase.quota_period_start(conn, ref).date().isoformat() == "2025-12-20"


def test_wipe_jambase_data_keeps_plex_artists(conn):
    """Trial terms require deleting JamBase data at day 14. Our own Plex data,
    and the review decisions' Plex side, are not JamBase data — but
    artist_link is keyed on JamBase ids, so it goes."""
    now = db.now()
    conn.execute(
        "INSERT INTO plex_artist (rating_key, name, norm_name, first_seen_at,"
        " last_seen_at) VALUES ('1', 'A', 'a', ?, ?)", (now, now))
    conn.execute(
        "INSERT INTO event (jambase_id, first_seen_at, last_seen_at)"
        " VALUES ('jambase:e1', ?, ?)", (now, now))
    conn.execute(
        "INSERT INTO jb_artist (jambase_id, name, norm_name, last_seen_at)"
        " VALUES ('jambase:a1', 'A', 'a', ?)", (now,))
    conn.execute(
        "INSERT INTO artist_link (plex_rating_key, jambase_id, status, method,"
        " created_at) VALUES ('1', 'jambase:a1', 'confirmed', 'name', ?)", (now,))
    conn.commit()

    counts = db.wipe_jambase_data(conn)
    assert counts["event"] == 1 and counts["artist_link"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM event").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM jb_artist").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM artist_link").fetchone()["n"] == 0
    # Plex data survives — it was never theirs.
    assert conn.execute("SELECT COUNT(*) AS n FROM plex_artist").fetchone()["n"] == 1
