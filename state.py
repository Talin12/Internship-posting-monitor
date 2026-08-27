"""Persistent 'seen postings' state.

State lives in seen.json (committed to the repo) with the shape:

    {"ids": ["greenhouse:stripe:123", ...], "last_run": "2026-08-27T12:00:00+00:00"}

The `ids` list is ordered oldest-first and capped at MAX_IDS; when the cap is
exceeded the oldest ids are dropped. IDs are only ever added AFTER a successful
notification send (see main.py), so a failed notification never marks postings
as seen.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

log = logging.getLogger("jobmon.state")

DEFAULT_PATH = "seen.json"
MAX_IDS = 20_000


@dataclass
class State:
    ids: list[str] = field(default_factory=list)
    last_run: str | None = None
    # Signature of the last run's fetch errors, so we alert once per distinct
    # problem (and once on recovery) instead of every run. None/"" = healthy.
    last_error: str | None = None
    # A set kept in sync with `ids` for O(1) membership tests.
    _id_set: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self._id_set = set(self.ids)

    def is_seen(self, posting_id: str) -> bool:
        return posting_id in self._id_set

    def new_ids(self, postings: Iterable) -> list:
        """Return the subset of `postings` whose ids we have never seen."""
        return [p for p in postings if p.id not in self._id_set]

    def add(self, posting_ids: Iterable[str]) -> int:
        """Add ids (oldest-first order preserved), dedup, and cap the list.

        Returns the number of genuinely new ids added.
        """
        added = 0
        for pid in posting_ids:
            if pid not in self._id_set:
                self.ids.append(pid)
                self._id_set.add(pid)
                added += 1
        if len(self.ids) > MAX_IDS:
            drop = len(self.ids) - MAX_IDS
            dropped, self.ids = self.ids[:drop], self.ids[drop:]
            self._id_set.difference_update(dropped)
            log.info("state capped at %d ids, dropped %d oldest", MAX_IDS, drop)
        return added

    def touch(self) -> None:
        self.last_run = datetime.now(timezone.utc).isoformat()


def load(path: str = DEFAULT_PATH) -> State:
    if not os.path.exists(path):
        log.info("no state file at %s, starting fresh", path)
        return State()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("could not read state %s (%s); starting fresh", path, exc)
        return State()
    ids = data.get("ids", [])
    if not isinstance(ids, list):
        ids = []
    return State(
        ids=list(ids),
        last_run=data.get("last_run"),
        last_error=data.get("last_error"),
    )


def save(state: State, path: str = DEFAULT_PATH) -> None:
    """Atomically write state to `path` (temp file + rename)."""
    payload = {
        "ids": state.ids,
        "last_run": state.last_run,
        "last_error": state.last_error,
    }
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".seen-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=0, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
