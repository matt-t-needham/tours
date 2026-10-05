"""Saved trip searches: shows by library artists in a place and date range.

A search is a place + date range, run at will (CLI or the page's trip form)
and re-run on demand with "update", which replaces its results. Searches are
grouped into tabs by region (the JamBase metro of the resolved city) and never
reach the main west-coast list.

Deliberately kept out of `event`/`jb_artist`/`artist_link`: a trip's shows
would otherwise land on the main page and in the digests, and the next
west-coast sync would mark them all removed. Matching reads the existing link
decisions (so confirmed/rejected matches hold) but never writes new ones;
uncertain name matches are shown as "possible" instead of queued for review.

Only matched shows are stored, and a search is deleted once its end date has
passed or its results are older than JAMBASE_RETENTION_DAYS (Developer terms).

Cost: one call to resolve the city, plus one per 100 events in range.
"""
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

import config
import db
import events
import match as matcher
import norm
import render

log = logging.getLogger("tour_tracker.trips")

_STATE_SUFFIX = re.compile(r"^(.*?),\s*([A-Za-z]{2})$")


class TripError(ValueError):
    pass


def parse_place(text: str) -> tuple[str, str | None]:
    """'Boston, MA' -> ('Boston', 'US-MA'); 'Boston' -> ('Boston', None).

    A state matters: there are two Portlands (and jambase:53 "Portland Area"
    is Maine), so without one the busiest match wins.
    """
    text = " ".join((text or "").split())
    m = _STATE_SUFFIX.match(text)
    if m:
        return m.group(1).strip(), f"US-{m.group(2).upper()}"
    return text, None


def region_label(metro_name: str | None, city: str) -> str:
    """Tab label: the metro without JamBase's ' Area' suffix, else the city."""
    if metro_name:
        return re.sub(r"\s+Area$", "", metro_name.strip()) or city
    return city


def resolve_city(client, query: str) -> dict:
    name, state = parse_place(query)
    if not name:
        raise TripError("enter a city, e.g. 'Boston, MA'")
    params = {"geoCityName": name, "perPage": 20}
    if state:
        params["geoStateIso"] = state
    cities = client.get("geographies/cities", params).get("cities") or []
    exact = [c for c in cities if (c.get("name") or "").lower() == name.lower()]
    pool = exact or cities
    if not pool:
        raise TripError(f"JamBase has no city called {query!r}")
    best = max(pool, key=lambda c: c.get("x-numUpcomingEvents") or 0)
    lat, lon = events.geo_of(best)
    if lat is None or lon is None:
        raise TripError(f"JamBase gave no coordinates for {best.get('name')!r}")
    metro = best.get("containedInPlace") or {}
    region = events.coerce_code((best.get("address") or {}).get("addressRegion"))
    city = best.get("name") or name
    return {
        "city": city,
        "place": f"{city}, {region}" if region else city,
        "region": region_label(metro.get("name"), city),
        "lat": lat,
        "lon": lon,
    }


def _clean_dates(date_from: str, date_to: str, today: str) -> tuple[str, str]:
    try:
        d_from, d_to = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except (TypeError, ValueError):
        raise TripError("dates must be YYYY-MM-DD")
    if d_to < d_from:
        raise TripError("the end date is before the start date")
    if d_to.isoformat() < today:
        raise TripError("that date range has already passed")
    return max(d_from.isoformat(), today), d_to.isoformat()


def match_events(conn, raws: list[dict]) -> list[dict]:
    """Artist rows (render.artist_rows' shape) for every library artist found.

    Existing decisions win: a confirmed/auto link matches, a rejected one never
    does. Unknown performers are name-matched like `match.run` would, but
    nothing is written — uncertain ones come back with `possible: True`.
    """
    links: dict[str, list[tuple[str, str]]] = {}
    for r in conn.execute("SELECT jambase_id, plex_rating_key, status FROM artist_link"):
        links.setdefault(r["jambase_id"], []).append((r["plex_rating_key"], r["status"]))
    names = {r["rating_key"]: r["name"] for r in conn.execute(
        "SELECT rating_key, name FROM plex_artist WHERE ignored = 0")}
    index = matcher._plex_index(conn)

    artists: dict[str, dict] = {}
    for raw in raws:
        e = events.parse_event(raw)
        if not e["jambase_id"]:
            continue
        performers = events.parse_performers(raw)
        heads = sorted((p for p in performers if p["is_headliner"]),
                       key=lambda p: p["rank"] if p["rank"] is not None else 999)
        headliner = heads[0]["name"] if heads else None
        for p in performers:
            known = links.get(p["jambase_id"])
            if known:
                keys = [(k, False) for k, s in known if s in ("auto", "confirmed")]
                keys += [(k, True) for k, s in known if s == "pending"]
            else:
                cands = index.get(norm.normalise(p["name"])) or []
                if not cands:
                    continue
                status, _ = matcher.classify(p["name"], p["genres"], cands)
                keys = [(c["rating_key"], status != "auto") for c in cands]
            for key, possible in keys:
                artist = names.get(key)
                if not artist:
                    continue
                show = render._show({
                    "event_id": e["jambase_id"], "event_name": e["name"],
                    "start_local": e["start_local"], "venue": e["venue"],
                    "city": e["city"], "metro_id": e["metro_id"],
                    "status": e["status"], "event_type": e["event_type"],
                    "event_url": e["event_url"], "ticket_url": e["ticket_url"],
                    "ticket_seller": e["ticket_seller"], "first_seen_at": None,
                    "date_published": e["date_published"],
                    "is_headliner": p["is_headliner"], "headliner": headliner,
                })
                show["possible"] = possible
                entry = artists.setdefault(artist, {"artist": artist, "shows": []})
                if not any(s["event_id"] == show["event_id"] for s in entry["shows"]):
                    entry["shows"].append(show)

    out = []
    for entry in artists.values():
        shows = sorted(entry["shows"], key=lambda s: s["start_local"] or "")
        row = dict(shows[0])
        row.update(artist=entry["artist"], shows=shows, other_shows=len(shows) - 1,
                   possible=all(s["possible"] for s in shows))
        out.append(row)
    out.sort(key=lambda r: r["start_local"] or "")
    return out


