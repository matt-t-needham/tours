import json

import pytest

import db
import match


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init(c)
    return c


def add_plex(conn, rating_key, name, genres=None):
    import norm
    now = db.now()
    conn.execute(
        "INSERT INTO plex_artist (rating_key, name, norm_name, genres,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
        (rating_key, name, norm.normalise(name), json.dumps(genres or []), now, now))
    conn.commit()


def add_jb(conn, jambase_id, name, genres=None):
    import norm
    conn.execute(
        "INSERT INTO jb_artist (jambase_id, name, norm_name, genres, last_seen_at)"
        " VALUES (?,?,?,?,?)",
        (jambase_id, name, norm.normalise(name), json.dumps(genres or []), db.now()))
    conn.commit()


def test_exact_match_is_auto(conn):
    add_plex(conn, "1", "Arlo Parks")
    add_jb(conn, "jambase:1", "Arlo Parks")
    assert match.run(conn) == {"auto": 1, "pending": 0}
    row = conn.execute("SELECT status, method FROM artist_link").fetchone()
    assert (row["status"], row["method"]) == ("auto", "name")


def test_no_match_creates_nothing(conn):
    add_plex(conn, "1", "Arlo Parks")
    add_jb(conn, "jambase:1", "Someone Else")
    assert match.run(conn) == {"auto": 0, "pending": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM artist_link").fetchone()["n"] == 0


def test_short_name_goes_to_review(conn):
    add_plex(conn, "1", "11")
    add_jb(conn, "jambase:1", "11")
    assert match.run(conn) == {"auto": 0, "pending": 1}


def test_common_word_goes_to_review(conn):
    add_plex(conn, "1", "War")
    add_jb(conn, "jambase:1", "War")
    assert match.run(conn)["pending"] == 1


def test_duplicate_plex_names_go_to_review(conn):
    add_plex(conn, "1", "Nirvana")
    add_plex(conn, "2", "Nirvana")
    add_jb(conn, "jambase:1", "Nirvana")
    assert match.run(conn)["pending"] == 1
    # One review decision covers both Plex rows.
    assert conn.execute("SELECT COUNT(*) AS n FROM artist_link").fetchone()["n"] == 2


def test_genre_mismatch_does_not_block_a_name_match(conn):
    """Plex and JamBase use incompatible genre taxonomies, so a mismatch is not
    evidence of anything. Measured on the real library, gating on it sent 131
    of 144 correct matches to review."""
    add_plex(conn, "1", "Arlo Parks", genres=["Pop/Rock", "R&B"])
    add_jb(conn, "jambase:1", "Arlo Parks", genres=["indie", "rhythm-and-blues-soul"])
    assert match.run(conn) == {"auto": 1, "pending": 0}


def test_reset_rebuilds_undecided_links_but_keeps_decisions(conn):
    add_plex(conn, "1", "Arlo Parks")
    add_plex(conn, "2", "War")
    add_jb(conn, "jambase:1", "Arlo Parks")
    add_jb(conn, "jambase:2", "War")
    match.run(conn)
    match.decide(conn, "jambase:2", "confirmed")
    match.run(conn, reset=True)
    statuses = {r["jambase_id"]: r["status"] for r in
                conn.execute("SELECT jambase_id, status FROM artist_link")}
    assert statuses["jambase:2"] == "confirmed"   # human decision survived
    assert statuses["jambase:1"] == "auto"        # rebuilt


def test_rejected_links_never_reappear(conn):
    add_plex(conn, "1", "Arlo Parks")
    add_jb(conn, "jambase:1", "Arlo Parks")
    match.run(conn)
    match.decide(conn, "jambase:1", "rejected")
    match.run(conn)  # a later sync must not resurrect it
    assert conn.execute("SELECT status FROM artist_link").fetchone()["status"] == "rejected"


def test_confirmed_links_are_not_downgraded(conn):
    add_plex(conn, "1", "War")
    add_jb(conn, "jambase:1", "War")
    match.run(conn)
    match.decide(conn, "jambase:1", "confirmed")
    match.run(conn)
    assert conn.execute("SELECT status FROM artist_link").fetchone()["status"] == "confirmed"


def test_ignore_artist_rejects_and_blocks_future_matches(conn):
    add_plex(conn, "1", "War")
    add_jb(conn, "jambase:1", "War")
    match.run(conn)
    match.ignore_artist(conn, "1")
    assert conn.execute("SELECT status FROM artist_link").fetchone()["status"] == "rejected"
    add_jb(conn, "jambase:2", "War")
    assert match.run(conn) == {"auto": 0, "pending": 0}
