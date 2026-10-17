"""tour_tracker — upcoming shows by artists in the Plex music library.

Serves the rendered page and the review pane. The scheduled jobs run through
cli.py (host cron, `docker exec`), not from here.
"""
import logging
import time
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

import config
import db
import jambase
import match as matcher
import render
import trips

# Log stamps are UTC whatever the container TZ (set for show dates).
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("tour_tracker")

app = FastAPI(title="tour_tracker")


def _conn():
    conn = db.connect()
    db.init(conn)
    return conn


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index():
    with _conn() as conn:
        return render.render(conn)


@app.get("/review", response_class=HTMLResponse)
async def review_page():
    return FileResponse(config.BASE_DIR / "static" / "review.html")


@app.get("/api/pending")
async def api_pending():
    with _conn() as conn:
        return {"pending": matcher.pending(conn)}


class Decision(BaseModel):
    jambase_id: str
    action: str  # confirm | reject | ignore
    plex_rating_key: str | None = None


@app.post("/api/decide")
async def api_decide(d: Decision):
    with _conn() as conn:
        if d.action == "ignore":
            if not d.plex_rating_key:
                raise HTTPException(400, "plex_rating_key required to ignore")
            matcher.ignore_artist(conn, d.plex_rating_key)
        elif d.action in ("confirm", "reject"):
            status = "confirmed" if d.action == "confirm" else "rejected"
            matcher.decide(conn, d.jambase_id, status, d.plex_rating_key)
        else:
            raise HTTPException(400, f"unknown action {d.action!r}")
        remaining = conn.execute(
            "SELECT COUNT(DISTINCT jambase_id) AS n FROM artist_link"
            " WHERE status = 'pending'"
        ).fetchone()["n"]
    return {"ok": True, "remaining": remaining}


@app.get("/api/status")
async def api_status():
    with _conn() as conn:
        return JSONResponse({
            "quota": jambase.quota(conn),
            "charging": db.get_state(conn, "charging") or None,
            "rate_remaining_hour": db.get_state(conn, "rate_remaining_hour"),
            "plex_sync_at": db.get_state(conn, "plex_sync_at"),
            "jambase_sync_at": db.get_state(conn, "jambase_sync_at"),
            "paused": bool(db.get_state(conn, "paused_at")),
        })


# --- trip searches ---------------------------------------------------------------
# These spend JamBase quota, so they refuse cross-site requests: a sibling
# *.bix.computer page is "same-site" and still carries the Access cookie.

def _same_origin(request: Request) -> None:
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        raise HTTPException(403, "cross-site request refused")
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-origin request refused")


class TripIn(BaseModel):
    place: str
    date_from: str
    date_to: str
    radius_mi: int | None = None


def _spend(fn):
    """Run a quota-spending trip action with the same guards as the CLI."""
    with _conn() as conn:
        jambase.refresh_quota(conn)
        try:
            with jambase.Client(conn) as client:
                return fn(conn, client)
        except trips.TripError as exc:
            raise HTTPException(400, str(exc))
        except jambase.BudgetExhausted as exc:
            raise HTTPException(429, str(exc))
        except jambase.JamBaseError as exc:
            log.error("trip search failed: %s", exc)
            raise HTTPException(502, "JamBase request failed")


# Plain `def`: these block on JamBase, so FastAPI runs them in its threadpool.
@app.post("/api/trips")
def api_trip_create(t: TripIn, request: Request):
    _same_origin(request)
    tid = _spend(lambda conn, client: trips.create(
        conn, client, t.place, t.date_from, t.date_to, t.radius_mi))
    return {"ok": True, "id": tid}


@app.post("/api/trips/{search_id}/update")
def api_trip_update(search_id: int, request: Request):
    _same_origin(request)
    _spend(lambda conn, client: trips.update(conn, client, search_id))
    return {"ok": True, "id": search_id}


@app.post("/api/trips/{search_id}/delete")
def api_trip_delete(search_id: int, request: Request):
    _same_origin(request)
    with _conn() as conn:
        if not trips.delete(conn, search_id):
            raise HTTPException(404, "no such search")
    return {"ok": True}
