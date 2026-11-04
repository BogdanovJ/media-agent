"""Minimal qBittorrent Web API client for media-agent.

Listing finished torrents is read-only. Deleting a torrent record requires an
explicit confirmation at the call site and never requests file deletion.
"""

import re
from urllib import response

import requests

# Only states representing completed, stable payloads. A torrent can be
# reported under the API's 'completed' filter while checking or moving files.
READY_STATES = frozenset(
    {
        "uploading",
        "stalledUP",
        "queuedUP",
        "forcedUP",
        "pausedUP",  # Older qBittorrent versions
        "stoppedUP",  # Newer qBittorrent versions
    }
)

HASH_PATTERN = re.compile(r"^[a-fA-F0-9]{40}$")


class Qbit:
    def __init__(self, url, username, password):
        self.url = url.rstrip("/")
        self.session = requests.Session()
        response = self.session.post(
            self.url + "/api/v2/auth/login",
            data={"username": username, "password": password},
            timeout=15,
        )
        response.raise_for_status()

        if not (
            (response.status_code == 200 and response.text.strip() == "Ok.")
            or response.status_code == 204
        ):
            raise RuntimeError(f"qBittorrent login failed: HTTP {response.status_code}")

    def completed(self):
        """Return completed, stable torrents for inspection.

        This is NOT a permission to convert or retire a torrent. In particular,
        a periodically updated torrent cannot be identified reliably from its
        completion state alone; that policy belongs in the processing layer.
        """
        response = self.session.get(
            self.url + "/api/v2/torrents/info",
            params={"filter": "completed"},
            timeout=30,
        )
        response.raise_for_status()
        torrents = response.json()
        if not isinstance(torrents, list):
            raise RuntimeError("Unexpected qBittorrent torrent-list response")
        return [
            torrent
            for torrent in torrents
            if isinstance(torrent, dict)
            and torrent.get("state") in READY_STATES
            and torrent.get("progress") == 1
            and torrent.get("amount_left") == 0
        ]

    def files(self, torrent_hash):
        self._check_hash(torrent_hash)
        response = self.session.get(
            self.url + "/api/v2/torrents/files",
            params={"hash": torrent_hash},
            timeout=30,
        )
        response.raise_for_status()
        files = response.json()
        if not isinstance(files, list):
            raise RuntimeError("Unexpected qBittorrent files response")
        return files

    @staticmethod
    def _check_hash(torrent_hash):
        if not isinstance(torrent_hash, str) or not HASH_PATTERN.fullmatch(
            torrent_hash
        ):
            raise ValueError("Invalid torrent hash")

    def remove_record_only(self, torrent_hash, *, confirmed=False):
        """Remove a torrent record, NEVER its data, after external validation.

        This function must not be called during observation-only deployment.
        An operator/transaction coordinator must explicitly confirm completion
        of all required processing and safe recovery before setting confirmed.
        """
        self._check_hash(torrent_hash)
        if not confirmed:
            raise PermissionError("Torrent removal requires explicit confirmation")
        response = self.session.post(
            self.url + "/api/v2/torrents/delete",
            data={"hashes": torrent_hash, "deleteFiles": "false"},
            timeout=30,
        )
        response.raise_for_status()
        if response.text.strip() not in ("", "Ok."):
            raise RuntimeError("qBittorrent rejected torrent record removal")
