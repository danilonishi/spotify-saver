import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from starlette.requests import Request

from spotifysaver.api.app import create_app
from spotifysaver.api.config import APIConfig
from spotifysaver.api.routers.backups import (
    ConfigurationBackup,
    download_configuration_backup,
    restore_configuration_backup,
)
from spotifysaver.api.services.channel_watcher import ChannelWatcherStore
from spotifysaver.api.services.download_directory_store import DownloadDirectoryStore
from spotifysaver.api.services.download_limits_store import DownloadLimitsStore
from spotifysaver.api.services.youtube_confirmation_store import (
    YouTubeConfirmationStore,
)
from spotifysaver.api.services.jellyfin_library_scan import (
    JellyfinLibraryScanService,
    JellyfinLibraryScanStore,
)
from spotifysaver.api.services.security_store import SecurityStore
from spotifysaver.api.services.notification_hub import NotificationHub
from spotifysaver.api.services.storage_protection import (
    GB,
    StorageProtectionService,
    StorageProtectionStore,
)
from spotifysaver.config import Config


class _StoppedWatcher:
    def __init__(self, store):
        self.store = store
        self.stopped = False
        self.started = False

    async def stop(self):
        self.stopped = True

    def start(self):
        self.started = True


def _request(state):
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/configuration-backup",
            "raw_path": b"/api/v1/configuration-backup",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 8000),
            "client": ("127.0.0.1", 50000),
            "root_path": "",
            "app": SimpleNamespace(state=state),
        }
    )


def _stores(tmp_path, label):
    database_path = str(tmp_path / f"{label}.sqlite3")
    channel_store = ChannelWatcherStore(database_path)
    channel_store.initialize()
    directory_store = DownloadDirectoryStore(database_path)
    directory_store.initialize()
    download_limits_store = DownloadLimitsStore(database_path)
    download_limits_store.initialize()
    jellyfin_store = JellyfinLibraryScanStore(database_path)
    jellyfin_store.initialize()
    security_store = SecurityStore(str(tmp_path / f"{label}-security.sqlite3"))
    security_store.initialize()
    return (
        channel_store,
        directory_store,
        download_limits_store,
        jellyfin_store,
        security_store,
    )


def _state(tmp_path, label):
    database_path = str(tmp_path / f"{label}.sqlite3")
    (
        channel_store,
        directory_store,
        download_limits_store,
        jellyfin_store,
        security_store,
    ) = _stores(
        tmp_path, label
    )
    storage_store = StorageProtectionStore(database_path)
    storage_store.initialize()
    youtube_confirmation_store = YouTubeConfirmationStore(database_path)
    youtube_confirmation_store.initialize()

    async def queue_kick():
        return None

    storage_protection = StorageProtectionService(
        storage_store,
        paths_provider=lambda: [],
        notification_hub=NotificationHub(),
        queue_kick=queue_kick,
    )
    watcher = _StoppedWatcher(channel_store)
    jellyfin = JellyfinLibraryScanService(jellyfin_store, is_queue_idle=lambda: True)
    return (
        SimpleNamespace(
            channel_watcher=watcher,
            download_directory_store=directory_store,
            download_limits_store=download_limits_store,
            youtube_confirmation_store=youtube_confirmation_store,
            jellyfin_library_scan=jellyfin,
            security_store=security_store,
            storage_protection=storage_protection,
        ),
        channel_store,
        directory_store,
        jellyfin_store,
        security_store,
    )


async def _asgi_request(app, method, path, payload=None, cookie=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    request_received = False
    messages = []

    async def receive():
        nonlocal request_received
        if not request_received:
            request_received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    headers = [(b"host", b"testserver")]
    if payload is not None:
        headers.append((b"content-type", b"application/json"))
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "server": ("testserver", 8000),
            "client": ("127.0.0.1", 50000),
            "root_path": "",
        },
        receive,
        send,
    )
    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return response_start["status"], json.loads(response_body)


