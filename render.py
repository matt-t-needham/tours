"""Render the page from the database. Zero API calls.

One row per ARTIST: the question is "who that I listen to is touring", so an
artist playing four cities is one row, not four. The row shows their Portland
date where they play more than one region, and the region filter reselects
which date is shown rather than hiding the row.
"""
import json
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

import config
import db
import jambase

# metro id -> the chip it appears under
METRO_LABELS = {
    "jambase:17": "Portland",
    "jambase:12": "Seattle",
    "jambase:4": "SF Bay",
}


def _env():
    return Environment(
        loader=FileSystemLoader(config.BASE_DIR / "templates"),
        autoescape=select_autoescape(["html"]),
    )


def fmt_when(start_local: str | None) -> tuple[str, str]:
    """(date label, time label) from a venue-local timestamp with no offset.

    Never convert: the string is already in the venue's wall clock. Multi-day
    festivals arrive as a bare date with no time.
    """
    if not start_local:
        return "", ""
    try:
        if "T" in start_local:
            dt = datetime.fromisoformat(start_local)
            return dt.strftime("%a %-d %b"), dt.strftime("%-I:%M%p").lower()
        d = date.fromisoformat(start_local[:10])
        return d.strftime("%a %-d %b"), ""
    except ValueError:
        return start_local, ""


def context(conn, today: str | None = None) -> dict:
    rows = artist_rows(conn, today)
    counts = {
        mid: sum(1 for r in rows if any(s["metro_id"] == mid for s in r["shows"]))
        for mid in METRO_ORDER
    }
    pending = conn.execute(
        "SELECT COUNT(DISTINCT jambase_id) AS n FROM artist_link WHERE status = 'pending'"
    ).fetchone()["n"]
    synced = db.get_state(conn, "jambase_sync_at")
    stale = False
    if synced:
        stale = (datetime.now().astimezone() - datetime.fromisoformat(synced)).days > 9
    return {
        "title": config.SITE_TITLE,
        "rows": rows,
        "regions": [{"id": m, "label": METRO_LABEL[m], "count": counts[m]}
                    for m in METRO_ORDER],
        "home_metro": HOME_METRO,
        "pending": pending,
        "synced": synced,
        "stale": stale,
        "quota": jambase.quota(conn),
        "trips": _trips(conn, today),
        "trip_radius": config.TRIP_RADIUS_MI,
        "quota_resets": _resets(jambase.quota(conn)),
        "charging": db.get_state(conn, "charging"),
        "paused_reason": (db.get_state(conn, "paused_reason")
                          if db.get_state(conn, "paused_at") else None),
        "today": today or date.today().isoformat(),
    }


def _resets(q: dict) -> str | None:
    end = q.get("period_end")
    if not end:
        return None
    try:
        return datetime.fromisoformat(end.replace("Z", "+00:00")).strftime("%-d %b")
    except ValueError:
        return None


def _trips(conn, today: str | None) -> list[dict]:
    import trips  # trips imports render
    out = []
    for t in trips.all_searches(conn, today):
        t = {k: t[k] for k in ("id", "region", "query", "place", "radius_mi", "date_from",
                               "date_to", "fetched_at", "events_seen", "calls", "results")}
        out.append(t)
    return out


def render(conn, today: str | None = None) -> str:
    ctx = context(conn, today)
    ctx["region_pairs"] = [[r["label"], r["id"]] for r in ctx["regions"]]
    return _env().get_template("index.html").render(**ctx)


# --- artist-centric view -----------------------------------------------------
# One row per ARTIST, not per event: the question is "who that I listen to is
# touring", so an artist playing four cities is one row, not four. Portland is
# home, so a Portland date wins over an earlier Seattle or Bay Area one.

HOME_METRO = "jambase:17"


METRO_LABEL = {
    "jambase:17": "Portland",
    "jambase:12": "Seattle",
    "jambase:4": "San Francisco",
}
METRO_ORDER = ["jambase:17", "jambase:12", "jambase:4"]


