"""Event parsing. Fixtures are hand-written to the shapes observed live, so no
JamBase trial data is retained here."""
import json

import pytest

import db
import events


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init(c)
    return c


# Mirrors a real /v3/events row: addressRegion is an OBJECT here, empty strings
# stand in for nulls, and the metro id is on the address.
EVENT = {
    "identifier": "jambase:15864321",
    "name": "Arlo Parks at Wonder Ballroom",
    "startDate": "2026-09-23T19:00:00",
    "doorTime": "",
    "previousStartDate": "",
    "eventStatus": "scheduled",
    "url": "https://www.jambase.com/show/arlo-parks-wonder-ballroom-20260923",
    "location": {
        "name": "Wonder Ballroom",
        "identifier": "jambase:64448",
        "geo": {"@type": "GeoCoordinates", "latitude": 45.5408, "longitude": -122.6635},
        "address": {
            "addressLocality": "Portland",
            "addressRegion": {"@type": "State", "identifier": "US-OR", "name": "Oregon"},
            "addressCountry": {"@type": "Country", "identifier": "US", "name": "United States"},
            "x-timezone": "America/Los_Angeles",
            "x-jamBaseMetroId": "jambase:17",
        },
    },
    "offers": [
        {"category": "ticketingLinkPrimary", "url": "http://www.wonderballroom.com"},
        {"category": "ticketingLinkSecondary", "url": "https://stubhub.example/x"},
    ],
    "performer": [
        {"identifier": "jambase:5643953", "name": "Arlo Parks",
         "genre": ["indie", "rhythm-and-blues-soul"], "x-isHeadliner": True,
         "x-performanceRank": 1},
        {"identifier": "jambase:999", "name": "Support Act",
         "genre": [], "x-isHeadliner": False, "x-performanceRank": 2},
    ],
}


def test_coerce_code_handles_all_three_shapes():
    # /events venue address: object
    assert events.coerce_code({"identifier": "US-OR", "name": "Oregon"}) == "US-OR"
    # /geographies/metros metro level: ISO string
    assert events.coerce_code("US-OR") == "US-OR"
    # /geographies/metros city level: full name, no ISO available
    assert events.coerce_code("Oregon") == "Oregon"
    assert events.coerce_code(None) is None
    assert events.coerce_code("") is None


def test_geo_tolerates_both_type_spellings():
    assert events.geo_of({"geo": {"@type": "GeoCoordinates", "latitude": 1.5,
                                  "longitude": -2.5}}) == (1.5, -2.5)
    # City nodes in /geographies/metros use "type", without the @.
    assert events.geo_of({"geo": {"type": "GeoCoordinates", "latitude": 1.5,
                                  "longitude": -2.5}}) == (1.5, -2.5)
    assert events.geo_of({}) == (None, None)


def test_blank_strings_become_none():
    parsed = events.parse_event(EVENT)
    assert parsed["door_local"] is None
    assert parsed["previous_start"] is None


def test_parse_event_core_fields():
    p = events.parse_event(EVENT)
    assert p["jambase_id"] == "jambase:15864321"
    assert p["start_local"] == "2026-09-23T19:00:00"  # venue-local, no offset
    assert p["tz"] == "America/Los_Angeles"
    assert p["venue"] == "Wonder Ballroom"
    assert p["city"] == "Portland"
    assert p["metro_id"] == "jambase:17"   # free on the address, not derived
    assert p["region"] == "US-OR"
    assert (p["city_lat"], p["city_lon"]) == (45.5408, -122.6635)


def test_ticket_url_prefers_primary_and_ignores_resale():
    assert events.parse_event(EVENT)["ticket_url"] == "http://www.wonderballroom.com"


def test_resale_only_event_has_no_ticket_url():
    raw = json.loads(json.dumps(EVENT))
    raw["offers"] = [{"category": "ticketingLinkSecondary", "url": "https://stubhub.example/x"}]
    assert events.parse_event(raw)["ticket_url"] is None


