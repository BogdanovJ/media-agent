"""Best-effort Telegram notifications for media-agent.

No file-processing code should depend on successful delivery. This module does
not claim exactly-once delivery; durable event deduplication will be implemented
at the journal/integration layer.
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
                if payload.get("ok") is True:
                    log.info("Telegram delivered event=%s", event)
                    return True
                # Telegram JSON errors may contain sensitive response data; do not log it.
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
