import logging
import asyncio
import importlib
from threading import Event
from pathlib import Path

from click.testing import CliRunner
from fastapi import BackgroundTasks
from fastapi import HTTPException
import pytest
from yt_dlp.utils import DownloadCancelled

import spotifysaver.api.routers.download as download_router

from spotifysaver.api.routers.download import (
    YOUTUBE_RATE_LIMIT_WARNING,
    _report_yt_dlp_error,
    _run_task,
    active,
    cancel_download,
    cancellation_events,
    clear_queue,
    discard_after_stop,
    order,
    pending,
    pause_queue,
    paused_task_ids,
    preserve_after_stop,
    retry_download,
    requests_by_task,
    start_download,
    tasks,
)


def _forget_task(task_id):
    tasks.pop(task_id, None)
    cancellation_events.pop(task_id, None)
    requests_by_task.pop(task_id, None)
    if task_id in pending:
        pending.remove(task_id)
    if task_id in order:
        order.remove(task_id)
from spotifysaver.api.schemas import DownloadRequest, DownloadStatus, TrackInfo
from spotifysaver.api.services.download_service import DownloadService
from spotifysaver.downloader.youtube_downloader import YouTubeDownloader
from spotifysaver.downloader.youtube_downloader_for_cli import YouTubeDownloaderForCLI
from spotifysaver.spotlog.ydd_logger import YDLLogger
from spotifysaver.enums import AudioFormat
import spotifysaver.downloader.youtube_downloader_for_cli as cli_downloader_module

cli_download_module = importlib.import_module(
    "spotifysaver.cli.commands.download.download"
)
download_command = cli_download_module.download


ALBUM_URL = (
    "https://music.youtube.com/playlist?"
    "list=OLAK5uy_nAKi7j0JupK6KpHDrw4zO_zGIOMCs_RdQ"
)
TRACK_URL = "https://music.youtube.com/watch?v=YPnMd26jwrU"


def test_yt_dlp_rate_limit_error_is_reported_without_flagging_other_errors():
    task = DownloadStatus(task_id="rate-limit-test", status="processing", progress=0)
    ydl_logger = YDLLogger(lambda message: _report_yt_dlp_error(task, message))

    ydl_logger.error(
        "The current session has been rate-limited by YouTube for up to an hour."
    )
    assert task.warning_message == YOUTUBE_RATE_LIMIT_WARNING

    task.warning_message = None
    ydl_logger.error("Requested format is not available.")
    assert task.warning_message is None


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
    assert YouTubeDownloader.is_youtube_track_url(
        TRACK_URL
    )
    assert YouTubeDownloader.is_youtube_track_url(
        "https://youtu.be/YPnMd26jwrU"
    )
    assert not YouTubeDownloader.is_youtube_track_url(ALBUM_URL)


