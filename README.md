# Job Posting Monitor

Polls company ATS job boards on a schedule, finds **new** internship / new-grad
postings matching your filters, and pushes them to Telegram. It only ever
notifies about postings it has never seen before.

**It does not apply to anything.** Detection and notification only — no
auto-apply, no resume generation, no browser automation. All job data comes from
public ATS JSON APIs (no auth, no HTML scraping).

Supported ATS platforms: **Greenhouse**, **Lever**, **Ashby**.

Targeting is done by **keyword filters** on the job title (tuned here for a
software-engineering / backend profile). An optional **résumé-matching** stage
(free Hugging Face embeddings) can score each posting for semantic relevance too,
but it ships **disabled** — on these already-filtered software roles the free
model was too coarse to rank reliably. It's a drop-in hybrid if you want it later
(see *Résumé matching* below).

---

## How it works

```
config.yaml ─▶ sources.py ─▶ filters.py ─▶ state.py ─▶ matcher.py ─▶ notify.py
 (companies)   (fetch all     (title/loc    (diff vs    (résumé       (Telegram or
                concurrently)  matching)     seen.json)   relevance)    stdout)
```

Each run fetches every company concurrently, normalizes results into one
`Posting` dataclass, filters to internships, diffs against `seen.json`,
optionally scores the new ones against your résumé, and sends **one batched
Telegram message** listing only the matches. Posting IDs are recorded as "seen"
**only after a successful send**, so a failed notification (or a transient ATS
error) never permanently hides a posting.

Each posting gets a stable ID of the form `platform:token:atsID`
(e.g. `greenhouse:stripe:8031833`) so it is recognized run over run even if its
title or location changes.

---

## Quick start (local)

Requires **Python 3.11+**.

```bash
# 1. Install dependencies (a virtualenv is recommended)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Try it without a bot — prints matches to stdout, touches nothing
python main.py --dry-run

# 3. Seed state with everything currently posted, so your first real
#    run doesn't notify you about 80+ existing postings
python main.py --init

# 4. From now on, this notifies only about postings that appear later
python main.py
```

Without Telegram configured, notifications print to stdout with a warning, so
the tool is usable before you set up a bot.

### CLI

| Command | What it does |
| --- | --- |
| `python main.py` | Normal run: fetch, filter, diff, notify, update state. |
| `python main.py --dry-run` | Fetch, filter, print to stdout. Touches no state, sends nothing. |
| `python main.py --init` | Seed state with everything current. Notifies nothing. |
| `python main.py --company Stripe` | Only process the named company (debugging). |
| `python main.py -v` | Verbose/debug logging. |

---

## Setting up Telegram

1. In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`,
   and follow the prompts. It gives you a **bot token** like
   `123456789:AAE...`.
2. Send your new bot any message (so it's allowed to message you back).
3. Get your **chat ID**: open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read
   `result[].message.chat.id`.
4. Locally, create a `.env` file (copy from `.env.example`):

   ```
   TELEGRAM_BOT_TOKEN=123456789:AAE...
   TELEGRAM_CHAT_ID=987654321
   ```

   `main.py` parses `.env` itself — no `python-dotenv` needed. `.env` is
   git-ignored; never commit it.

---

## Résumé matching (optional, disabled by default)

> **Disabled by default** (`matching.enabled: false`). On job listings already
> narrowed to software roles by the title filters, the free embedding model was
> too coarse to rank reliably (it scored Product Manager above Software Engineer),
> so keyword filters do the targeting. Enable this only if you want a soft
> relevance signal or broaden your filters. The plumbing below is a drop-in.

When enabled, after keyword filtering and diffing, each **new** posting is scored
for semantic relevance to your résumé so you only get pinged about roles that fit.
It uses the **free Hugging Face sentence-similarity API** (Inference Providers
router) with the `sentence-transformers/all-MiniLM-L6-v2` embedding model — no
LLM, no per-token cost, and the whole batch scores in a single request per run.

**Setup:**

1. Get a free Hugging Face token at
   [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
   (a read token is enough). Add it to `.env`:

   ```
   HF_API_TOKEN=hf_your_token_here
   ```

2. Put your résumé text in **`resume.txt`** (plain text) in the repo root. Both
   `resume.txt` and `*.pdf` are git-ignored — your résumé never enters the
   (public) repo. In GitHub Actions it comes from the `RESUME_TEXT` secret.

3. Configure the `matching:` block in `config.yaml`:

   ```yaml
   matching:
     enabled: true
     provider: huggingface
     model: sentence-transformers/all-MiniLM-L6-v2
     threshold: 0.30       # keep postings scoring >= this (cosine similarity 0-1)
     max_to_score: 20      # cap postings scored per run
   ```

**Tuning the threshold:** run `python main.py --dry-run`. When matching is
configured, dry-run prints each posting's relevance score (sorted, with a ✓ on
the ones that would notify) so you can pick a `threshold`. `all-MiniLM`
résumé-vs-JD scores typically land in the **0.2–0.5** range; start at `0.30` and
adjust.

**Graceful fallback:** if `HF_API_TOKEN` or the résumé is missing, or
`enabled: false`, matching turns off and the monitor notifies about **all** new
postings (keyword-filtered only). Postings scored below threshold are recorded
as seen, so they're never re-scored — only genuinely-new postings cost an API
call. Notifications show the score, e.g. `(relevance 0.42)`.

**Want a natural-language "why it matches" instead of a score?** The matcher
sits behind a small `Matcher` interface in `matcher.py` — add an LLM-backed
implementation and register it in `build_matcher()`; nothing else changes.

---

## Configuring companies (`config.yaml`)

```yaml
companies:
  - name: Stripe
    platform: greenhouse   # greenhouse | lever | ashby
    token: stripe
  - name: Palantir
    platform: lever
    token: palantir

