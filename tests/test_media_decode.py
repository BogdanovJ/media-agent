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
