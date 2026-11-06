"""Offline tests for the best-effort Telegram notifier."""

import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

import pytest

from app.notifier import TelegramNotifier, TelegramSettings


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return io.BytesIO(json.dumps(self.payload).encode())

    def __exit__(self, *_args):
        return False


def settings(attempts=3):
    return TelegramSettings("test-token", "123456", attempts=attempts)


def test_send_success():
    requests = []

    def opener(req, timeout):
        requests.append((req, timeout))
        return FakeResponse({"ok": True})

    notifier = TelegramNotifier(settings(), opener=opener, sleep=lambda _s: None)
    assert notifier.send("Completed", "Sample.mp4") is True
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url.startswith("https://api.telegram.org/")
    assert request.get_method() == "POST"
    assert timeout == 10.0
    fields = parse_qs(request.data.decode())
    assert fields["chat_id"] == ["123456"]
    assert fields["text"] == ["Media Agent — Completed\nSample.mp4"]


def test_transient_connection_failure_retries():
    calls = []
    sleeps = []

    def opener(_req, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise URLError("offline")
        return FakeResponse({"ok": True})

    notifier = TelegramNotifier(settings(), opener=opener, sleep=sleeps.append)
    assert notifier.send("Processed", "A.mp4") is True
    assert len(calls) == 2
    assert sleeps == [1]


def test_http_403_does_not_retry():
    calls = []

    def opener(req, timeout):
        calls.append(timeout)
        raise HTTPError(req.full_url, 403, "Forbidden", {}, None)

    notifier = TelegramNotifier(settings(), opener=opener, sleep=lambda _s: None)
    assert notifier.send("Failed", "A.mp4") is False
    assert len(calls) == 1


def test_http_429_retries_until_success():
    calls = []

    def opener(req, timeout):
        calls.append(timeout)
        if len(calls) < 3:
            raise HTTPError(req.full_url, 429, "Rate limited", {}, None)
        return FakeResponse({"ok": True})

    notifier = TelegramNotifier(settings(), opener=opener, sleep=lambda _s: None)
    assert notifier.send("Completed", "A.mp4") is True
    assert len(calls) == 3


def test_api_rejection_does_not_retry():
    calls = []

    def opener(_req, timeout):
        calls.append(timeout)
        return FakeResponse({"ok": False, "description": "bad chat"})

    notifier = TelegramNotifier(settings(), opener=opener, sleep=lambda _s: None)
    assert notifier.send("Completed", "A.mp4") is False
    assert len(calls) == 1


def test_environment_credentials_required(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert TelegramSettings.from_environment() is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    assert TelegramSettings.from_environment() is None
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    assert TelegramSettings.from_environment() == TelegramSettings("token", "123")


def test_invalid_configuration_rejected():
    with pytest.raises(ValueError):
        TelegramNotifier(TelegramSettings("", "chat"))
    with pytest.raises(ValueError):
        TelegramNotifier(TelegramSettings("token", "chat", attempts=0))
    with pytest.raises(ValueError):
        TelegramNotifier(TelegramSettings("token", "chat", timeout_seconds=0))


@pytest.mark.parametrize("payload", [None, [], ["ok"], "hello", 42, True])
def test_malformed_json_shapes_fail_closed(payload):
    calls = []
    def opener(req, timeout):
        calls.append(1)
        return FakeResponse(payload)
    notifier = TelegramNotifier(settings(), opener=opener, sleep=lambda _s: None)
    assert notifier.send("Completed", "A.mp4") is False
    assert len(calls) == 1


def test_compact_notice_escapes_filename_and_uses_short_monospace_hash():
    from app.notifier import format_notice
    msg = format_notice(dict(event_id=44, event="review", attempts=2,
                             message="Torrent: 123456789abcd\nSource: /downloads/A&B <film>.mkv\nAudio missing"))
    assert "<code>9abcd</code>" in msg and "12345678" not in msg
    assert "Media Agent #44 · Needs review" in msg and "Attempt 2 · possible duplicate" in msg
    assert "A&amp;B &lt;film&gt;.mkv" in msg and "/downloads" not in msg
