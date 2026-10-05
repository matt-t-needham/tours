# CLAUDE.md — tour_tracker

Finds upcoming concerts in Portland, Seattle and the SF Bay Area by artists in
the Plex music library, and publishes them as a private page. Port 3011
(compose service `tours`), intended for `tours.bix.computer` behind Cloudflare
Access.

## Commands

```bash
# Dev (outside Docker) — PLEX_URL must point at the LAN address, not
# host.docker.internal
set -a; . ../bix-infra/.env; set +a
export PLEX_URL=http://192.168.0.47:32400
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest==8.3.4
.venv/bin/python -m pytest -q              # 94 offline tests
.venv/bin/uvicorn main:app --reload --port 8011

.venv/bin/python -m cli plex-sync --mbids  # 0 API calls
.venv/bin/python -m cli jambase-sync       # ~45 calls
.venv/bin/python -m cli match              # 0 API calls
.venv/bin/python -m cli quota              # free; exits 1 if JamBase is charging
.venv/bin/python -m cli trip add "Boston, MA" 2026-11-10 2026-11-15 [--radius 25]  # ~3 calls
.venv/bin/python -m cli trip update <id>   # ~2 calls; replaces results
.venv/bin/python -m cli trip list | delete <id>
.venv/bin/python -m cli status

# Production (via Docker Compose in bix-infra/) — pytest gates the build
cd /home/matt/apps/bix-infra
docker compose build tours && docker compose up -d tours
```

## Operations

Scans Plex music section 3 only; section 4 "Audiobooks" is also
`type=artist` and is never scanned. Sole event source is the JamBase Data API
v3, queried by metro and matched against Plex locally, so cost tracks region
event counts rather than library size. Saved "trip" searches cover any city
and date range, each in its own tab.

Full pulls only — no delta/watermark/tombstone logic. A full pull of all three
metros is ~45 calls (~4,300 events). Events absent from a pull get
`removed_at` set.

Cron: `plex-sync` daily 06:10, `quota` daily 06:15 (free; logs `JAMBASE
CHARGING` on any overage or paid plan), `jambase-sync` **every two days**
06:20 (`*/2`, at most 16 runs ≈ 720 calls/month against a 1,000 quota),
`match` daily 06:30, RSS digests Monday 06:40 (`new`) and Thursday 06:40
(`portland`) — digests cost nothing. 06:xx because 03:50–05:10 is congested
by the log dump, the 04:00 container recreate, the rage jobs and the log
review.

## Module map

- `config.py` — all tunables/env vars
- `db.py` — SQLite schema, migrations (`_ADDED_COLUMNS`), `wipe_jambase_data`
- `jambase.py` — budget-guarded API client, RateLimit header parsing, pagination
- `plex.py` — library sync; `resolve_metadata` fetches MBID **and** genres in one call
- `events.py` — event ingest, the three-shape coercion helpers, metro-region assertion
- `match.py` — performer → Plex artist linking, review queue
- `render.py` — page context; one row per ARTIST (Portland preferred), `ticket_label` naming the real seller
- `trips.py` — saved trip searches (place + date range): resolve city → metro tab, search by lat/lon radius, match against existing link decisions without writing any; results stored as JSON on `trip_search`
- `digest.py` — immutable digest files for the RSS feed (`new` Mondays, `portland` Thursdays); written to `data/digests/<kind>-<date>.json`
- `cli.py` — click subcommands (what cron runs)
- `main.py` — FastAPI: `/`, `/review`, `/api/pending`, `/api/decide`, `/api/status`, `/healthz`, `POST /api/trips`, `/api/trips/{id}/update|delete` (same-origin only)
- `templates/index.html`, `static/review.html` — no build step
- `tests/` — fully offline; Docker build has a hard test gate

## Gotchas

- **`JAMBASE_METRO_IDS` must stay quoted in `.env`.** It contains pipes;
  unquoted, any shell that sources `.env` splits on them and silently sets the
  variable to the first ID alone.
- **`jambase:53` is also named "Portland Area" — it is Portland, MAINE.**
  `events.assert_metro_regions` refuses to sync if metro 17 stops reporting
  `US-OR`.
