"""Endpoints for server-wide download-folder preferences."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import APIConfig
from ..schemas import DownloadDirectoryPreferenceUpdate
from ..services.download_directory_store import DownloadDirectoryStore
from ..services.download_limits_store import DownloadLimitsStore
from ..services.youtube_confirmation_store import YouTubeConfirmationStore
from . import download as download_router

router = APIRouter()


class DownloadLimitsUpdate(BaseModel):
    max_concurrent_downloads: int = Field(ge=1, le=16)
    bandwidth_limit_mib_per_second: float = Field(ge=0, le=10000)


class YouTubeConfirmationUpdate(BaseModel):
    confirm_matched_downloads: bool
    confirm_channel_watcher_downloads: bool


def _store(request: Request) -> DownloadDirectoryStore:
    store: Optional[DownloadDirectoryStore] = getattr(
        request.app.state, "download_directory_store", None
    )
    if store is None:
        raise HTTPException(
            status_code=503, detail="Download-folder preferences are unavailable"
        )
    return store


def _download_limits_store(request: Request) -> DownloadLimitsStore:
    store: Optional[DownloadLimitsStore] = getattr(
        request.app.state, "download_limits_store", None
    )
    if store is None:
        raise HTTPException(
            status_code=503, detail="Download limits are unavailable"
        )
    return store


def _youtube_confirmation_store(request: Request) -> YouTubeConfirmationStore:
    store: Optional[YouTubeConfirmationStore] = getattr(
        request.app.state, "youtube_confirmation_store", None
    )
    if store is None:
        raise HTTPException(
            status_code=503, detail="YouTube confirmation preferences are unavailable"
        )
    return store


@router.get("/preferences/download-directories")
async def get_download_directory_preferences(request: Request):
    """Return the server-wide audio, video, and Channel Watcher folders."""
    return _store(request).get_download_directories()


@router.put("/preferences/download-directories/{kind}")
async def set_download_directory_preference(
    kind: str,
    settings: DownloadDirectoryPreferenceUpdate,
    request: Request,
):
    """Persist one server-wide audio, video, or Channel Watcher folder."""
    if kind not in {"audio", "video", "watcher"}:
        raise HTTPException(status_code=400, detail="Unknown download-folder preference")
    directory_value = settings.directory.strip()
    if not directory_value:
        raise HTTPException(status_code=400, detail="A download folder must be provided")
    directory = Path(directory_value).expanduser().resolve()
    if not directory.is_dir():
        raise HTTPException(
            status_code=400,
            detail="The preferred download folder must be an existing directory",
        )
    return _store(request).set_download_directory(kind, str(directory))


@router.get("/preferences/download-limits")
async def get_download_limits(request: Request):
    """Return server-wide download concurrency and bandwidth limits."""
    return _download_limits_store(request).get_limits()


@router.put("/preferences/download-limits")
async def set_download_limits(settings: DownloadLimitsUpdate, request: Request):
    """Persist download limits and apply them to the running server queue."""
    limits = _download_limits_store(request).set_limits(
        settings.max_concurrent_downloads,
        settings.bandwidth_limit_mib_per_second,
    )
    APIConfig.set_download_limits(
        limits["max_concurrent_downloads"],
        limits["bandwidth_limit_mib_per_second"],
    )
    await download_router._try_start_next()
    return limits


@router.get("/preferences/youtube-confirmation")
async def get_youtube_confirmation_preferences(request: Request):
    """Return whether automatically matched YouTube downloads need approval."""
    return _youtube_confirmation_store(request).get_preferences()


@router.put("/preferences/youtube-confirmation")
async def set_youtube_confirmation_preferences(
    settings: YouTubeConfirmationUpdate, request: Request
):
    """Persist YouTube confirmation settings and apply them to new downloads."""
    preferences = _youtube_confirmation_store(request).set_preferences(
        settings.confirm_matched_downloads,
        settings.confirm_channel_watcher_downloads,
    )
    APIConfig.set_youtube_confirmation_preferences(
        preferences["confirm_matched_downloads"],
        preferences["confirm_channel_watcher_downloads"],
    )
    return preferences