def _fetch(conn, client, lat, lon, radius_mi, date_from, date_to) -> tuple[list[dict], int]:
    raws = []
    params = {"geoLatitude": lat, "geoLongitude": lon, "geoRadiusAmount": radius_mi,
              "geoRadiusUnits": "mi", "eventDateFrom": date_from, "eventDateTo": date_to}
    for page in client.paginate("events", params):
        raws.extend(page.get("events") or [])
    return match_events(conn, raws), len(raws)


def create(conn, client, query: str, date_from: str, date_to: str,
           radius_mi: int | None = None, today: str | None = None) -> int:
    today = today or date.today().isoformat()
    date_from, date_to = _clean_dates(date_from, date_to, today)
    radius_mi = int(radius_mi or config.TRIP_RADIUS_MI)
    if not 1 <= radius_mi <= 150:
        raise TripError("radius must be 1–150 miles")
    before = db_calls(conn)
    where = resolve_city(client, query)
    results, seen = _fetch(conn, client, where["lat"], where["lon"], radius_mi,
                           date_from, date_to)
    now = db.now()
    cur = conn.execute(
        "INSERT INTO trip_search (region, query, place, lat, lon, radius_mi, date_from,"
        " date_to, created_at, fetched_at, events_seen, calls, results)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (where["region"], query.strip(), where["place"], where["lat"], where["lon"],
         radius_mi, date_from, date_to, now, now, seen, db_calls(conn) - before,
         json.dumps(results)))
    conn.commit()
    log.info("trip %s: %s %s..%s, %d events, %d artists", cur.lastrowid,
             where["place"], date_from, date_to, seen, len(results))
    return cur.lastrowid


def update(conn, client, search_id: int, today: str | None = None) -> dict:
    """Re-run a saved search with its stored coordinates; replace its results.

    The city isn't re-resolved, so an update costs only the event pages. A
    failure part-way leaves the old results in place.
    """
    s = get(conn, search_id)
    if s is None:
        raise TripError(f"no saved search {search_id}")
    today = today or date.today().isoformat()
    date_from = max(s["date_from"], today)
    before = db_calls(conn)
    results, seen = _fetch(conn, client, s["lat"], s["lon"], s["radius_mi"],
                           date_from, s["date_to"])
    conn.execute(
        "UPDATE trip_search SET fetched_at = ?, events_seen = ?, calls = ?, results = ?"
        " WHERE id = ?",
        (db.now(), seen, db_calls(conn) - before, json.dumps(results), search_id))
    conn.commit()
    return get(conn, search_id)


def delete(conn, search_id: int) -> bool:
    cur = conn.execute("DELETE FROM trip_search WHERE id = ?", (search_id,))
    conn.commit()
    return cur.rowcount > 0


def prune(conn, today: str, now: datetime | None = None) -> int:
    """Drop searches whose end date has passed or whose results are too old."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=config.JAMBASE_RETENTION_DAYS)).isoformat(timespec="seconds")
    cur = conn.execute("DELETE FROM trip_search WHERE date_to < ? OR fetched_at < ?",
                       (today, cutoff))
    conn.commit()
    return cur.rowcount


def get(conn, search_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM trip_search WHERE id = ?", (search_id,)).fetchone()
    return _row(r) if r else None


def all_searches(conn, today: str | None = None) -> list[dict]:
    """Every live search, by region then date. Expired ones are pruned first."""
    prune(conn, today or date.today().isoformat())
    rows = conn.execute(
        "SELECT * FROM trip_search ORDER BY region, date_from, id").fetchall()
    return [_row(r) for r in rows]


def _row(r) -> dict:
    d = dict(r)
    d["results"] = json.loads(d["results"] or "[]")
    return d


def db_calls(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM api_call").fetchone()["n"]
