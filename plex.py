"""Plex music-library sync. Costs zero JamBase calls.

Section 3 is "Music". Section 4 ("Audiobooks") is ALSO type="artist" and is
never scanned — config.PLEX_MUSIC_SECTION_ID pins the right one.

MusicBrainz IDs: the artist *list* view returns guid="plex://artist/..." for
almost every artist, but GET /library/metadata/{ratingKey} returns
<Guid id="mbid://..."/>. So MBIDs are free from Plex, one local call per
artist — no JamBase spend. Resolution is resumable: only artists with no mbid
recorded are fetched.
"""
import json
import logging
import xml.etree.ElementTree as ET

import httpx

import config
import db
import norm

log = logging.getLogger("tour_tracker.plex")


def _get(path: str, params: dict | None = None) -> ET.Element:
    url = f"{config.PLEX_URL.rstrip('/')}{path}"
    headers = {"X-Plex-Token": config.PLEX_TOKEN}
    resp = httpx.get(url, params=params, headers=headers, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    return ET.fromstring(resp.text)


def fetch_artists() -> list[dict]:
    """Every artist in the music section, as {rating_key, name, mbid|None}."""
    root = _get(
        f"/library/sections/{config.PLEX_MUSIC_SECTION_ID}/all",
        {"type": 8},  # 8 = artist
    )
    artists = []
    for d in root.findall("Directory"):
        rating_key = d.get("ratingKey")
        name = d.get("title")
        if not rating_key or not name:
            continue
        guid = d.get("guid") or ""
        mbid = guid[len("mbid://"):] if guid.startswith("mbid://") else None
        artists.append({"rating_key": rating_key, "name": name, "mbid": mbid})
    return artists


def fetch_metadata(rating_key: str) -> tuple[str | None, list[str]]:
    """(mbid, genres) from one metadata call — both live on the same response,
    so never make two."""
    root = _get(f"/library/metadata/{rating_key}")
    mbid = None
    for guid in root.iter("Guid"):
        gid = guid.get("id") or ""
        if gid.startswith("mbid://"):
            mbid = gid[len("mbid://"):]
            break
    genres = [g.get("tag") for g in root.iter("Genre") if g.get("tag")]
    return mbid, genres


def sync(conn, artists: list[dict] | None = None) -> dict:
    """Upsert artists into plex_artist. Idempotent."""
    artists = artists if artists is not None else fetch_artists()
    now = db.now()
    added = updated = 0
    for a in artists:
        norm_name = norm.normalise(a["name"])
        existing = conn.execute(
            "SELECT rating_key, name, mbid FROM plex_artist WHERE rating_key = ?",
            (a["rating_key"],),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO plex_artist (rating_key, name, norm_name, mbid,"
                " first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                (a["rating_key"], a["name"], norm_name, a.get("mbid"), now, now),
            )
            added += 1
        else:
            # Never clear an mbid we already resolved just because the list
            # view does not carry one.
            mbid = a.get("mbid") or existing["mbid"]
            conn.execute(
                "UPDATE plex_artist SET name = ?, norm_name = ?, mbid = ?,"
                " last_seen_at = ? WHERE rating_key = ?",
                (a["name"], norm_name, mbid, now, a["rating_key"]),
            )
            if existing["name"] != a["name"]:
                updated += 1
    conn.commit()
    db.set_state(conn, "plex_sync_at", now)
    return {"total": len(artists), "added": added, "renamed": updated}


def resolve_metadata(conn, limit: int | None = None, progress=None) -> dict:
    """Fill in missing MBIDs and genres, one Plex call each. Resumable."""
    rows = conn.execute(
        "SELECT rating_key, name FROM plex_artist"
        " WHERE (mbid IS NULL OR genres IS NULL) AND ignored = 0"
        + (" LIMIT ?" if limit else ""),
        (limit,) if limit else (),
    ).fetchall()
    found = 0
    for i, row in enumerate(rows, 1):
        try:
            mbid, genres = fetch_metadata(row["rating_key"])
        except httpx.HTTPError as exc:
            log.warning("metadata lookup failed for %s: %s", row["name"], exc)
            continue
        conn.execute(
            "UPDATE plex_artist SET mbid = COALESCE(?, mbid), genres = ?"
            " WHERE rating_key = ?",
            (mbid, json.dumps(genres), row["rating_key"]),
        )
        if mbid:
            found += 1
        if i % 50 == 0:
            conn.commit()
            if progress:
                progress(i, len(rows), found)
    conn.commit()
    return {"checked": len(rows), "found": found}
