import asyncio
import logging
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from spotifysaver.api.config import APIConfig
from spotifysaver.api.routers import download as download_router
from spotifysaver.api.routers.user_preferences import (
    YouTubeConfirmationUpdate,
    get_youtube_confirmation_preferences,
    set_youtube_confirmation_preferences,
)
from spotifysaver.api.schemas import DownloadRequest, DownloadStatus, TrackInfo
from spotifysaver.api.services.download_service import DownloadService
from spotifysaver.api.services.youtube_confirmation_store import (
    YouTubeConfirmationStore,
)
from spotifysaver.api.services.notification_hub import NotificationHub
from spotifysaver.downloader.youtube_downloader import YouTubeDownloader
from spotifysaver.downloader.youtube_downloader_for_cli import YouTubeDownloaderForCLI
from spotifysaver.models import Album, Playlist, Track


def _request(store):
    app = SimpleNamespace(state=SimpleNamespace(youtube_confirmation_store=store))
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/preferences/youtube-confirmation",
            "raw_path": b"/api/v1/preferences/youtube-confirmation",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 8000),
            "client": ("testclient", 50000),
            "root_path": "",
            "app": app,
        }
    )


def test_youtube_confirmation_preferences_persist_and_apply(tmp_path, monkeypatch):
    store = YouTubeConfirmationStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    request = _request(store)
    monkeypatch.setattr(APIConfig, "set_youtube_confirmation_preferences", lambda *args: None)

    assert asyncio.run(get_youtube_confirmation_preferences(request)) == {
        "confirm_matched_downloads": False,
        "confirm_channel_watcher_downloads": False,
    }

    saved = asyncio.run(
        set_youtube_confirmation_preferences(
            YouTubeConfirmationUpdate(
                confirm_matched_downloads=True,
                confirm_channel_watcher_downloads=False,
            ),
            request,
        )
    )
    assert saved == {
        "confirm_matched_downloads": True,
        "confirm_channel_watcher_downloads": False,
    }
    assert YouTubeConfirmationStore(str(tmp_path / "settings.sqlite3")).get_preferences() == saved


def test_youtube_confirmation_preferences_reject_non_boolean_values():
    with pytest.raises(ValidationError):
        YouTubeConfirmationUpdate(
            confirm_matched_downloads="maybe",
            confirm_channel_watcher_downloads=False,
        )


@pytest.mark.parametrize(
    ("url", "media_type", "expected"),
    [
        ("https://open.spotify.com/album/example", "audio", True),
        ("https://open.spotify.com/playlist/example", "audio", True),
        ("https://open.spotify.com/track/example", "audio", False),
        ("https://open.spotify.com/album/example", "video", False),
    ],
)
def test_spotify_collection_confirmation_includes_albums(url, media_type, expected):
    request = DownloadRequest(
        spotify_url=url,
        media_type=media_type,
        output_dir="Music",
    )

    assert download_router._uses_spotify_collection_confirmation(request) is expected


def test_matched_youtube_download_is_not_started_until_callback_approves(tmp_path, monkeypatch):
    track = Track(
        number=1,
        total_tracks=1,
        name="Example track",
        duration=180,
        uri="spotify:track:example",
        artists=["Example artist"],
        album_artist=["Example artist"],
        release_date="2024-01-01",
        album_name="Example album",
    )
    downloader = object.__new__(YouTubeDownloader)
    downloader.logger = logging.getLogger("test-youtube-confirmation")
    downloader.searcher = SimpleNamespace(
        search_track=lambda _track: "https://music.youtube.com/watch?v=matched"
    )
    monkeypatch.setattr(
        YouTubeDownloader,
        "_get_output_path",
        lambda *_args, **_kwargs: tmp_path / "Example.m4a",
    )
    monkeypatch.setattr(
        YouTubeDownloader,
        "_save_cover_album",
        lambda *_args: None,
    )
    downloader.youtube_confirmation_callback = lambda _track, _url: False

    result = downloader.download_track(track)

    assert result == (None, None)


