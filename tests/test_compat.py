import pytest

from app.compat import decision
from app.main import within


def video(
    codec="h264",
    pix_fmt="yuv420p",
    level=41,
    width=1920,
    height=1080,
):
    return {
        "codec_type": "video",
        "codec_name": codec,
        "pix_fmt": pix_fmt,
        "level": level,
        "width": width,
        "height": height,
    }


def audio(codec="aac", profile="LC", channels=2):
    return {
        "codec_type": "audio",
        "codec_name": codec,
        "profile": profile,
        "channels": channels,
    }


def media(filename="movie.mp4", streams=None):
    return {
        "filename": filename,
        "streams": (
            streams
            if streams is not None
            else [
                video(),
                audio(),
            ]
        ),
    }


# --------------------------------------------------
# Compatible formats
# --------------------------------------------------


def test_compatible_mp4_is_copied():
    action, reason = decision(media(), 100_000_000)

    assert action == "copy"
    assert "compatible" in reason.lower()


def test_compatible_mkv_requires_remux():
    action, _ = decision(
        media(filename="movie.mkv"),
        100_000_000,
    )

    assert action == "remux"


def test_aac_mono_requires_remux():
    action, _ = decision(
        media(
            streams=[
                video(),
                audio(channels=1),
            ]
        ),
        100_000_000,
    )

    assert action == "remux"


def test_eac3_requires_remux():
    action, _ = decision(
        media(
            streams=[
                video(),
                audio(codec="eac3", profile=None, channels=6),
            ]
        ),
        100_000_000,
    )

    assert action == "remux"


def test_multiple_audio_streams_require_remux():
    action, _ = decision(
        media(
            streams=[
                video(),
                audio(),
                audio(codec="ac3", profile=None, channels=6),
            ]
        ),
        100_000_000,
    )

    assert action == "remux"


def test_timed_id3_requires_remux():
    action, _ = decision(
        media(
            streams=[
                video(),
                audio(),
                {"codec_type": "data", "codec_name": "timed_id3"},
            ]
        ),
        100_000_000,
    )

    assert action == "remux"


# --------------------------------------------------
# Video transcoding
# --------------------------------------------------


@pytest.mark.parametrize(
    "stream",
    [
        video(codec="hevc"),
        video(codec="av1"),
        video(pix_fmt="yuv420p10le"),
        video(level=50),
        video(width=3840, height=2160),
    ],
)
def test_incompatible_video_requires_transcoding(stream):
    action, _ = decision(
        media(
            streams=[
                stream,
                audio(),
            ]
        ),
        100_000_000,
    )

    assert action == "transcode"


# --------------------------------------------------
# Safety and manual review
# --------------------------------------------------


def test_file_over_4gib_requires_review():
    action, reason = decision(
        media(),
        5_000_000_000,
    )

    assert action == "review"
    assert "size" in reason.lower()


def test_custom_size_threshold():
    action, _ = decision(
        media(),
        2_000_000_000,
        review_over_bytes=1_000_000_000,
    )

    assert action == "review"


def test_missing_audio_requires_review():
    action, _ = decision(
        media(streams=[video()]),
        100_000_000,
    )

    assert action == "review"


def test_multiple_video_streams_require_review():
    action, _ = decision(
        media(
            streams=[
                video(),
                video(),
                audio(),
            ]
        ),
        100_000_000,
    )

    assert action == "review"


def test_embedded_cover_is_ignored():
    cover = {
        **video(),
        "disposition": {"attached_pic": 1},
    }

    action, _ = decision(
        media(
            streams=[
                video(),
                audio(),
                cover,
            ]
        ),
        100_000_000,
    )

    assert action == "remux"


# --------------------------------------------------
# Filesystem safety
# --------------------------------------------------


def test_path_inside_download_root(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()

    assert within(root / "movie.mp4", root)


def test_path_outside_download_root(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()

    assert not within(
        root / "../outside.mp4",
        root,
    )


def test_download_root_itself_is_not_a_file(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()

    assert not within(root, root)


def test_similarly_named_directory_is_rejected(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()

    assert not within(
        tmp_path / "downloads-backup" / "movie.mp4",
        root,
    )
