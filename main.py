"""Job posting monitor — orchestration and CLI.

Detection and notification only. This never applies to anything.

Flow per run:
  1. Load config + state.
  2. Fetch all (or one) companies concurrently.
  3. Filter postings to internships matching config.
  4. Diff against seen state -> new postings.
  5. Notify (batched), then record the notified ids in state.

A single company failing (bad token, transient 5xx, timeout) is logged and
skipped; its results are NOT written to state, so a transient error can never
permanently suppress that company's postings.
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

import notify
import state as state_mod
from filters import Filters, apply_filters
from sources import Posting, fetch_company

log = logging.getLogger("jobmon")

MAX_WORKERS = 8
CONFIG_PATH = "config.yaml"


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def fetch_all(companies: list[dict]) -> tuple[list[Posting], list[str]]:
    """Fetch companies concurrently.

    Returns (postings, failed_company_names). Only postings from companies that
    fetched successfully are returned; failures are collected so main() can avoid
    treating a fetch error as "no postings".
    """
    postings: list[Posting] = []
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(
                fetch_company, c["name"], c["platform"], c["token"]
            ): c["name"]
            for c in companies
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                result = fut.result()
                postings.extend(result)
                log.info("fetched %s: %d postings", name, len(result))
            except Exception as exc:  # noqa: BLE001
                failed.append(name)
                log.error("FETCH FAILED for %s: %s", name, exc)
    return postings, failed


def select_companies(config: dict, only: str | None) -> list[dict]:
    companies = config.get("companies", [])
    if only:
        companies = [c for c in companies if c["name"].lower() == only.lower()]
        if not companies:
            log.error("no company named %r in config", only)
    return companies


def run(args: argparse.Namespace) -> int:
    config = load_config()
    filters = Filters.from_config(config.get("filters", {}))
    companies = select_companies(config, args.company)
    if not companies:
        return 1

    postings, failed = fetch_all(companies)
    matched = apply_filters(postings, filters)
    log.info("%d postings fetched, %d match filters", len(postings), len(matched))

    # --dry-run: fetch, filter, print, touch nothing (no state, no notify).
    if args.dry_run:
        print(f"\n[DRY RUN] {len(matched)} postings match filters "
              f"({len(failed)} companies failed to fetch):")
        for p in sorted(matched, key=lambda x: (x.company, x.title)):
            loc = f" — {p.location}" if p.location else ""
            print(f"  • {p.company}: {p.title}{loc}\n    {p.url}")
        return 0

    st = state_mod.load()

    # --init: seed state with everything current, notify nothing.
    if args.init:
        added = st.add([p.id for p in matched])
        st.touch()
        state_mod.save(st)
        print(f"[INIT] seeded state with {added} current postings; no notifications sent.")
        return 0

    new = st.new_ids(matched)
    log.info("%d new postings after diffing against seen state", len(new))

    if not new:
        st.touch()
        state_mod.save(st)
        print("No new postings.")
        return 0

    # Notify first; only record ids as seen if the send fully succeeded.
    notifier = notify.build_notifier()
    messages = notify.build_messages(new)
    sent = notifier.send(messages)

    if sent:
        added = st.add([p.id for p in new])
        st.touch()
        state_mod.save(st)
        log.info("notified via %s and recorded %d new ids", notifier.name, added)
        print(f"Notified {len(new)} new posting(s) via {notifier.name}.")
        return 0
    else:
        # Do NOT record ids — we want to retry these on the next run.
        log.error("notification failed; state left unchanged so postings retry next run")
        print("Notification FAILED; state unchanged (will retry next run).")
        return 1


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Poll ATS job boards for new internships.")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch, filter, print to stdout; touch no state and notify nothing")
    p.add_argument("--init", action="store_true",
                   help="seed state with everything currently posted; notify nothing")
    p.add_argument("--company", metavar="NAME",
                   help="only process the named company (for debugging)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.init and args.dry_run:
        log.error("--init and --dry-run are mutually exclusive")
        return 2
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