def test_parked_youtube_match_exposes_title_and_track_approval(tmp_path, monkeypatch):
    task_id = "matched-task"
    task = DownloadStatus(
        task_id=task_id,
        status="processing",
        progress=0,
        tracks=[
            TrackInfo(
                name="Example track",
                artists=["Example artist"],
                duration=180,
                number=1,
                uri="spotify:track:example",
            )
        ],
    )
    monkeypatch.setattr(download_router, "tasks", {task_id: task})
    monkeypatch.setattr(download_router, "active", {task_id})
    monkeypatch.setattr(download_router, "APIConfig", APIConfig)
    monkeypatch.setattr(download_router, "queue_lock", asyncio.Lock())
    monkeypatch.setattr(download_router, "_try_start_next", _no_op)
    monkeypatch.setattr(APIConfig, "set_active_downloads", lambda _count: None)
    hub = NotificationHub()
    monkeypatch.setattr(download_router, "notification_hub", hub)

    asyncio.run(
        download_router._park_for_youtube_confirmation(
            task_id,
            1,
            "Resolved YouTube title",
            "https://music.youtube.com/watch?v=matched",
            "YouTube channel",
        )
    )

    assert task.status == "awaiting_confirmation"
    assert task.confirmation_required is True
    assert task.confirmation_title == "Resolved YouTube title"
    assert task.confirmation_channel == "YouTube channel"
    assert task.tracks[0].confirmation_required is True
    assert task.tracks[0].youtube_url == "https://music.youtube.com/watch?v=matched"
    assert task.tracks[0].youtube_channel == "YouTube channel"
    assert task_id not in download_router.active
    assert hub._history[-1]["category"] == "download_confirmation"
    assert hub._history[-1]["task_id"] == task_id
    assert "YouTube channel" in hub._history[-1]["message"]


async def _no_op():
    return None


def test_confirmation_endpoint_queues_an_approved_parked_task(monkeypatch):
    task_id = "matched-task"
    task = DownloadStatus(
        task_id=task_id,
        status="awaiting_confirmation",
        progress=0,
        confirmation_required=True,
        confirmation_source="matched",
        confirmation_track_number=1,
        tracks=[
            TrackInfo(
                name="Example track",
                artists=["Example artist"],
                duration=180,
                number=1,
                uri="spotify:track:example",
                confirmation_required=True,
            )
        ],
    )
    monkeypatch.setattr(download_router, "tasks", {task_id: task})
    monkeypatch.setattr(download_router, "pending", [])
    monkeypatch.setattr(download_router, "confirmation_resume_events", {task_id: Event()})
    monkeypatch.setattr(download_router, "confirmation_approved", set())
    monkeypatch.setattr(download_router, "queue_lock", asyncio.Lock())
    monkeypatch.setattr(download_router, "_try_start_next", _no_op)

    result = asyncio.run(download_router.confirm_youtube_download(task_id))

    assert result == {"task_id": task_id, "status": "queued"}
    assert task.confirmation_required is False
    assert task.tracks[0].confirmation_required is False
    assert download_router.pending == [task_id]
    assert task_id in download_router.confirmation_approved


