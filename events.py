"""JamBase event ingest.

Full pulls only. The delta/watermark/tombstone machinery the original design
called for existed to avoid a full pull assumed to cost 100-200 calls; it costs
~40, so events absent from a pull simply get `removed_at` set and the whole
tombstone path disappears.

JamBase's JSON-LD is inconsistent — the same logical field has three encodings
depending on endpoint (see `coerce_code`). Parse defensively or Washington
venues silently drop out of the Seattle metro.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import config
import db
import norm

log = logging.getLogger("tour_tracker.events")


def coerce_code(value) -> str | None:
    """Region/country -> ISO code, whatever shape JamBase used this time.

    Seen in the wild:
      "US-OR"                                        (metros, metro level)
      "Oregon"                                       (metros, city level)
      {"identifier": "US-OR", "name": "Oregon", ...} (events, venue address)
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return value.get("identifier") or value.get("name") or None
    return None


def coerce_name(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return value.get("name") or value.get("identifier") or None
    return None


def canon_metro(value) -> str | None:
    """Normalise a metro id to the `jambase:N` form used everywhere else.

    JamBase sends `x-jamBaseMetroId` as a bare integer (4), while
    JAMBASE_METRO_IDS and METRO_EXPECTED_REGION use `jambase:4`. Storing the
    raw value silently broke every comparison against a configured metro —
    including the Portland-preferred rule, which never once fired.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s if s.startswith("jambase:") else f"jambase:{s}"


def blank_to_none(value):
    """JamBase sends "" rather than null for absent values (doorTime,
    previousStartDate). Without this, every "has a door time" check lies."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def geo_of(node) -> tuple[float | None, float | None]:
    """Coordinates, tolerating both "@type" and "type" spellings."""
    geo = (node or {}).get("geo") or {}
    lat, lon = geo.get("latitude"), geo.get("longitude")
    try:
        return (float(lat) if lat is not None else None,
                float(lon) if lon is not None else None)
    except (TypeError, ValueError):
        return None, None


def parse_event(raw: dict) -> dict:
    loc = raw.get("location") or {}
    addr = loc.get("address") or {}
    lat, lon = geo_of(loc)

    ticket_url = ticket_seller = None
    for offer in raw.get("offers") or []:
        # ticketingLinkSecondary entries are RESALE (Gametime, SeatGeek,
        # StubHub). Only the primary is the actual seller.
        if offer.get("category") == "ticketingLinkPrimary" and offer.get("url"):
            ticket_url = offer["url"]
            seller = offer.get("seller")
            if isinstance(seller, dict):
                ticket_seller = seller.get("name")
            elif isinstance(seller, str):
                ticket_seller = seller or None
            break

    return {
        "jambase_id": raw.get("identifier"),
        "name": blank_to_none(raw.get("name")),
        "start_local": blank_to_none(raw.get("startDate")),
        "door_local": blank_to_none(raw.get("doorTime")),
        "tz": blank_to_none(addr.get("x-timezone")),
        "venue": blank_to_none(loc.get("name")),
        "city": blank_to_none(addr.get("addressLocality")),
        "city_lat": lat,
        "city_lon": lon,
        "metro_id": canon_metro(addr.get("x-jamBaseMetroId")),
        "region": coerce_code(addr.get("addressRegion")),
        "status": blank_to_none(raw.get("eventStatus")) or "scheduled",
        "previous_start": blank_to_none(raw.get("previousStartDate")),
        "event_type": blank_to_none(raw.get("@type")) or "Concert",
        "date_published": blank_to_none(raw.get("datePublished")),
        "date_modified": blank_to_none(raw.get("dateModified")),
        "event_url": blank_to_none(raw.get("url")),
        "ticket_url": ticket_url,
        "ticket_seller": ticket_seller,
    }


def parse_performers(raw: dict) -> list[dict]:
    out = []
    for p in raw.get("performer") or []:
        pid = p.get("identifier")
        if not pid:
            continue
        out.append({
            "jambase_id": pid,
            "name": p.get("name") or "",
            "genres": [g for g in (p.get("genre") or []) if g],
            "is_headliner": 1 if p.get("x-isHeadliner") else 0,
            "rank": p.get("x-performanceRank"),
        })
    return out


def upsert_event(conn, raw: dict, seen_at: str) -> None:
    e = parse_event(raw)
    if not e["jambase_id"]:
        return
    existing = conn.execute(
        "SELECT status FROM event WHERE jambase_id = ?", (e["jambase_id"],)
    ).fetchone()

    if existing is None:
        conn.execute(
            "INSERT INTO event (jambase_id, name, start_local, door_local, tz, venue,"
            " city, city_lat, city_lon, metro_id, status, status_changed_at,"
            " previous_start, event_type, date_published, date_modified, event_url,"
            " ticket_url, ticket_seller, first_seen_at, last_seen_at, raw_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e["jambase_id"], e["name"], e["start_local"], e["door_local"], e["tz"],
             e["venue"], e["city"], e["city_lat"], e["city_lon"], e["metro_id"],
             e["status"], seen_at, e["previous_start"], e["event_type"],
             e["date_published"], e["date_modified"], e["event_url"],
             e["ticket_url"], e["ticket_seller"], seen_at, seen_at, json.dumps(raw)),
        )
    else:
        # status_changed_at only moves when the status actually changes — the
        # page's 7-day cancelled grace period keys off it.
        changed = existing["status"] != e["status"]
        conn.execute(
            "UPDATE event SET name=?, start_local=?, door_local=?, tz=?, venue=?,"
            " city=?, city_lat=?, city_lon=?, metro_id=?, status=?,"
            " status_changed_at=CASE WHEN ? THEN ? ELSE status_changed_at END,"
            " previous_start=?, event_type=?, date_published=?, date_modified=?,"
            " event_url=?, ticket_url=?, ticket_seller=?, last_seen_at=?,"
            " removed_at=NULL, raw_json=? WHERE jambase_id=?",
            (e["name"], e["start_local"], e["door_local"], e["tz"], e["venue"],
             e["city"], e["city_lat"], e["city_lon"], e["metro_id"], e["status"],
             1 if changed else 0, seen_at, e["previous_start"], e["event_type"],
             e["date_published"], e["date_modified"], e["event_url"],
             e["ticket_url"], e["ticket_seller"], seen_at, json.dumps(raw),
             e["jambase_id"]),
        )

    conn.execute("DELETE FROM event_performer WHERE event_id = ?", (e["jambase_id"],))
    for p in parse_performers(raw):
        conn.execute(
            "INSERT INTO jb_artist (jambase_id, name, norm_name, genres, last_seen_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(jambase_id) DO UPDATE SET"
            " name=excluded.name, norm_name=excluded.norm_name,"
            " genres=excluded.genres, last_seen_at=excluded.last_seen_at",
            (p["jambase_id"], p["name"], norm.normalise(p["name"]),
             json.dumps(p["genres"]), seen_at),
        )
        conn.execute(
            "INSERT OR REPLACE INTO event_performer (event_id, jambase_artist_id,"
            " is_headliner, rank) VALUES (?,?,?,?)",
            (e["jambase_id"], p["jambase_id"], p["is_headliner"], p["rank"]),
        )


