"""Best-effort Telegram notifications for media-agent.

No file-processing code should depend on successful delivery. This module does
not claim exactly-once delivery. The journal outbox persists pending messages
and delivery attempts across restarts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger("media-agent.telegram")


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str
    chat_id: str
    timeout_seconds: float = 10.0
    attempts: int = 3

    @classmethod
    def from_environment(cls) -> TelegramSettings | None:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token and not chat_id:
            return None
        if not token or not chat_id:
            log.error("Telegram disabled: both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
            return None
        return cls(bot_token=token, chat_id=chat_id)


class TelegramNotifier:
    """Send short, plain-text Telegram messages; never expose credentials in logs."""

    def __init__(self, settings: TelegramSettings, *, opener=None, sleep=None):
        if not settings.bot_token or not settings.chat_id:
            raise ValueError("Telegram token and chat ID must be configured")
        if settings.timeout_seconds <= 0 or not 1 <= settings.attempts <= 5:
            raise ValueError("Invalid Telegram timeout or retry count")
        self.settings = settings
        self._open = opener or urlopen
        self._sleep = sleep or time.sleep

    def send(self, event: str, message: str) -> bool:
        """Return delivery success; failures are logged, never raised to the caller.

        Call this outside any SQLite transaction or filesystem-critical section.
        """
        body = f"Media Agent — {event}\n{message}"[:4096]
        data = urlencode({"chat_id": self.settings.chat_id, "text": body}).encode("utf-8")
        # The token is transmitted only over HTTPS and is not logged.
        request = Request(
            f"https://api.telegram.org/bot{self.settings.bot_token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        for attempt in range(1, self.settings.attempts + 1):
            try:
                with self._open(request, timeout=self.settings.timeout_seconds) as response:
                    payload = json.load(response)
                # Telegram should return a JSON object. A valid JSON list,
                # string or null is still an invalid API response.
                if not isinstance(payload, dict):
                    log.warning(
                        "Telegram malformed API response event=%s attempt=%d",
                        event,
                        attempt,
                    )
                    return False
                if payload.get("ok") is True:
                    log.info("Telegram delivered event=%s", event)
                    return True
                # Never log arbitrary remote response fields or the request URL.
                log.warning("Telegram API declined event=%s attempt=%d", event, attempt)
                return False
            except HTTPError as exc:
                # 400/401/403 are configuration errors; repeated attempts won't help.
                log.warning("Telegram HTTP status=%d event=%s attempt=%d", exc.code, event, attempt)
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.settings.attempts:
                    return False
            except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                log.warning("Telegram transport failure type=%s event=%s attempt=%d", type(exc).__name__, event, attempt)
                if attempt == self.settings.attempts:
                    return False
            self._sleep(min(2 ** (attempt - 1), 4))
        return False


class NotificationDispatcher:
    """Drain a bounded batch, keeping all network calls outside transactions.

    No notifier means credentials are disabled: leave pending rows untouched.
    A delivery failure never changes the media job's status.
    """

    def __init__(self, journal, notifier, *, clock=None):
        self.journal = journal
        self.notifier = notifier
        self.clock = clock or time.time

    def dispatch(self, *, limit=10):
        if self.notifier is None:
            return 0
        delivered_count = 0
        for _ in range(limit):
            try:
                # Cover the configured HTTP timeouts and internal retry sleeps.
                settings = self.notifier.settings
                lease_seconds = max(
                    300, settings.attempts * (settings.timeout_seconds + 4) + 60,
                )
                item = self.journal.claim_notification(
                    timestamp=self.clock(), lease_seconds=lease_seconds,
                )
                if item is None:
                    break
                try:
                    notice = f"Event #{item['event_id']} | delivery attempt {item['attempts']}\n"
                    if item['attempts'] > 1:
                        notice += ("RETRY — possible duplicate. An earlier delivery may have succeeded "
                                   "without confirmation; the same event ID is not a new media action.\n")
                    delivered = self.notifier.send(item["event"], notice + item["message"]) is True
                except Exception:
                    # An injected transport can raise unexpected exceptions.
                    # Do not log arbitrary exception text containing credentials.
                    log.warning("Telegram delivery raised event_id=%s", item["event_id"])
                    delivered = False
                delay = min(60 * 2 ** min(item["attempts"] - 1, 6), 3600)
                acknowledged = self.journal.finish_notification(
                    item["event_id"], item["claim_token"], delivered=delivered,
                    retry_at=self.clock() + delay,
                )
                if acknowledged and delivered:
                    delivered_count += 1
            except Exception:
                # Leave unacknowledged claims to expire, including when SQLite
                # is unavailable after Telegram accepted the message.
                log.warning("Notification outbox unavailable; delivery deferred")
                break
        return delivered_count