- **JamBase's JSON-LD has three shapes for the same field.** Region is a bare
  ISO string at metro level, a full name at city level, and an object in event
  venue addresses. `geo` uses `@type` in some places and `type` in others. Use
  `events.coerce_code` / `events.geo_of`; never key on `@type` existing.
- **Empty means `""`, not `null`** (`doorTime`, `previousStartDate`) — see
  `events.blank_to_none`.
- **`startDate` is venue-local with no offset.** Never convert it; the
  timezone is at `location.address.x-timezone`.
- **No MBID matching.** `expandArtistSameAs=true` returns social links only,
  never a musicbrainz identifier. Plex MBIDs (1,717/1,748) are stored anyway so
  matching can upgrade for free if a tier ever exposes them.
- **No genre gating.** Plex uses AllMusic-style genres ("Pop/Rock", "R&B"),
  JamBase uses its own slugs ("indie", "rhythm-and-blues-soul"). Gating on
  overlap sent 131 of 144 correct matches to review. Genres are shown in the
  review UI as evidence, never used as a gate.
- **`ticketingLinkSecondary` offers are resale** (StubHub, SeatGeek, Gametime).
  Only `ticketingLinkPrimary` is the real seller.
- **Quota comes from `GET /v3/quota`**, which is free (doesn't count against
  the quota; own limit 3,600/h). `jambase-sync` and `status` refresh it; the
  guard uses JamBase's `usedCalls` plus calls logged since the reading, stops
  at exactly the quota (`JAMBASE_QUOTA_RESERVE` = 0; the fallback budget is
  also 1,000), and refuses a sync up front if the last
  sync's call count won't fit (no half-pulls). Only when no reading covers the
  current period does it fall back to the local estimate against
  `JAMBASE_MONTHLY_BUDGET`. The Developer plan does **not** block at quota —
  it bills overage per call — so this guard is what stops charges.
- **Charging is silent except in `/v3/quota`.** Overage calls aren't blocked
  or marked in responses. Every quota reading (each sync, `status`, and the
  daily 06:15 `cli quota`) checks `overageCalls`/`usedCalls > quota`/a non-free
  plan; any hit logs `JAMBASE CHARGING` at ERROR to `logs/tours/cron.log` (so
  the nightly compiler flags it), shows a "!" banner on the page, and makes the
  guard refuse every API call until a clean reading.
- **30-day retention (Developer terms §4).** `events.prune` runs at the end of
  every sync: shows whose date has passed go immediately; events, JamBase
  artists and their match decisions not re-seen in 30 days go too; digest
  files older than 30 days are deleted (so RSS items age out of the feed).
  `cli wipe-jambase` also deletes the digests.
- **Trip searches never touch the west-coast tables.** Writing their events
  into `event` would put them on the main page and in digests, and the next
  sync would mark them all removed. They read `artist_link` (rejected stays
  rejected, pending shows as "possible") but never write it — no review queue
  entries for a one-off city. Region tab = the city's JamBase metro minus
  " Area". Deleted once `date_to` passes or results are 30 days old.
  Trip searches share the monthly quota with the sync: if they leave fewer
  calls than the last sync used, the sync's preflight refuses it.
- **Digests are written, never recomputed.** rss_feed's guids derive from
  kind + date; a live "last two weeks" query would change the content behind a
  fixed guid on every poll and make readers re-show old items. The file is
  written once via tmp+rename (rss_feed reads the directory on every request).
- **Digest bodies are HTML inside an RSS description** — every artist, venue
  and city is `html.escape`d. A hostile name must not inject markup into a
  reader.
- **Festivals match many artists at once** — `render.collect` groups by event
  or one festival floods the page.
- **Metro ids arrive as bare integers** (`x-jamBaseMetroId: 4`) but the config
  and region assertion use `jambase:4`. `events.canon_metro` normalises them —
  without it the Portland-preferred rule silently never fires.
- **No tour names.** JamBase has no field for it: `description` is null on all
  3,919 events, `x-customTitle` (10) is alternative billing and `x-subtitle`
  (24) is festival names. setlist.fm could supply them via MBID but was
  declined.
- **Trial terms (verified 2026-10-05, `/legal/trial-terms` v3.0.0):** "Upon
  expiration of the Trial … any cached or stored data must be deleted." The
  account moved to Developer on 2026-10-05; all trial-era data was wiped and
  re-pulled under the Developer key.
