import shutil
import subprocess

import pytest

from app.media import probe, validate
from app.worker import run


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
@pytest.mark.parametrize("action,codec", [("remux", "libx264"), ("transcode", "mpeg4")])
def test_actual_staged_output_decodes(tmp_path, action, codec):
    source = tmp_path / "Original.mkv"
    directory = tmp_path / ".media-agent"
    directory.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=96x64:rate=10",
                    "-f", "lavfi", "-i", "sine", "-t", "1", "-c:v", codec, "-pix_fmt", "yuv420p",
                    "-c:a", "pcm_s16le", str(source)], check=True)
    run(source, directory, action, None, 1)
    output = directory / "output.mp4"
    validate(probe(source), output)
    with output.open("r+b") as f:
        f.truncate(100)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        validate(probe(source), output)


def test_quicktime_chapter_track_is_accepted_only_with_chapters(monkeypatch):
    from app import media
    from types import SimpleNamespace
    video = dict(codec_type="video", codec_name="h264", pix_fmt="yuv420p", width=1920, height=1080, level=40)
    audio = dict(codec_type="audio", codec_name="aac", channels=2, profile="LC")
    chapter = dict(codec_type="data", codec_name="bin_data", codec_tag_string="text")
    info = dict(filename="out.mp4", streams=[video, audio, chapter], chapters=[dict(id=1)],
                format=dict(format_name="mov,mp4", duration="120"))
    monkeypatch.setattr(media, "probe", lambda _: info)
    monkeypatch.setattr(media.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0))
    media.validate(info, "out.mp4")
    info["chapters"] = []
    with pytest.raises(ValueError):
        media.validate(info, "out.mp4")