def test_confirmation_endpoint_rejects_tasks_not_waiting_for_approval(monkeypatch):
    task_id = "ordinary-task"
    monkeypatch.setattr(
        download_router,
        "tasks",
        {task_id: DownloadStatus(task_id=task_id, status="queued", progress=0)},
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(download_router.confirm_youtube_download(task_id))

    assert error.value.status_code == 409


def test_confirmation_endpoint_approves_an_individual_playlist_track(monkeypatch):
    task_id = "playlist-task"
    approval_event = Event()
    task = DownloadStatus(
        task_id=task_id,
        status="processing",
        progress=0,
        tracks=[
            TrackInfo(
                name="Playlist track",
                artists=["Example artist"],
                duration=180,
                number=1,
                uri="spotify:track:example",
                youtube_title="Matched video",
                youtube_url="https://music.youtube.com/watch?v=matched",
                confirmation_required=True,
            )
        ],
    )
    monkeypatch.setattr(download_router, "tasks", {task_id: task})
    monkeypatch.setattr(
        download_router,
        "track_confirmation_events",
        {(task_id, 1): approval_event},
    )

    result = asyncio.run(download_router.confirm_youtube_download(task_id, track_number=1))

    assert result == {
        "task_id": task_id,
        "track_number": 1,
        "status": "processing",
    }
    assert task.tracks[0].confirmation_required is False
    assert approval_event.is_set()


def test_playlist_checks_continue_during_first_approval_and_download(tmp_path, monkeypatch):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))
    tracks = [
        Track(
            number=index,
            total_tracks=3,
            name=f"Track {index}",
            duration=180,
            uri=f"spotify:track:{index}",
            artists=["Example artist"],
            album_artist=["Example artist"],
            release_date="2024-01-01",
            source_type="playlist",
            playlist_name="Example playlist",
            playlist_position=index,
            album_name="Example album",
        )
        for index in (1, 2, 3)
    ]
    playlist = Playlist(
        name="Example playlist",
        description="",
        owner="owner",
        uri="spotify:playlist:example",
        cover_url="",
        tracks=tracks,
    )
    metadata_checked = set()
    metadata_lock = Lock()
    first_two_checks_finished = Event()
    all_checks_finished = Event()
    first_download_started = Event()

    class FakeYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download):
            assert download is False
            video_id = url.rsplit("=", 1)[-1]
            return {"id": video_id, "title": f"Title {video_id}", "channel": "Channel"}

    monkeypatch.setattr(
        "spotifysaver.downloader.youtube_downloader_for_cli.yt_dlp.YoutubeDL",
        FakeYoutubeDL,
    )

    def search_track(_searcher, track):
        if track.playlist_position == 3:
            assert first_download_started.wait(timeout=2)
        return f"https://music.youtube.com/watch?v=video-{track.number}"

    monkeypatch.setattr(type(downloader.searcher), "search_track", search_track)

    def metadata_callback(track_number, _track, _url, _info):
        with metadata_lock:
            metadata_checked.add(track_number)
            if {1, 2}.issubset(metadata_checked):
                first_two_checks_finished.set()
            if len(metadata_checked) == 3:
                all_checks_finished.set()

    downloader.youtube_confirmation_metadata_callback = metadata_callback
    first_approval_waiting = Event()

    def approve_track(track, _url):
        if track.playlist_position == 1:
            first_approval_waiting.set()
            return first_two_checks_finished.wait(timeout=2)
        return True

    downloader.youtube_confirmation_callback = approve_track

    def download_track(**kwargs):
        assert downloader.youtube_confirmation_callback(kwargs["track"], kwargs["youtube_url"])
        if kwargs["track"].playlist_position == 1:
            first_download_started.set()
            assert all_checks_finished.wait(
                timeout=2
            ), "Another video check was blocked by the first track download"
        return (
            Path(tmp_path / f"{kwargs['track'].number}.m4a"),
            kwargs["track"].with_youtube_url(kwargs["youtube_url"]),
        )

    monkeypatch.setattr(downloader, "download_track", download_track)

    result = downloader.download_playlist_cli(
        playlist,
        cover=False,
    )

    assert result == (3, 3, [])
    assert metadata_checked == {1, 2, 3}
    assert first_approval_waiting.is_set()


