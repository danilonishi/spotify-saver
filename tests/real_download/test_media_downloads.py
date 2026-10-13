import asyncio
import json
import os
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spotifysaver.api.services.video_download_service import VideoDownloadService
from spotifysaver.downloader.youtube_downloader import YouTubeDownloader
from spotifysaver.downloader.youtube_downloader_for_cli import YouTubeDownloaderForCLI
from spotifysaver.enums import AudioFormat, Bitrate
from spotifysaver.models.track import Track

YOUTUBE_URL = "https://www.youtube.com/watch?v=KOvVKNuISpg"
AUDIO_CASES = (
    (AudioFormat.M4A, Bitrate.B96),
    (AudioFormat.MP3, Bitrate.B128),
    (AudioFormat.OPUS, Bitrate.B192),
    (AudioFormat.MP3, Bitrate.B256),
)
VIDEO_CASES = (
    ("webm", 360),
    ("mkv", 720),
    ("mp4", 1080),
)
IMAGE_SIGNATURES = (
    b"\xff\xd8\xff",
    b"\x89PNG\r\n\x1a\n",
)


@pytest.fixture(scope="session")
def real_download_root(tmp_path_factory):
    configured_root = os.environ.get("SPOTIFYSAVER_REAL_DOWNLOAD_DIR")
    base = (
        Path(configured_root).expanduser().resolve()
        if configured_root
        else tmp_path_factory.mktemp("spotifysaver-real-downloads")
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_root = base / f"run-{run_id}"
    run_root.mkdir(parents=True, exist_ok=False)
    return run_root


@pytest.fixture(scope="session")
def ffprobe():
    executable = shutil.which("ffprobe")
    if not executable:
        pytest.skip("ffprobe is required to inspect downloaded media")
    return executable


def _probe(ffprobe, media_path):
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name:format_tags=title:stream=codec_type,codec_name,width,height,bit_rate:stream_tags=title,artist",
            "-of",
            "json",
            str(media_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _assert_nonempty_image(image_path):
    assert image_path.is_file(), f"Expected cover image at {image_path}"
    image = image_path.read_bytes()
    assert len(image) > 12, f"Cover image is empty or truncated: {image_path}"
    assert any(image.startswith(signature) for signature in IMAGE_SIGNATURES) or (
        image.startswith(b"RIFF") and image[8:12] == b"WEBP"
    ), f"Cover file does not have a recognized image signature: {image_path}"


@pytest.mark.real_download
@pytest.mark.parametrize(("audio_format", "bitrate"), AUDIO_CASES)
def test_real_owned_youtube_audio_download_has_expected_codec_and_bitrate(
    real_download_root, ffprobe, audio_format, bitrate
):
    case_dir = real_download_root / f"audio-{audio_format.value}-{bitrate.value}"
    downloader = YouTubeDownloaderForCLI(base_dir=str(case_dir))
    result = downloader.download_youtube_track_cli(
        YOUTUBE_URL,
        output_format=audio_format,
        bitrate=bitrate,
        overwrite_existing=True,
    )

    assert result["completed_tracks"] == 1, result
    output_dir = Path(result["output_directory"])
    files = list(output_dir.glob(f"*.{audio_format.value}"))
    assert len(files) == 1, f"Expected one {audio_format.value} file in {output_dir}"
    audio_path = files[0]
    assert audio_path.stat().st_size > 1024

    probe = _probe(ffprobe, audio_path)
    audio_streams = [
        stream for stream in probe["streams"] if stream.get("codec_type") == "audio"
    ]
    assert len(audio_streams) == 1, probe
    expected_codec = {
        AudioFormat.M4A: "aac",
        AudioFormat.MP3: "mp3",
        AudioFormat.OPUS: "opus",
    }[audio_format]
    assert audio_streams[0]["codec_name"] == expected_codec, probe
    duration = float(probe["format"]["duration"])
    assert duration > 0
    tags = {
        **probe.get("format", {}).get("tags", {}),
        **audio_streams[0].get("tags", {}),
    }
    assert tags.get("title"), probe
    measured_bitrate = int(
        audio_streams[0].get("bit_rate")
        or audio_path.stat().st_size * 8 / duration
    )
    assert measured_bitrate > 0, probe
    if audio_format == AudioFormat.M4A:
        assert measured_bitrate <= bitrate.value * 1500, (
            f"Output bitrate exceeded requested {bitrate.value} kbps: {probe}"
        )
    else:
        assert bitrate.value * 700 <= measured_bitrate <= bitrate.value * 1300, (
            f"Unexpected encoded bitrate for requested {bitrate.value} kbps: {probe}"
        )
    _assert_nonempty_image(output_dir / "cover.jpg")


@pytest.mark.real_download
@pytest.mark.parametrize(("video_format", "resolution"), VIDEO_CASES)
def test_real_owned_youtube_video_download_has_expected_container_and_nfo(
    real_download_root, ffprobe, video_format, resolution
):
    case_dir = real_download_root / f"video-{video_format}-{resolution}p"
    service = VideoDownloadService(
        output_dir=str(case_dir),
        video_format=video_format,
        video_resolution=resolution,
    )
    result = asyncio.run(service.download_from_url(YOUTUBE_URL))

    assert result["completed_tracks"] == 1, result
    output_dir = Path(result["output_directory"])
    files = list(output_dir.glob(f"*.{video_format}"))
    assert len(files) == 1, f"Expected one {video_format} file in {output_dir}"
    video_path = files[0]
    assert video_path.stat().st_size > 1024

    probe = _probe(ffprobe, video_path)
    video_streams = [
        stream for stream in probe["streams"] if stream.get("codec_type") == "video"
    ]
    assert len(video_streams) == 1, probe
    assert 0 < video_streams[0]["height"] <= resolution, probe
    assert any(stream.get("codec_type") == "audio" for stream in probe["streams"]), probe
    assert float(probe["format"]["duration"]) > 0

    nfo_path = video_path.with_suffix(".nfo")
    nfo_root = ET.parse(nfo_path).getroot()
    assert nfo_root.tag == "movie"
    assert nfo_root.findtext("title")
    assert nfo_root.findtext("url") == YOUTUBE_URL
    assert nfo_root.findtext("plot")
    _assert_nonempty_image(output_dir / "cover.jpg")


@pytest.mark.real_download
def test_real_downloaded_audio_accepts_generated_lrc_sidecar(real_download_root):
    case_dir = real_download_root / "lyrics"
    downloader = YouTubeDownloaderForCLI(base_dir=str(case_dir))
    result = downloader.download_youtube_track_cli(
        YOUTUBE_URL,
        output_format=AudioFormat.M4A,
        bitrate=Bitrate.B128,
        overwrite_existing=True,
    )

    assert result["completed_tracks"] == 1, result
    output_dir = Path(result["output_directory"])
    audio_path = next(output_dir.glob("*.m4a"))
    track = Track(
        number=1,
        total_tracks=1,
        name=audio_path.stem,
        duration=1,
        uri=YOUTUBE_URL,
        artists=["Channel owner"],
        album_artist=["Channel owner"],
        release_date="2026",
        album_name="Owned channel test upload",
    )
    test_lyrics = "[00:00.00]SpotifySaver integration-test lyric fixture"

    class TestLyricsClient:
        def get_lyrics_with_fallback(self, _track):
            return test_lyrics

    lyric_writer = YouTubeDownloader(base_dir=str(case_dir))
    lyric_writer.lrc_client = TestLyricsClient()

    assert lyric_writer._save_lyrics(track, audio_path)
    lyric_path = audio_path.with_suffix(".lrc")
    assert lyric_path.is_file()
    assert lyric_path.read_text(encoding="utf-8") == test_lyrics
