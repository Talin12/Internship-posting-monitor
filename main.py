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

import matcher as matcher_mod
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
    # Load .env once up front so HF_API_TOKEN / RESUME_TEXT / Telegram vars are
    # all available to matching and notification alike.
    notify.load_dotenv()
    config = load_config()
    filters = Filters.from_config(config.get("filters", {}))
    companies = select_companies(config, args.company)
    if not companies:
        return 1

    postings, failed = fetch_all(companies)
    matched = apply_filters(postings, filters)
    log.info("%d postings fetched, %d match filters", len(postings), len(matched))

    # --dry-run: fetch, filter, print, touch nothing (no state, no notify).
    # If resume matching is configured, score here too (embeddings are free) so
    # you can eyeball the spread and pick a threshold. State is still untouched.
    if args.dry_run:
        resume = matcher_mod.load_resume()
        mtchr = matcher_mod.build_matcher(config, resume)
        scores: dict[str, float] = {}
        if mtchr:
            to_score = sorted(matched, key=lambda x: (x.company, x.title))[
                : matcher_mod.max_to_score(config)
            ]
            try:
                for r in mtchr.score_batch(resume, to_score):
                    scores[r.posting_id] = r.score
                print(f"\n[DRY RUN] scored {len(scores)} postings against your resume "
                      f"(threshold {mtchr.threshold:.2f}); ✓ = would notify:")
            except Exception as exc:  # noqa: BLE001
                log.error("scoring failed in dry-run: %s", exc)

        print(f"\n[DRY RUN] {len(matched)} postings match filters "
              f"({len(failed)} companies failed to fetch):")
        ordered = sorted(
            matched, key=lambda x: (-scores.get(x.id, -1), x.company, x.title)
        )
        for p in ordered:
            loc = f" — {p.location}" if p.location else ""
            tag = ""
            if p.id in scores:
                mark = "✓" if mtchr and scores[p.id] >= mtchr.threshold else " "
                tag = f"[{scores[p.id]:.2f} {mark}] "
            print(f"  {tag}• {p.company}: {p.title}{loc}\n      {p.url}")
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

    # Resume matching (optional). Score the new postings; keep the relevant ones
    # to notify, and remember the rejected ones so we never re-score them. If
    # matching is off/unavailable, notify about all new postings.
    resume = matcher_mod.load_resume()
    mtchr = matcher_mod.build_matcher(config, resume)
    annotations: dict[str, str] = {}
    rejected_ids: list[str] = []          # scored below threshold -> mark seen
    to_notify = new

    if mtchr:
        cap = matcher_mod.max_to_score(config)
        # Score at most `cap` per run; any beyond the cap stay unseen (to_notify
        # already covers them via the fallback below) and are scored next run.
        batch = new[:cap]
        try:
            results = mtchr.score_batch(resume, batch)
        except Exception as exc:  # noqa: BLE001
            # Scoring failed (bad token, cold model, rate limit, ...). Degrade to
            # keyword-only: notify about all new postings rather than going silent.
            # A persistent HF misconfig then shows up as loud logs + unscored
            # notifications, never as total silence.
            log.error("resume scoring failed (%s); falling back to notify-all", exc)
            print("Resume scoring FAILED; notifying all new (keyword-only) this run.")
            to_notify = new
        else:
            by_id = {p.id: p for p in batch}
            scored_beyond_cap = new[cap:]  # not scored this run -> notify unscored
            to_notify = list(scored_beyond_cap)
            for r in results:
                if r.matched:
                    to_notify.append(by_id[r.posting_id])
                    annotations[r.posting_id] = r.detail
                else:
                    rejected_ids.append(r.posting_id)
            log.info(
                "scored %d new via %s: %d matched, %d below threshold, %d beyond cap",
                len(results), mtchr.name, len(to_notify) - len(scored_beyond_cap),
                len(rejected_ids), len(scored_beyond_cap),
            )

    # Notify first; only record notified ids if the send fully succeeded.
    notifier = notify.build_notifier()
    messages = notify.build_messages(to_notify, annotations)
    sent = notifier.send(messages) if to_notify else True

    # Rejected-after-scoring ids are recorded regardless of the send — they were
    # never going to be notified, and we don't want to pay to re-score them.
    st.add(rejected_ids)

    if sent:
        added = st.add([p.id for p in to_notify])
        st.touch()
        state_mod.save(st)
        if to_notify:
            log.info("notified via %s and recorded %d new ids", notifier.name, added)
            print(f"Notified {len(to_notify)} matching posting(s) via {notifier.name}.")
        else:
            print(f"No postings matched your resume ({len(rejected_ids)} scored, none "
                  f"above threshold).")
        return 0
    else:
        # Notification failed: don't record the to-notify ids (retry next run).
        # Rejected ids were already added above and will still be saved.
        st.touch()
        state_mod.save(st)
        log.error("notification failed; matched postings will retry next run")
        print("Notification FAILED; matched postings unchanged (will retry next run).")
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