def mark_absent_removed(conn, run_started_at: str, today: str) -> int:
    """Events not returned by this pull are gone from JamBase.

    Only future events count: a past show drops out of an upcoming-events query
    because it happened, not because it was cancelled.
    """
    cur = conn.execute(
        "UPDATE event SET removed_at = ? WHERE removed_at IS NULL"
        " AND last_seen_at < ? AND start_local >= ?",
        (db.now(), run_started_at, today),
    )
    conn.commit()
    return cur.rowcount


def assert_metro_regions(conn, client, metro_ids: list[str]) -> None:
    """Guard against the ID space shifting under us.

    jambase:53 is ALSO named "Portland Area" and is Portland, MAINE. Costs one
    call, cached for a week.
    """
    cached = db.get_state(conn, "metro:regions_checked_at")
    if cached and (db.now()[:10] == cached[:10]):
        return
    data = client.get("geographies/metros", {"metroHasUpcomingEvents": "true"})
    by_id = {m.get("identifier"): m for m in data.get("metros") or []}
    for mid in metro_ids:
        expected = config.METRO_EXPECTED_REGION.get(mid)
        if not expected:
            continue
        metro = by_id.get(mid)
        if metro is None:
            raise RuntimeError(f"metro {mid} not found in geographies response")
        actual = coerce_code((metro.get("address") or {}).get("addressRegion"))
        if actual != expected:
            raise RuntimeError(
                f"metro {mid} is region {actual!r}, expected {expected!r} "
                f"(name={metro.get('name')!r}) — refusing to sync"
            )
        db.set_state(conn, f"metro:{mid}:name", metro.get("name"))
    db.set_state(conn, "metro:regions_checked_at", db.now())


