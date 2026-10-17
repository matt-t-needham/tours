"""tour_tracker CLI. Run: python -m cli <subcommand>  (or `tours` in Docker)."""
import logging
import sys
import time

import click

import config
import db
import digest as digests
import events
import jambase
import match as matcher
import plex
import trips

# Log stamps are UTC whatever the container TZ (set for show dates).
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("tour_tracker")


def _conn():
    conn = db.connect()
    db.init(conn)
    return conn


@click.group()
def cli():
    pass


@cli.command("init-db")
def init_db():
    conn = _conn()
    click.echo(f"schema v{db.get_state(conn, 'schema_version')} at {config.DB_PATH}")


@cli.command("plex-sync")
@click.option("--mbids", is_flag=True, help="Also resolve missing MusicBrainz IDs and genres "
                                            "(one local Plex call per artist).")
@click.option("--limit", type=int, default=None, help="Cap the MBID pass (for testing).")
def plex_sync(mbids, limit):
    """Import every artist from the Plex music section. Zero JamBase calls."""
    conn = _conn()
    if not config.PLEX_TOKEN:
        raise click.ClickException("PLEX_TOKEN is not set")
    result = plex.sync(conn)
    click.echo(
        f"plex-sync: {result['total']} artists "
        f"({result['added']} new, {result['renamed']} renamed)"
    )
    if mbids:
        def progress(i, total, found):
            click.echo(f"  mbids: {i}/{total} checked, {found} found", err=True)

        r = plex.resolve_metadata(conn, limit=limit, progress=progress)
        click.echo(f"mbids: {r['found']}/{r['checked']} resolved")


@cli.command("jambase-sync")
@click.option("--metros", default=None, help="Override JAMBASE_METRO_IDS.")
def jambase_sync(metros):
    """Full pull of every event in the configured metros (~45 calls)."""
    conn = _conn()
    if jambase.refresh_quota(conn) is None:
        log.warning("quota endpoint unavailable, guarding on the local estimate")
    with jambase.Client(conn) as client:
        before = jambase.calls_this_period(conn)
        try:
            # Refuse up front rather than stop halfway: a partial pull would
            # leave the page half-updated until the next run.
            client.check_budget(need=int(db.get_state(conn, "last_sync_calls", "50")))
            result = events.sync_full(conn, client, metros)
        except jambase.BudgetExhausted as exc:
            raise click.ClickException(str(exc))
        spent = jambase.calls_this_period(conn) - before
    db.set_state(conn, "last_sync_calls", spent)
    db.set_state(conn, "paused_at", "")
    p = result["pruned"]
    click.echo(f"jambase-sync: {result['events']} events, "
               f"{result['removed']} marked removed, {spent} calls used; pruned "
               f"{p['past_events']} past + {p['stale_events']} stale events, "
               f"{p['stale_artists']} artists, {p['links']} links, {p['digests']} digests, "
               f"{p['trip_searches']} trip searches")


@cli.command("quota")
def quota_cmd():
    """Check JamBase usage (free call). Exits 1 if JamBase is charging."""
    conn = _conn()
    if jambase.refresh_quota(conn) is None:
        raise click.ClickException("quota endpoint unavailable")
    q = jambase.quota(conn)
    click.echo(f"quota: {q['plan']} {q['used']}/{q['quota']} "
               f"({q['period_start'][:10]} to {q['period_end'][:10]})")
    charging = db.get_state(conn, "charging")
    if charging:
        raise click.ClickException(f"JAMBASE CHARGING: {charging}")


@cli.group("trip")
def trip():
    """Saved trip searches: shows by library artists in a place + date range."""


def _trip_line(t):
    return (f"#{t['id']} [{t['region']}] {t['place']} {t['date_from']}..{t['date_to']} "
            f"{t['radius_mi']}mi: {len(t['results'])} artists of {t['events_seen']} events "
            f"({t['calls']} calls, fetched {t['fetched_at'][:16]})")


@trip.command("add")
@click.argument("place")
@click.argument("date_from")
@click.argument("date_to")
@click.option("--radius", type=int, default=None, help="Miles (default TRIP_RADIUS_MI).")
def trip_add(place, date_from, date_to, radius):
    """Search PLACE ('Boston, MA') from DATE_FROM to DATE_TO (YYYY-MM-DD)."""
    conn = _conn()
    jambase.refresh_quota(conn)
    try:
        with jambase.Client(conn) as client:
            tid = trips.create(conn, client, place, date_from, date_to, radius)
    except (trips.TripError, jambase.BudgetExhausted, jambase.JamBaseError) as exc:
        raise click.ClickException(str(exc))
    t = trips.get(conn, tid)
    click.echo(_trip_line(t))
    for r in t["results"]:
        click.echo(f"  {r['day']:<12} {r['artist']}{' (possible)' if r['possible'] else ''}"
                   f" — {r['venue']}, {r['city']}")


