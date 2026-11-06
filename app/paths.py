import hashlib
from pathlib import Path

from .compat import VIDEO_EXTS
from .qbit import Qbit


def checked(path, root):
    path = Path(path)
    if root not in path.parents or path.resolve() != path:
        raise ValueError(f"Unsafe path: {path}")
    return path


def sources(torrent, entries, root, archive):
    base = Path(torrent["save_path"])
    if base != root:
        checked(base, root)
    result = []
    for entry in entries:
        name = Path(entry["name"])
        if entry.get("priority") == 0 or name.name.startswith("._") or name.suffix.lower() not in VIDEO_EXTS:
            continue
        if name.is_absolute() or ".." in name.parts:
            raise ValueError(f"Unsafe torrent entry: {name}")
        path = checked(base / name, root)
        if archive in path.parents or root / ".media-agent" in path.parents:
            raise ValueError(f"Internal path in torrent: {path}")
        result.append(path)
    return sorted(result)


def final_path(source):
    return source if source.suffix.lower() == ".mp4" else source.with_suffix(".mp4")


def legacy_path(source):
    if source.suffix.lower() == ".mp4":
        return source.with_name(source.stem + ".media-agent.mp4")
    return final_path(source)


def work_dir(root, torrent_hash, source):
    Qbit._check_hash(torrent_hash)
    key = hashlib.sha256(str(source).encode()).hexdigest()
    return checked(root / ".media-agent" / torrent_hash / key, root)
