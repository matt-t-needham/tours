"""Budget-guarded JamBase Data API v3 client.

Every request goes through Client.get(), which logs the call to `api_call`
before returning and refuses to send once the local monthly estimate reaches
the budget.

On quota: `GET /v3/quota` reports the account's real usage for the current
billing period and does not count against it (its own limit is 3,600/hour,
60/minute). `refresh_quota` stores a reading in `sync_state['quota_remote']`;
the budget guard then uses that reading plus every call logged since it, and
falls back to the local estimate only when no reading for the current period
exists (endpoint down, 503 `quota_usage_unavailable`, or never fetched).

The RateLimit response header covers hour and minute windows only:

    ratelimit-policy: hour;q=3600;w=3600, minute;q=120;w=60
    ratelimit:        hour;r=3599;t=1,    minute;r=119;t=1
"""
import json
import logging
import re
import time
from datetime import datetime, timezone

import httpx

import config
import db

log = logging.getLogger("tour_tracker.jambase")

# Anything matching this is redacted before a params dict reaches `api_call`.
# The key travels in the Authorization header, not in params, so this is
# defence in depth rather than the primary control.
_SECRET_KEYS = re.compile(r"(key|token|secret|auth)", re.IGNORECASE)


class BudgetExhausted(RuntimeError):
    pass


class JamBaseError(RuntimeError):
    pass


def scrub(params: dict) -> dict:
    return {k: ("<redacted>" if _SECRET_KEYS.search(k) else v) for k, v in params.items()}


def parse_ratelimit(header: str | None) -> dict[str, int]:
    """Parse `hour;r=3599;t=1, minute;r=119;t=1` into {'hour': 3599, ...}."""
    out: dict[str, int] = {}
    if not header:
        return out
    for part in header.split(","):
        bits = [b.strip() for b in part.split(";")]
        if not bits:
            continue
        window = bits[0]
        for b in bits[1:]:
            k, _, v = b.partition("=")
            if k == "r" and v.strip().lstrip("-").isdigit():
                out[window] = int(v)
    return out


def quota_period_start(conn, ref: datetime | None = None) -> datetime:
    """Start of the current quota month.

    The anchor day is configurable because we do not know whether JamBase
    resets on the calendar month or the signup date — the dashboard will say,
    and `sync_state['quota_anchor_day']` records it when it does.
    """
    ref = ref or datetime.now(timezone.utc)
    anchor = int(db.get_state(conn, "quota_anchor_day", "1"))
    anchor = max(1, min(28, anchor))
    if ref.day >= anchor:
        return ref.replace(day=anchor, hour=0, minute=0, second=0, microsecond=0)
    month = ref.month - 1 or 12
    year = ref.year - 1 if ref.month == 1 else ref.year
    return ref.replace(
        year=year, month=month, day=anchor, hour=0, minute=0, second=0, microsecond=0
    )


def calls_this_period(conn) -> int:
    start = quota_period_start(conn).isoformat(timespec="seconds")
    return _calls_since(conn, start)


def _calls_since(conn, ts: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM api_call WHERE ts >= ?", (ts,)
    ).fetchone()
    return row["n"]


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def remote_reading(conn, now: datetime | None = None) -> dict | None:
    """The stored /v3/quota reading, if it still covers the current period."""
    raw = db.get_state(conn, "quota_remote")
    if not raw:
        return None
    try:
        reading = json.loads(raw)
        expired = (now or datetime.now(timezone.utc)) >= _parse_ts(reading["periodEnd"])
    except (ValueError, KeyError, TypeError):
        return None
    return None if expired else reading


