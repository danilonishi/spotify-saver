import asyncio
import logging
from threading import Event
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
from spotifysaver.api.services.youtube_confirmation_store import (
    YouTubeConfirmationStore,
)
from spotifysaver.downloader.youtube_downloader import YouTubeDownloader
from spotifysaver.models import Track


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
