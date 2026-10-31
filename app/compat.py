from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".mov", ".avi", ".m4v", ".ts"}


def decision(
    probe: dict,
    size: int,
    review_over_bytes: int = 4294967296,
) -> tuple[str, str]:
    """
    Determine the safest processing action.

    Actions:
        copy      - Copy an already compatible MP4.
        remux     - Copy H.264 video, convert audio to AAC stereo.
        transcode - Convert video to H.264 and audio to AAC stereo.
        review    - Do not process automatically.
    """

    streams = probe.get("streams", [])

    videos = [
        stream
        for stream in streams
        if stream.get("codec_type") == "video"
        and not stream.get("disposition", {}).get("attached_pic")
    ]

    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]

    if len(videos) != 1:
        return "review", "Expected exactly one video stream"

    if not audios:
        return "review", "No audio streams detected"

    video = videos[0]
    audio = audios[0]

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    level = int(video.get("level") or 999)

    video_compatible = (
        video.get("codec_name") == "h264"
        and video.get("pix_fmt") == "yuv420p"
        and 0 < width <= 1920
        and 0 < height <= 1080
        and level <= 41
    )

    audio_compatible = (
        audio.get("codec_name") == "aac"
        and audio.get("profile") == "LC"
        and int(audio.get("channels") or 0) == 2
    )

    filename = probe.get("filename", "")
    is_mp4 = Path(filename).suffix.lower() == ".mp4"

    # We have not yet confirmed that the Samsung can
    # handle files larger than 4 GiB over Plex DLNA.
    if size > review_over_bytes:
        return ("review", "File exceeds unverified Samsung size threshold")

    if not video_compatible:
        return ("transcode", "Video requires H.264 compatibility conversion")

    # Avoid rewriting an already compatible file.
    if is_mp4 and audio_compatible and len(audios) == 1 and len(streams) == 2:
        return "copy", "Already Samsung-compatible"

    return ("remux", "Preserve H.264 video and convert primary audio to AAC stereo")
