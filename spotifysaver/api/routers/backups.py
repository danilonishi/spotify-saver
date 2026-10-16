"""Configuration backup and restore endpoints."""

from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal, Optional
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ...config import Config
from ..config import APIConfig
from ..services.channel_watcher import (
    _content_filter_settings,
    _get_timezone,
    _normalize_channel_url,
)
from ..services.jellyfin_library_scan import _validate_server_url
from ..services.security_store import SecurityStore
from ..services.storage_protection import GB
from ..services.youtube_confirmation_store import YouTubeConfirmationStore
from .jellyfin import _service as _jellyfin_service
from .spotify_settings import (
    CALLBACK_PATH,
    _apply_spotify_settings,
    _store as _security_store,
    _validate_redirect_uri,
)

router = APIRouter()


class BackupChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    platform: Literal["youtube"]
    url: str = Field(min_length=1, max_length=2048)
    title: Optional[str] = Field(default=None, max_length=500)
    output_dir: str = Field(min_length=1, max_length=4096)
    interval: Literal["manual", "hourly", "daily", "weekly"]
    timezone: str = Field(min_length=1, max_length=128)
    check_hour: int = Field(ge=0, le=23)
    check_minute: int = Field(ge=0, le=59)
    check_weekday: Optional[int] = Field(default=None, ge=0, le=6)
    video_format: Literal["mp4", "mkv", "webm"]
    video_resolution: int = Field(ge=0, le=4320)
    include_subtitles: bool
    include_keywords: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list, max_length=20
    )
    exclude_keywords: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list, max_length=20
    )
    min_duration_seconds: Optional[int] = Field(default=None, ge=0, le=86400)
    max_duration_seconds: Optional[int] = Field(default=None, ge=0, le=86400)
    published_within_days: Optional[int] = Field(default=None, ge=1, le=36500)
    published_after: Optional[date] = None
    published_before: Optional[date] = None
    exclude_shorts: bool = False
    exclude_livestreams: bool = False
    active: bool = True
    created_at: datetime
    last_checked_at: Optional[datetime] = None
    discovery_cursor_video_id: Optional[str] = Field(default=None, max_length=256)
    discovery_initialized: bool = False
    next_check_at: datetime


class DownloadDirectoriesBackup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audio: Optional[str] = Field(default=None, max_length=4096)
    video: Optional[str] = Field(default=None, max_length=4096)
    watcher: Optional[str] = Field(default=None, max_length=4096)


class DownloadLimitsBackup(BaseModel):
    max_concurrent_downloads: int = Field(default=3, ge=1, le=16)
    bandwidth_limit_mib_per_second: float = Field(default=0, ge=0, le=10000)


class YouTubeConfirmationBackup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_matched_downloads: bool = False
    confirm_channel_watcher_downloads: bool = False


class JellyfinBackup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_url: Optional[str] = Field(default=None, max_length=2048)
    scan_interval_minutes: int = Field(ge=1, le=1440)


class SpotifyBackup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(default="", max_length=128)
    redirect_uri: str = Field(default="", max_length=2048)


class ServerConfigurationBackup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watched_channels: list[BackupChannel] = Field(max_length=1000)
    download_directories: DownloadDirectoriesBackup
    download_limits: DownloadLimitsBackup = Field(
        default_factory=DownloadLimitsBackup
    )
    youtube_confirmation: YouTubeConfirmationBackup = Field(
        default_factory=YouTubeConfirmationBackup
    )
    jellyfin: JellyfinBackup
    spotify: SpotifyBackup
    minimum_free_space_gb: float = Field(default=0, ge=0, le=100000)


class BrowserPreferencesBackup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dark_mode: Optional[bool] = None
    browser_notifications_enabled: Optional[bool] = None
    notification_preferences: dict[str, bool] = Field(default_factory=dict, max_length=16)
    panel_visibility: dict[str, bool] = Field(default_factory=dict, max_length=32)
    panel_open: dict[str, bool] = Field(default_factory=dict, max_length=32)
    panel_layout: dict[str, list[str]] = Field(default_factory=dict, max_length=2)


class ConfigurationBackup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    created_at: datetime
    server: ServerConfigurationBackup
    browser_preferences: BrowserPreferencesBackup = Field(
        default_factory=BrowserPreferencesBackup
    )


def _configuration(request: Request) -> ServerConfigurationBackup:
    watcher = request.app.state.channel_watcher
    directory_store = request.app.state.download_directory_store
    download_limits_store = request.app.state.download_limits_store
    youtube_confirmation_store: YouTubeConfirmationStore = (
        request.app.state.youtube_confirmation_store
    )
    storage_protection = request.app.state.storage_protection
    jellyfin = _jellyfin_service(request).get_status()
    stored_spotify = _security_store(request).get_spotify_settings()
    spotify_client_id = (
        stored_spotify["client_id"]
        if stored_spotify
        else (Config.SPOTIFY_CLIENT_ID or "")
    )
    spotify_redirect_uri = (
        stored_spotify["redirect_uri"]
        if stored_spotify
        else (Config.SPOTIFY_REDIRECT_URI or "")
    )
    if spotify_redirect_uri and urlsplit(spotify_redirect_uri).path != CALLBACK_PATH:
        spotify_redirect_uri = ""

    channels = []
    for channel in watcher.store.list_channels():
        channels.append(
            BackupChannel.model_validate(
                {
                    key: channel.get(key)
                    for key in BackupChannel.model_fields
                }
            )
        )

    return ServerConfigurationBackup(
        watched_channels=channels,
        download_directories=directory_store.get_download_directories(),
        download_limits=download_limits_store.get_limits(),
        youtube_confirmation=youtube_confirmation_store.get_preferences(),
        jellyfin={
            "server_url": jellyfin["server_url"],
            "scan_interval_minutes": jellyfin["scan_interval_minutes"],
        },
        spotify={
            "client_id": spotify_client_id,
            "redirect_uri": spotify_redirect_uri,
        },
        minimum_free_space_gb=round(
            storage_protection.store.get_minimum_free_bytes() / GB, 2
        ),
    )