def test_youtube_collection_download_embeds_metadata_and_reports_failures(
    monkeypatch, tmp_path
):
    progress = []
    track_results = []

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
                    {
                        "id": "success",
                        "title": "First Track",
                        "channel": "Sample Artist",
                        "duration": 195,
                    },
                    {
                        "id": "failure",
                        "title": "Second Track",
                        "playlist_index": 3,
                    },
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

        def add_postprocessor_hook(self, _hook):
            pass

        def download(self, urls):
            assert urls == [ALBUM_URL]
            assert self.params["playlist_items"] == "2"
            assert track_results == [(1, "First Track", "completed")]
            self.progress_hook(
                {
                    "status": "downloading",
                    "info_dict": {"playlist_index": 3, "title": "Second Track"},
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
    titles = []
    queue_tracks = []

    result = downloader.download_youtube_playlist_cli(
        ALBUM_URL,
        progress_callback=lambda *args: progress.append(args),
        track_result_callback=lambda *args: track_results.append(args),
        title_callback=titles.append,
        tracks_callback=queue_tracks.extend,
    )

    assert result["completed_tracks"] == 1
    assert result["failed_tracks"] == 1
    assert result["failed_track_names"] == ["Second Track"]
    assert result["total_tracks"] == 2
    assert progress == [(2, 2, "Second Track")]
    assert track_results == [
        (1, "First Track", "completed"),
        (2, "Second Track", "error"),
    ]
    assert titles == ["Album: Sample"]
    assert queue_tracks == [
        {
            "name": "First Track",
            "artists": ["Sample Artist"],
            "album_name": "Album: Sample",
            "duration": 195,
            "number": 1,
            "uri": "https://www.youtube.com/watch?v=success",
        },
        {
            "name": "Second Track",
            "artists": [],
            "album_name": "Album: Sample",
            "duration": 0,
            "number": 2,
            "uri": "https://www.youtube.com/watch?v=failure",
        },
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


def test_youtube_collection_reports_each_download_after_postprocessing(
    monkeypatch, tmp_path
):
    track_results = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.params = options
            self.params["outtmpl"] = {"default": self.params["outtmpl"]}
            self.progress_hook = None
            self.postprocessor_hook = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            assert download is False
            return {
                "title": "Sample Playlist",
                "playlist_count": 1,
                "entries": [{"id": "video-1", "title": "First Track"}],
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

        def add_postprocessor_hook(self, hook):
            self.postprocessor_hook = hook

        def download(self, urls):
            assert urls == [ALBUM_URL]
            final_path = Path(
                self.prepare_filename({"playlist_index": 1, "title": "First Track"})
            ).with_suffix(".m4a")
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.touch()
            self.postprocessor_hook(
                {
                    "status": "finished",
                    "postprocessor": "FFmpegExtractAudio",
                    "info_dict": {"playlist_index": 1, "title": "First Track"},
                }
            )
            self.postprocessor_hook(
                {
                    "status": "finished",
                    "postprocessor": "FFmpegMetadata",
                    "info_dict": {"playlist_index": 1, "title": "First Track"},
                }
            )
            assert track_results == [(1, "First Track", "completed")]

    monkeypatch.setattr(cli_downloader_module.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(
        YouTubeDownloader,
        "_get_ydl_opts",
        lambda self, output_path, output_format, bitrate: {
            "postprocessors": [{"key": "FFmpegExtractAudio"}]
        },
    )
    downloader = object.__new__(YouTubeDownloaderForCLI)
    downloader.base_dir = tmp_path
    downloader.logger = logging.getLogger(__name__)

    result = downloader.download_youtube_playlist_cli(
        ALBUM_URL,
        download_cover=False,
        track_result_callback=lambda *args: track_results.append(args),
    )

    assert result["completed_tracks"] == 1
    assert result["failed_tracks"] == 0
    assert track_results == [(1, "First Track", "completed")]


def test_youtube_collection_reports_track_completion_before_next_track_starts(
    monkeypatch, tmp_path
):
    events = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.params = options
            self.params["outtmpl"] = {"default": self.params["outtmpl"]}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            assert download is False
            return {
                "title": "Sample Playlist",
                "playlist_count": 2,
                "entries": [
                    {"id": "video-1", "title": "First Track", "playlist_index": 1},
                    {"id": "video-2", "title": "Second Track", "playlist_index": 2},
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

        def add_postprocessor_hook(self, _hook):
            pass

        def download(self, _urls):
            self.progress_hook(
                {
                    "status": "downloading",
                    "info_dict": {"playlist_index": 1, "title": "First Track"},
                }
            )
            first_path = Path(
                self.prepare_filename(
                    {"playlist_index": 1, "title": "First Track"}
                )
            ).with_suffix(".m4a")
            first_path.parent.mkdir(parents=True, exist_ok=True)
            first_path.touch()
            self.progress_hook(
                {
                    "status": "downloading",
                    "info_dict": {"playlist_index": 2, "title": "Second Track"},
                }
            )
            second_path = Path(
                self.prepare_filename(
                    {"playlist_index": 2, "title": "Second Track"}
                )
            ).with_suffix(".m4a")
            second_path.touch()

    monkeypatch.setattr(cli_downloader_module.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(
        YouTubeDownloader,
        "_get_ydl_opts",
        lambda self, output_path, output_format, bitrate: {
            "postprocessors": [{"key": "FFmpegExtractAudio"}]
        },
    )
    downloader = object.__new__(YouTubeDownloaderForCLI)
    downloader.base_dir = tmp_path
    downloader.logger = logging.getLogger(__name__)

    result = downloader.download_youtube_playlist_cli(
        ALBUM_URL,
        download_cover=False,
        progress_callback=lambda number, *_args: events.append(
            ("downloading", number)
        ),
        track_result_callback=lambda number, _name, state: events.append(
            (state, number)
        ),
    )

    assert result["completed_tracks"] == 2
    assert events == [
        ("downloading", 1),
        ("completed", 1),
        ("downloading", 2),
        ("completed", 2),
    ]


def test_api_routes_youtube_collection_without_spotify_client():
    cancellation_event = Event()
    titles = []
    queue_tracks = []

    class FakeDownloader:
        def download_youtube_playlist_cli(self, *args):
            assert args[0] == ALBUM_URL
            assert args[7] is False
            assert args[8] is cancellation_event
            args[9]("Sample Album")
            args[10](
                [
                    {
                        "name": "First Track",
                        "artists": ["Sample Artist"],
                        "album_name": "Sample Album",
                        "duration": 195,
                        "number": 1,
                        "uri": "https://www.youtube.com/watch?v=success",
                    }
                ]
            )
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
        service.download_from_url(
            ALBUM_URL,
            cancellation_event=cancellation_event,
            title_callback=titles.append,
            tracks_callback=queue_tracks.extend,
        )
    )

    assert result["content_type"] == "playlist"
    assert result["completed_tracks"] == 1
    assert result["total_tracks"] == 1
    assert titles == ["Sample Album"]
    assert queue_tracks[0]["name"] == "First Track"


def test_download_task_persists_track_start_events(monkeypatch):
    task_id = "track-start-event-test"
    cancellation_event = Event()
    request = DownloadRequest(spotify_url=ALBUM_URL, output_dir="Music")

    class FakeDownloadService:
        def __init__(self, **_kwargs):
            self.progress_fraction_callback = None

        async def download_from_url(
            self,
            _url,
            progress_callback,
            track_result_callback,
            **_kwargs,
        ):
            progress_callback(2, 2, "Second Track")
            track_result_callback(1, "First Track", "completed")
            return {
                "completed_tracks": 1,
                "failed_tracks": 0,
                "failed_track_names": [],
                "total_tracks": 2,
                "output_directory": "Music",
                "collection_name": "Sample Playlist",
            }

    monkeypatch.setattr(download_router, "DownloadService", FakeDownloadService)
    tasks[task_id] = DownloadStatus(
        task_id=task_id,
        status="queued",
        progress=0,
        spotify_url=ALBUM_URL,
    )
    requests_by_task[task_id] = request
    cancellation_events[task_id] = cancellation_event

    try:
        asyncio.run(download_router.download_task(task_id, request))

        assert [
            (update["track_number"], update["status"])
            for update in tasks[task_id].track_updates
        ] == [(2, "downloading"), (1, "completed")]
        assert tasks[task_id].completed_tracks == 1
    finally:
        _forget_task(task_id)


def test_partial_playlist_failure_has_distinct_terminal_status(monkeypatch):
    task_id = "partial-playlist-failure-status-test"
    request = DownloadRequest(spotify_url=ALBUM_URL, output_dir="Music")

    class FakeDownloadService:
        def __init__(self, **_kwargs):
            self.progress_fraction_callback = None

        async def download_from_url(
            self,
            _url,
            progress_callback,
            track_result_callback,
            tracks_callback,
            **_kwargs,
        ):
            tracks_callback(
                [
                    {
                        "name": "Downloaded Track",
                        "artists": [],
                        "duration": 60,
                        "number": 1,
                        "uri": "",
                    },
                    {
                        "name": "Unavailable Track",
                        "artists": [],
                        "duration": 60,
                        "number": 2,
                        "uri": "",
                    },
                ]
            )
            progress_callback(1, 2, "Downloaded Track")
            track_result_callback(1, "Downloaded Track", "completed")
            progress_callback(2, 2, "Unavailable Track")
            track_result_callback(2, "Unavailable Track", "error")
            return {
                "completed_tracks": 1,
                "failed_tracks": 1,
                "failed_track_names": ["Unavailable Track"],
                "total_tracks": 2,
                "output_directory": "Music",
                "collection_name": "Sample Playlist",
            }

    monkeypatch.setattr(download_router, "DownloadService", FakeDownloadService)
    tasks[task_id] = DownloadStatus(
        task_id=task_id,
        status="queued",
        progress=0,
        spotify_url=ALBUM_URL,
    )
    requests_by_task[task_id] = request
    cancellation_events[task_id] = Event()

    try:
        asyncio.run(download_router.download_task(task_id, request))

        assert tasks[task_id].status == "completed_with_errors"
        assert tasks[task_id].completed_tracks == 1
        assert tasks[task_id].failed_tracks == 1
        assert tasks[task_id].failed_track_names == ["Unavailable Track"]
        assert "Failed: Unavailable Track" in tasks[task_id].error_message

        asyncio.run(download_router.remove_download(task_id))
        assert task_id not in tasks
    finally:
        _forget_task(task_id)


def test_resumed_youtube_playlist_reports_reverification_phase(monkeypatch):
    task_id = "playlist-reverification-phase-test"
    request = DownloadRequest(spotify_url=ALBUM_URL, output_dir="Music")
    tasks[task_id] = DownloadStatus(
        task_id=task_id,
        status="queued",
        progress=25,
        total_tracks=1,
        content_type="playlist",
        spotify_url=ALBUM_URL,
        tracks=[
            TrackInfo(
                name="Previously Listed Track",
                artists=[],
                duration=60,
                number=1,
                uri="",
            )
        ],
    )
    requests_by_task[task_id] = request
    cancellation_events[task_id] = Event()

    class FakeDownloadService:
        def __init__(self, **_kwargs):
            self.progress_fraction_callback = None

        async def download_from_url(self, _url, tracks_callback, **_kwargs):
            assert tasks[task_id].playlist_verification == "rechecking"
            tracks_callback(
                [
                    {
                        "name": "Refreshed Track",
                        "artists": [],
                        "duration": 60,
                        "number": 1,
                        "uri": "",
                    }
                ]
            )
            assert tasks[task_id].playlist_verification is None
            return {
                "completed_tracks": 1,
                "failed_tracks": 0,
                "failed_track_names": [],
                "total_tracks": 1,
                "output_directory": "Music",
                "collection_name": "Sample Playlist",
            }

    monkeypatch.setattr(download_router, "DownloadService", FakeDownloadService)

    try:
        asyncio.run(download_router.download_task(task_id, request))

        assert tasks[task_id].status == "completed"
        assert tasks[task_id].playlist_verification is None
    finally:
        _forget_task(task_id)


def test_api_routes_direct_youtube_track_without_spotify_client():
    class FakeDownloader:
        def download_youtube_track_cli(self, *args):
            assert args[0] == TRACK_URL
            assert args[1] == AudioFormat.MP3
            return {
                "completed_tracks": 1,
                "failed_tracks": 0,
                "failed_track_names": [],
                "total_tracks": 1,
                "output_directory": "Music/YouTube/Artist",
            }

    service = object.__new__(DownloadService)
    service.downloader = FakeDownloader()
    service.output_format = AudioFormat.MP3
    service.bit_rate = 128
    service.overwrite_existing = False
    service.cancellation_event = None

    result = asyncio.run(service.download_from_url(TRACK_URL))

    assert result["content_type"] == "track"
    assert result["completed_tracks"] == 1


def test_user_cancellation_is_not_logged_as_download_error(caplog):
    class FakeDownloader:
        def download_youtube_playlist_cli(self, *_args):
            raise DownloadCancelled("Download cancelled by user")

    service = object.__new__(DownloadService)
    service.downloader = FakeDownloader()
    service.output_format = AudioFormat.MP3
    service.bit_rate = 128
    service.download_cover = True
    service.overwrite_existing = False

    with caplog.at_level(logging.ERROR):
        try:
            asyncio.run(
                service.download_from_url(ALBUM_URL, cancellation_event=Event())
            )
        except DownloadCancelled:
            pass
        else:
            raise AssertionError("Expected yt-dlp's cancellation exception to propagate")

    assert not any("Error downloading from" in record.getMessage() for record in caplog.records)


def test_api_accepts_direct_youtube_track_url():
    request = DownloadRequest(spotify_url=TRACK_URL, output_format="mp3")

    response = asyncio.run(start_download(request, BackgroundTasks()))

    try:
        assert response.content_type == "track"
        assert response.spotify_url == TRACK_URL
    finally:
        _forget_task(response.task_id)


def test_api_accepts_youtube_collection_url():
    request = DownloadRequest(spotify_url=ALBUM_URL)

    response = asyncio.run(start_download(request, BackgroundTasks()))

    try:
        assert response.content_type == "playlist"
        assert response.spotify_url == ALBUM_URL
    finally:
        _forget_task(response.task_id)


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


def test_retry_download_requeues_original_request_and_resets_task(monkeypatch):
    task_id = "retry-test-task"
    original_event = Event()
    original_event.set()
    request = DownloadRequest(spotify_url=TRACK_URL)
    tasks[task_id] = DownloadStatus(
        task_id=task_id,
        status="completed_with_errors",
        progress=75,
        current_track="failed track",
        total_tracks=2,
        completed_tracks=1,
        failed_tracks=1,
        error_message="Download failed",
        completed_at="2024-01-01T00:00:00",
    )
    requests_by_task[task_id] = request
    cancellation_events[task_id] = original_event
    monkeypatch.setattr(download_router, "paused", True)

    try:
        response = asyncio.run(retry_download(task_id))

        assert response["status"] == "queued"
        assert tasks[task_id].status == "queued"
        assert tasks[task_id].progress == 0
        assert tasks[task_id].current_track is None
        assert tasks[task_id].completed_tracks == 0
        assert tasks[task_id].failed_tracks == 0
        assert tasks[task_id].error_message is None
        assert tasks[task_id].completed_at is None
        assert requests_by_task[task_id] is request
        assert cancellation_events[task_id] is not original_event
        assert not cancellation_events[task_id].is_set()
        assert task_id in pending
        assert task_id in order
    finally:
        _forget_task(task_id)
        download_router.download_archives.pop(task_id, None)
        active.discard(task_id)
        paused_task_ids.discard(task_id)


def test_retry_download_rejects_non_retryable_task():
    task_id = "retry-invalid-status-task"
    tasks[task_id] = DownloadStatus(
        task_id=task_id,
        status="processing",
        progress=20,
    )
    requests_by_task[task_id] = DownloadRequest(spotify_url=TRACK_URL)

    try:
        with pytest.raises(HTTPException) as error:
            asyncio.run(retry_download(task_id))

        assert error.value.status_code == 409
    finally:
        _forget_task(task_id)


def test_cancellation_hook_uses_yt_dlp_cancel_signal():
    cancellation_event = Event()
    options = {}
    downloader = object.__new__(YouTubeDownloader)
    downloader._add_cancellation_hook(options, cancellation_event)
    cancellation_event.set()

    try:
        options["progress_hooks"][0]({})
    except DownloadCancelled:
        pass
    else:
        raise AssertionError("Expected yt-dlp's dedicated cancellation exception")


def test_clear_queue_discards_active_download_interrupted_by_pause(monkeypatch):
    task_id = "clear-paused-download-test"
    cancellation_event = Event()
    request = DownloadRequest(spotify_url=ALBUM_URL)
    tasks[task_id] = DownloadStatus(
        task_id=task_id,
        status="processing",
        progress=25,
    )
    requests_by_task[task_id] = request
    cancellation_events[task_id] = cancellation_event
    active.add(task_id)
    order.append(task_id)
    previous_paused = download_router.paused

    async def finish_interrupted_download(_task_id, _request):
        tasks[task_id].status = "cancelled"

    monkeypatch.setattr(download_router, "download_task", finish_interrupted_download)

    try:
        asyncio.run(pause_queue())
        assert task_id in paused_task_ids
        assert cancellation_event.is_set()

        result = asyncio.run(clear_queue())

        assert result["removed"] == 1
        assert task_id not in order
        assert task_id not in pending
        assert task_id in discard_after_stop

        asyncio.run(_run_task(task_id, request))

        assert task_id not in tasks
        assert task_id not in requests_by_task
        assert task_id not in cancellation_events
        assert task_id not in discard_after_stop
        assert task_id not in active
    finally:
        download_router.paused = previous_paused
        tasks.pop(task_id, None)
        requests_by_task.pop(task_id, None)
        cancellation_events.pop(task_id, None)
        active.discard(task_id)
        paused_task_ids.discard(task_id)
        discard_after_stop.discard(task_id)
        preserve_after_stop.discard(task_id)
        if task_id in order:
            order.remove(task_id)
        if task_id in pending:
            pending.remove(task_id)


def test_clear_queue_preserves_completed_tracks_from_paused_playlist():
    task_id = "clear-paused-playlist-with-completed-tracks"
    request = DownloadRequest(spotify_url=ALBUM_URL)
    cancellation_event = Event()
    tasks[task_id] = DownloadStatus(
        task_id=task_id,
        status="queued",
        progress=25,
        completed_tracks=1,
        error_message="Paused by user",
        tracks=[
            TrackInfo(name="Finished Track", artists=[], duration=60, number=1, uri=""),
            TrackInfo(name="Waiting Track", artists=[], duration=60, number=2, uri=""),
        ],
        track_updates=[
            {
                "track_number": 1,
                "track_name": "Finished Track",
                "status": "completed",
            },
            {
                "track_number": 2,
                "track_name": "Waiting Track",
                "status": "downloading",
            },
        ],
    )
    requests_by_task[task_id] = request
    cancellation_events[task_id] = cancellation_event
    pending.append(task_id)
    order.append(task_id)

    try:
        result = asyncio.run(clear_queue())

        assert result["removed"] == 1
        assert task_id not in pending
        assert task_id in tasks
        assert tasks[task_id].status == "cancelled"
        assert [update["status"] for update in tasks[task_id].track_updates] == [
            "completed",
            "downloading",
        ]
        assert [track.name for track in tasks[task_id].tracks] == ["Finished Track"]
        assert tasks[task_id].error_message == "Remaining tracks cleared by user"
    finally:
        _forget_task(task_id)


def test_clear_queue_preserves_in_progress_playlist_after_worker_stops(monkeypatch):
    task_id = "clear-active-paused-playlist-with-completed-tracks"
    request = DownloadRequest(spotify_url=ALBUM_URL)
    cancellation_event = Event()
    tasks[task_id] = DownloadStatus(
        task_id=task_id,
        status="queued",
        progress=25,
        completed_tracks=1,
        tracks=[
            TrackInfo(name="Finished Track", artists=[], duration=60, number=1, uri=""),
            TrackInfo(name="Waiting Track", artists=[], duration=60, number=2, uri=""),
        ],
        track_updates=[
            {
                "track_number": 1,
                "track_name": "Finished Track",
                "status": "completed",
            },
            {
                "track_number": 2,
                "track_name": "Waiting Track",
                "status": "downloading",
            },
        ],
    )
    requests_by_task[task_id] = request
    cancellation_events[task_id] = cancellation_event
    active.add(task_id)
    paused_task_ids.add(task_id)
    order.append(task_id)

    async def finish_interrupted_download(_task_id, _request):
        tasks[task_id].status = "cancelled"

    monkeypatch.setattr(download_router, "download_task", finish_interrupted_download)

    try:
        result = asyncio.run(clear_queue())

        assert result["removed"] == 1
        assert task_id in preserve_after_stop
        assert task_id in tasks
        assert tasks[task_id].status == "cancelling"
        assert [track.name for track in tasks[task_id].tracks] == ["Finished Track"]
        assert task_id in order

        asyncio.run(download_router._run_task(task_id, request))

        assert task_id in tasks
        assert tasks[task_id].status == "cancelled"
        assert tasks[task_id].track_updates[0]["status"] == "completed"
        assert [track.name for track in tasks[task_id].tracks] == ["Finished Track"]
        assert task_id not in preserve_after_stop
    finally:
        tasks.pop(task_id, None)
        requests_by_task.pop(task_id, None)
        cancellation_events.pop(task_id, None)
        active.discard(task_id)
        paused_task_ids.discard(task_id)
        preserve_after_stop.discard(task_id)
        if task_id in order:
            order.remove(task_id)
        if task_id in pending:
            pending.remove(task_id)


def test_clear_queue_stops_active_playlist_without_pausing_queue():
    task_id = "clear-active-playlist-without-pausing"
    request = DownloadRequest(spotify_url=ALBUM_URL)
    cancellation_event = Event()
    tasks[task_id] = DownloadStatus(
        task_id=task_id,
        status="processing",
        progress=50,
        completed_tracks=1,
        total_tracks=3,
        content_type="playlist",
        tracks=[
            TrackInfo(name="Finished Track", artists=[], duration=60, number=1, uri=""),
            TrackInfo(name="Current Track", artists=[], duration=60, number=2, uri=""),
            TrackInfo(name="Waiting Track", artists=[], duration=60, number=3, uri=""),
        ],
        track_updates=[
            {
                "track_number": 1,
                "track_name": "Finished Track",
                "status": "completed",
            },
            {
                "track_number": 2,
                "track_name": "Current Track",
                "status": "downloading",
            },
        ],
    )
    requests_by_task[task_id] = request
    cancellation_events[task_id] = cancellation_event
    active.add(task_id)
    order.append(task_id)

    try:
        result = asyncio.run(clear_queue())

        assert result["removed"] == 1
        assert cancellation_event.is_set()
        assert tasks[task_id].status == "cancelling"
        assert tasks[task_id].error_message == "Remaining tracks cleared by user"
        assert task_id in preserve_after_stop
        assert [track.name for track in tasks[task_id].tracks] == ["Finished Track"]
    finally:
        tasks.pop(task_id, None)
        requests_by_task.pop(task_id, None)
        cancellation_events.pop(task_id, None)
        active.discard(task_id)
        paused_task_ids.discard(task_id)
        preserve_after_stop.discard(task_id)
        if task_id in order:
            order.remove(task_id)
        if task_id in pending:
            pending.remove(task_id)


def test_clear_queue_does_not_stop_active_single_track():
    task_id = "clear-queue-keeps-active-single-track"
    request = DownloadRequest(spotify_url=TRACK_URL)
    cancellation_event = Event()
    tasks[task_id] = DownloadStatus(
        task_id=task_id,
        status="processing",
        progress=50,
        total_tracks=1,
        content_type="track",
    )
    requests_by_task[task_id] = request
    cancellation_events[task_id] = cancellation_event
    active.add(task_id)
    order.append(task_id)

    try:
        result = asyncio.run(clear_queue())

        assert result["removed"] == 0
        assert not cancellation_event.is_set()
        assert tasks[task_id].status == "processing"
    finally:
        tasks.pop(task_id, None)
        requests_by_task.pop(task_id, None)
        cancellation_events.pop(task_id, None)
        active.discard(task_id)
        if task_id in order:
            order.remove(task_id)


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