"""Client behaviour, entirely offline. No trial data is used as a fixture."""
import httpx
import pytest

import config
import db
import jambase


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init(c)
    return c


def test_parse_ratelimit_real_header():
    # Verbatim shape from a live JamBase response.
    got = jambase.parse_ratelimit("hour;r=3599;t=1, minute;r=119;t=1")
    assert got == {"hour": 3599, "minute": 119}


def test_parse_ratelimit_missing_is_empty():
    assert jambase.parse_ratelimit(None) == {}
    assert jambase.parse_ratelimit("") == {}


def test_scrub_removes_secrets():
    out = jambase.scrub({"apiKey": "abc", "geoMetroId": "jambase:17"})
    assert out["apiKey"] == "<redacted>"
    assert out["geoMetroId"] == "jambase:17"


def test_budget_guard_blocks_and_records_pause(conn, monkeypatch):
    client = jambase.Client(conn, api_key="x", budget=5)
    for _ in range(5):
        client._record("events", {}, 200, 10, 3599)
    assert client.remaining_budget() == 0
    with pytest.raises(jambase.BudgetExhausted):
        client.check_budget()
    assert db.get_state(conn, "paused_at") is not None
    assert "5/5" in db.get_state(conn, "paused_reason")


def test_retries_count_against_budget(conn, monkeypatch):
    """A retried request bills every attempt, so the guard must see all three."""
    calls = {"n": 0}

    def fake_get(self, url, params=None, headers=None):
        calls["n"] += 1
        return httpx.Response(503, text="upstream down", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr(jambase.time, "sleep", lambda *_: None)
    client = jambase.Client(conn, api_key="x", budget=10)
    with pytest.raises(jambase.JamBaseError):
        client.get("events", {"geoMetroId": "jambase:17"})
    assert calls["n"] == config.MAX_RETRIES
    assert jambase.calls_this_period(conn) == config.MAX_RETRIES


def test_4xx_does_not_retry(conn, monkeypatch):
    calls = {"n": 0}

    def fake_get(self, url, params=None, headers=None):
        calls["n"] += 1
        return httpx.Response(400, text="bad param", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr(jambase.time, "sleep", lambda *_: None)
    client = jambase.Client(conn, api_key="x", budget=10)
    with pytest.raises(jambase.JamBaseError):
        client.get("events", {})
    assert calls["n"] == 1


def test_missing_key_raises_before_sending(conn):
    client = jambase.Client(conn, api_key="", budget=10)
    with pytest.raises(jambase.JamBaseError, match="JAMBASE_API_KEY"):
        client.get("events", {})
    assert jambase.calls_this_period(conn) == 0


# --- /v3/quota -----------------------------------------------------------------

QUOTA_BODY = {
    # Shape of a live /v3/quota response (values invented).
    "plan": "developer", "quota": 1000, "usedCalls": 400, "remainingCalls": 600,
    "overageCalls": 0, "periodStart": "2026-10-08T00:00:00.000Z",
    "periodEnd": "2099-11-08T00:00:00.000Z", "periodSource": "anchor",
    "limitType": "soft", "blocksAtQuota": False,
}


def _quota_http(status=200, body=None):
    def handler(request):
        assert request.url.path.endswith("/quota")
        return httpx.Response(status, json=body if body is not None else QUOTA_BODY)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_refresh_quota_is_not_logged_as_a_call(conn):
    reading = jambase.refresh_quota(conn, api_key="x", http=_quota_http())
    assert reading["usedCalls"] == 400
    assert jambase.calls_this_period(conn) == 0


def test_quota_adds_calls_made_since_the_reading(conn):
    jambase.refresh_quota(conn, api_key="x", http=_quota_http())
    client = jambase.Client(conn, api_key="x", budget=5)
    for _ in range(3):
        client._record("events", {}, 200, 10, 3599)
    q = jambase.quota(conn)
    assert (q["source"], q["used"], q["remaining"]) == ("jambase", 403, 597)
    # JamBase's count governs, not the local budget of 5.
    assert client.remaining_budget() == 597 - config.JAMBASE_QUOTA_RESERVE


def test_guard_stops_at_the_reserve(conn):
    body = dict(QUOTA_BODY, usedCalls=1000 - config.JAMBASE_QUOTA_RESERVE)
    jambase.refresh_quota(conn, api_key="x", http=_quota_http(body=body))
    client = jambase.Client(conn, api_key="x", budget=10_000)
    with pytest.raises(jambase.BudgetExhausted):
        client.check_budget()
    assert "JamBase count" in db.get_state(conn, "paused_reason")


def test_unavailable_quota_falls_back_to_the_estimate(conn):
    assert jambase.refresh_quota(
        conn, api_key="x", http=_quota_http(503, {"code": "quota_usage_unavailable"})
    ) is None
    assert jambase.quota(conn)["source"] == "estimate"


def test_expired_reading_is_ignored(conn):
    body = dict(QUOTA_BODY, periodEnd="2020-01-01T00:00:00.000Z")
    jambase.refresh_quota(conn, api_key="x", http=_quota_http(body=body))
    assert jambase.quota(conn)["source"] == "estimate"


def test_unlimited_plan_never_blocks(conn):
    body = dict(QUOTA_BODY, quota=None, remainingCalls=None)
    jambase.refresh_quota(conn, api_key="x", http=_quota_http(body=body))
    jambase.Client(conn, api_key="x", budget=0).check_budget(need=100)


def test_overage_is_flagged_and_blocks_every_call(conn, caplog):
    body = dict(QUOTA_BODY, usedCalls=1003, remainingCalls=0, overageCalls=3)
    jambase.refresh_quota(conn, api_key="x", http=_quota_http(body=body))
    assert "JAMBASE CHARGING" in caplog.text
    assert "3 overage calls" in db.get_state(conn, "charging")
    with pytest.raises(jambase.BudgetExhausted, match="charging"):
        jambase.Client(conn, api_key="x", budget=10_000).check_budget()


def test_paid_plan_is_flagged(conn):
    jambase.refresh_quota(conn, api_key="x", http=_quota_http(body=dict(QUOTA_BODY, plan="startup")))
    assert "'startup'" in db.get_state(conn, "charging")


def test_free_plan_under_quota_is_not_charging(conn):
    jambase.refresh_quota(conn, api_key="x", http=_quota_http())
    assert db.get_state(conn, "charging") == ""
    jambase.Client(conn, api_key="x").check_budget()


def test_quota_exceeded_429_is_not_retried(conn, monkeypatch):
    calls = {"n": 0}

    def fake_get(self, url, params=None, headers=None):
        calls["n"] += 1
        return httpx.Response(429, json={"type": "https://x/errors/quota-exceeded"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr(jambase.time, "sleep", lambda *_: None)
    with pytest.raises(jambase.JamBaseError):
        jambase.Client(conn, api_key="x", budget=10).get("events", {})
    assert calls["n"] == 1
