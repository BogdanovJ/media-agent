"""Minimal qBittorrent Web API client for media-agent.

Listing finished torrents is read-only. Deleting a torrent record requires an
explicit confirmation at the call site and never requests file deletion.
"""

from pathlib import Path
import re


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
        self.credentials = {"username": username, "password": password}
        self.login()

    def login(self):
        self.session.cookies.clear()
        response = self.session.post(
            self.url + "/api/v2/auth/login",
            data=self.credentials, allow_redirects=False,
            timeout=15,
        )
        response.raise_for_status()

        if not (
            (response.status_code == 200 and response.text.strip() == "Ok.")
            or response.status_code == 204
        ):
            raise RuntimeError(f"qBittorrent login failed: HTTP {response.status_code}")

    def request(self, method, url, **kwargs):
        for attempt in range(2):
            response = self.session.request(method, url, allow_redirects=False, **kwargs)
            if response.status_code == 403 and attempt == 0:
                self.login()
                if method != "GET":
                    response.raise_for_status()  # Reconcile before any new removal attempt.
                continue
            response.raise_for_status()
            if response.status_code != 200:
                raise RuntimeError("Unexpected qBittorrent response")
            return response

    def check_shared_paths(self, torrent_hash, paths):
        # A different torrent, including one still downloading, may share media.
        rows = self.request("GET", self.url + "/api/v2/torrents/info", timeout=30).json()
        if not isinstance(rows, list):
            raise ValueError("Invalid torrent inventory")
        for row in rows:
            if row["hash"].lower() == torrent_hash.lower():
                continue
            base = Path(row["save_path"])
            if not base.is_absolute():
                raise ValueError("Invalid torrent save path")
            if not any(path.is_relative_to(base) for path in paths):
                continue
            if any((base / entry["name"]).resolve() in paths for entry in self.files(row["hash"])):
                raise ValueError("Another torrent references a file in this retirement")

    def lookup(self, torrent_hash):
        self._check_hash(torrent_hash)
        rows = self.request("GET", self.url + "/api/v2/torrents/info",
                            params={"hashes": torrent_hash}, timeout=30).json()
        if rows == []:
            return None
        if not isinstance(rows, list) or len(rows) != 1 or rows[0]["hash"].lower() != torrent_hash.lower():
            raise RuntimeError("Unexpected qBittorrent hash lookup")
        return rows[0]

    def completed(self):
        """Return completed, stable torrents for inspection.

        This is NOT a permission to convert or retire a torrent. In particular,
        a periodically updated torrent cannot be identified reliably from its
        completion state alone; that policy belongs in the processing layer.
        """
        response = self.request("GET",
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
        response = self.request("GET",
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
        response = self.request("POST",
            self.url + "/api/v2/torrents/delete",
            data={"hashes": torrent_hash, "deleteFiles": "false"},
            timeout=30,
        )
        response.raise_for_status()
        if response.text.strip() not in ("", "Ok."):
            raise RuntimeError("qBittorrent rejected torrent record removal")
