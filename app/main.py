import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
import yaml
from .compat import VIDEO_EXTS, decision
from .journal import Journal
from .qbit import Qbit
from .safe_move import move_no_replace

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("media-agent")


def probe(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    data = json.loads(result.stdout)
    data["filename"] = str(path)
    return data


def within(path, root):
    p, r = Path(path).resolve(), Path(root).resolve()
    return p != r and r in p.parents


def destination_for(src, action):
    """Return the final path without changing the source directory structure."""
    src = Path(src)
    if action == "copy":
        return src  # Already compatible: no duplicate is necessary.
    if action in ("remux", "transcode"):
        return src.with_suffix(".mp4")
    raise ValueError(f"Unsupported conversion action: {action}")


def convert(src, dest, action, threads):
    """
    Convert a video and publish it only after validation.

    The source is never modified.
    The completed output is published without replacing an existing file.
    """

    dest.parent.mkdir(parents=True, exist_ok=True)

    partial = dest.with_name(dest.stem + ".media-agent.partial" + dest.suffix)

    if src == dest:
        raise ValueError("Refusing to convert a source onto itself")
    if dest.exists() or dest.is_symlink():
        raise FileExistsError(f"Destination already exists: {dest}")
    if partial.exists() or partial.is_symlink():
        raise FileExistsError(f"Previous partial output requires inspection: {partial}")

    source_info = probe(src)

    try:
        if action == "copy":
            shutil.copy2(src, partial)

        elif action in ("remux", "transcode"):
            cmd = [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-n",
                "-i",
                str(src),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-c:v",
                ("copy" if action == "remux" else "libx264"),
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ac",
                "2",
                "-sn",
                "-dn",
                "-threads",
                str(threads),
                "-movflags",
                "+faststart",
                str(partial),
            ]

            subprocess.run(cmd, check=True)

        else:
            raise ValueError(f"Unsupported conversion action: {action}")

        # --------------------------------------
        # Validate output metadata
        # --------------------------------------

        output_info = probe(partial)

        videos = [
            s
            for s in output_info["streams"]
            if s["codec_type"] == "video"
            and not s.get("disposition", {}).get("attached_pic")
        ]

        audios = [s for s in output_info["streams"] if s["codec_type"] == "audio"]

        if len(videos) != 1 or len(audios) != 1:
            raise RuntimeError("Output must contain one video " "and one audio stream")

        if videos[0]["codec_name"] != "h264":
            raise RuntimeError("Output video is not H.264")

        if audios[0]["codec_name"] != "aac":
            raise RuntimeError("Output audio is not AAC")

        if int(audios[0].get("channels") or 0) != 2:
            raise RuntimeError("Output audio is not stereo")

        # --------------------------------------
        # Validate duration
        # --------------------------------------

        source_duration = float(source_info["format"]["duration"])

        output_duration = float(output_info["format"]["duration"])

        tolerance = max(2.0, source_duration * 0.01)

        if abs(source_duration - output_duration) > tolerance:
            raise RuntimeError(
                "Duration mismatch: "
                f"source={source_duration:.2f}s, "
                f"output={output_duration:.2f}s"
            )

        # --------------------------------------
        # Decode complete output
        # --------------------------------------

        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(partial),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            check=True,
        )

        # --------------------------------------
        # Publish verified output
        # --------------------------------------

        # Publish without overwriting a file that appeared during conversion.
        # move_no_replace either performs a no-clobber move or fails safely.
        move_no_replace(partial, dest)

        log.info(
            "Verified output: %s (%.2f seconds)",
            dest,
            output_duration,
        )

    except Exception:
        log.exception(
            "Processing or validation failed: %s",
            src,
        )

        # Retain the incomplete output for inspection.
        # It will never be published as a completed file.
        raise


def process_torrent(q, t, cfg, journal):
    h = t["hash"]
    root = Path(cfg["download_root"]).resolve()
    # Output is deliberately published alongside the original source.
    # Neither the quarantine directory nor any other special directory may be a source.
    quarantine_root = Path(cfg["quarantine_root"]).resolve()
    # qBittorrent file names are relative to the torrent's save_path.
    base = Path(t["save_path"]).resolve()
    if base != root and not within(base, root):
        raise ValueError(f"Torrent save path outside configured download root: {base}")
    entries = q.files(h)
    for entry in entries:
        if entry.get("priority") == 0:
            continue
        name = entry["name"]
        candidate = base / name
        if candidate.is_symlink():
            raise ValueError(f"Refusing symlink source: {candidate}")
        src = candidate.resolve()
        if src.suffix.lower() not in VIDEO_EXTS:
            continue
        if (
            not within(src, root)
            or src == quarantine_root
            or within(src, quarantine_root)
        ):
            raise ValueError(f"Unsafe source path: {src}")
        if not src.is_file():
            journal.put(h, str(src), "missing", reason="Source not found")
            continue
        if journal.status(h, str(src)) in ("done", "review"):
            continue
        metadata = probe(src)
        action, reason = decision(
            metadata, src.stat().st_size, cfg["review_over_bytes"]
        )
        log.info("hash=%s file=%s decision=%s reason=%s", h, src, action, reason)
        if action == "review":
            journal.put(h, str(src), "review", action, reason)
            continue
        if not cfg["process_enabled"]:
            journal.put(h, str(src), "planned", action, reason)
            continue
        dest = destination_for(src, action)
        if dest == src:
            # No conversion is necessary; the original filename remains untouched.
            journal.put(h, str(src), "done", action, reason, str(src))
            continue
        if dest.exists() or dest.is_symlink():
            journal.put(
                h, str(src), "review", action, "Output already exists; manual review"
            )
            continue
        if (
            shutil.disk_usage(src.parent).free
            < cfg["min_free_bytes"]
        ):
            journal.put(h, str(src), "blocked", action, "Insufficient free space")
            continue
        journal.put(h, str(src), "running", action, reason)
        try:
            convert(src, dest, action, cfg["ffmpeg_threads"])
            journal.put(h, str(src), "done", action, reason, str(dest))
        except Exception as e:
            log.exception("Conversion failed: %s", src)
            journal.put(h, str(src), "failed", action, str(e))
    # Original files are intentionally kept untouched after publishing.
    # Removal/quarantine requires a separate, journalled transaction; do not
    # enable it until an entire multi-file torrent has been validated.


def main():
    with open(
        os.getenv("CONFIG_FILE", "/opt/media-agent/config/samsung-dlna.yaml"),
        encoding="utf-8",
    ) as f:
        cfg = yaml.safe_load(f)
    Path(cfg["state_db"]).parent.mkdir(parents=True, exist_ok=True)
    journal = Journal(cfg["state_db"])
    log.info(
        "media-agent v0.1 started process_enabled=%s finalize_enabled=%s",
        cfg["process_enabled"],
        cfg["finalize_enabled"],
    )
    while True:
        try:
            q = Qbit(
                os.environ["QBIT_URL"],
                os.environ["QBIT_USERNAME"],
                os.environ["QBIT_PASSWORD"],
            )
            for torrent in q.completed():
                try:
                    process_torrent(q, torrent, cfg, journal)
                except Exception:
                    log.exception("Torrent processing error: %s", torrent.get("hash"))
        except Exception:
            log.exception("Polling error")
        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    main()
