"""Periodic digests, written as immutable files for the RSS feed to pick up.

Why files rather than letting rss_feed query the database live: a feed item's
guid is what readers dedupe on, and rss_feed's own notes warn that unstable
guids make readers re-show old items as new. "New in the last two weeks" is a
moving window — computed live it would change content under a fixed guid on
every poll. Generating it once on the day freezes both.

Two kinds:
  new       — shows first seen in the last 14 days   (cron: Mondays)
  portland  — Portland shows in the next 14 days     (cron: Thursdays)
"""
import html
import json
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

import config
import render

WINDOW_DAYS = 14

KINDS = {
    "new": "New shows added in the last two weeks",
    "portland": "Playing in Portland in the next two weeks",
}

# A first run reports the whole bootstrap as "new"; cap the body so the feed
# item stays readable and say how many were left out.
MAX_LISTED = 40


def _describe(show) -> str:
    if show["role"] == "festival":
        return f"festival — {show['detail']}" if show["detail"] else "festival"
    if show["role"] == "support":
        return f"support for {show['detail']}" if show["detail"] else "support"
    return "headline"


def collect(conn, kind: str, today: str | None = None) -> list[dict]:
    if kind not in KINDS:
        raise ValueError(f"unknown digest kind {kind!r}")
    today = today or _date.today().isoformat()
    rows = render.artist_rows(conn, today)
    out = []

    if kind == "new":
        cutoff = (_date.fromisoformat(today) - timedelta(days=WINDOW_DAYS)).isoformat()
        for r in rows:
            fresh = [s for s in r["shows"] if (s["found_at"] or "")[:10] >= cutoff]
            if fresh:
                out.append({"artist": r["artist"], **min(
                    fresh, key=lambda s: s["start_local"] or "")})
    else:
        end = (_date.fromisoformat(today) + timedelta(days=WINDOW_DAYS)).isoformat()
        for r in rows:
            local = [s for s in r["shows"]
                     if s["metro_id"] == render.HOME_METRO
                     and today <= (s["start_local"] or "")[:10] <= end]
            if local:
                out.append({"artist": r["artist"], **min(
                    local, key=lambda s: s["start_local"] or "")})

    out.sort(key=lambda s: s["start_local"] or "")
    return out


def render_html(shows: list[dict], total: int) -> str:
    if not shows:
        return "<p>Nothing this time.</p>"
    lines = ["<ul>"]
    for s in shows:
        bits = [
            f"<strong>{html.escape(s['artist'])}</strong>",
            html.escape(s["day"] + (" · " + s["time"] if s["time"] else "")),
            html.escape(f"{s['venue'] or ''} · {s['city'] or ''}".strip(" ·")),
            html.escape(_describe(s)),
        ]
        line = " — ".join(b for b in bits if b)
        url = s.get("ticket_url") or s.get("event_url")
        if url:
            label = html.escape(s.get("ticket_label") or "Tickets")
            line += f' — <a href="{html.escape(url, quote=True)}">{label}</a>'
        lines.append(f"<li>{line}</li>")
    lines.append("</ul>")
    if total > len(shows):
        lines.append(f"<p>…and {total - len(shows)} more.</p>")
    return "".join(lines)


def build(conn, kind: str, today: str | None = None) -> dict:
    today = today or _date.today().isoformat()
    shows = collect(conn, kind, today)
    listed = shows[:MAX_LISTED]
    return {
        "kind": kind,
        "date": today,
        "title": KINDS[kind],
        "count": len(shows),
        "summary_html": render_html(listed, len(shows)),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "shows": listed,
    }


def write(conn, kind: str, out_dir: Path | None = None,
          today: str | None = None) -> Path:
    out_dir = Path(out_dir or config.DIGEST_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = build(conn, kind, today)
    path = out_dir / f"{payload['kind']}-{payload['date']}.json"
    # Atomic: rss_feed reads this directory on every feed request.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(path)
    return path
