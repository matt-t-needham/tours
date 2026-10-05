import json

import pytest

import db
import events
import match
import render
from tests.test_events import EVENT


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init(c)
    return c


def add_plex(conn, rating_key, name):
    import norm
    now = db.now()
    conn.execute(
        "INSERT INTO plex_artist (rating_key, name, norm_name, first_seen_at,"
        " last_seen_at) VALUES (?,?,?,?,?)",
        (rating_key, name, norm.normalise(name), now, now))
    conn.commit()


def seed(conn, event=EVENT, plex_name="Arlo Parks"):
    add_plex(conn, "1", plex_name)
    events.upsert_event(conn, event, db.now())
    conn.commit()
    match.run(conn)


def seattle_copy(start="2026-09-30T20:00:00"):
    raw = json.loads(json.dumps(EVENT))
    raw["identifier"] = "jambase:seattle1"
    raw["startDate"] = start
    raw["location"]["name"] = "Neptune Theatre"
    raw["location"]["address"]["addressLocality"] = "Seattle"
    raw["location"]["address"]["x-jamBaseMetroId"] = 12
    return raw


def test_fmt_when_never_converts_timezone():
    assert render.fmt_when("2026-09-23T19:00:00") == ("Wed 23 Sep", "7:00pm")


def test_fmt_when_handles_dateless_festival_rows():
    assert render.fmt_when("2026-09-26") == ("Sat 26 Sep", "")
    assert render.fmt_when(None) == ("", "")


def test_one_row_per_artist_not_per_show(conn):
    seed(conn)
    events.upsert_event(conn, seattle_copy(), db.now())
    conn.commit()
    rows = render.artist_rows(conn, today="2026-09-01")
    assert len(rows) == 1
    assert rows[0]["other_shows"] == 1
    assert len(rows[0]["shows"]) == 2


def test_portland_show_is_preferred_over_an_earlier_one_elsewhere(conn):
    """The Portland-preferred rule silently never fired while metro ids were
    stored bare (`17`) and compared against `jambase:17`."""
    seed(conn, event=seattle_copy(start="2026-09-20T20:00:00"))  # earlier, Seattle
    events.upsert_event(conn, EVENT, db.now())                    # later, Portland
    conn.commit()
    match.run(conn)
    row = render.artist_rows(conn, today="2026-09-01")[0]
    assert row["city"] == "Portland"
    assert row["metro_id"] == render.HOME_METRO


def test_ticket_label_names_the_actual_seller():
    assert render.ticket_label("https://ticketmaster.example/event/123", "Ticketmaster") == "Ticketmaster"
    assert render.ticket_label("https://etix.example/e/99", "eTix") == "eTix"


def test_bare_venue_domain_is_not_labelled_tickets():
    """22 of 334 primary offers are the venue's homepage, not the event page.
    Calling that "Tickets" would be a lie."""
    assert render.ticket_label("https://www.crystalballroompdx.com/?utm_source=jambase",
                               "Venue Website") == "Venue home"
    assert render.ticket_label("http://www.wonderballroom.com", "Venue Website") == "Venue home"
    # The same seller WITH a real event path is a genuine ticket page.
    assert render.ticket_label("https://www.wonderballroom.com/e/arlo", "Venue Website") == "Venue"


def test_missing_ticket_url_falls_back_to_jambase():
    assert render.ticket_label(None, None) == "JamBase"


def test_past_events_drop_after_seven_days(conn):
    seed(conn)
    assert len(render.artist_rows(conn, today="2026-09-26")) == 1   # 3 days after
    assert render.artist_rows(conn, today="2026-10-05") == []       # 12 days after


def test_unmatched_and_pending_artists_do_not_appear(conn):
    events.upsert_event(conn, EVENT, db.now())
    conn.commit()
    assert render.artist_rows(conn, today="2026-09-01") == []
    seed(conn)
    conn.execute("UPDATE artist_link SET status = 'pending'")
    conn.commit()
    assert render.artist_rows(conn, today="2026-09-01") == []
    assert render.context(conn, today="2026-09-01")["pending"] == 1


def test_region_counts_cover_every_region_an_artist_plays(conn):
    seed(conn)
    events.upsert_event(conn, seattle_copy(), db.now())
    conn.commit()
    counts = {r["id"]: r["count"] for r in render.context(conn, today="2026-09-01")["regions"]}
    assert counts["jambase:17"] == 1
    assert counts["jambase:12"] == 1


def test_render_produces_html_with_attribution(conn):
    seed(conn)
    html = render.render(conn, today="2026-09-01")
    assert "Arlo Parks" in html
    assert "Wonder Ballroom" in html
    assert "jambase.com" in html   # attribution is required on the free tier


def test_embedded_json_is_not_html_escaped(conn):
    """Jinja autoescape turns quotes in a pre-serialised JSON string into
    &quot;, which kills the script before a single row renders."""
    seed(conn)
    html = render.render(conn, today="2026-09-01")
    # Scope to the DATA literal itself — esc()'s own escape map further down
    # legitimately contains &quot;.
    literal = html.split("const DATA = ")[1].split(";\n", 1)[0]
    assert "&quot;" not in literal
    assert "&#34;" not in literal
    assert '"artist":' in literal


def test_quota_links_to_the_usage_dashboard(conn):
    seed(conn)
    html = render.render(conn, today="2026-09-01")
    assert "data.jambase.com/account/usage" in html


def test_jambase_attribution_survives(conn):
    """Attribution is a condition of the free Developer tier — the usage link
    is additional to it, not a replacement."""
    seed(conn)
    html = render.render(conn, today="2026-09-01")
    assert "Concert data from" in html
    assert "https://www.jambase.com" in html


def test_tickets_column_has_a_header(conn):
    seed(conn)
    assert '<th class="c-act">Tickets</th>' in render.render(conn, today="2026-09-01")
