"""Endpoints for server-wide download-folder preferences."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..schemas import DownloadDirectoryPreferenceUpdate
from ..services.download_directory_store import DownloadDirectoryStore

router = APIRouter()


def _store(request: Request) -> DownloadDirectoryStore:
    store: Optional[DownloadDirectoryStore] = getattr(
        request.app.state, "download_directory_store", None
    )
    if store is None:
        raise HTTPException(
            status_code=503, detail="Download-folder preferences are unavailable"
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
