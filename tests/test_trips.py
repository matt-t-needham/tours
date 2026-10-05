"""Saved trip searches, entirely offline. Fixtures are hand-written."""
import json
from datetime import datetime, timezone

import pytest

import db
import trips
from tests.test_events import EVENT
from tests.test_render import add_plex


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init(c)
    return c


def boston_event(eid="jambase:b1", performer=("jambase:5643953", "Arlo Parks"),
                 start="2026-11-12T20:00:00"):
    raw = json.loads(json.dumps(EVENT))
    raw["identifier"] = eid
    raw["startDate"] = start
    raw["location"]["name"] = "Paradise Rock Club"
    raw["location"]["address"].update(addressLocality="Boston", **{"x-jamBaseMetroId": 7})
    raw["performer"] = [{"identifier": performer[0], "name": performer[1], "genre": [],
                         "x-isHeadliner": True, "x-performanceRank": 1}]
    return raw


CITY = {"name": "Cambridge", "identifier": "jambase:c1",
        "geo": {"latitude": 42.37, "longitude": -71.11},
        "address": {"addressRegion": "US-MA", "addressCountry": "US"},
        "x-numUpcomingEvents": 40,
        "containedInPlace": {"identifier": "jambase:7", "name": "Boston Area"}}


class FakeClient:
    """Stands in for jambase.Client; logs calls the way the real one does."""

    def __init__(self, conn, cities=None, pages=None):
        self.conn, self.cities, self.pages, self.requests = conn, cities or [CITY], pages, []

    def _log(self, endpoint, params):
        self.requests.append((endpoint, dict(params)))
        self.conn.execute("INSERT INTO api_call (ts, endpoint, http_status) VALUES (?, ?, 200)",
                          (db.now(), endpoint))

    def get(self, endpoint, params=None):
        self._log(endpoint, params or {})
        return {"cities": self.cities}

    def paginate(self, endpoint, params):
        for page in self.pages:
            self._log(endpoint, params)
            yield {"events": page}


def test_parse_place_reads_a_state_suffix():
    assert trips.parse_place("Portland, me") == ("Portland", "US-ME")
    assert trips.parse_place("  Boston  ") == ("Boston", None)
    assert trips.parse_place("Washington, DC") == ("Washington", "US-DC")


def test_region_is_the_metro_without_area():
    assert trips.region_label("Boston Area", "Cambridge") == "Boston"
    assert trips.region_label(None, "Marfa") == "Marfa"


def test_create_stores_only_matched_shows(conn):
    add_plex(conn, "1", "Arlo Parks")
    client = FakeClient(conn, pages=[[boston_event(),
                                      boston_event("jambase:b2", ("jambase:x", "Nobody"))]])
    tid = trips.create(conn, client, "Cambridge, MA", "2026-11-10", "2026-11-15",
                       today="2026-10-05")
    t = trips.get(conn, tid)
    assert (t["region"], t["place"], t["events_seen"], t["calls"]) == ("Boston", "Cambridge, US-MA", 2, 2)
    assert [r["artist"] for r in t["results"]] == ["Arlo Parks"]
    assert t["results"][0]["possible"] is False
    # The state went to JamBase, and the event search used the resolved point.
    assert client.requests[0][1]["geoStateIso"] == "US-MA"
    ev = client.requests[1][1]
    assert (ev["geoLatitude"], ev["eventDateFrom"], ev["eventDateTo"]) == (42.37, "2026-11-10", "2026-11-15")
    # Kept out of the west-coast tables.
    assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM artist_link").fetchone()[0] == 0


def test_busiest_city_wins_without_a_state(conn):
    maine = dict(CITY, name="Portland", **{"x-numUpcomingEvents": 5},
                 address={"addressRegion": "US-ME"})
    oregon = dict(CITY, name="Portland", **{"x-numUpcomingEvents": 900},
                  address={"addressRegion": "US-OR"})
    got = trips.resolve_city(FakeClient(conn, cities=[maine, oregon]), "Portland")
    assert got["place"] == "Portland, US-OR"


def test_rejected_link_never_matches_and_pending_is_possible(conn):
    add_plex(conn, "1", "Arlo Parks")
    add_plex(conn, "2", "Beach House")
    conn.executescript("""
      INSERT INTO artist_link VALUES ('1', 'jambase:5643953', 'rejected', 'name', NULL, 'x');
      INSERT INTO artist_link VALUES ('2', 'jambase:bh', 'pending', 'name', NULL, 'x');
    """)
    rows = trips.match_events(conn, [boston_event(),
                                     boston_event("jambase:b2", ("jambase:bh", "Beach House"))])
    assert [(r["artist"], r["possible"]) for r in rows] == [("Beach House", True)]


def test_date_validation(conn):
    with pytest.raises(trips.TripError, match="before"):
        trips._clean_dates("2026-11-15", "2026-11-10", "2026-10-05")
    with pytest.raises(trips.TripError, match="passed"):
        trips._clean_dates("2026-09-01", "2026-09-05", "2026-10-05")
    assert trips._clean_dates("2026-10-01", "2026-10-09", "2026-10-05") == ("2026-10-05", "2026-10-09")


def test_update_replaces_results_and_skips_city_lookup(conn):
    add_plex(conn, "1", "Arlo Parks")
    add_plex(conn, "2", "Beach House")
    tid = trips.create(conn, FakeClient(conn, pages=[[boston_event()]]),
                       "Boston, MA", "2026-11-10", "2026-11-15", today="2026-10-05")
    client = FakeClient(conn, pages=[[boston_event("jambase:b2", ("jambase:bh", "Beach House"))]])
    t = trips.update(conn, client, tid, today="2026-10-06")
    assert [r["artist"] for r in t["results"]] == ["Beach House"]
    assert [e for e, _ in client.requests] == ["events"]
    assert t["calls"] == 1


def test_prune_drops_ended_and_stale_searches(conn):
    add_plex(conn, "1", "Arlo Parks")
    for frm, to in (("2026-10-01", "2026-10-04"), ("2026-11-10", "2026-11-15")):
        trips.create(conn, FakeClient(conn, pages=[[]]), "Boston, MA", frm, to, today="2026-10-01")
    assert trips.prune(conn, "2026-10-05") == 1
    later = datetime(2026, 11, 20, tzinfo=timezone.utc)
    conn.execute("UPDATE trip_search SET fetched_at = '2026-10-05T00:00:00+00:00'")
    assert trips.prune(conn, "2026-11-01", now=later) == 1
    assert trips.all_searches(conn, "2026-11-01") == []


def test_wipe_includes_trip_searches(conn):
    trips.create(conn, FakeClient(conn, pages=[[]]), "Boston, MA", "2026-11-10", "2026-11-15",
                 today="2026-10-05")
    assert db.wipe_jambase_data(conn)["trip_search"] == 1


def test_trip_routes_refuse_cross_site_requests(tmp_path, monkeypatch):
    import config
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(main, "_conn", lambda: (lambda c: (db.init(c), c)[1])(db.connect(tmp_path / "t.db")))
    client = TestClient(main.app)
    r = client.post("/api/trips/1/delete", headers={"Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403
    r = client.post("/api/trips/1/delete", headers={"Origin": "https://evil.bix.computer"})
    assert r.status_code == 403
    r = client.post("/api/trips/1/delete", headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 404  # allowed through; there's just no such search