def quota(conn) -> dict:
    """Usage for display and for the budget guard.

    With a current /v3/quota reading: JamBase's own count at the time of the
    reading plus our calls logged since, against JamBase's quota and period.
    Without one: the local estimate against JAMBASE_MONTHLY_BUDGET.
    """
    reading = remote_reading(conn)
    if reading is not None:
        since = _calls_since(conn, reading["checked_at"])
        limit = reading.get("quota")
        used = int(reading.get("usedCalls") or 0) + since
        return {
            "source": "jambase",
            "used": used,
            "quota": limit,
            "remaining": None if limit is None else limit - used,
            "plan": reading.get("plan"),
            "limit_type": reading.get("limitType"),
            "period_start": reading.get("periodStart"),
            "period_end": reading.get("periodEnd"),
            "checked_at": reading["checked_at"],
        }
    used = calls_this_period(conn)
    return {
        "source": "estimate",
        "used": used,
        "quota": config.JAMBASE_MONTHLY_BUDGET,
        "remaining": config.JAMBASE_MONTHLY_BUDGET - used,
        "plan": None,
        "limit_type": None,
        "period_start": quota_period_start(conn).isoformat(timespec="seconds"),
        "period_end": None,
        "checked_at": None,
    }


def charge_warnings(reading: dict) -> list[str]:
    """Anything in a /v3/quota reading that means JamBase is billing us.

    Overage is silent on the Developer plan — calls are not blocked or marked
    in their responses — so this reading is the only place it shows.
    """
    out = []
    overage = int(reading.get("overageCalls") or 0)
    used, limit = reading.get("usedCalls"), reading.get("quota")
    if overage > 0:
        out.append(f"{overage} overage calls this period, billed per call")
    elif limit is not None and used is not None and used > limit:
        out.append(f"{used}/{limit} calls used, over quota (billed per call)")
    plan = reading.get("plan")
    if plan not in config.JAMBASE_FREE_PLANS:
        out.append(f"plan is {plan!r}, not a free plan")
    return out