filters:
  title_include: ["intern", "internship", "co-op", "new grad"]
  title_exclude: ["senior", "staff", "principal", "manager", "director"]
  location_include: []     # empty = match any location
  location_exclude: []     # e.g. ["india", "remote - emea"]
```

The six companies shipped in `config.yaml` were verified live against each ATS,
so `--dry-run` produces real results immediately.

### How filter matching works

Filter terms match on **word boundaries**, not raw substrings. This is
deliberate: with raw substrings, `intern` also matches "**Intern**al Audit" and
"**Intern**ational", flooding every run with false positives. Word-boundary
matching keeps `intern` → *Intern*, `internship` → *Internship*, `co-op`,
`new grad`, while ignoring *internal* / *international*.

- A posting is kept only if its title passes the include/exclude **and** its
  location passes the include/exclude.
- Empty `*_include` = match anything for that field. Empty `*_exclude` = exclude
  nothing.
- Exclude always wins over include.

To revert to plain substring matching, see the note at the top of `filters.py`.

### How to find a company's ATS token

The token is the company slug in the ATS URL. Confirm it returns JSON before
adding it (no auth required):

| Platform | Public board URL looks like | API to verify |
| --- | --- | --- |
| **Greenhouse** | `boards.greenhouse.io/<token>` or `job-boards.greenhouse.io/<token>` | `https://boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true` |
| **Lever** | `jobs.lever.co/<token>` | `https://api.lever.co/v0/postings/<token>?mode=json` |
| **Ashby** | `jobs.ashbyhq.com/<token>` | `https://api.ashbyhq.com/posting-api/job-board/<token>` |

```bash
# Example: does "figma" exist on Greenhouse?
curl -s "https://boards-api.greenhouse.io/v1/boards/figma/jobs?content=true" | head -c 200
```

A `200` with a JSON body means the token is good. A `404` means wrong token or
wrong platform. Then add a block to `companies:` and run
`python main.py --company <Name> --dry-run`.

---

## State (`seen.json`)

```json
{"ids": ["greenhouse:stripe:8031833", "..."], "last_run": "2026-08-27T12:00:00+00:00"}
```

- Committed to the repo so state survives across GitHub Actions runs.
- IDs are added **only after a successful notification**.
- Capped at **20,000 IDs**, dropping the oldest first.

If more than **25** new postings appear in a single run, the monitor sends a
short **summary count** instead of the full list — that many at once means a
first run or an ATS change, not real news.

---

## Running unattended in GitHub Actions

`.github/workflows/poll.yml` runs every 30 minutes (and on demand via **Run
workflow**), then commits the updated `seen.json` back to the repo with a
`[skip ci]` message. It handles the no-change case without failing.

Setup:

1. Push this repo to GitHub.
2. Add repository **secrets** (Settings → Secrets and variables → Actions):
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — required for notifications.
   - `HF_API_TOKEN`, `RESUME_TEXT` — optional, only for résumé matching.
     `RESUME_TEXT` holds your résumé text (since `resume.txt` is git-ignored and
     not in the repo).
3. The workflow already has `permissions: contents: write` so it can push
   `seen.json`. If pushes are rejected, enable **Settings → Actions → General →
   Workflow permissions → Read and write permissions**.
4. (Recommended) run `python main.py --init` once locally and commit the seeded
   `seen.json` so the first cloud run doesn't notify about every existing posting.

> ⚠️ **GitHub disables scheduled workflows after 60 days of repository
> inactivity.** If notifications go quiet, push any commit or click **Run
> workflow** to re-enable the schedule.

---

## Error alerts

Failures are surfaced to Telegram (or stdout), so the monitor never breaks
silently:

- **A company failing to fetch** (rate-limit `429`, bad token, timeout, ...) —
  that source is skipped and retried next run, and you get a `⚠️` alert naming
  the company and reason. Alerts are **de-duplicated**: a persistent problem
  pings you *once*, and again with a `✅` when it clears — not every 30 minutes.
- **An unexpected crash** — a `🚨` alert with the error, and the GitHub Actions
  run is marked failed (exit 1).

The last error signature is stored in `seen.json` (`last_error`) to drive the
de-duplication.

---

## Extending

- **New notification channel** (email, Discord): add a class implementing the
  `Notifier` protocol in `notify.py` and wire it into `build_notifier()`.
  `main.py` doesn't change.
- **New ATS platform**: add a `fetch_<platform>()` in `sources.py` that returns
  `Posting` objects and register it in the `FETCHERS` dict.

## Tests

```bash
python -m unittest -v test_monitor
```

Covers the filtering, state, and matcher logic (the network layer is stubbed).
Exercise the live path with `python main.py --dry-run`.

## Files

| File | Purpose |
| --- | --- |
| `config.yaml` | Companies, filters, and résumé-matching config. |
| `sources.py` | The three ATS fetchers + normalization into `Posting`. |
| `filters.py` | Title/location matching. |
| `state.py` | Load/save/diff `seen.json`. |
| `matcher.py` | `Matcher` interface + Hugging Face résumé-relevance scoring. |
| `notify.py` | `Notifier` interface, Telegram sender, `.env` parsing, batching. |
| `main.py` | Orchestration + CLI. |
| `seen.json` | Seen-ID state (committed). |
| `resume.txt` | Your résumé text (git-ignored; `RESUME_TEXT` secret in CI). |
| `test_monitor.py` | Unit tests for filters, state, and matcher. |
| `.github/workflows/poll.yml` | Scheduled GitHub Actions runner. |
