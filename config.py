"""All tunables/env vars for tour_tracker."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "tours.db"))
# Immutable digest files for the RSS feed; mounted read-only into the rss
# container (see bix-infra/docker-compose.yml).
DIGEST_DIR = Path(os.environ.get("DIGEST_DIR", DATA_DIR / "digests"))
# JamBase Developer terms forbid storing API data beyond 30 days; digests are
# JamBase data, so they age out with everything else (events.prune).
DIGEST_KEEP_DAYS = int(os.environ.get("DIGEST_KEEP_DAYS", "30"))

# --- JamBase -----------------------------------------------------------------
JAMBASE_API_KEY = os.environ.get("JAMBASE_API_KEY", "")
JAMBASE_BASE_URL = os.environ.get("JAMBASE_BASE_URL", "https://api.data.jambase.com/v3")

# Fallback only: used when no current /v3/quota reading exists (see
# jambase.refresh_quota). Then the monthly figure is our own count, labelled
# "estimated" wherever it surfaces.
JAMBASE_MONTHLY_BUDGET = int(os.environ.get("JAMBASE_MONTHLY_BUDGET", "1000"))
# With a /v3/quota reading, stop this many calls short of JamBase's quota. 0
# means the guard stops at exactly the quota: the reading plus our own logged
# calls is exact, and the Developer plan only bills calls *above* it.
JAMBASE_QUOTA_RESERVE = int(os.environ.get("JAMBASE_QUOTA_RESERVE", "0"))
# Trip searches (trips.py): default radius around the resolved city.
TRIP_RADIUS_MI = int(os.environ.get("TRIP_RADIUS_MI", "25"))

# Developer terms §4: no storing or caching API data beyond 30 days. Anything
# not re-seen in a pull within this window is deleted by events.prune.
JAMBASE_RETENTION_DAYS = int(os.environ.get("JAMBASE_RETENTION_DAYS", "30"))
# Plans that never bill. Any other plan, or any overage, is flagged as charging.
JAMBASE_FREE_PLANS = {"trial", "developer", "mcp"}

# Pipe-separated, and MUST stay quoted in .env — unquoted, a shell that sources
# .env splits on the pipe and silently sets this to the first ID alone.
JAMBASE_METRO_IDS = os.environ.get(
    "JAMBASE_METRO_IDS", "jambase:17|jambase:12|jambase:4"
)

# jambase:53 is ALSO named "Portland Area" — it is Portland, MAINE. Asserted
# before every sync; a mismatch means the ID space shifted under us and the
# page would fill with plausible concerts 3,000 miles away.
METRO_EXPECTED_REGION = {
    "jambase:17": "US-OR",  # Portland Area
    "jambase:12": "US-WA",  # Seattle - Tacoma Area
    "jambase:4": "US-CA",   # San Francisco Bay Area
}

PER_PAGE = 100  # the maximum; default is 40, which would triple the call cost

# --- Plex --------------------------------------------------------------------
PLEX_URL = os.environ.get("PLEX_URL", "http://host.docker.internal:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
# Section 3 = "Music". Section 4 ("Audiobooks") is ALSO type="artist" and must
# never be scanned.
PLEX_MUSIC_SECTION_ID = os.environ.get("PLEX_MUSIC_SECTION_ID", "3")

# --- HTTP --------------------------------------------------------------------
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
# JamBase guidance is ~1 req/sec sustained (hourly policy q=3600, minute q=120).
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "0.5"))

# --- Page --------------------------------------------------------------------
SITE_TITLE = os.environ.get("SITE_TITLE", "Tours")
CANCELLED_GRACE_DAYS = int(os.environ.get("CANCELLED_GRACE_DAYS", "7"))
NEW_BADGE_DAYS = int(os.environ.get("NEW_BADGE_DAYS", "7"))
