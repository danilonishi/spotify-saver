"""Endpoints for managing watched channels."""

import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..schemas import ChannelWatcherStatus, WatchedChannel, WatchedChannelRequest
from ..services.channel_watcher import ChannelWatcherService, utc_now

router = APIRouter()


def _service(request: Request) -> ChannelWatcherService:
    service = getattr(request.app.state, "channel_watcher", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Channel watcher is unavailable")
    return service


def _validate_output_dir(output_dir: str) -> str:
    path = Path(output_dir).expanduser().resolve()
    if not path.is_dir():
        raise HTTPException(
            status_code=400,
            detail="The watched channel download folder must be an existing directory",
        )
    return str(path)


@router.get("/channel-watcher", response_model=list[WatchedChannel])
async def list_watched_channels(request: Request):
    """Return the server's persisted watched channel list."""
    return _service(request).store.list_channels()


@router.get("/channel-watcher/status", response_model=ChannelWatcherStatus)
async def get_channel_watcher_status(request: Request):
    """Return global watcher state, including any active YouTube cooldown."""
    return {"youtube_cooldown_until": _service(request).store.get_youtube_cooldown_until()}


@router.post("/channel-watcher", response_model=WatchedChannel, status_code=201)
async def create_watched_channel(settings: WatchedChannelRequest, request: Request):
    """Start watching a channel using its own download and schedule settings."""
    try:
        values = settings.model_dump()
        values["output_dir"] = _validate_output_dir(values["output_dir"])
        return _service(request).store.create_channel(values, utc_now())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except sqlite3.IntegrityError as error:
        raise HTTPException(
            status_code=409, detail="This channel is already being watched"
        ) from error


@router.put("/channel-watcher/{channel_id}", response_model=WatchedChannel)
async def update_watched_channel(
    channel_id: str, settings: WatchedChannelRequest, request: Request
):
    """Update an existing channel's schedule and video download settings."""
    try:
        values = settings.model_dump()
        values["output_dir"] = _validate_output_dir(values["output_dir"])
        updated = _service(request).store.update_channel(channel_id, values, utc_now())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except sqlite3.IntegrityError as error:
        raise HTTPException(
            status_code=409, detail="This channel is already being watched"
        ) from error
    if updated is None:
        raise HTTPException(status_code=404, detail="Watched channel not found")
    return updated


@router.delete("/channel-watcher/{channel_id}")
async def delete_watched_channel(channel_id: str, request: Request):
    """Stop watching and remove a channel's persistent schedule."""
    if not _service(request).store.delete_channel(channel_id):
        raise HTTPException(status_code=404, detail="Watched channel not found")
    return {"message": "Watched channel removed"}