def test_album_checks_matches_and_waits_for_approval_using_collection_positions(
    tmp_path, monkeypatch
):
    downloader = object.__new__(YouTubeDownloaderForCLI)
    downloader.logger = logging.getLogger("test-album-youtube-confirmation")
    downloader.searcher = SimpleNamespace(
        search_track=lambda track: (
            f"https://music.youtube.com/watch?v=video-{track.playlist_position}"
        )
    )
    tracks = [
        Track(
            number=1,
            total_tracks=2,
            name=f"Disc {disc_number} track",
            duration=180,
            uri=f"spotify:track:{disc_number}",
            artists=["Example artist"],
            album_artist=["Example artist"],
            release_date="2024-01-01",
            disc_number=disc_number,
            playlist_position=disc_number,
            album_name="Example album",
        )
        for disc_number in (1, 2)
    ]
    album = Album(
        name="Example album",
        artists=["Example artist"],
        release_date="2024-01-01",
        genres=[],
        cover_url="",
        tracks=tracks,
        uri="spotify:album:example",
    )
    metadata_positions = []
    approved_positions = []

    class FakeYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download):
            assert download is False
            return {
                "id": url.rsplit("-", 1)[-1],
                "title": "Matched video",
                "channel": "Example channel",
            }

    monkeypatch.setattr(
        "spotifysaver.downloader.youtube_downloader_for_cli.yt_dlp.YoutubeDL",
        FakeYoutubeDL,
    )
    monkeypatch.setattr(
        downloader,
        "_get_output_path",
        lambda track, **_kwargs: tmp_path / f"{track.playlist_position}.m4a",
    )
    monkeypatch.setattr(downloader, "_get_album_dir", lambda _album: tmp_path)

    def metadata_callback(track_number, track, _url, _info):
        assert track_number == track.playlist_position
        metadata_positions.append(track_number)

    def approval_callback(track, _url):
        approved_positions.append(track.playlist_position)
        return True

    downloader.youtube_confirmation_metadata_callback = metadata_callback
    downloader.youtube_confirmation_callback = approval_callback

    def download_track(**kwargs):
        track = kwargs["track"]
        youtube_url = kwargs["youtube_url"]
        assert approval_callback(track, youtube_url)
        return (
            tmp_path / f"{track.playlist_position}.m4a",
            track.with_youtube_url(youtube_url),
        )

    monkeypatch.setattr(downloader, "download_track", download_track)

    result = downloader.download_album_cli(album, cover=False)

    assert result == (2, 2, [])
    assert metadata_positions == [1, 2]
    assert approved_positions == [1, 2]


def test_download_service_passes_playlist_approval_callbacks(tmp_path, monkeypatch):
    service = DownloadService(output_dir=str(tmp_path))
    playlist = Playlist(
        name="Example playlist",
        description="",
        owner="owner",
        uri="spotify:playlist:example",
        cover_url="",
        tracks=[],
    )
    service.spotify = SimpleNamespace(get_playlist=lambda _url: playlist)
    approval_callback = lambda _track, _url: True
    metadata_callback = lambda *_args: None
    service.youtube_confirmation_callback = approval_callback
    service.youtube_confirmation_metadata_callback = metadata_callback

    def download_playlist(*_args):
        assert service.downloader.youtube_confirmation_callback is approval_callback
        assert service.downloader.youtube_confirmation_metadata_callback is metadata_callback
        return 0, 0, []

    monkeypatch.setattr(service.downloader, "download_playlist_cli", download_playlist)

    result = asyncio.run(service._download_playlist("spotify:playlist:example"))

    assert result["content_type"] == "playlist"


def test_download_service_passes_album_approval_callbacks(tmp_path, monkeypatch):
    service = DownloadService(output_dir=str(tmp_path))
    album = Album(
        name="Example album",
        artists=["Example artist"],
        release_date="2024-01-01",
        genres=[],
        cover_url="",
        tracks=[],
        uri="spotify:album:example",
    )
    service.spotify = SimpleNamespace(get_album=lambda _url: album)
    approval_callback = lambda _track, _url: True
    metadata_callback = lambda *_args: None
    service.youtube_confirmation_callback = approval_callback
    service.youtube_confirmation_metadata_callback = metadata_callback

    def download_album(*_args):
        assert service.downloader.youtube_confirmation_callback is approval_callback
        assert service.downloader.youtube_confirmation_metadata_callback is metadata_callback
        return 0, 0, []

    monkeypatch.setattr(service.downloader, "download_album_cli", download_album)
    monkeypatch.setattr(service.downloader, "_get_album_dir", lambda _album: tmp_path)

    result = asyncio.run(service._download_album("spotify:album:example"))

    assert result["content_type"] == "album"


