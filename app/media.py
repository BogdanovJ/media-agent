import json
import logging
import math
import shutil
import subprocess
import uuid

from .compat import decision
from .safe_move import move_no_replace

log = logging.getLogger(__name__)

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


def convert(src, dest, action, threads):
    """
    Convert a video and publish it only after validation.

    The source is never modified.
    The completed output is published without replacing an existing file.
    """

    dest.parent.mkdir(parents=True, exist_ok=True)

    partial = dest.with_name(f"{uuid.uuid4().hex}.partial.mp4")

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
            video_options = []
            if action == "transcode":
                scale = r"scale=w=min(1920\,iw):h=min(1080\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2"
                video_options = ["-vf", scale, "-pix_fmt", "yuv420p", "-level:v", "4.1"]
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
                *video_options,
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

        validate(source_info, partial)
        move_no_replace(partial, dest)

    except Exception:
        log.exception(
            "Processing or validation failed: %s",
            src,
        )

        partial.unlink(missing_ok=True)
        raise



def validate(source_info, partial):
    output_info = probe(partial)
    if "mp4" not in output_info["format"]["format_name"].split(",") or decision(output_info, 0)[0] != "copy":
        raise ValueError("Output does not meet the Samsung MP4 profile")
    source_duration = float(source_info["format"]["duration"])

    output_duration = float(output_info["format"]["duration"])

    if not all(math.isfinite(d) and d > 0 for d in (source_duration, output_duration)):
        raise ValueError("Invalid duration")

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
