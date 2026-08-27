"""Resume-to-posting relevance scoring.

Given your resume and a batch of postings, produce a relevance score in [0, 1]
for each so the monitor can notify you only about roles that actually fit your
background — not every internship that passes the keyword filters.

Default provider: the Hugging Face **sentence-similarity** pipeline (via the
Inference Providers router, `router.huggingface.co/hf-inference`) with a free
sentence-embedding model (`sentence-transformers/all-MiniLM-L6-v2`). It embeds
your resume and each job description and returns cosine similarity — no LLM, no
per-token cost, and the whole batch scores in a single HTTP request.

Everything sits behind the small `Matcher` interface, so a different backend
(a hosted LLM that also explains *why* it matched, a local model, ...) can be
dropped in via `build_matcher` without touching main.py.

Config (config.yaml):

    matching:
      enabled: true
      provider: huggingface
      model: sentence-transformers/all-MiniLM-L6-v2
      threshold: 0.30        # keep postings scoring >= this
      max_to_score: 20       # cap API work per run

Auth: HF_API_TOKEN from the environment (a free token from
https://huggingface.co/settings/tokens). Resume text: RESUME_TEXT env var, or a
local resume.txt (both gitignored — the repo is public).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol

import requests

from sources import Posting

log = logging.getLogger("jobmon.matcher")

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_THRESHOLD = 0.30
DEFAULT_MAX_TO_SCORE = 20
REQUEST_TIMEOUT = 30  # embedding models can be briefly "cold"; give them room


@dataclass
class MatchResult:
    posting_id: str
    score: float          # relevance in [0, 1]
    matched: bool         # score >= threshold
    detail: str           # short human string, e.g. "relevance 0.42"


def posting_text(p: Posting) -> str:
    """Compact text representation of a posting for embedding.

    Title first (it carries the most signal and the model truncates long input),
    then company, location, and a slice of the description.
    """
    parts = [p.title]
    if p.company:
        parts.append(f"at {p.company}")
    if p.location:
        parts.append(p.location)
    if p.description:
        parts.append(p.description)
    return ". ".join(parts)


# --------------------------------------------------------------------------- #
# Matcher interface + implementations
# --------------------------------------------------------------------------- #
class Matcher(Protocol):
    name: str
    threshold: float

    def score_batch(self, resume: str, postings: list[Posting]) -> list[MatchResult]:
        """Score every posting against the resume. Raises on transport failure."""
        ...


class HuggingFaceMatcher:
    name = "huggingface-embeddings"

    def __init__(
        self,
        token: str,
        model: str = DEFAULT_MODEL,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self.token = token
        self.model = model
        self.threshold = threshold
        # HF's current Inference Providers router. The legacy
        # api-inference.huggingface.co host is retired.
        self.url = (
            f"https://router.huggingface.co/hf-inference/models/{model}"
            f"/pipeline/sentence-similarity"
        )

    def score_batch(self, resume: str, postings: list[Posting]) -> list[MatchResult]:
        if not postings:
            return []
        sentences = [posting_text(p) for p in postings]
        scores = self._similarities(resume, sentences)
        results = []
        for p, s in zip(postings, scores):
            score = max(0.0, min(1.0, float(s)))  # clamp; cosine can be slightly <0
            results.append(
                MatchResult(
                    posting_id=p.id,
                    score=score,
                    matched=score >= self.threshold,
                    detail=f"relevance {score:.2f}",
                )
            )
        return results

    def _similarities(self, source: str, sentences: list[str]) -> list[float]:
        """Call HF sentence-similarity; one request returns one score per sentence.

        Retries once on a cold-model 503. Raises on any other non-200 or on a
        response shape we don't recognize, so the caller can skip scoring this
        run without marking postings as seen.
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {"inputs": {"source_sentence": source, "sentences": sentences}}
        last_exc: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                resp = requests.post(
                    self.url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
                )
                if resp.status_code == 503 and attempt == 1:
                    log.warning("HF model loading (503); retrying once")
                    continue
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, list) or len(data) != len(sentences):
                    raise ValueError(f"unexpected HF response shape: {data!r:.200}")
                return data
            except Exception as exc:  # noqa: BLE001 - retry once, then propagate
                last_exc = exc
                if attempt == 1:
                    log.warning("HF request failed (%s); retrying once", exc)
        assert last_exc is not None
        raise last_exc


# --------------------------------------------------------------------------- #
# Resume loading + matcher selection
# --------------------------------------------------------------------------- #
def load_resume(path: str = "resume.txt") -> Optional[str]:
    """Resume text from RESUME_TEXT env (CI) or a local resume.txt. None if absent."""
    env = os.environ.get("RESUME_TEXT")
    if env and env.strip():
        return env.strip()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read().strip()
            return text or None
        except OSError as exc:
            log.warning("could not read %s: %s", path, exc)
    return None


def build_matcher(config: dict, resume: Optional[str]) -> Optional[Matcher]:
    """Construct a matcher from config + environment, or None to disable matching.

    Returns None (and logs why) when matching is off, the resume is missing, or
    the HF token is unset — in which case main.py falls back to notifying about
    every new posting, so the tool still works before matching is set up.
    """
    cfg = (config or {}).get("matching", {}) or {}
    if not cfg.get("enabled", False):
        log.info("resume matching disabled in config")
        return None
    if not resume:
        log.warning("resume matching enabled but no resume found; notifying all new")
        return None
    provider = cfg.get("provider", "huggingface")
    if provider != "huggingface":
        log.warning("unknown matching provider %r; notifying all new", provider)
        return None
    token = os.environ.get("HF_API_TOKEN")
    if not token:
        log.warning("HF_API_TOKEN not set; notifying all new (matching off)")
        return None
    return HuggingFaceMatcher(
        token=token,
        model=cfg.get("model", DEFAULT_MODEL),
        threshold=float(cfg.get("threshold", DEFAULT_THRESHOLD)),
    )


def max_to_score(config: dict) -> int:
    cfg = (config or {}).get("matching", {}) or {}
    return int(cfg.get("max_to_score", DEFAULT_MAX_TO_SCORE))
