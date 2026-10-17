import asyncio
import logging
from pathlib import Path
from threading import Event, local

import pytest

from spotifysaver.api.services.download_service import DownloadService
from spotifysaver.api.routers.download import _content_type_for_url
from spotifysaver.downloader.soundcloud_downloader import SoundCloudDownloader
from spotifysaver.enums import AudioFormat, Bitrate
from spotifysaver.models import Track


SOUNDCLOUD_TRACK_URL = "https://soundcloud.com/example-artist/example-track"


def _make_downloader(tmp_path):
    downloader = object.__new__(SoundCloudDownloader)
    downloader.base_dir = tmp_path
    downloader.logger = logging.getLogger("test-soundcloud-download")
    downloader.rate_limit_provider = None
    downloader.image_downloader = None
    downloader.lrc_client = None
    return downloader


def test_soundcloud_track_url_detection_limits_support_to_track_pages():
    assert SoundCloudDownloader.is_track_url(SOUNDCLOUD_TRACK_URL)
    assert SoundCloudDownloader.is_track_url(
        f"{SOUNDCLOUD_TRACK_URL}/s-secret-token"
    )
    assert not SoundCloudDownloader.is_track_url(
        "https://soundcloud.com/example-artist"
    )
    assert not SoundCloudDownloader.is_track_url(
        "https://soundcloud.com/example-artist/sets/example-set"
    )
    assert not SoundCloudDownloader.is_track_url(
        "https://good.example/soundcloud.com/example-artist/example-track"
    )
    assert not SoundCloudDownloader.is_track_url(
        "ftp://soundcloud.com/example-artist/example-track"
    )
    assert not SoundCloudDownloader.is_track_url(
        "https://user@soundcloud.com/example-artist/example-track"
    )


def test_soundcloud_track_is_classified_as_audio_track_by_api():
    assert _content_type_for_url(SOUNDCLOUD_TRACK_URL) == "track"


def test_soundcloud_progress_hooks_report_download_and_postprocessing():
    downloader = object.__new__(SoundCloudDownloader)
    fractions = []
    downloader.progress_fraction_callback = lambda fraction, _info: fractions.append(
        fraction
    )
    options = {"postprocessors": [{"key": "FFmpegExtractAudio"}]}

    downloader._add_progress_split_hooks(options)
    options["progress_hooks"][0](
        {
            "status": "downloading",
            "downloaded_bytes": 50,
            "total_bytes": 100,
            "info_dict": {},
        }
    )
    options["postprocessor_hooks"][0](
        {"status": "finished", "info_dict": {"id": "track-id"}}
    )

    assert fractions == [0.25, 1.0]


def test_soundcloud_track_without_original_download_is_returned_for_fallback(
    tmp_path, monkeypatch
):
    class FakeYoutubeDL:
        def __init__(self, _options):
            self.params = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_info(self, _url, download):
            assert download is False
            return {
                "id": "123",
                "title": "Example Track",
                "artists": ["Example Artist"],
                "duration": 180,
                "formats": [{"format_id": "http_mp3_128"}],
            }

    downloader = _make_downloader(tmp_path)
    monkeypatch.setattr(
        SoundCloudDownloader,
        "_get_ydl_opts",
        lambda *_args: {},
    )
    monkeypatch.setattr(
        "spotifysaver.downloader.soundcloud_downloader.yt_dlp.YoutubeDL",
        FakeYoutubeDL,
    )

    available, track, output_path = downloader.download_track(
        SOUNDCLOUD_TRACK_URL
    )

    assert available is False
    assert track is not None
    assert track.name == "Example Track"
    assert track.artists == ["Example Artist"]
    assert output_path is None


def test_soundcloud_track_with_original_download_uses_original_format(
    tmp_path, monkeypatch
):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.params = {
                "outtmpl": {"default": options["outtmpl"]},
                "format": options["format"],
            }
            self.format_selector = options["format"]
            self.processed_format = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_info(self, _url, download):
            assert download is False
            return {
                "id": "123",
                "title": "Example Track",
                "artists": ["Example Artist"],
                "duration": 180,
                "thumbnail": "https://example.test/cover.jpg",
                "formats": [{"format_id": "download"}],
            }

        def process_ie_result(self, _info, download):
            assert download is True
            assert self.params["format"] == "download"
            assert self.format_selector == "download"
            output_template = self.params["outtmpl"]["default"]
            Path(output_template.replace("%(ext)s", "m4a")).touch()

        def build_format_selector(self, format_spec):
            return format_spec

    downloader = _make_downloader(tmp_path)
    monkeypatch.setattr(
        SoundCloudDownloader,
        "_get_ydl_opts",
        lambda _self, output_path, *_args: {
            "outtmpl": str(output_path),
            "format": "bestaudio/best",
        },
    )
    monkeypatch.setattr(
        SoundCloudDownloader,
        "_output_path",
        lambda _self, _track, _format: tmp_path / "Example Track.m4a",
    )
    monkeypatch.setattr(
        SoundCloudDownloader,
        "_save_cover",
        lambda *_args: pytest.fail("Cover download should respect download_cover=False"),
    )
    monkeypatch.setattr(
        "spotifysaver.downloader.soundcloud_downloader.MusicFileMetadata.add_metadata",
        lambda _self: True,
    )
    monkeypatch.setattr(
        "spotifysaver.downloader.soundcloud_downloader.ImageDownloader.get_image_from_url",
        lambda *_args: pytest.fail(
            "Cover embedding should respect download_cover=False"
        ),
    )
    monkeypatch.setattr(
        "spotifysaver.downloader.soundcloud_downloader.yt_dlp.YoutubeDL",
        FakeYoutubeDL,
    )

    available, _track, output_path = downloader.download_track(
        SOUNDCLOUD_TRACK_URL,
        output_format=AudioFormat.M4A,
        bitrate=Bitrate.B128,
        download_cover=False,
    )

    assert available is True
    assert output_path == tmp_path / "Example Track.m4a"
    assert output_path.is_file()


