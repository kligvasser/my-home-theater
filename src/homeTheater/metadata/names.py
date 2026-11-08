"""Does a provider's result name the title we searched for?

TMDb's search is forgiving: it always returns *something*, so taking the top hit
turned ``bkd-tk`` into a random show. Results are only accepted when a name
matches, and the names checked include originals and translations so a Hebrew or
romanized title stays matchable (see :meth:`.tmdb.TMDbClient.search`).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher

# Close enough for a typo or a spelling variant ("Traveler"/"Traveller"), and
# past "Alien"/"Aliens" thanks to the length floor below.
_SIMILARITY = 0.88
_FUZZY_MIN_LENGTH = 8

_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}
_PART_WORDS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
}
_ARTICLES = frozenset({"the", "a", "an"})
# Apostrophes join a word ("Don't" = "Dont"), Hebrew geresh/gershayim included.
_JOINERS_RE = re.compile(r"['’`ʼ׳״]")
_TOKEN_RE = re.compile(r"[^\W_]+")


def _tokens(text: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", text)
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    words = _TOKEN_RE.findall(_JOINERS_RE.sub("", bare.casefold().replace("&", " and ")))
    out: list[str] = []
    for word in words:
        if out and out[-1] == "part" and word in _PART_WORDS:
            out.append(_PART_WORDS[word])
        else:
            out.append(_ROMAN.get(word, word))
    return out


def fold_title(text: str) -> str:
    """Comparable form: no case, accents, niqqud or punctuation; numerals unified."""

    return " ".join(_tokens(text))


def _without_article(tokens: list[str]) -> list[str]:
    return tokens[1:] if len(tokens) > 1 and tokens[0] in _ARTICLES else tokens


def _numbers(tokens: list[str]) -> set[str]:
    return {t for t in tokens if t.isdigit()}


def _is_prefix(short: list[str], long: list[str]) -> bool:
    return len(short) >= 2 and long[: len(short)] == short


def _same_title(query: list[str], name: list[str], *, exact_year: bool) -> bool:
    if query == name:
        return True
    # Sequels differ by a digit and little else: "Rocky 2" is not "Rocky 3".
    if _numbers(query) != _numbers(name):
        return False
    left, right = " ".join(query), " ".join(name)
    if min(len(left), len(right)) >= _FUZZY_MIN_LENGTH:
        ratio = SequenceMatcher(None, left, right, autojunk=False).ratio()
        if ratio >= _SIMILARITY:
            return True
    # A shortened name ("Star Wars Episode IV") needs the year to vouch for it.
    return exact_year and (_is_prefix(query, name) or _is_prefix(name, query))


def names_match(query: str, names: Iterable[str | None], *, exact_year: bool = False) -> bool:
    """True if any of ``names`` is the same title as ``query``.

    ``exact_year`` also allows one name to be a prefix of the other: a subtitle
    left out of a filename is only believable when the release year agrees.
    """

    wanted = _without_article(_tokens(query))
    if not wanted:
        return False
    for name in names:
        tokens = _without_article(_tokens(name or ""))
        if tokens and _same_title(wanted, tokens, exact_year=exact_year):
            return True
    return False
