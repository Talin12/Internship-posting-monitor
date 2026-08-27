"""Title and location matching.

A `Filters` object holds four case-insensitive term lists. A posting is kept
only when its title passes the include/exclude title rules AND its location
passes the include/exclude location rules. Empty include lists mean "match
anything" for that field; empty exclude lists mean "exclude nothing".

Terms match on WORD BOUNDARIES, not raw substrings. This is deliberate: with
raw substring matching the term "intern" also matches "Internal Audit" and
"International", flooding every run with false positives. Word-boundary matching
makes "intern" match the standalone word "Intern" (and, via the separate
"internship" term, "Internship") while ignoring "internal"/"international".
Multi-word and hyphenated terms like "co-op" and "new grad" work too.

To fall back to pure substring matching, replace `_term_matches` below with
`return term in text.lower()`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable

from sources import Posting


@dataclass
class Filters:
    title_include: list[str] = field(default_factory=list)
    title_exclude: list[str] = field(default_factory=list)
    location_include: list[str] = field(default_factory=list)
    location_exclude: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, data: dict) -> "Filters":
        data = data or {}
        return cls(
            title_include=[s.lower() for s in data.get("title_include", [])],
            title_exclude=[s.lower() for s in data.get("title_exclude", [])],
            location_include=[s.lower() for s in data.get("location_include", [])],
            location_exclude=[s.lower() for s in data.get("location_exclude", [])],
        )


@lru_cache(maxsize=256)
def _compile(term: str) -> "re.Pattern[str]":
    # Lookarounds act like word boundaries but behave sanely even when the term
    # begins or ends with punctuation (e.g. "co-op", ".net").
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)")


def _term_matches(text: str, term: str) -> bool:
    return _compile(term).search(text) is not None


def _any_match(haystack: str, needles: Iterable[str]) -> bool:
    h = haystack.lower()
    return any(_term_matches(h, n) for n in needles)


def matches(posting: Posting, filters: Filters) -> bool:
    """Return True if `posting` should be kept under `filters`."""
    title = posting.title or ""
    location = posting.location or ""

    # Title: must include (if any include terms) and must not be excluded.
    if filters.title_include and not _any_match(title, filters.title_include):
        return False
    if filters.title_exclude and _any_match(title, filters.title_exclude):
        return False

    # Location: same logic. An empty include list matches any location, so a
    # posting with no location string still passes when location_include is empty.
    if filters.location_include and not _any_match(
        location, filters.location_include
    ):
        return False
    if filters.location_exclude and _any_match(location, filters.location_exclude):
        return False

    return True


def apply_filters(postings: Iterable[Posting], filters: Filters) -> list[Posting]:
    return [p for p in postings if matches(p, filters)]
