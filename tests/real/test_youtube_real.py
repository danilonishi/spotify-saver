from pathlib import Path

import pytest
import yt_dlp

from spotifysaver.api.services.video_download_service import VideoDownloadService
from spotifysaver.downloader.youtube_downloader import YouTubeDownloader

YOUTUBE_URL = "https://www.youtube.com/watch?v=KOvVKNuISpg"


def _selected_formats(options):
    options["quiet"] = True
    options["skip_download"] = True
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(YOUTUBE_URL, download=False)
        selected = list(ydl.build_format_selector(ydl.params["format"])(info))
    assert info.get("id") == "KOvVKNuISpg"
    assert selected, f"No format selected for {YOUTUBE_URL}"
    return selected


@pytest.mark.real
def test_real_youtube_url_resolves_playable_audio_format():
    options = YouTubeDownloader()._get_ydl_opts(Path("real-audio-test"))

    selected = _selected_formats(options)

    assert any(fmt.get("acodec") != "none" for fmt in selected), (
        f"No audio stream selected for {YOUTUBE_URL}"
    )


@pytest.mark.real
def test_real_youtube_url_resolves_playable_video_format():
    options = VideoDownloadService(video_format="mp4")._ydl_opts(
        "real-video-test.%(ext)s"
    )

    selected = _selected_formats(options)

    assert any(
        fmt.get("acodec") != "none" and fmt.get("vcodec") != "none"
        for fmt in selected
    ), f"No combined audio/video stream selected for {YOUTUBE_URL}"
