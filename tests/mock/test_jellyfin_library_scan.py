import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
from urllib.request import Request

import pytest
from fastapi import HTTPException
from starlette.requests import Request as StarletteRequest

from spotifysaver.api.routers.jellyfin import (
    get_jellyfin_library_scan_settings,
    request_jellyfin_library_scan,
    set_jellyfin_library_scan_settings,
)
from spotifysaver.api.schemas import JellyfinLibraryScanSettingsUpdate
from spotifysaver.api.services import jellyfin_library_scan
from spotifysaver.api.services.jellyfin_library_scan import (
    JellyfinLibraryScanService,
    JellyfinLibraryScanStore,
)


def _request(service):
    app = SimpleNamespace(
        state=SimpleNamespace(jellyfin_library_scan=service)
    )
    return StarletteRequest(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/preferences/jellyfin-library-scan",
            "raw_path": b"/api/v1/preferences/jellyfin-library-scan",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 8000),
            "client": ("testclient", 50000),
            "root_path": "",
            "app": app,
        }
    )


def test_jellyfin_settings_endpoint_does_not_return_api_key(tmp_path):
    store = JellyfinLibraryScanStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    service = JellyfinLibraryScanService(store, is_queue_idle=lambda: True)
    request = _request(service)

    updated = asyncio.run(
        set_jellyfin_library_scan_settings(
            JellyfinLibraryScanSettingsUpdate(
                server_url="http://jellyfin.local:8096",
                api_key="secret-api-key",
                scan_interval_minutes=10,
            ),
            request,
        )
    )
    loaded = asyncio.run(get_jellyfin_library_scan_settings(request))

    assert updated["api_key_configured"] is True
    assert loaded["scan_interval_minutes"] == 10
    assert "api_key" not in loaded
    assert "secret-api-key" not in str(loaded)


def test_jellyfin_settings_endpoint_reports_invalid_server_url(tmp_path):
    store = JellyfinLibraryScanStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    service = JellyfinLibraryScanService(store, is_queue_idle=lambda: True)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            set_jellyfin_library_scan_settings(
                JellyfinLibraryScanSettingsUpdate(server_url="ftp://jellyfin.local"),
                _request(service),
            )
        )

    assert error.value.status_code == 400


def test_manual_scan_endpoint_requires_jellyfin_configuration(tmp_path):
    store = JellyfinLibraryScanStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    service = JellyfinLibraryScanService(store, is_queue_idle=lambda: True)

    with pytest.raises(HTTPException) as error:
        asyncio.run(request_jellyfin_library_scan(_request(service)))

    assert error.value.status_code == 400
    assert "API key" in error.value.detail


def test_jellyfin_settings_persist_without_exposing_api_key(tmp_path):
    database_path = str(tmp_path / "settings.sqlite3")
    store = JellyfinLibraryScanStore(database_path)
    store.initialize()
    service = JellyfinLibraryScanService(store, is_queue_idle=lambda: True)

    settings = service.save_settings(
        "http://jellyfin.local:8096/",
        "secret-api-key",
        clear_api_key=False,
        scan_interval_minutes=15,
    )

    reloaded = JellyfinLibraryScanStore(database_path)
    assert settings["server_url"] == "http://jellyfin.local:8096"
    assert settings["configured"] is True
    assert settings["scan_interval_minutes"] == 15
    assert "api_key" not in settings
    assert "secret-api-key" not in str(settings)
    assert reloaded.get_settings()["api_key"] == "secret-api-key"
    assert reloaded.get_settings()["scan_interval_minutes"] == 15


def test_jellyfin_settings_preserve_or_clear_api_key(tmp_path):
    store = JellyfinLibraryScanStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    service = JellyfinLibraryScanService(store, is_queue_idle=lambda: True)
    service.save_settings(
        "http://jellyfin.local:8096",
        "secret-api-key",
        clear_api_key=False,
        scan_interval_minutes=5,
    )

    service.save_settings(
        "http://jellyfin.local:8096",
        None,
        clear_api_key=False,
        scan_interval_minutes=5,
    )
    assert store.get_settings()["api_key"] == "secret-api-key"

    service.save_settings(
        "http://jellyfin.local:8096",
        None,
        clear_api_key=True,
        scan_interval_minutes=5,
    )
    assert store.get_settings()["api_key"] is None
    assert service.get_status()["configured"] is False


def test_removing_api_key_keeps_other_settings_unchanged(tmp_path):
    store = JellyfinLibraryScanStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    service = JellyfinLibraryScanService(store, is_queue_idle=lambda: True)
    service.save_settings(
        "http://jellyfin.local:8096",
        "secret-api-key",
        clear_api_key=False,
        scan_interval_minutes=12,
    )

    settings = service.remove_api_key()

    assert settings["server_url"] == "http://jellyfin.local:8096"
    assert settings["scan_interval_minutes"] == 12
    assert settings["api_key_configured"] is False
    assert store.get_settings()["api_key"] is None


def test_download_completion_during_scan_leaves_follow_up_scan_pending(tmp_path):
    store = JellyfinLibraryScanStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    store.request_scan()
    generation_at_scan_start = store.get_settings()["scan_generation"]

    store.request_scan()
    store.record_success(datetime.now(timezone.utc).isoformat(), generation_at_scan_start)

    assert store.get_settings()["scan_pending"] is True