def refresh_quota(conn, api_key: str | None = None, http: httpx.Client | None = None) -> dict | None:
    """Fetch /v3/quota (free — it does not count against the quota).

    Not logged to `api_call`, for the same reason. Returns the stored reading,
    or None when the endpoint is unavailable; the guard then falls back to the
    local estimate.
    """
    api_key = api_key if api_key is not None else config.JAMBASE_API_KEY
    if not api_key:
        return None
    own = http is None
    http = http or httpx.Client(timeout=config.HTTP_TIMEOUT)
    try:
        resp = http.get(
            f"{config.JAMBASE_BASE_URL}/quota",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if resp.status_code != 200:
            log.warning("quota endpoint returned %s: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        _parse_ts(data["periodEnd"])  # refuse a reading we can't age out
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("quota endpoint failed: %s", exc)
        return None
    finally:
        if own:
            http.close()
    reading = {
        k: data.get(k)
        for k in ("plan", "quota", "usedCalls", "remainingCalls", "periodStart",
                  "periodEnd", "periodSource", "limitType", "blocksAtQuota")
    }
    reading["overageCalls"] = data.get("overageCalls")
    reading["checked_at"] = db.now()
    db.set_state(conn, "quota_remote", reading)
    warnings = charge_warnings(reading)
    for w in warnings:
        log.error("JAMBASE CHARGING: %s", w)
    db.set_state(conn, "charging", "; ".join(warnings))
    return reading


class Client:
    def __init__(self, conn, api_key: str | None = None, budget: int | None = None):
        self.conn = conn
        self.api_key = api_key if api_key is not None else config.JAMBASE_API_KEY
        self.budget = budget if budget is not None else config.JAMBASE_MONTHLY_BUDGET
        self._client = httpx.Client(timeout=config.HTTP_TIMEOUT)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def remaining_budget(self) -> int:
        """Calls left before the guard stops us.

        From JamBase's own count (less QUOTA_RESERVE) when a current /v3/quota
        reading exists, otherwise the local estimate against `self.budget`.
        """
        q = quota(self.conn)
        if q["source"] == "jambase":
            if q["remaining"] is None:  # unlimited plan
                return 10**9
            return q["remaining"] - config.JAMBASE_QUOTA_RESERVE
        return self.budget - q["used"]

    def check_budget(self, need: int = 1) -> None:
        charging = db.get_state(self.conn, "charging")
        if charging:
            db.set_state(self.conn, "paused_at", db.now())
            db.set_state(self.conn, "paused_reason", f"JamBase charging: {charging}")
            raise BudgetExhausted(f"JamBase charging: {charging}")
        remaining = self.remaining_budget()
        if remaining < need:
            q = quota(self.conn)
            jb = q["source"] == "jambase"
            what = "JamBase count" if jb else "estimated"
            limit = q["quota"] if jb else self.budget
            db.set_state(self.conn, "paused_at", db.now())
            db.set_state(
                self.conn,
                "paused_reason",
                f"budget: {q['used']}/{limit} calls used ({what}), "
                f"{remaining} usable, needed {need}",
            )
            raise BudgetExhausted(
                f"budget reached: {q['used']}/{limit} ({what}), "
                f"{remaining} usable, needed {need}"
            )

    def _record(self, endpoint, params, status, items, rate_remaining, error=None):
        self.conn.execute(
            "INSERT INTO api_call (ts, endpoint, params, http_status, items_returned,"
            " rate_remaining, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                db.now(),
                endpoint,
                json.dumps(scrub(params)),
                status,
                items,
                rate_remaining,
                error,
            ),
        )
        self.conn.commit()

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        """One JamBase GET. Every attempt counts as a call and is recorded."""
        params = dict(params or {})
        if not self.api_key:
            raise JamBaseError("JAMBASE_API_KEY is not set")
        url = f"{config.JAMBASE_BASE_URL}/{endpoint.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_error = None

        for attempt in range(1, config.MAX_RETRIES + 1):
            # Budget is checked before every attempt: a retry bills like any
            # other call.
            self.check_budget()
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                # No HTTP response means nothing was billed, but record it so
                # failures are visible in `status`.
                self._record(endpoint, params, None, None, None, error=str(exc))
                last_error = exc
                time.sleep(2**attempt)
                continue

            rate = parse_ratelimit(resp.headers.get("ratelimit"))
            rate_hour = rate.get("hour")
            if rate_hour is not None:
                db.set_state(self.conn, "rate_remaining_hour", rate_hour)
            if rate.get("minute") is not None:
                db.set_state(self.conn, "rate_remaining_minute", rate["minute"])

            if resp.status_code == 200:
                data = resp.json()
                items = None
                for key in ("events", "metros", "artists", "venues"):
                    if isinstance(data.get(key), list):
                        items = len(data[key])
                        break
                self._record(endpoint, params, 200, items, rate_hour)
                db.set_state(self.conn, "last_call_at", db.now())
                time.sleep(config.REQUEST_DELAY)
                return data

            self._record(endpoint, params, resp.status_code, None, rate_hour,
                         error=resp.text[:500])
            # Retry only on 429 / 5xx. A 4xx is our bug and retrying just burns
            # quota.
            if (resp.status_code != 429 and resp.status_code < 500) or "quota" in resp.text[:500]:
                raise JamBaseError(f"{resp.status_code} from {endpoint}: {resp.text[:300]}")
            last_error = JamBaseError(f"{resp.status_code} from {endpoint}")
            time.sleep(2**attempt)

        raise JamBaseError(f"{endpoint} failed after {config.MAX_RETRIES} attempts: {last_error}")

    def paginate(self, endpoint: str, params: dict, max_pages: int = 100):
        """Yield each page, following pagination.nextPage until absent."""
        params = dict(params)
        params.setdefault("perPage", config.PER_PAGE)
        page = 1
        while page <= max_pages:
            params["page"] = page
            data = self.get(endpoint, params)
            yield data
            pagination = data.get("pagination") or {}
            if not pagination.get("nextPage"):
                return
            page += 1
        log.warning("%s: hit max_pages=%s, stopping", endpoint, max_pages)
