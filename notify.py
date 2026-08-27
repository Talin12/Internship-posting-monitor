"""Notification delivery.

Everything sits behind the small `Notifier` interface so a new channel (email,
Discord, ...) is a new subclass plus one line in `build_notifier` — main.py never
changes.

Currently implemented:
  - TelegramNotifier : Telegram Bot API sendMessage
  - StdoutNotifier   : prints to stdout (used as a fallback when Telegram env
                       vars are missing, so the tool works before a bot is set up)

Message formatting lives here too: a run produces ONE batched message listing all
new postings (split across multiple sends at Telegram's 4096-char limit), or a
short summary line when there are too many to be real news.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Protocol

import requests

log = logging.getLogger("jobmon.notify")

TELEGRAM_LIMIT = 4096  # hard API limit per message
SUMMARY_THRESHOLD = 25  # more than this in one run -> summary instead of full list
REQUEST_TIMEOUT = 15


# --------------------------------------------------------------------------- #
# Message construction
# --------------------------------------------------------------------------- #
def _format_posting(p) -> str:
    loc = f" — {p.location}" if p.location else ""
    return f"• {p.company}: {p.title}{loc}\n  {p.url}"


def build_messages(postings: list) -> list[str]:
    """Turn new postings into one or more message strings.

    - 0 postings          -> [] (caller should not send)
    - > SUMMARY_THRESHOLD -> a single summary line (first run / ATS change, not news)
    - otherwise           -> a header + one bullet per posting, split at TELEGRAM_LIMIT
    """
    n = len(postings)
    if n == 0:
        return []
    if n > SUMMARY_THRESHOLD:
        companies = sorted({p.company for p in postings})
        return [
            f"🗂️ {n} new postings across {len(companies)} companies "
            f"({', '.join(companies)}).\n"
            f"That's more than {SUMMARY_THRESHOLD} — likely a first run or an ATS "
            f"change, so the full list is suppressed. Next run will notify normally."
        ]

    header = f"🎯 {n} new internship posting{'s' if n != 1 else ''}:"
    blocks = [header, *[_format_posting(p) for p in postings]]
    return _pack(blocks, TELEGRAM_LIMIT)


def _pack(blocks: list[str], limit: int) -> list[str]:
    """Greedily pack text blocks into messages under `limit` chars.

    A single block longer than `limit` is hard-truncated so a send can't fail.
    """
    messages: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > limit:
            block = block[: limit - 1] + "…"
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                messages.append(current)
            current = block
    if current:
        messages.append(current)
    return messages


# --------------------------------------------------------------------------- #
# Notifier interface + implementations
# --------------------------------------------------------------------------- #
class Notifier(Protocol):
    name: str

    def send(self, messages: Iterable[str]) -> bool:
        """Send already-formatted messages. Return True if ALL sends succeeded."""
        ...


class StdoutNotifier:
    name = "stdout"

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason

    def send(self, messages: Iterable[str]) -> bool:
        if self.reason:
            log.warning("Telegram not configured (%s); printing instead.", self.reason)
        for msg in messages:
            print("\n--- notification ---")
            print(msg)
        return True


class TelegramNotifier:
    name = "telegram"

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    def send(self, messages: Iterable[str]) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        all_ok = True
        for msg in messages:
            payload = {
                "chat_id": self.chat_id,
                "text": msg,
                "disable_web_page_preview": True,
            }
            try:
                resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                log.error("Telegram send failed: %s", exc)
                all_ok = False
                break  # stop so we don't mark postings seen after a partial failure
        return all_ok


# --------------------------------------------------------------------------- #
# .env loading + notifier selection
# --------------------------------------------------------------------------- #
def load_dotenv(path: str = ".env") -> None:
    """Minimal .env parser: KEY=VALUE per line. No external dependency.

    Existing environment variables win over .env (CI secrets take precedence).
    Ignores blank lines, comments, and surrounding quotes/whitespace.
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)


def build_notifier() -> Notifier:
    """Pick a notifier from the environment, falling back to stdout."""
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return TelegramNotifier(token, chat_id)
    missing = ", ".join(
        k
        for k, v in (
            ("TELEGRAM_BOT_TOKEN", token),
            ("TELEGRAM_CHAT_ID", chat_id),
        )
        if not v
    )
    return StdoutNotifier(reason=f"missing {missing}")