@router.get("/configuration-backup")
async def download_configuration_backup(request: Request):
    """Return portable settings only; credentials and tokens are never exported."""
    return {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "server": _configuration(request).model_dump(mode="json"),
    }


def _validate_server_configuration(
    configuration: ServerConfigurationBackup,
) -> None:
    channel_ids: set[UUID] = set()
    normalized_channels: set[tuple[str, str]] = set()
    for channel in configuration.watched_channels:
        if channel.id in channel_ids:
            raise ValueError("Backup contains duplicate watched channel IDs")
        channel_ids.add(channel.id)
        identity = (channel.platform, _normalize_channel_url(channel.url))
        if identity in normalized_channels:
            raise ValueError("Backup contains duplicate watched channels")
        normalized_channels.add(identity)
        _get_timezone(channel.timezone)
        if channel.interval == "weekly" and channel.check_weekday is None:
            raise ValueError("Weekly watched channels require a weekday")
        _content_filter_settings(channel.model_dump(mode="python"))

    if configuration.spotify.redirect_uri:
        _validate_redirect_uri(configuration.spotify.redirect_uri)


@router.post("/configuration-backup/restore")
async def restore_configuration_backup(
    backup: ConfigurationBackup, request: Request
):
    """Replace backed-up settings without importing source-instance secrets."""
    try:
        _validate_server_configuration(backup.server)
        if backup.server.jellyfin.server_url:
            _validate_server_url(backup.server.jellyfin.server_url)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    watcher = request.app.state.channel_watcher
    security_store: SecurityStore = _security_store(request)
    jellyfin_service = _jellyfin_service(request)
    directory_store = request.app.state.download_directory_store
    download_limits_store = request.app.state.download_limits_store
    youtube_confirmation_store: YouTubeConfirmationStore = (
        request.app.state.youtube_confirmation_store
    )
    storage_protection = request.app.state.storage_protection

    current_spotify = security_store.get_spotify_settings()
    destination_secret = (
        current_spotify["client_secret"]
        if current_spotify
        else (Config.SPOTIFY_CLIENT_SECRET or "")
    )
    previous_spotify = {
        "client_id": Config.SPOTIFY_CLIENT_ID or "",
        "redirect_uri": Config.SPOTIFY_REDIRECT_URI or "",
    }
    imported_spotify = {
        "client_id": backup.server.spotify.client_id,
        "client_secret": destination_secret,
        "redirect_uri": backup.server.spotify.redirect_uri,
    }

    await watcher.stop()
    try:
        watcher.store.replace_channels(
            [
                channel.model_dump(mode="json")
                for channel in backup.server.watched_channels
            ]
        )
        directory_store.restore_download_directories(
            backup.server.download_directories.model_dump()
        )
        limits = download_limits_store.set_limits(
            backup.server.download_limits.max_concurrent_downloads,
            backup.server.download_limits.bandwidth_limit_mib_per_second,
        )
        APIConfig.set_download_limits(
            limits["max_concurrent_downloads"],
            limits["bandwidth_limit_mib_per_second"],
        )
        confirmation_preferences = youtube_confirmation_store.set_preferences(
            backup.server.youtube_confirmation.confirm_matched_downloads,
            backup.server.youtube_confirmation.confirm_channel_watcher_downloads,
        )
        APIConfig.set_youtube_confirmation_preferences(
            confirmation_preferences["confirm_matched_downloads"],
            confirmation_preferences["confirm_channel_watcher_downloads"],
        )
        storage_protection.store.set_minimum_free_bytes(
            round(backup.server.minimum_free_space_gb * GB)
        )
        jellyfin_service.save_settings(
            server_url=backup.server.jellyfin.server_url or "",
            api_key=None,
            clear_api_key=False,
            scan_interval_minutes=backup.server.jellyfin.scan_interval_minutes,
        )
        security_store.save_spotify_settings(**imported_spotify)
        if (
            previous_spotify["client_id"] != imported_spotify["client_id"]
            or previous_spotify["redirect_uri"] != imported_spotify["redirect_uri"]
        ):
            cache_path = Path(Config.SPOTIFY_AUTH_CACHE_PATH).expanduser()
            if cache_path.exists():
                cache_path.unlink()
        _apply_spotify_settings(imported_spotify)
    finally:
        watcher.start()

    await storage_protection.refresh()
    await storage_protection.queue_kick()
    return {"restored": True}
