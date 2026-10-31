import requests

class Qbit:
    def __init__(self, url, username, password):
        self.url = url.rstrip("/")
        self.session = requests.Session()
        r = self.session.post(self.url + "/api/v2/auth/login",
                              data={"username": username, "password": password}, timeout=15)
        r.raise_for_status()
        if r.text.strip() != "Ok.":
            raise RuntimeError("qBittorrent login failed")

    def completed(self):
        r = self.session.get(self.url + "/api/v2/torrents/info",
                             params={"filter": "completed"}, timeout=30)
        r.raise_for_status()
        return r.json()

    def files(self, torrent_hash):
        r = self.session.get(self.url + "/api/v2/torrents/files",
                             params={"hash": torrent_hash}, timeout=30)
        r.raise_for_status()
        return r.json()

    def remove_record_only(self, torrent_hash):
        r = self.session.post(self.url + "/api/v2/torrents/delete",
                              data={"hashes": torrent_hash, "deleteFiles": "false"}, timeout=30)
        r.raise_for_status()