def test_soundcloud_unavailable_track_returns_matched_youtube_url(tmp_path):
    class FakeDownloader:
        def download_track(self, *_args):
            return False, track, None

    track = Track(
        number=1,
        total_tracks=1,
        name="Example Track",
        duration=180,
        uri=SOUNDCLOUD_TRACK_URL,
        artists=["Example Artist"],
        album_artist=["Example Artist"],
        release_date="2024-01-01",
        album_name="Example Track",
    )
    service = object.__new__(DownloadService)
    service.soundcloud_downloader = FakeDownloader()
    service.output_format = AudioFormat.M4A
    service.bit_rate = Bitrate.B128
    service.download_lyrics = False
    service.download_cover = True
    service.overwrite_existing = False
    service.generate_nfo = False
    service.output_dir = str(tmp_path)
    service.cancellation_event = None
    service._position = local()
    youtube_url = "https://music.youtube.com/watch?v=matched123"
    service._download_track_sync = lambda source_track, *_args: (
        tmp_path / "Example Track.m4a",
        source_track.with_youtube_url(youtube_url),
    )

    result = asyncio.run(
        service._download_soundcloud_track(
            SOUNDCLOUD_TRACK_URL,
            progress_callback=None,
            cancellation_event=Event(),
            title_callback=None,
            tracks_callback=None,
        )
    )

    assert result["completed_tracks"] == 1
    assert result["download_url"] == youtube_url


def test_direct_soundcloud_download_returns_source_url(tmp_path):
    track = Track(
        number=1,
        total_tracks=1,
        name="Example Track",
        duration=180,
        uri=SOUNDCLOUD_TRACK_URL,
        artists=["Example Artist"],
        album_artist=["Example Artist"],
        release_date="2024-01-01",
        album_name="Example Track",
    )

    class FakeDownloader:
        def download_track(self, *_args):
            return True, track, tmp_path / "Example Track.m4a"

    service = object.__new__(DownloadService)
    service.soundcloud_downloader = FakeDownloader()
    service.output_format = AudioFormat.M4A
    service.bit_rate = Bitrate.B128
    service.download_lyrics = False
    service.download_cover = True
    service.overwrite_existing = False
    service.generate_nfo = False
    service.output_dir = str(tmp_path)

    result = asyncio.run(
        service._download_soundcloud_track(
            SOUNDCLOUD_TRACK_URL,
            progress_callback=None,
            cancellation_event=None,
            title_callback=None,
            tracks_callback=None,
        )
    )

    assert result["download_url"] == SOUNDCLOUD_TRACK_URL


def test_spotify_track_download_returns_matched_youtube_url(tmp_path):
    track = Track(
        number=1,
        total_tracks=1,
        name="Example Track",
        duration=180,
        uri="spotify:track:example",
        artists=["Example Artist"],
        album_artist=["Example Artist"],
        release_date="2024-01-01",
        album_name="Example Track",
    )
    youtube_url = "https://music.youtube.com/watch?v=example123"
    service = object.__new__(DownloadService)
    service.cancellation_event = None
    service._position = local()
    service._download_track_sync = lambda *_args: (
        tmp_path / "Example Track.m4a",
        track.with_youtube_url(youtube_url),
    )

    result = asyncio.run(service._download_track("spotify:track:example", track=track))

    assert result["download_url"] == youtube_url


def test_direct_youtube_track_download_returns_source_url():
    youtube_url = "https://www.youtube.com/watch?v=example123"
    service = object.__new__(DownloadService)
    service.downloader = type(
        "FakeDownloader",
        (),
        {
            "download_youtube_track_cli": lambda self, *_args: {
                "completed_tracks": 1,
                "failed_tracks": 0,
                "failed_track_names": [],
                "total_tracks": 1,
                "output_directory": "Music",
            }
        },
    )()
    service.output_format = AudioFormat.M4A
    service.bit_rate = Bitrate.B128
    service.overwrite_existing = False
    service.cancellation_event = None
    service.generate_nfo = False

    result = asyncio.run(service._download_youtube_track(youtube_url))

    assert result["download_url"] == youtube_url


def test_soundcloud_extraction_errors_do_not_trigger_youtube_fallback():
    class FakeDownloader:
        def download_track(self, *_args):
            raise RuntimeError("SoundCloud extraction failed")

    service = object.__new__(DownloadService)
    service.soundcloud_downloader = FakeDownloader()
    service.output_format = AudioFormat.M4A
    service.bit_rate = Bitrate.B128
    service.download_lyrics = False
    service.download_cover = True
    service.overwrite_existing = False
    service.generate_nfo = False
    service.output_dir = "Music"

    async def fallback(*_args, **_kwargs):
        pytest.fail("YouTube fallback must only run when direct download is unavailable")

    service._download_track = fallback

    with pytest.raises(RuntimeError, match="SoundCloud extraction failed"):
        asyncio.run(
            service._download_soundcloud_track(
                SOUNDCLOUD_TRACK_URL,
                progress_callback=None,
                cancellation_event=None,
                title_callback=None,
                tracks_callback=None,
            )
        )