def test_scheduler_resumes_approved_worker_without_starting_a_duplicate(monkeypatch):
    task_id = "matched-task"
    resume_event = Event()
    task = DownloadStatus(
        task_id=task_id,
        status="queued",
        progress=0,
    )
    monkeypatch.setattr(download_router, "tasks", {task_id: task})
    monkeypatch.setattr(
        download_router,
        "requests_by_task",
        {
            task_id: DownloadRequest(
                spotify_url="https://open.spotify.com/track/2kd0T6zgABT8P0s2h9QU5O"
            )
        },
    )
    monkeypatch.setattr(download_router, "pending", [task_id])
    monkeypatch.setattr(download_router, "active", set())
    monkeypatch.setattr(download_router, "confirmation_resume_events", {task_id: resume_event})
    monkeypatch.setattr(download_router, "confirmation_approved", {task_id})
    monkeypatch.setattr(download_router, "queue_lock", asyncio.Lock())
    monkeypatch.setattr(download_router, "storage_protection_service", None)
    monkeypatch.setattr(download_router, "paused", False)
    monkeypatch.setattr(APIConfig, "MAX_CONCURRENT_DOWNLOADS", 1)
    monkeypatch.setattr(APIConfig, "set_active_downloads", lambda _count: None)
    created_tasks = []
    monkeypatch.setattr(
        download_router.asyncio,
        "create_task",
        lambda coroutine: created_tasks.append(coroutine),
    )

    asyncio.run(download_router._try_start_next())

    assert download_router.active == {task_id}
    assert download_router.pending == []
    assert task.status == "processing"
    assert resume_event.is_set()
    assert created_tasks == []


def test_channel_watcher_enqueue_waits_for_confirmation_when_enabled(tmp_path, monkeypatch):
    task_state = {}
    monkeypatch.setattr(APIConfig, "CONFIRM_CHANNEL_WATCHER_DOWNLOADS", True)
    monkeypatch.setattr(download_router, "tasks", task_state)
    monkeypatch.setattr(download_router, "requests_by_task", {})
    monkeypatch.setattr(download_router, "download_archives", {})
    monkeypatch.setattr(download_router, "order", [])
    monkeypatch.setattr(download_router, "pending", [])
    monkeypatch.setattr(download_router, "queue_lock", asyncio.Lock())
    hub = NotificationHub()
    monkeypatch.setattr(download_router, "notification_hub", hub)
    monkeypatch.setattr(
        download_router.asyncio,
        "create_task",
        lambda coroutine: coroutine.close(),
    )

    result = asyncio.run(
        download_router.enqueue_watched_video(
            {
                "id": "channel-1",
                "title": "Watched YouTube Channel",
                "output_dir": str(tmp_path),
                "video_format": "mp4",
                "video_resolution": 1080,
                "include_subtitles": False,
                "download_archive": str(tmp_path / "archive.txt"),
                "video_title": "New upload",
            },
            "https://www.youtube.com/watch?v=video-1",
        )
    )

    task = next(iter(task_state.values()))
    assert result == "awaiting_confirmation"
    assert task.status == "awaiting_confirmation"
    assert task.confirmation_source == "channel_watcher"
    assert task.confirmation_required is True
    assert task.confirmation_title == "New upload"
    assert task.confirmation_channel == "Watched YouTube Channel"
    assert download_router.pending == []
    assert hub._history[-1]["category"] == "download_confirmation"
    assert hub._history[-1]["task_id"] == task.task_id