def _http_app(tmp_path, label):
    app = create_app()
    state, channel_store, directory_store, jellyfin_store, security_store = _state(
        tmp_path, label
    )
    app.state.channel_watcher = state.channel_watcher
    app.state.download_directory_store = directory_store
    app.state.download_limits_store = state.download_limits_store
    app.state.youtube_confirmation_store = state.youtube_confirmation_store
    app.state.jellyfin_library_scan = state.jellyfin_library_scan
    app.state.security_store = security_store
    app.state.storage_protection = state.storage_protection
    setup_token = security_store.setup_token
    assert setup_token is not None
    assert security_store.complete_setup(setup_token, "integration-test-password")
    token, _ = security_store.create_session()
    return (
        app,
        channel_store,
        directory_store,
        jellyfin_store,
        security_store,
        f"spotifysaver_session={token}",
    )


def test_configuration_backup_excludes_all_credentials(tmp_path):
    state, channel_store, _, _, security_store = _state(tmp_path, "source")
    security_store.save_spotify_settings(
        "public-client-id",
        "spotify-client-secret",
        "https://spotify.example/api/v1/spotify/callback",
    )
    state.jellyfin_library_scan.save_settings(
        "http://jellyfin.local:8096",
        "jellyfin-api-key",
        clear_api_key=False,
        scan_interval_minutes=15,
    )

    backup = asyncio.run(download_configuration_backup(_request(state)))
    serialized = str(backup)

    assert backup["schema_version"] == 1
    assert backup["server"]["spotify"]["client_id"] == "public-client-id"
    assert "spotify-client-secret" not in serialized
    assert "jellyfin-api-key" not in serialized
    assert "api_key" not in serialized
    assert "client_secret" not in serialized
    assert channel_store.list_channels() == []


