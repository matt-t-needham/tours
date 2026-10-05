"""Plex sync, offline. Fixtures mirror the real server's response shapes."""
import pytest

import db
import plex

# Trimmed from a real /library/sections/3/all response. Note the mix: almost
# every artist carries guid="plex://artist/...", one carries mbid://, and the
# names include the ones that break naive matching.
SECTION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="4" librarySectionID="3" librarySectionTitle="Music">
  <Directory ratingKey="1001" guid="plex://artist/5d07bf93403c640290771417" type="artist" title="!!!" />
  <Directory ratingKey="1002" guid="mbid://772096dc-db98-4ef5-8157-6eec36a33b27" type="artist" title="2-Sided Triangle" />
  <Directory ratingKey="1003" guid="local://1003" type="artist" title="Godspeed You! Black Emperor" />
  <Directory ratingKey="1004" guid="plex://artist/aaaa" type="artist" title="The Beatles" />
</MediaContainer>"""

METADATA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="1">
  <Directory ratingKey="1004" guid="plex://artist/aaaa" type="artist" title="The Beatles">
    <Country id="27" tag="United Kingdom" />
    <Genre id="9" tag="Rock" />
    <Guid id="mbid://b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d" />
  </Directory>
</MediaContainer>"""


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init(c)
    return c


@pytest.fixture
def fake_plex(monkeypatch):
    import xml.etree.ElementTree as ET

    def fake_get(path, params=None):
        if "/all" in path:
            return ET.fromstring(SECTION_XML)
        return ET.fromstring(METADATA_XML)

    monkeypatch.setattr(plex, "_get", fake_get)


def test_fetch_artists_extracts_mbid_only_from_mbid_guids(fake_plex):
    artists = plex.fetch_artists()
    assert len(artists) == 4
    by_name = {a["name"]: a for a in artists}
    assert by_name["2-Sided Triangle"]["mbid"] == "772096dc-db98-4ef5-8157-6eec36a33b27"
    # plex:// and local:// guids are not MusicBrainz IDs.
    assert by_name["The Beatles"]["mbid"] is None
    assert by_name["!!!"]["mbid"] is None


def test_sync_is_idempotent(conn, fake_plex):
    first = plex.sync(conn)
    assert first == {"total": 4, "added": 4, "renamed": 0}
    second = plex.sync(conn)
    assert second == {"total": 4, "added": 0, "renamed": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM plex_artist").fetchone()["n"] == 4


def test_sync_stores_normalised_names(conn, fake_plex):
    plex.sync(conn)
    row = conn.execute(
        "SELECT norm_name FROM plex_artist WHERE name = 'The Beatles'"
    ).fetchone()
    assert row["norm_name"] == "beatles"


def test_resolve_metadata_fills_gaps_and_is_resumable(conn, fake_plex):
    plex.sync(conn)
    result = plex.resolve_metadata(conn)
    # All four need the pass: three lack an mbid, and the fourth still has no
    # genres (mbid and genres come from the same call, so never make two).
    assert result["checked"] == 4
    assert result["found"] == 4
    # Re-running finds nothing left to do.
    assert plex.resolve_metadata(conn)["checked"] == 0


def test_resolve_metadata_stores_genres(conn, fake_plex):
    import json
    plex.sync(conn)
    plex.resolve_metadata(conn)
    row = conn.execute(
        "SELECT genres FROM plex_artist WHERE rating_key = '1004'").fetchone()
    assert json.loads(row["genres"]) == ["Rock"]


def test_sync_never_clears_a_resolved_mbid(conn, fake_plex):
    """The list view has no mbid for most artists — a re-sync must not wipe one
    that resolve_mbids already found."""
    plex.sync(conn)
    conn.execute("UPDATE plex_artist SET mbid = 'resolved-id' WHERE rating_key = '1004'")
    conn.commit()
    plex.sync(conn)
    row = conn.execute(
        "SELECT mbid FROM plex_artist WHERE rating_key = '1004'"
    ).fetchone()
    assert row["mbid"] == "resolved-id"
