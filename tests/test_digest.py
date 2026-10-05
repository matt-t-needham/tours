import json

import pytest

import db
import digest
import events
import match
from tests.test_events import EVENT
from tests.test_render import add_plex, seattle_copy


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init(c)
    return c


def seed(conn, event=EVENT, name="Arlo Parks", seen_at=None):
    add_plex(conn, "1", name)
    events.upsert_event(conn, event, seen_at or db.now())
    conn.commit()
    match.run(conn)


def test_unknown_kind_rejected(conn):
    with pytest.raises(ValueError):
        digest.collect(conn, "nonsense")


def test_new_includes_recently_seen_shows(conn):
    seed(conn, seen_at="2026-09-20T00:00:00+00:00")
    out = digest.collect(conn, "new", today="2026-09-23")
    assert [s["artist"] for s in out] == ["Arlo Parks"]


def test_new_excludes_shows_seen_before_the_window(conn):
    seed(conn, seen_at="2026-08-01T00:00:00+00:00")
    assert digest.collect(conn, "new", today="2026-09-23") == []


def test_portland_only_includes_home_metro(conn):
    """The Seattle copy must not appear in a Portland digest."""
    add_plex(conn, "1", "Arlo Parks")
    events.upsert_event(conn, seattle_copy(start="2026-09-25T20:00:00"), db.now())
    conn.commit()
    match.run(conn)
    assert digest.collect(conn, "portland", today="2026-09-23") == []


def test_portland_window_is_two_weeks(conn):
    seed(conn)   # EVENT is 2026-09-23, Portland
    assert len(digest.collect(conn, "portland", today="2026-09-23")) == 1
    # Same show, asked about a fortnight too early — outside the window.
    assert digest.collect(conn, "portland", today="2026-09-01") == []


def test_build_caps_the_listing_but_reports_the_true_count(conn, monkeypatch):
    monkeypatch.setattr(digest, "MAX_LISTED", 1)
    for i, name in enumerate(["Arlo Parks", "Second Act"], start=1):
        add_plex(conn, str(i), name)
    raw = json.loads(json.dumps(EVENT))
    raw["performer"].append({"identifier": "jambase:777", "name": "Second Act",
                             "genre": [], "x-isHeadliner": False,
                             "x-performanceRank": 2})
    events.upsert_event(conn, raw, db.now())
    conn.commit()
    match.run(conn)

    payload = digest.build(conn, "portland", today="2026-09-23")
    assert payload["count"] == 2
    assert len(payload["shows"]) == 1
    assert "and 1 more" in payload["summary_html"]


def test_summary_escapes_artist_names(conn):
    """The digest body is HTML embedded in an RSS description — a hostile
    artist or venue name must not be able to inject markup into a reader."""
    nasty = "Sunn O))) <script>alert(1)</script>"
    raw = json.loads(json.dumps(EVENT))
    raw["performer"][0]["name"] = nasty
    raw["location"]["name"] = 'The <b>Venue</b>'
    add_plex(conn, "1", nasty)
    events.upsert_event(conn, raw, db.now())
    conn.commit()
    match.run(conn)

    payload = digest.build(conn, "portland", today="2026-09-23")
    assert payload["count"] == 1
    body = payload["summary_html"]
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "<b>Venue</b>" not in body


def test_write_is_atomic_and_named_by_kind_and_date(conn, tmp_path):
    seed(conn)
    path = digest.write(conn, "portland", out_dir=tmp_path, today="2026-09-23")
    assert path.name == "portland-2026-09-23.json"
    assert not list(tmp_path.glob("*.tmp"))
    payload = json.loads(path.read_text())
    assert payload["title"] == "Playing in Portland in the next two weeks"
    assert payload["count"] == 1