def test_restore_replaces_configuration_and_preserves_destination_secrets(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(APIConfig, "MAX_CONCURRENT_DOWNLOADS", 3)
    monkeypatch.setattr(APIConfig, "BANDWIDTH_LIMIT_MIB_PER_SECOND", 0)
    monkeypatch.setattr(APIConfig, "ACTIVE_DOWNLOADS", 0)
    source, source_channels, source_dirs, _, source_security = _state(
        tmp_path, "source"
    )
    target, target_channels, target_dirs, target_jellyfin, target_security = _state(
        tmp_path, "target"
    )
    source_security.save_spotify_settings(
        "source-client-id",
        "source-client-secret",
        "https://source.example/api/v1/spotify/callback",
    )
    target_security.save_spotify_settings(
        "target-client-id",
        "target-client-secret",
        "https://target.example/api/v1/spotify/callback",
    )
    target.jellyfin_library_scan.save_settings(
        "http://old-jellyfin.local:8096",
        "target-jellyfin-key",
        clear_api_key=False,
        scan_interval_minutes=5,
    )

    channel = source_channels.create_channel(
        {
            "platform": "youtube",
            "url": "https://www.youtube.com/@example",
            "output_dir": str(tmp_path),
            "interval": "daily",
            "timezone": "UTC",
            "check_hour": 9,
            "check_minute": 15,
            "check_weekday": None,
            "video_format": "mp4",
            "video_resolution": 1080,
            "include_subtitles": False,
        },
        datetime.now(timezone.utc),
    )
    with source_channels._connect() as connection:
        connection.execute(
            """
            UPDATE watched_channels
            SET discovery_cursor_video_id = ?, discovery_initialized = 1
            WHERE id = ?
            """,
            ("cursor-id", channel["id"]),
        )
    source_dirs.restore_download_directories(
        {
            "audio": str(tmp_path / "audio"),
            "video": str(tmp_path / "video"),
            "watcher": str(tmp_path / "watcher"),
        }
    )
    source.download_limits_store.set_limits(5, 1.5)
    for directory in ("audio", "video", "watcher"):
        (tmp_path / directory).mkdir()
    source.jellyfin_library_scan.save_settings(
        "https://source-jellyfin.example",
        None,
        clear_api_key=False,
        scan_interval_minutes=20,
    )
    source.youtube_confirmation_store.set_preferences(True, True)
    source_backup = asyncio.run(download_configuration_backup(_request(source)))
    cache_path = tmp_path / ".spotify-cache"
    cache_path.write_text("temporary authorization cache")
    monkeypatch.setattr(Config, "SPOTIFY_AUTH_CACHE_PATH", str(cache_path))
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_ID", "target-client-id")
    monkeypatch.setattr(
        Config,
        "SPOTIFY_REDIRECT_URI",
        "https://target.example/api/v1/spotify/callback",
    )
    backup = {
        **source_backup,
        "browser_preferences": {
            "panel_visibility": {"download-queue-panel": False},
            "panel_open": {"download-queue-panel": False},
            "panel_layout": {
                "left": ["download-settings-panel"],
                "right": ["download-queue-panel"],
            },
        },
    }
    target_request = _request(target)

    result = asyncio.run(
        restore_configuration_backup(
            ConfigurationBackup.model_validate(backup), target_request
        )
    )

    assert result == {"restored": True}
    assert target.channel_watcher.stopped and target.channel_watcher.started
    restored_channels = target_channels.list_channels()
    assert len(restored_channels) == 1
    assert restored_channels[0]["id"] == channel["id"]
    assert restored_channels[0]["discovery_cursor_video_id"] == "cursor-id"
    assert bool(restored_channels[0]["discovery_initialized"])
    assert target_dirs.get_download_directories() == {
        "audio": str(tmp_path / "audio"),
        "video": str(tmp_path / "video"),
        "watcher": str(tmp_path / "watcher"),
    }
    assert target.download_limits_store.get_limits() == {
        "max_concurrent_downloads": 5,
        "bandwidth_limit_mib_per_second": 1.5,
    }
    assert target.youtube_confirmation_store.get_preferences() == {
        "confirm_matched_downloads": True,
        "confirm_channel_watcher_downloads": True,
    }
    assert (
        target_jellyfin.get_settings()["server_url"]
        == "https://source-jellyfin.example"
    )
    assert target_jellyfin.get_settings()["scan_interval_minutes"] == 20
    assert target_jellyfin.get_settings()["api_key"] == "target-jellyfin-key"
    restored_spotify = target_security.get_spotify_settings()
    assert restored_spotify["client_id"] == "source-client-id"
    assert restored_spotify["client_secret"] == "target-client-secret"
    assert (
        restored_spotify["redirect_uri"]
        == "https://source.example/api/v1/spotify/callback"
    )
    assert not cache_path.exists()


def test_configuration_backup_http_round_trip_restores_changed_settings(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(APIConfig, "MAX_CONCURRENT_DOWNLOADS", 3)
    monkeypatch.setattr(APIConfig, "BANDWIDTH_LIMIT_MIB_PER_SECOND", 0)
    monkeypatch.setattr(APIConfig, "ACTIVE_DOWNLOADS", 0)
    source_app, source_channels, source_dirs, _, source_security, source_cookie = (
        _http_app(tmp_path, "http-source")
    )
    (
        target_app,
        target_channels,
        target_dirs,
        target_jellyfin,
        target_security,
        target_cookie,
    ) = _http_app(tmp_path, "http-target")

    source_security.save_spotify_settings(
        "source-client-id",
        "source-client-secret",
        "https://source.example/api/v1/spotify/callback",
    )
    source_app.state.jellyfin_library_scan.save_settings(
        "https://source-jellyfin.example",
        "source-jellyfin-key",
        clear_api_key=False,
        scan_interval_minutes=20,
    )
    source_channels.create_channel(
        {
            "platform": "youtube",
            "url": "https://www.youtube.com/@roundtrip",
            "output_dir": str(tmp_path / "source-videos"),
            "interval": "daily",
            "timezone": "Europe/Paris",
            "check_hour": 8,
            "check_minute": 25,
            "check_weekday": None,
            "video_format": "mkv",
            "video_resolution": 720,
            "include_subtitles": True,
        },
        datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc),
    )
    source_dirs.restore_download_directories(
        {
            "audio": str(tmp_path / "source-audio"),
            "video": str(tmp_path / "source-video"),
            "watcher": str(tmp_path / "source-watcher"),
        }
    )
    source_app.state.download_limits_store.set_limits(5, 1.5)
    source_app.state.youtube_confirmation_store.set_preferences(True, True)
    source_app.state.storage_protection.store.set_minimum_free_bytes(
        round(2.5 * GB)
    )

    target_security.save_spotify_settings(
        "target-client-id",
        "target-client-secret",
        "https://target.example/api/v1/spotify/callback",
    )
    target_app.state.jellyfin_library_scan.save_settings(
        "https://target-jellyfin.example",
        "target-jellyfin-key",
        clear_api_key=False,
        scan_interval_minutes=5,
    )
    target_channels.create_channel(
        {
            "platform": "youtube",
            "url": "https://www.youtube.com/@old-target",
            "output_dir": str(tmp_path / "target-videos"),
            "interval": "hourly",
            "timezone": "UTC",
            "check_hour": 0,
            "check_minute": 0,
            "check_weekday": None,
            "video_format": "mp4",
            "video_resolution": 1080,
            "include_subtitles": False,
        },
        datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc),
    )
    target_dirs.restore_download_directories(
        {
            "audio": str(tmp_path / "target-audio"),
            "video": str(tmp_path / "target-video"),
            "watcher": str(tmp_path / "target-watcher"),
        }
    )
    target_app.state.storage_protection.store.set_minimum_free_bytes(GB)

    auth_cache = tmp_path / "http-test-spotify-cache"
    auth_cache.write_text("test-only OAuth state", encoding="utf-8")
    monkeypatch.setattr(Config, "SPOTIFY_AUTH_CACHE_PATH", str(auth_cache))
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_ID", "target-client-id")
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_SECRET", "target-client-secret")
    monkeypatch.setattr(
        Config,
        "SPOTIFY_REDIRECT_URI",
        "https://target.example/api/v1/spotify/callback",
    )

    status, original_backup = asyncio.run(
        _asgi_request(
            source_app,
            "GET",
            "/api/v1/configuration-backup",
            cookie=source_cookie,
        )
    )
    assert status == 200
    status, modified_backup = asyncio.run(
        _asgi_request(
            target_app,
            "GET",
            "/api/v1/configuration-backup",
            cookie=target_cookie,
        )
    )
    assert status == 200
    assert modified_backup["server"] != original_backup["server"]
    assert original_backup["server"]["minimum_free_space_gb"] == 2.5

    status, result = asyncio.run(
        _asgi_request(
            target_app,
            "POST",
            "/api/v1/configuration-backup/restore",
            payload=original_backup,
            cookie=target_cookie,
        )
    )
    assert status == 200
    assert result == {"restored": True}

    status, restored_backup = asyncio.run(
        _asgi_request(
            target_app,
            "GET",
            "/api/v1/configuration-backup",
            cookie=target_cookie,
        )
    )
    assert status == 200
    assert restored_backup["server"] == original_backup["server"]
    backup_with_browser_preferences = {
        **original_backup,
        "browser_preferences": {
            "dark_mode": True,
            "browser_notifications_enabled": False,
            "notification_preferences": {
                "new_content": False,
                "check_failed": True,
                "download_attention": False,
                "download_confirmation": True,
                "low_storage": False,
            },
            "panel_visibility": {},
            "panel_open": {},
            "panel_layout": {},
        },
    }
    validated_backup = ConfigurationBackup.model_validate(
        backup_with_browser_preferences
    )
    assert validated_backup.browser_preferences.dark_mode is True
    assert (
        validated_backup.browser_preferences.browser_notifications_enabled is False
    )
    assert validated_backup.browser_preferences.notification_preferences == {
        "new_content": False,
        "check_failed": True,
        "download_attention": False,
        "download_confirmation": True,
        "low_storage": False,
    }
    assert target_app.state.storage_protection.store.get_minimum_free_bytes() == (
        round(2.5 * GB)
    )
    assert target_security.get_spotify_settings()["client_secret"] == (
        "target-client-secret"
    )
    assert target_jellyfin.get_settings()["api_key"] == "target-jellyfin-key"
    assert not auth_cache.exists()