@trip.command("update")
@click.argument("search_id", type=int)
def trip_update(search_id):
    """Re-run a saved search and replace its results."""
    conn = _conn()
    jambase.refresh_quota(conn)
    try:
        with jambase.Client(conn) as client:
            t = trips.update(conn, client, search_id)
    except (trips.TripError, jambase.BudgetExhausted, jambase.JamBaseError) as exc:
        raise click.ClickException(str(exc))
    click.echo(_trip_line(t))


@trip.command("list")
def trip_list():
    """Saved searches (expired ones are pruned first). Zero API calls."""
    for t in trips.all_searches(_conn()):
        click.echo(_trip_line(t))


@trip.command("delete")
@click.argument("search_id", type=int)
def trip_delete(search_id):
    """Delete a saved search. Zero API calls."""
    if not trips.delete(_conn(), search_id):
        raise click.ClickException(f"no saved search {search_id}")
    click.echo(f"deleted #{search_id}")


@cli.command("match")
@click.option("--reset", is_flag=True, help="Discard undecided links and rebuild "
                                            "(use after changing matching rules). "
                                            "Confirmed/rejected decisions survive.")
def match_cmd(reset):
    """Link JamBase performers to Plex artists. Zero API calls."""
    conn = _conn()
    result = matcher.run(conn, reset=reset)
    click.echo(f"match: {result['auto']} auto, {result['pending']} pending review")


@cli.command("render")
@click.option("--out", default=None, help="Write the page to a file instead of stdout.")
def render_cmd(out):
    """Render the page. Zero API calls."""
    import render as renderer
    conn = _conn()
    html = renderer.render(conn)
    if out:
        with open(out, "w") as fh:
            fh.write(html)
        click.echo(f"wrote {out} ({len(html)} bytes)")
    else:
        click.echo(html)


@cli.command("digest")
@click.option("--kind", type=click.Choice(sorted(digests.KINDS)), required=True)
@click.option("--date", "on_date", default=None, help="Override today (testing).")
def digest_cmd(kind, on_date):
    """Write an RSS digest file. Zero API calls."""
    conn = _conn()
    path = digests.write(conn, kind, today=on_date)
    import json as _json
    payload = _json.loads(path.read_text())
    click.echo(f"digest: {payload['count']} shows -> {path}")


@cli.command("status")
def status():
    """Quota, sync times and pending reviews."""
    conn = _conn()
    jambase.refresh_quota(conn)
    q = jambase.quota(conn)
    artists = conn.execute("SELECT COUNT(*) AS n FROM plex_artist").fetchone()["n"]
    with_mbid = conn.execute(
        "SELECT COUNT(*) AS n FROM plex_artist WHERE mbid IS NOT NULL"
    ).fetchone()["n"]
    events = conn.execute(
        "SELECT COUNT(*) AS n FROM event WHERE removed_at IS NULL"
    ).fetchone()["n"]
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM artist_link WHERE status = 'pending'"
    ).fetchone()["n"]

    if q["source"] == "jambase":
        click.echo(f"quota ({q['plan']}, {q['limit_type']} limit, from JamBase /v3/quota):")
        click.echo(f"  {q['used']}/{q['quota']} calls, period "
                   f"{q['period_start'][:10]} to {q['period_end'][:10]}")
    else:
        click.echo("quota (ESTIMATED — /v3/quota unavailable):")
        click.echo(f"  {q['used']}/{q['quota']} calls since {q['period_start'][:10]}")
    hour = db.get_state(conn, "rate_remaining_hour")
    if hour:
        click.echo(f"  rate limit remaining this hour: {hour} (from JamBase)")
    click.echo(f"plex:   {artists} artists, {with_mbid} with MBIDs "
               f"(synced {db.get_state(conn, 'plex_sync_at', 'never')})")
    click.echo(f"events: {events} live (synced {db.get_state(conn, 'jambase_sync_at', 'never')})")
    click.echo(f"review: {pending} pending")
    charging = db.get_state(conn, "charging")
    if charging:
        click.echo(f"JAMBASE CHARGING: {charging}")
    paused = db.get_state(conn, "paused_at")
    if paused:
        click.echo(f"PAUSED at {paused}: {db.get_state(conn, 'paused_reason')}")


@cli.command("wipe-jambase")
@click.confirmation_option(
    prompt="Delete every row obtained from JamBase (events, performers, artists "
           "and ALL review decisions)?"
)
def wipe_jambase():
    """Trial-terms compliance: delete all JamBase-derived data. Plex data stays."""
    conn = _conn()
    counts = db.wipe_jambase_data(conn)
    for table, n in counts.items():
        click.echo(f"  {table}: {n} rows deleted")
    # Digest bodies are JamBase data too.
    digests_gone = 0
    for f in config.DIGEST_DIR.glob("*.json") if config.DIGEST_DIR.is_dir() else []:
        f.unlink()
        digests_gone += 1
    click.echo(f"  digests: {digests_gone} files deleted")
    click.echo("JamBase data wiped. plex_artist and api_call history retained.")


if __name__ == "__main__":
    cli()
