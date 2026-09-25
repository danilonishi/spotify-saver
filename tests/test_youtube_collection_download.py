import logging
import asyncio
import importlib
from threading import Event
from pathlib import Path

from click.testing import CliRunner
from fastapi import BackgroundTasks

from spotifysaver.api.routers.download import (
    cancel_download,
    cancellation_events,
    start_download,
    tasks,
)
from spotifysaver.api.schemas import DownloadRequest, DownloadStatus
from spotifysaver.api.services.download_service import DownloadService
from spotifysaver.downloader.youtube_downloader import YouTubeDownloader
from spotifysaver.downloader.youtube_downloader_for_cli import YouTubeDownloaderForCLI
import spotifysaver.downloader.youtube_downloader_for_cli as cli_downloader_module

cli_download_module = importlib.import_module(
    "spotifysaver.cli.commands.download.download"
)
download_command = cli_download_module.download


ALBUM_URL = (
    "https://music.youtube.com/playlist?"
    "list=OLAK5uy_nAKi7j0JupK6KpHDrw4zO_zGIOMCs_RdQ"
)


def test_youtube_collection_url_detection():
    assert YouTubeDownloader.is_youtube_collection_url(ALBUM_URL)
    assert YouTubeDownloader.is_youtube_collection_url(
        "https://www.youtube.com/watch?v=video-id&list=playlist-id"
    )
    assert not YouTubeDownloader.is_youtube_collection_url(
        "https://www.youtube.com/watch?v=video-id"
    )
    assert not YouTubeDownloader.is_youtube_collection_url(
        "https://example.com/playlist?list=playlist-id"
    )


def test_youtube_collection_download_embeds_metadata_and_reports_failures(
    monkeypatch, tmp_path
):
    class FakeYoutubeDL:
        options = None

        def __init__(self, options):
            self.params = options
            self.params["outtmpl"] = {"default": self.params["outtmpl"]}
            FakeYoutubeDL.options = options
            self.progress_hook = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download):
            assert url == ALBUM_URL
            assert download is False
            return {
                "title": "Album: Sample",
                "playlist_count": 2,
                "entries": [
                    {"id": "success", "title": "First Track"},
                    {"id": "failure", "title": "Second Track"},
                ],
            }

        def prepare_filename(self, entry):
            filename = self.params["outtmpl"]["default"]
            filename = filename.replace(
                "%(playlist_index)02d", f"{entry['playlist_index']:02d}"
            )
            filename = filename.replace("%(title)s", entry["title"])
            return filename.replace("%(ext)s", "webm")

        def add_progress_hook(self, hook):
            self.progress_hook = hook

        def download(self, urls):
            assert urls == [ALBUM_URL]
            assert self.params["playlist_items"] == "2"
            self.progress_hook(
                {
                    "status": "downloading",
                    "info_dict": {"playlist_index": 2, "title": "Second Track"},
                }
            )

    monkeypatch.setattr(cli_downloader_module.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(
        YouTubeDownloader,
        "_get_ydl_opts",
        lambda self, output_path, output_format, bitrate: {"postprocessors": []},
    )
    downloader = object.__new__(YouTubeDownloaderForCLI)
    downloader.base_dir = tmp_path
    downloader.logger = logging.getLogger(__name__)
    existing_dir = tmp_path / "YouTube" / "Album_ Sample"
    existing_dir.mkdir(parents=True)
    (existing_dir / "01 - First Track.m4a").touch()
    progress = []
    track_results = []

    result = downloader.download_youtube_playlist_cli(
        ALBUM_URL,
        progress_callback=lambda *args: progress.append(args),
        track_result_callback=lambda *args: track_results.append(args),
    )

    assert result["completed_tracks"] == 1
    assert result["failed_tracks"] == 1
    assert result["failed_track_names"] == ["Second Track"]
    assert result["total_tracks"] == 2
    assert progress == [(1, 2, "First Track"), (2, 2, "Second Track")]
    assert track_results == [
        (1, "First Track", "completed"),
        (2, "Second Track", "error"),
    ]
    assert any(
        processor["key"] == "FFmpegMetadata"
        for processor in FakeYoutubeDL.options["postprocessors"]
    )
    assert any(
        processor["key"] == "EmbedThumbnail"
        for processor in FakeYoutubeDL.options["postprocessors"]
    )
    assert "Album_ Sample" in FakeYoutubeDL.options["outtmpl"]["default"]
    assert FakeYoutubeDL.options["playlist_items"] == "2"


def test_api_routes_youtube_collection_without_spotify_client():
    cancellation_event = Event()

    class FakeDownloader:
        def download_youtube_playlist_cli(self, *args):
            assert args[0] == ALBUM_URL
            assert args[7] is False
            assert args[8] is cancellation_event
            return {
                "collection_name": "Sample Album",
                "completed_tracks": 1,
                "failed_tracks": 0,
                "failed_track_names": [],
                "total_tracks": 1,
                "output_directory": "Music/YouTube/Sample Album",
                "dry_run": False,
            }

    service = object.__new__(DownloadService)
    service.downloader = FakeDownloader()
    service.output_format = "m4a"
    service.bit_rate = 128
    service.download_cover = True
    service.overwrite_existing = False

    result = asyncio.run(
        service.download_from_url(ALBUM_URL, cancellation_event=cancellation_event)
    )

    assert result["content_type"] == "playlist"
    assert result["completed_tracks"] == 1
    assert result["total_tracks"] == 1


def test_api_accepts_youtube_collection_url():
    request = DownloadRequest(spotify_url=ALBUM_URL)

    response = asyncio.run(start_download(request, BackgroundTasks()))

    try:
        assert response.content_type == "playlist"
        assert response.spotify_url == ALBUM_URL
    finally:
        tasks.pop(response.task_id, None)
        cancellation_events.pop(response.task_id, None)


def test_cancel_download_signals_worker_and_reports_cancelling():
    task_id = "cancel-test-task"
    cancellation_event = Event()
    tasks[task_id] = DownloadStatus(
        task_id=task_id,
        status="processing",
        progress=25,
    )
    cancellation_events[task_id] = cancellation_event

    try:
        response = asyncio.run(cancel_download(task_id))

        assert cancellation_event.is_set()
        assert tasks[task_id].status == "cancelling"
        assert response["message"] == "Download cancellation requested"
    finally:
        tasks.pop(task_id, None)
        cancellation_events.pop(task_id, None)


def test_cli_routes_youtube_collection_without_spotify_client(monkeypatch, tmp_path):
    def fake_init(self, base_dir):
        self.base_dir = Path(base_dir)

    def fake_download(self, **kwargs):
        assert kwargs["url"] == ALBUM_URL
        return {
            "collection_name": "Sample Album",
            "completed_tracks": 2,
            "failed_tracks": 0,
            "failed_track_names": [],
            "total_tracks": 2,
            "output_directory": str(tmp_path / "YouTube" / "Sample Album"),
            "dry_run": False,
        }

    monkeypatch.setattr(YouTubeDownloaderForCLI, "__init__", fake_init)
    monkeypatch.setattr(
        YouTubeDownloaderForCLI,
        "download_youtube_playlist_cli",
        fake_download,
    )
    monkeypatch.setattr(
        cli_download_module,
        "SpotifyAPI",
        lambda: (_ for _ in ()).throw(AssertionError("Spotify should not initialize")),
    )

    result = CliRunner().invoke(
        download_command,
        [ALBUM_URL, "--output", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Downloaded 2/2 tracks" in result.output