# JamBase's primary offer is usually a real ticket page, but for 22 of 334
# matched shows the seller is "Venue Website" and the URL is a bare domain —
# the venue's homepage, not the event. Labelling those "Tickets" would be a
# lie, so the link says where it actually goes.
def ticket_label(url: str | None, seller: str | None) -> str:
    if not url:
        return "JamBase"
    path = urlparse(url).path.strip("/")
    if not path:
        return "Venue home"          # bare domain: you land on the homepage
    if seller == "Venue Website":
        return "Venue"               # the venue's own site, but a real event page
    if seller == "Ticketing Site":
        return "Ticketing site"
    return seller or "Tickets"


def _show(r) -> dict:
    day, time = fmt_when(r["start_local"])
    if r["event_type"] == "Festival":
        role, detail = "festival", r["event_name"]
    elif r["is_headliner"]:
        role, detail = "headline", None
    else:
        role, detail = "support", r["headliner"]
    return {
        "event_id": r["event_id"],
        "day": day, "time": time, "start_local": r["start_local"],
        "month": (r["start_local"] or "")[:7],
        "venue": r["venue"], "city": r["city"], "metro_id": r["metro_id"],
        "role": role, "detail": detail, "status": r["status"],
        "ticket_url": r["ticket_url"], "event_url": r["event_url"],
        "ticket_seller": r["ticket_seller"],
        "ticket_label": ticket_label(r["ticket_url"], r["ticket_seller"]),
        "found_at": r["first_seen_at"], "announced_at": r["date_published"],
    }


def artist_rows(conn, today: str | None = None) -> list[dict]:
    """One entry per matched artist, carrying every upcoming show they play.

    The region filter reselects which show is displayed rather than hiding
    rows: with Portland preferred by default, an artist playing both Portland
    and Seattle would otherwise disappear from the Seattle filter — precisely
    when you want to see them. The full `shows` list also backs the "+N" hover.
    """
    from datetime import date as _date

    today = today or _date.today().isoformat()
    cutoff = (_date.fromisoformat(today) - timedelta(days=7)).isoformat()

    rows = conn.execute(
        """
        SELECT p.rating_key, p.name AS artist,
               e.jambase_id AS event_id, e.name AS event_name, e.start_local,
               e.venue, e.city, e.metro_id, e.status, e.event_type,
               e.event_url, e.ticket_url, e.ticket_seller, e.first_seen_at,
               e.date_published,
               ep.is_headliner,
               (SELECT a2.name FROM event_performer ep2
                  JOIN jb_artist a2 ON a2.jambase_id = ep2.jambase_artist_id
                 WHERE ep2.event_id = e.jambase_id AND ep2.is_headliner = 1
                 ORDER BY ep2.rank LIMIT 1) AS headliner
          FROM artist_link l
          JOIN plex_artist p ON p.rating_key = l.plex_rating_key
          JOIN event_performer ep ON ep.jambase_artist_id = l.jambase_id
          JOIN event e ON e.jambase_id = ep.event_id
         WHERE l.status IN ('auto', 'confirmed')
           AND e.removed_at IS NULL
           AND e.start_local >= ?
         ORDER BY e.start_local
        """,
        (cutoff,),
    ).fetchall()

    artists: dict[str, list] = {}
    for r in rows:
        # An artist can be linked from more than one Plex row; the same event
        # must not appear twice in their show list.
        shows = artists.setdefault(r["artist"], [])
        if not any(s["event_id"] == r["event_id"] for s in shows):
            shows.append(_show(r))

    out = []
    for artist, shows in artists.items():
        primary = next((s for s in shows if s["metro_id"] == HOME_METRO), shows[0])
        entry = dict(primary)
        entry["artist"] = artist
        entry["shows"] = shows
        entry["other_shows"] = len(shows) - 1
        out.append(entry)

    out.sort(key=lambda x: x["start_local"] or "")
    return out
