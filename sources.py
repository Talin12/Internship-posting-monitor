"""ATS fetchers and normalization.

Each supported platform exposes a public JSON API with a different shape. The
fetchers here pull postings from those APIs and normalize them into a single
`Posting` dataclass so the rest of the program never has to care which ATS a
job came from.

Supported platforms:
  - greenhouse : https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
  - lever      : https://api.lever.co/v0/postings/{token}?mode=json
  - ashby      : https://api.ashbyhq.com/posting-api/job-board/{token}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

log = logging.getLogger("jobmon.sources")

USER_AGENT = "job-posting-monitor/1.0 (+https://github.com/)"
REQUEST_TIMEOUT = 15  # seconds, per request


@dataclass(frozen=True)
class Posting:
    """A single normalized job posting.

    `id` is stable across runs: it is the ATS's own posting id, prefixed with
    the platform and company token, e.g. "greenhouse:stripe:4567890". We never
    hash the title or fold in a timestamp, so a posting keeps the same id run
    over run even if its title or location is edited.
    """

    id: str
    company: str
    title: str
    location: str
    url: str
    posted_at: Optional[datetime]
    source: str  # platform name: greenhouse | lever | ashby


# --------------------------------------------------------------------------- #
# HTTP helper: 15s timeout, one retry on failure.
# --------------------------------------------------------------------------- #
def _get_json(url: str):
    """GET `url` and return parsed JSON. Times out at 15s, retries once.

    Raises the underlying exception if both attempts fail; callers are expected
    to catch this so one bad company never aborts the whole run.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    last_exc: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - deliberately broad; retry then re-raise
            last_exc = exc
            if attempt == 1:
                log.warning("request failed (%s), retrying once: %s", url, exc)
    assert last_exc is not None
    raise last_exc


# --------------------------------------------------------------------------- #
# Date parsing helpers
# --------------------------------------------------------------------------- #
def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string (with or without offset) to an aware datetime."""
    if not value:
        return None
    try:
        # Python 3.11+ fromisoformat handles offsets like -05:00 and Z-less input.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.debug("could not parse ISO date: %r", value)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_epoch_ms(value) -> Optional[datetime]:
    """Parse an epoch-milliseconds integer (Lever's createdAt) to a datetime."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------- #
# Per-platform fetchers
# --------------------------------------------------------------------------- #
def fetch_greenhouse(company: str, token: str) -> list[Posting]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    data = _get_json(url)
    postings: list[Posting] = []
    for job in data.get("jobs", []):
        job_id = job.get("id")
        if job_id is None:
            continue
        location = (job.get("location") or {}).get("name") or ""
        posted_at = _parse_iso(job.get("first_published")) or _parse_iso(
            job.get("updated_at")
        )
        postings.append(
            Posting(
                id=f"greenhouse:{token}:{job_id}",
                company=company,
                title=(job.get("title") or "").strip(),
                location=location.strip(),
                url=job.get("absolute_url") or "",
                posted_at=posted_at,
                source="greenhouse",
            )
        )
    return postings


def fetch_lever(company: str, token: str) -> list[Posting]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    data = _get_json(url)  # top-level JSON array
    postings: list[Posting] = []
    for job in data:
        job_id = job.get("id")
        if not job_id:
            continue
        categories = job.get("categories") or {}
        location = categories.get("location") or ""
        all_locs = categories.get("allLocations") or []
        if all_locs and len(all_locs) > 1:
            location = ", ".join(all_locs)
        postings.append(
            Posting(
                id=f"lever:{token}:{job_id}",
                company=company,
                title=(job.get("text") or "").strip(),
                location=location.strip(),
                url=job.get("hostedUrl") or job.get("applyUrl") or "",
                posted_at=_parse_epoch_ms(job.get("createdAt")),
                source="lever",
            )
        )
    return postings


def fetch_ashby(company: str, token: str) -> list[Posting]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    data = _get_json(url)
    postings: list[Posting] = []
    for job in data.get("jobs", []):
        # Ashby returns unlisted/draft jobs too; only surface live ones.
        if job.get("isListed") is False:
            continue
        job_id = job.get("id")
        if not job_id:
            continue
        location = job.get("location") or ""
        secondary = job.get("secondaryLocations") or []
        if secondary:
            extra = [s.get("location") for s in secondary if s.get("location")]
            if extra:
                location = ", ".join([location, *extra]) if location else ", ".join(extra)
        postings.append(
            Posting(
                id=f"ashby:{token}:{job_id}",
                company=company,
                title=(job.get("title") or "").strip(),
                location=location.strip(),
                url=job.get("jobUrl") or job.get("applyUrl") or "",
                posted_at=_parse_iso(job.get("publishedAt")),
                source="ashby",
            )
        )
    return postings


# Platform registry. Adding a new ATS is one entry + one fetcher above.
FETCHERS: dict[str, Callable[[str, str], list[Posting]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_company(name: str, platform: str, token: str) -> list[Posting]:
    """Dispatch to the right fetcher for a configured company."""
    try:
        fetcher = FETCHERS[platform]
    except KeyError:
        raise ValueError(
            f"unknown platform {platform!r} for {name!r}; "
            f"supported: {', '.join(sorted(FETCHERS))}"
        )
    return fetcher(name, token)


if __name__ == "__main__":
    # Step 1/2 smoke test: fetch a hardcoded company per platform and print.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for plat, tok, nm in [
        ("greenhouse", "stripe", "Stripe"),
        ("lever", "palantir", "Palantir"),
        ("ashby", "ramp", "Ramp"),
    ]:
        results = fetch_company(nm, plat, tok)
        print(f"\n=== {nm} ({plat}) : {len(results)} postings ===")
        for p in results[:3]:
            print(f"  {p.id}")
            print(f"    {p.title!r} | {p.location!r} | posted={p.posted_at}")
            print(f"    {p.url}")
