import asyncio
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import spotifysaver.api.routers.download as download_router
import spotifysaver.api.services.storage_protection as storage_module
from spotifysaver.api.app import create_app
from spotifysaver.api.schemas import DownloadRequest, DownloadStatus
from spotifysaver.api.services.notification_hub import NotificationHub
from spotifysaver.api.services.storage_protection import (
    GB,
    StorageProtectionService,
    StorageProtectionStore,
)


class _NotificationRecorder:
    def __init__(self):
        self.events = []

    async def publish(self, category, message, **details):
        self.events.append((category, message, details))


def _service(tmp_path, paths_provider, hub):
    store = StorageProtectionStore(str(tmp_path / "storage.sqlite3"))
    store.initialize()
    return StorageProtectionService(
        store,
        paths_provider,
        hub,
        queue_kick=lambda: asyncio.sleep(0),
    )


def test_storage_reserve_defaults_disabled_and_persists(tmp_path):
    database_path = str(tmp_path / "settings.sqlite3")
    store = StorageProtectionStore(database_path)
    store.initialize()

    assert store.get_minimum_free_bytes() == 0
    store.set_minimum_free_bytes(3 * GB)

    assert StorageProtectionStore(database_path).get_minimum_free_bytes() == 3 * GB


def test_storage_report_groups_destinations_on_the_same_filesystem(tmp_path):
    audio = tmp_path / "audio"
    video = tmp_path / "video"
    audio.mkdir()
    video.mkdir()
    hub = _NotificationRecorder()
    service = _service(
        tmp_path,
        lambda: [
            {"label": "Audio", "path": str(audio)},
            {"label": "Video", "path": str(video)},
        ],
        hub,
    )

    report = service.get_report()

    assert len(report["filesystems"]) == 1
    assert {item["label"] for item in report["filesystems"][0]["paths"]} == {
        "Audio",
        "Video",
    }
    assert report["protection_enabled"] is False


def test_storage_report_separates_distinct_filesystems(tmp_path, monkeypatch):
    audio = tmp_path / "audio"
    video = tmp_path / "video"
    audio.mkdir()
    video.mkdir()
    hub = _NotificationRecorder()
    service = _service(
        tmp_path,
        lambda: [
            {"label": "Audio", "path": str(audio)},
            {"label": "Video", "path": str(video)},
        ],
        hub,
    )
    monkeypatch.setattr(service, "_existing_path", lambda path: Path(path))
    monkeypatch.setattr(
        storage_module.os,
        "stat",
        lambda path: SimpleNamespace(st_dev=1 if Path(path).name == "audio" else 2),
    )
    DiskUsage = namedtuple("DiskUsage", "total used free")
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: DiskUsage(10 * GB, 5 * GB, 5 * GB),
    )

    report = service.get_report()

    assert len(report["filesystems"]) == 2
    assert [item["paths"][0]["label"] for item in report["filesystems"]] == [
        "Audio",
        "Video",
    ]


def test_low_storage_blocks_destination_and_notifies_once_per_filesystem(tmp_path, monkeypatch):
    DiskUsage = namedtuple("DiskUsage", "total used free")
    available_bytes = {"free": GB // 2}
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: DiskUsage(
            10 * GB, 10 * GB - available_bytes["free"], available_bytes["free"]
        ),
    )
    destination = tmp_path / "video"
    destination.mkdir()
    hub = _NotificationRecorder()
    service = _service(tmp_path, lambda: [{"label": "Video", "path": str(destination)}], hub)
    service.store.set_minimum_free_bytes(2 * GB)

    first = asyncio.run(service.check_destination(str(destination)))
    second = asyncio.run(service.check_destination(str(destination)))

    assert first[0] is True
    assert "0.50 GB available" in first[1]
    assert second == first
    assert len(hub.events) == 1
    assert hub.events[0][0] == "low_storage"

    available_bytes["free"] = 3 * GB
    assert asyncio.run(service.check_destination(str(destination))) == (False, None)
    available_bytes["free"] = GB // 2
    asyncio.run(service.check_destination(str(destination)))
    assert len(hub.events) == 2


