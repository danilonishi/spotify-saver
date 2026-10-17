import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Event, Lock
from time import sleep
from types import SimpleNamespace
from pathlib import Path

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from spotifysaver.api.config import APIConfig
from spotifysaver.api.routers import download as download_router
from spotifysaver.api.routers.user_preferences import (
    DownloadLimitsUpdate,
    get_download_limits,
    set_download_limits,
)
from spotifysaver.api.services.download_limits_store import DownloadLimitsStore
from spotifysaver.api.schemas import DownloadRequest
from spotifysaver.downloader.youtube_downloader import YouTubeDownloader
from spotifysaver.download_limiter import track_download_limiter


def _request(store):
    app = SimpleNamespace(state=SimpleNamespace(download_limits_store=store))
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/preferences/download-limits",
            "raw_path": b"/api/v1/preferences/download-limits",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 8000),
            "client": ("testclient", 50000),
            "root_path": "",
            "app": app,
        }
    )


def test_download_limits_are_persisted_and_applied_to_runtime_queue(tmp_path, monkeypatch):
    store = DownloadLimitsStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    request = _request(store)
    started_queue = []

    async def kick_queue():
        started_queue.append(True)

    monkeypatch.setattr(
        "spotifysaver.api.routers.user_preferences.download_router._try_start_next",
        kick_queue,
    )
    monkeypatch.setattr(APIConfig, "MAX_CONCURRENT_DOWNLOADS", 3)
    monkeypatch.setattr(APIConfig, "BANDWIDTH_LIMIT_MIB_PER_SECOND", 0)

    assert asyncio.run(get_download_limits(request)) == {
        "max_concurrent_downloads": 3,
        "bandwidth_limit_mib_per_second": 0,
    }

    result = asyncio.run(
        set_download_limits(
            DownloadLimitsUpdate(
                max_concurrent_downloads=4,
                bandwidth_limit_mib_per_second=2.5,
            ),
            request,
        )
    )

    assert result == {
        "max_concurrent_downloads": 4,
        "bandwidth_limit_mib_per_second": 2.5,
    }
    assert APIConfig.MAX_CONCURRENT_DOWNLOADS == 4
    assert APIConfig.BANDWIDTH_LIMIT_MIB_PER_SECOND == 2.5
    assert APIConfig.get_download_rate_limit_bytes_per_second() == 655360
    assert started_queue == [True]
    assert DownloadLimitsStore(str(tmp_path / "settings.sqlite3")).get_limits() == result


def test_download_limits_reject_values_outside_supported_range():
    invalid_settings = [
        {"max_concurrent_downloads": 0, "bandwidth_limit_mib_per_second": 0},
        {"max_concurrent_downloads": 17, "bandwidth_limit_mib_per_second": 0},
        {"max_concurrent_downloads": 3, "bandwidth_limit_mib_per_second": -1},
        {"max_concurrent_downloads": 3, "bandwidth_limit_mib_per_second": float("nan")},
        {"max_concurrent_downloads": 3, "bandwidth_limit_mib_per_second": float("inf")},
    ]
    for settings in invalid_settings:
        with pytest.raises(ValidationError):
            DownloadLimitsUpdate(**settings)


def test_download_rate_limit_is_disabled_by_zero_bandwidth_cap(monkeypatch):
    monkeypatch.setattr(APIConfig, "MAX_CONCURRENT_DOWNLOADS", 3)
    monkeypatch.setattr(APIConfig, "BANDWIDTH_LIMIT_MIB_PER_SECOND", 0)

    assert APIConfig.get_download_rate_limit_bytes_per_second() is None


def test_rate_limit_accounts_for_active_tracks_when_concurrency_is_lowered():
    APIConfig.set_download_limits(3, 6)
    with ExitStack() as stack:
        for _ in range(3):
            acquired = stack.enter_context(track_download_limiter.download_slot())
            assert acquired
        APIConfig.set_download_limits(1, 6)
        assert APIConfig.get_download_rate_limit_bytes_per_second() == 2 * 1024 * 1024
    APIConfig.set_download_limits(3, 0)


def test_download_queue_job_limit_is_independent_from_track_limit(
    monkeypatch,
):
    monkeypatch.setattr(APIConfig, "MAX_CONCURRENT_DOWNLOADS", 1)
    monkeypatch.setattr(APIConfig, "MAX_ACTIVE_DOWNLOAD_JOBS", 2)
    monkeypatch.setattr(download_router, "paused", False)
    monkeypatch.setattr(download_router, "storage_protection_service", None)
    monkeypatch.setattr(download_router, "active", set())
    monkeypatch.setattr(download_router, "pending", ["one", "two", "three"])
    monkeypatch.setattr(download_router, "order", ["one", "two", "three"])
    monkeypatch.setattr(
        download_router,
        "tasks",
        {
            task_id: SimpleNamespace(status="queued", storage_blocked=False)
            for task_id in ("one", "two", "three")
        },
    )
    monkeypatch.setattr(
        download_router,
        "requests_by_task",
        {
            task_id: DownloadRequest(
                spotify_url="https://open.spotify.com/track/2kd0T6zgABT8P0s2h9QU5O"
            )
            for task_id in ("one", "two", "three")
        },
    )
    monkeypatch.setattr(download_router, "cancellation_events", {})
    created_coroutines = []

    def capture_task(coroutine):
        created_coroutines.append(coroutine)

    monkeypatch.setattr(download_router.asyncio, "create_task", capture_task)
    try:
        asyncio.run(download_router._try_start_next())
        assert download_router.active == {"one", "two"}
        assert download_router.pending == ["three"]
        assert len(created_coroutines) == 2
    finally:
        for coroutine in created_coroutines:
            coroutine.close()


def test_global_track_download_limiter_caps_concurrent_transfers():
    APIConfig.set_download_limits(2, 0)
    active = 0
    max_active = 0
    lock = Lock()

    def transfer():
        nonlocal active, max_active
        with track_download_limiter.download_slot() as acquired:
            assert acquired
            with lock:
                active += 1
                max_active = max(max_active, active)
            sleep(0.03)
            with lock:
                active -= 1

    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda _: transfer(), range(6)))
        assert max_active == 2

        cancelled = Event()
        cancelled.set()
        with track_download_limiter.download_slot(cancelled) as acquired:
            assert not acquired
    finally:
        APIConfig.set_download_limits(3, 0)


def test_audio_download_options_use_the_shared_bandwidth_cap(monkeypatch):
    monkeypatch.setattr(APIConfig, "MAX_CONCURRENT_DOWNLOADS", 3)
    monkeypatch.setattr(APIConfig, "BANDWIDTH_LIMIT_MIB_PER_SECOND", 6)
    downloader = object.__new__(YouTubeDownloader)
    downloader.logger = logging.getLogger("test-download-limits")
    downloader.rate_limit_provider = APIConfig.get_download_rate_limit_bytes_per_second

    options = downloader._get_ydl_opts(Path("track.m4a"))

    assert options["ratelimit"] == 2 * 1024 * 1024