def sync_full(conn, client, metro_ids: str | None = None, today: str | None = None) -> dict:
    """Pull every event in the configured metros (~45 calls), then prune."""
    metro_ids = metro_ids or config.JAMBASE_METRO_IDS
    ids = [m for m in metro_ids.split("|") if m]
    today = today or date.today().isoformat()
    run_started_at = db.now()

    assert_metro_regions(conn, client, ids)

    total = 0
    for page in client.paginate("events", {"geoMetroId": metro_ids}):
        for raw in page.get("events") or []:
            upsert_event(conn, raw, run_started_at)
            total += 1
        conn.commit()

    removed = mark_absent_removed(conn, run_started_at, today)
    db.set_state(conn, "jambase_sync_at", db.now())
    db.set_state(conn, "last_sync_events", total)
    pruned = prune(conn, today)
    return {"events": total, "removed": removed, "pruned": pruned}


def prune(conn, today: str, now: datetime | None = None,
          digest_dir: Path | None = None) -> dict[str, int]:
    """Delete shows that have happened, and anything held past retention.

    Past shows go at every sync — the page only lists upcoming ones. Anything
    not re-seen within JAMBASE_RETENTION_DAYS (removed events, artists who
    stopped touring, their match decisions, old digests) goes too: the
    Developer terms forbid storing API data longer than 30 days. Saved trip
    searches go once their end date passes or they're 30 days stale.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=config.JAMBASE_RETENTION_DAYS)).isoformat(timespec="seconds")
    out = {}
    out["past_events"] = conn.execute(
        "DELETE FROM event WHERE substr(start_local, 1, 10) < ?", (today,)).rowcount
    out["stale_events"] = conn.execute(
        "DELETE FROM event WHERE last_seen_at < ?", (cutoff,)).rowcount
    conn.execute("DELETE FROM event_performer"
                 " WHERE event_id NOT IN (SELECT jambase_id FROM event)")
    out["stale_artists"] = conn.execute(
        "DELETE FROM jb_artist WHERE last_seen_at < ? AND jambase_id NOT IN"
        " (SELECT jambase_artist_id FROM event_performer)", (cutoff,)).rowcount
    out["links"] = conn.execute(
        "DELETE FROM artist_link"
        " WHERE jambase_id NOT IN (SELECT jambase_id FROM jb_artist)").rowcount
    conn.commit()
    import trips  # trips imports events
    out["trip_searches"] = trips.prune(conn, today, now=now)

    digest_dir = Path(digest_dir or config.DIGEST_DIR)
    oldest = (date.fromisoformat(today) - timedelta(days=config.DIGEST_KEEP_DAYS)).isoformat()
    out["digests"] = 0
    for f in digest_dir.glob("*-????-??-??.json") if digest_dir.is_dir() else []:
        if f.stem[-10:] < oldest:
            f.unlink()
            out["digests"] += 1
    log.info("prune: %s", out)
    return out