def test_upsert_stores_event_and_performers(conn):
    events.upsert_event(conn, EVENT, "2026-09-23T00:00:00+00:00")
    conn.commit()
    row = conn.execute("SELECT * FROM event").fetchone()
    assert row["venue"] == "Wonder Ballroom"
    perfs = conn.execute(
        "SELECT * FROM event_performer ORDER BY rank").fetchall()
    assert [p["is_headliner"] for p in perfs] == [1, 0]
    art = conn.execute(
        "SELECT * FROM jb_artist WHERE jambase_id='jambase:5643953'").fetchone()
    assert art["norm_name"] == "arlo parks"
    assert json.loads(art["genres"]) == ["indie", "rhythm-and-blues-soul"]


def test_status_changed_at_only_moves_on_real_change(conn):
    events.upsert_event(conn, EVENT, "2026-09-01T00:00:00+00:00")
    conn.commit()
    first = conn.execute("SELECT status_changed_at FROM event").fetchone()[0]

    events.upsert_event(conn, EVENT, "2026-09-08T00:00:00+00:00")
    conn.commit()
    assert conn.execute("SELECT status_changed_at FROM event").fetchone()[0] == first

    cancelled = json.loads(json.dumps(EVENT))
    cancelled["eventStatus"] = "cancelled"
    events.upsert_event(conn, cancelled, "2026-09-15T00:00:00+00:00")
    conn.commit()
    row = conn.execute("SELECT status, status_changed_at FROM event").fetchone()
    assert row["status"] == "cancelled"
    assert row["status_changed_at"] == "2026-09-15T00:00:00+00:00"


def test_absent_future_event_is_marked_removed(conn):
    events.upsert_event(conn, EVENT, "2026-09-01T00:00:00+00:00")
    conn.commit()
    n = events.mark_absent_removed(conn, "2026-09-08T00:00:00+00:00", "2026-09-01")
    assert n == 1
    assert conn.execute("SELECT removed_at FROM event").fetchone()[0] is not None


def test_past_event_is_not_marked_removed(conn):
    """A show drops out of an upcoming-events query because it happened, not
    because it was cancelled."""
    events.upsert_event(conn, EVENT, "2026-09-01T00:00:00+00:00")
    conn.commit()
    n = events.mark_absent_removed(conn, "2026-09-08T00:00:00+00:00", "2026-10-01")
    assert n == 0
    assert conn.execute("SELECT removed_at FROM event").fetchone()[0] is None


def test_reappearing_event_clears_removed_at(conn):
    events.upsert_event(conn, EVENT, "2026-09-01T00:00:00+00:00")
    conn.commit()
    events.mark_absent_removed(conn, "2026-09-08T00:00:00+00:00", "2026-09-01")
    events.upsert_event(conn, EVENT, "2026-09-15T00:00:00+00:00")
    conn.commit()
    assert conn.execute("SELECT removed_at FROM event").fetchone()[0] is None


class FakeClient:
    """Stands in for jambase.Client without touching the network."""
    def __init__(self, metros, pages):
        self.metros, self.pages, self.calls = metros, pages, []

    def get(self, endpoint, params=None):
        self.calls.append((endpoint, params))
        return self.metros

    def paginate(self, endpoint, params, max_pages=100):
        for p in self.pages:
            self.calls.append((endpoint, params))
            yield p


METROS_OK = {"metros": [
    {"identifier": "jambase:17", "name": "Portland Area",
     "address": {"addressRegion": "US-OR"}},
    {"identifier": "jambase:12", "name": "Seattle - Tacoma Area",
     "address": {"addressRegion": "US-WA"}},
    {"identifier": "jambase:4", "name": "San Francisco Bay Area",
     "address": {"addressRegion": "US-CA"}},
]}


def test_sync_full_ingests_and_records(conn):
    client = FakeClient(METROS_OK, [{"events": [EVENT]}])
    result = events.sync_full(conn, client, "jambase:17", today="2026-09-01")
    assert result["events"] == 1
    assert db.get_state(conn, "jambase_sync_at") is not None


