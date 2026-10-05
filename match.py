"""Link JamBase performers to Plex artists.

Exact normalised-name matching only — no fuzzy auto-matching. MBID matching is
not possible on the free/trial tier: expandArtistSameAs returns social links
(facebook, instagram, officialSite) and never a musicbrainz identifier. Plex
MBIDs are stored anyway so matching can upgrade later at no cost.

A decision, once made, is keyed on the JamBase artist id and never re-asked, so
a name collision costs exactly one review.
"""
import json
import logging

import db
import norm

log = logging.getLogger("tour_tracker.match")


def _plex_index(conn) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    rows = conn.execute(
        "SELECT rating_key, name, norm_name, genres FROM plex_artist WHERE ignored = 0"
    ).fetchall()
    for r in rows:
        index.setdefault(r["norm_name"], []).append(dict(r))
    return index


def classify(jb_name: str, jb_genres: list, candidates: list[dict]) -> tuple[str, str]:
    """(status, reason) for a name match against one or more Plex artists."""
    norm_name = norm.normalise(jb_name)
    risky, reason = norm.is_risky(norm_name, plex_name_count=len(candidates))
    if risky:
        return "pending", reason

    # NO genre gate. The original design used "no genre overlap" as a risk
    # signal, but the two sides use incompatible taxonomies: Plex carries
    # AllMusic-style genres ("Pop/Rock", "R&B", "Electronic") while JamBase
    # uses its own slugs ("indie", "rhythm-and-blues-soul", "edm"). Measured on
    # the real library, the rule sent 131 of 144 obviously-correct matches to
    # review (Arlo Parks: plex=[Pop/Rock, R&B] vs jb=[indie,
    # rhythm-and-blues-soul]) and caught nothing real. Genres are still shown
    # in the review UI, where a human can use them as evidence.
    return "auto", "exact name match"


def run(conn, reset: bool = False) -> dict:
    """Match every JamBase performer we have seen. Zero API calls.

    `reset` discards undecided (auto/pending) links so changed matching rules
    take effect. Human decisions — confirmed and rejected — always survive.
    """
    if reset:
        conn.execute("DELETE FROM artist_link WHERE status IN ('auto', 'pending')")
        conn.commit()
    index = _plex_index(conn)
    decided = {
        (r["jambase_id"]) for r in conn.execute(
            "SELECT DISTINCT jambase_id FROM artist_link").fetchall()
    }

    rows = conn.execute(
        "SELECT jambase_id, name, norm_name, genres FROM jb_artist"
    ).fetchall()

    now = db.now()
    created = {"auto": 0, "pending": 0}
    for r in rows:
        if r["jambase_id"] in decided:
            continue  # already confirmed, rejected, pending or auto — never re-ask
        candidates = index.get(r["norm_name"] or "")
        if not candidates:
            continue
        try:
            jb_genres = json.loads(r["genres"] or "[]")
        except (TypeError, ValueError):
            jb_genres = []
        status, reason = classify(r["name"], jb_genres, candidates)
        for c in candidates:
            conn.execute(
                "INSERT OR IGNORE INTO artist_link (plex_rating_key, jambase_id,"
                " status, method, decided_at, created_at) VALUES (?,?,?,?,?,?)",
                (c["rating_key"], r["jambase_id"], status, "name",
                 now if status == "auto" else None, now),
            )
        created[status] += 1
        log.debug("%s: %s (%s)", status, r["name"], reason)

    conn.commit()
    db.set_state(conn, "match_at", now)
    return created


def decide(conn, jambase_id: str, status: str, plex_rating_key: str | None = None) -> int:
    """Record a review decision. `status` is confirmed or rejected."""
    if status not in ("confirmed", "rejected"):
        raise ValueError(status)
    params = [status, db.now(), jambase_id]
    sql = "UPDATE artist_link SET status = ?, decided_at = ? WHERE jambase_id = ?"
    if plex_rating_key:
        sql += " AND plex_rating_key = ?"
        params.append(plex_rating_key)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def ignore_artist(conn, plex_rating_key: str) -> None:
    """Never match this Plex artist again (e.g. a bootleg or a common word)."""
    conn.execute("UPDATE plex_artist SET ignored = 1 WHERE rating_key = ?",
                 (plex_rating_key,))
    conn.execute(
        "UPDATE artist_link SET status = 'rejected', decided_at = ?"
        " WHERE plex_rating_key = ? AND status IN ('auto', 'pending')",
        (db.now(), plex_rating_key))
    conn.commit()


def pending(conn, limit: int = 100) -> list[dict]:
    """Pending links with enough context to decide, newest events first."""
    rows = conn.execute(
        """
        SELECT l.plex_rating_key, l.jambase_id, p.name AS plex_name,
               p.genres AS plex_genres, a.name AS jb_name, a.genres AS jb_genres,
               (SELECT COUNT(*) FROM event_performer ep
                  JOIN event e ON e.jambase_id = ep.event_id
                 WHERE ep.jambase_artist_id = l.jambase_id
                   AND e.removed_at IS NULL) AS event_count,
               (SELECT e.name FROM event_performer ep
                  JOIN event e ON e.jambase_id = ep.event_id
                 WHERE ep.jambase_artist_id = l.jambase_id
                   AND e.removed_at IS NULL
                 ORDER BY e.start_local LIMIT 1) AS next_event,
               (SELECT e.event_url FROM event_performer ep
                  JOIN event e ON e.jambase_id = ep.event_id
                 WHERE ep.jambase_artist_id = l.jambase_id
                   AND e.removed_at IS NULL
                 ORDER BY e.start_local LIMIT 1) AS event_url
          FROM artist_link l
          JOIN plex_artist p ON p.rating_key = l.plex_rating_key
          JOIN jb_artist a ON a.jambase_id = l.jambase_id
         WHERE l.status = 'pending'
         ORDER BY event_count DESC
         LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]