def test_existing_jellyfin_settings_database_gets_scan_generation_migration(tmp_path):
    database_path = tmp_path / "settings.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE jellyfin_library_scan_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                server_url TEXT,
                api_key TEXT,
                scan_interval_minutes INTEGER NOT NULL DEFAULT 5,
                scan_pending INTEGER NOT NULL DEFAULT 0,
                last_scan_at TEXT,
                last_attempt_at TEXT,
                last_error TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO jellyfin_library_scan_settings
                (id, server_url, api_key, scan_interval_minutes, scan_pending)
            VALUES (1, 'http://jellyfin.local:8096', 'saved-key', 10, 1)
            """
        )

    store = JellyfinLibraryScanStore(str(database_path))
    store.initialize()

    assert store.get_settings()["scan_generation"] == 0
    assert store.get_settings()["scan_pending"] is True
    assert store.get_settings()["api_key"] == "saved-key"


def test_jellyfin_server_url_without_scheme_defaults_to_http(tmp_path):
    store = JellyfinLibraryScanStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    service = JellyfinLibraryScanService(store, is_queue_idle=lambda: True)

    settings = service.save_settings(
        "192.168.1.10:8096",
        "secret-api-key",
        clear_api_key=False,
        scan_interval_minutes=5,
    )

    assert settings["server_url"] == "http://192.168.1.10:8096"


def test_completed_download_waits_for_idle_queue_then_requests_scan(
    tmp_path, monkeypatch
):
    store = JellyfinLibraryScanStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    idle = [False]
    service = JellyfinLibraryScanService(store, is_queue_idle=lambda: idle[0])
    service.save_settings(
        "http://jellyfin.local:8096",
        "secret-api-key",
        clear_api_key=False,
        scan_interval_minutes=1,
    )
    old_attempt = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    store.record_attempt(old_attempt)
    request_started = Event()
    request_arguments = []

    def fake_request(server_url, api_key):
        request_arguments.append((server_url, api_key))
        request_started.set()

    monkeypatch.setattr(jellyfin_library_scan, "_request_library_scan", fake_request)

    async def run():
        await service.start()
        try:
            await service.queue_changed(content_downloaded=True)
            await asyncio.sleep(0.05)
            assert not request_started.is_set()

            idle[0] = True
            await service.queue_changed(content_downloaded=False)
            completed = await asyncio.to_thread(request_started.wait, 2)
            assert completed
            for _ in range(100):
                if not store.get_settings()["scan_pending"]:
                    break
                await asyncio.sleep(0.01)
            assert store.get_settings()["scan_pending"] is False
        finally:
            await service.stop()

    asyncio.run(run())
    assert request_arguments == [
        ("http://jellyfin.local:8096", "secret-api-key")
    ]


def test_manual_scan_bypasses_cooldown_but_waits_for_idle_queue(
    tmp_path, monkeypatch
):
    store = JellyfinLibraryScanStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    idle = [False]
    service = JellyfinLibraryScanService(store, is_queue_idle=lambda: idle[0])
    service.save_settings(
        "http://jellyfin.local:8096",
        "secret-api-key",
        clear_api_key=False,
        scan_interval_minutes=60,
    )
    store.record_attempt(datetime.now(timezone.utc).isoformat())
    requested = Event()
    monkeypatch.setattr(
        jellyfin_library_scan,
        "_request_library_scan",
        lambda _server_url, _api_key: requested.set(),
    )

    async def run():
        await service.start()
        try:
            status = service.request_manual_scan()
            assert status["manual_scan_pending"] is True
            await asyncio.sleep(0.05)
            assert not requested.is_set()

            idle[0] = True
            await service.queue_changed(content_downloaded=False)
            assert await asyncio.to_thread(requested.wait, 2)
            for _ in range(100):
                if not store.get_settings()["scan_pending"]:
                    break
                await asyncio.sleep(0.01)
            assert store.get_settings()["scan_pending"] is False
        finally:
            await service.stop()

    asyncio.run(run())


def test_library_scan_posts_to_jellyfin_with_api_key_header(monkeypatch):
    captured = {}

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(jellyfin_library_scan, "urlopen", fake_urlopen)

    jellyfin_library_scan._request_library_scan(
        "http://jellyfin.local:8096", "secret-api-key"
    )

    request = captured["request"]
    assert isinstance(request, Request)
    assert request.full_url == "http://jellyfin.local:8096/Library/Refresh"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == (
        'MediaBrowser Token="secret-api-key"'
    )
    assert captured["timeout"] == 20


def test_jellyfin_scan_waits_for_cooldown(tmp_path, monkeypatch):
    store = JellyfinLibraryScanStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    service = JellyfinLibraryScanService(store, is_queue_idle=lambda: True)
    service.save_settings(
        "http://jellyfin.local:8096",
        "secret-api-key",
        clear_api_key=False,
        scan_interval_minutes=5,
    )
    store.record_attempt(datetime.now(timezone.utc).isoformat())
    store.request_scan()
    requested = Event()
    monkeypatch.setattr(
        jellyfin_library_scan,
        "_request_library_scan",
        lambda _server_url, _api_key: requested.set(),
    )

    async def run():
        await service.start()
        try:
            await asyncio.sleep(0.05)
            assert not requested.is_set()
            assert service.get_status()["scan_pending"] is True
        finally:
            await service.stop()

    asyncio.run(run())
