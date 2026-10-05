import norm


def test_normalise_basics():
    assert norm.normalise("The Beatles") == "beatles"
    assert norm.normalise("Simon & Garfunkel") == "simon and garfunkel"
    assert norm.normalise("Sigur Rós") == "sigur ros"
    assert norm.normalise("AC/DC") == "ac dc"
    assert norm.normalise("  Spaced   Out  ") == "spaced out"


def test_leading_the_dropped_only_as_a_word():
    assert norm.normalise("The The") == "the"
    assert norm.normalise("Theatre of Hate") == "theatre of hate"


def test_real_library_names_that_break_naive_matching():
    # All four exist in the Plex library.
    assert norm.normalise("!!!") == ""
    assert norm.is_risky(norm.normalise("!!!"))[0]
    assert norm.is_risky(norm.normalise("11"))[0]
    assert norm.is_risky(norm.normalise("$CVM"))[0]
    # 4hero is long enough and not a common word — safe to auto-match.
    assert not norm.is_risky(norm.normalise("4hero"))[0]


def test_common_word_goes_to_review():
    assert norm.is_risky("war")[0]
    assert norm.is_risky("phoenix")[0]
    assert not norm.is_risky("godspeed you black emperor")[0]


def test_duplicate_plex_names_go_to_review():
    risky, reason = norm.is_risky("nirvana", plex_name_count=2)
    assert risky and "2 Plex artists" in reason


def test_genres_overlap_unknown_when_missing():
    assert norm.genres_overlap([], ["rock"]) is None
    assert norm.genres_overlap(["Rock"], ["rock"]) is True
    assert norm.genres_overlap(["Jazz"], ["rock"]) is False