def test_wrong_region_refuses_to_sync(conn):
    """jambase:53 is also called "Portland Area" but is Portland, MAINE. If the
    ID space ever shifts, fail loudly rather than publish Maine concerts."""
    bad = {"metros": [{"identifier": "jambase:17", "name": "Portland Area",
                       "address": {"addressRegion": "US-ME"}}]}
    client = FakeClient(bad, [{"events": [EVENT]}])
    with pytest.raises(RuntimeError, match="US-ME"):
        events.sync_full(conn, client, "jambase:17", today="2026-09-01")
    assert conn.execute("SELECT COUNT(*) AS n FROM event").fetchone()["n"] == 0


def test_canon_metro_normalises_bare_ids():
    """JamBase sends x-jamBaseMetroId as a bare integer, but the config and the
    region assertion use `jambase:N`. Comparing the two forms silently failed."""
    assert events.canon_metro(4) == "jambase:4"
    assert events.canon_metro("17") == "jambase:17"
    assert events.canon_metro("jambase:12") == "jambase:12"
    assert events.canon_metro(None) is None
    assert events.canon_metro("") is None


def test_parse_event_canonicalises_metro_id():
    import json as _json
    raw = _json.loads(_json.dumps(EVENT))
    raw["location"]["address"]["x-jamBaseMetroId"] = 17   # integer, as sent
    assert events.parse_event(raw)["metro_id"] == "jambase:17"


# --- prune ---------------------------------------------------------------------

def _event(conn, eid, start, last_seen, performer="jambase:a1"):
    conn.execute(
        "INSERT INTO event (jambase_id, start_local, first_seen_at, last_seen_at)"
        " VALUES (?, ?, ?, ?)", (eid, start, last_seen, last_seen))
    conn.execute("INSERT INTO event_performer (event_id, jambase_artist_id)"
                 " VALUES (?, ?)", (eid, performer))


def _artist(conn, aid, last_seen):
    conn.execute("INSERT INTO jb_artist (jambase_id, name, norm_name, last_seen_at)"
                 " VALUES (?, ?, ?, ?)", (aid, aid, aid, last_seen))
    conn.execute("INSERT INTO artist_link (plex_rating_key, jambase_id, status, method,"
                 " created_at) VALUES ('1', ?, 'confirmed', 'name', ?)", (aid, last_seen))


def test_prune_drops_shows_that_have_happened(conn, tmp_path):
    from datetime import datetime, timezone
    now = datetime(2026, 10, 9, 13, 20, tzinfo=timezone.utc)
    seen = "2026-10-09T13:20:00+00:00"
    _event(conn, "past", "2026-10-08T20:00:00", seen)
    _event(conn, "tonight", "2026-10-09T20:00:00", seen)  # today stays
    _event(conn, "future", "2026-11-01T20:00:00", seen)
    out = events.prune(conn, "2026-10-09", now=now, digest_dir=tmp_path)
    left = {r[0] for r in conn.execute("SELECT jambase_id FROM event")}
    assert left == {"tonight", "future"}
    assert out["past_events"] == 1
    assert {r[0] for r in conn.execute("SELECT event_id FROM event_performer")} == left


def test_prune_enforces_30_day_retention(conn, tmp_path):
    from datetime import datetime, timezone
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    _event(conn, "removed", "2027-01-01T20:00:00", "2026-10-15T00:00:00+00:00", "jambase:old")
    _event(conn, "live", "2027-01-01T20:00:00", "2026-11-30T00:00:00+00:00", "jambase:new")
    _artist(conn, "jambase:old", "2026-10-15T00:00:00+00:00")
    _artist(conn, "jambase:new", "2026-11-30T00:00:00+00:00")
    for name in ("new-2026-10-01", "portland-2026-11-20"):
        (tmp_path / f"{name}.json").write_text("{}")
    events.prune(conn, "2026-12-01", now=now, digest_dir=tmp_path)
    assert [r[0] for r in conn.execute("SELECT jambase_id FROM event")] == ["live"]
    assert [r[0] for r in conn.execute("SELECT jambase_id FROM jb_artist")] == ["jambase:new"]
    assert [r[0] for r in conn.execute("SELECT jambase_id FROM artist_link")] == ["jambase:new"]
    assert [f.name for f in tmp_path.glob("*.json")] == ["portland-2026-11-20.json"]