def test_zero_reserve_skips_storage_check(tmp_path, monkeypatch):
    hub = _NotificationRecorder()
    service = _service(tmp_path, lambda: [], hub)

    def unexpected_disk_usage(_path):
        raise AssertionError("disabled storage protection should skip disk checks")

    monkeypatch.setattr(storage_module.shutil, "disk_usage", unexpected_disk_usage)

    assert asyncio.run(service.check_destination(str(tmp_path))) == (False, None)
    assert hub.events == []


def test_download_queue_skips_blocked_destination_for_available_storage(tmp_path, monkeypatch):
    blocked_path = str(tmp_path / "blocked")
    available_path = str(tmp_path / "available")
    Path(blocked_path).mkdir()
    Path(available_path).mkdir()

    monkeypatch.setattr(download_router, "tasks", {})
    monkeypatch.setattr(download_router, "requests_by_task", {})
    monkeypatch.setattr(download_router, "cancellation_events", {})
    monkeypatch.setattr(download_router, "order", ["blocked-task", "available-task"])
    monkeypatch.setattr(download_router, "pending", ["blocked-task", "available-task"])
    monkeypatch.setattr(download_router, "active", set())
    monkeypatch.setattr(download_router, "paused", False)
    monkeypatch.setattr(download_router, "queue_lock", asyncio.Lock())
    monkeypatch.setattr(download_router, "storage_protection_service", None)

    download_router.tasks.update(
        {
            task_id: DownloadStatus(task_id=task_id, status="queued", progress=0)
            for task_id in ("blocked-task", "available-task")
        }
    )
    download_router.requests_by_task.update(
        {
            "blocked-task": DownloadRequest(
                spotify_url="https://open.spotify.com/track/1",
                output_dir=blocked_path,
            ),
            "available-task": DownloadRequest(
                spotify_url="https://open.spotify.com/track/2",
                output_dir=available_path,
            ),
        }
    )

    class _Preflight:
        async def check_destination(self, path):
            if path == blocked_path:
                return True, "Waiting for free space."
            return False, None

    async def fake_run_task(task_id, _request):
        return None

    started = []

    async def record_task(task_id, request):
        started.append(task_id)
        await fake_run_task(task_id, request)

    monkeypatch.setattr(download_router, "storage_protection_service", _Preflight())
    monkeypatch.setattr(download_router, "_run_task", record_task)

    async def exercise_queue():
        await download_router._try_start_next()
        await asyncio.sleep(0)

    try:
        asyncio.run(exercise_queue())
        assert download_router.pending == ["blocked-task"]
        assert download_router.tasks["blocked-task"].storage_blocked is True
        assert download_router.tasks["blocked-task"].error_message == ("Waiting for free space.")
        assert started == ["available-task"]
        assert "available-task" in download_router.active
    finally:
        download_router.active.clear()
        download_router.APIConfig.set_active_downloads(0)


def test_app_lifespan_initializes_storage_service_in_configured_database(tmp_path, monkeypatch):
    database_path = str(tmp_path / "application.sqlite3")
    monkeypatch.setenv("SPOTIFYSAVER_CHANNEL_WATCHER_DB", database_path)
    monkeypatch.setenv("SPOTIFYSAVER_SECURITY_DB", str(tmp_path / "security.sqlite3"))

    async def exercise_lifespan():
        app = create_app()
        async with app.router.lifespan_context(app):
            report = app.state.storage_protection.get_report()
            assert report["minimum_free_space_gb"] == 0
            assert report["protection_enabled"] is False
            assert {item["label"] for item in report["filesystems"][0]["paths"]} >= {
                "Audio downloads",
                "Video downloads",
                "Channel Watcher default",
            }

    asyncio.run(exercise_lifespan())
    assert StorageProtectionStore(database_path).get_minimum_free_bytes() == 0
