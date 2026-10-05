"""Artist-name normalisation and collision risk.

Normalisation: Unicode NFKD, strip accents, casefold, drop a leading "the ",
& -> "and", strip punctuation, collapse whitespace.

The risk rules exist because the real library contains names like "!!!", "11",
"$CVM" and "4hero" — an exact normalised match on those is meaningless, so they
go to review instead of matching automatically.
"""
import re
import unicodedata

# Deliberately small. Every word here is a plausible band name AND a common
# word, so an exact match is not evidence on its own.
STOPWORDS = {
    "air", "alt", "america", "bad", "beach", "bear", "big", "black", "blue",
    "boston", "bread", "cake", "can", "chicago", "cream", "dance", "days",
    "dead", "dirt", "dog", "door", "dream", "drive", "east", "europe", "fire",
    "free", "fun", "game", "gold", "good", "green", "heart", "here", "high",
    "home", "hope", "ice", "iron", "journey", "kansas", "keep", "king", "kiss",
    "life", "light", "live", "love", "low", "man", "mars", "money", "moon",
    "mountain", "muse", "music", "name", "new", "night", "no", "nothing",
    "ocean", "one", "orange", "phoenix", "pink", "planet", "play", "pop",
    "queen", "rain", "red", "ride", "river", "rock", "rush", "sea", "shop",
    "silver", "sleep", "slow", "snow", "soul", "sound", "space", "star",
    "steel", "stone", "storm", "sun", "sweet", "test", "the", "them", "time",
    "today", "train", "tree", "war", "water", "west", "white", "wild", "wind",
    "wolf", "women", "work", "yes", "young",
}

MIN_SAFE_LENGTH = 5  # names of 4 chars or fewer always go to review

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalise(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold()
    s = s.replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    return s


def is_risky(norm_name: str, plex_name_count: int = 1) -> tuple[bool, str]:
    """Should this name match go to review rather than auto-confirm?

    Returns (risky, reason). `plex_name_count` is how many Plex artists share
    this normalised name.
    """
    if not norm_name:
        return True, "empty after normalisation"
    if len(norm_name) < MIN_SAFE_LENGTH:
        return True, f"short name ({len(norm_name)} chars)"
    if norm_name in STOPWORDS:
        return True, "common word"
    if plex_name_count > 1:
        return True, f"{plex_name_count} Plex artists share this name"
    return False, ""


def genres_overlap(plex_genres, jb_genres) -> bool | None:
    """True/False if both sides have genres, None if we cannot tell."""
    if not plex_genres or not jb_genres:
        return None
    a = {normalise(g) for g in plex_genres}
    b = {normalise(g) for g in jb_genres}
    return bool(a & b)
