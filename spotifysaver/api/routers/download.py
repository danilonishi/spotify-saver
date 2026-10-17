"""Download endpoints for the SpotifySaver API"""

import asyncio
import re
import uuid
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from typing import Dict
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import yt_dlp

from ..schemas import (
    DownloadRequest,
    DownloadResponse,
    DownloadStatus,
    ErrorResponse,
    AlbumInfo,
    PlaylistInfo,
    TrackInfo,
)
from ..services import DownloadService, VideoDownloadService
from ...downloader import SoundCloudDownloader, YouTubeDownloader
from ...services import SpotifyAPI
from ...spotlog import get_logger
from ..config import APIConfig
from ...config import Config
from ..services.channel_watcher import ChannelWatcherStore, utc_now
from ..services.jellyfin_library_scan import JellyfinLibraryScanService
from ..services.notification_hub import NotificationHub
from ..services.storage_protection import StorageProtectionService


logger = get_logger("API")
router = APIRouter()

YOUTUBE_RATE_LIMIT_WARNING = (
    "YouTube rate-limited this session. Some tracks may not have downloaded; "
    "please retry later."
)


def _is_youtube_rate_limit(message: str) -> bool:
    normalized = message.lower()
    return "rate-limit" in normalized or "rate limit" in normalized


def _download_progress_percent(
    completed_tracks: int,
    failed_tracks: int,
    total_tracks: int,
    active_track_fraction: float = 0,
) -> int:
    if total_tracks <= 0:
        return 0
    finished_tracks = completed_tracks + failed_tracks
    return min(
        100,
        int(((finished_tracks + active_track_fraction) / total_tracks) * 100),
    )


def _report_yt_dlp_error(task: DownloadStatus, message: str) -> None:
    if _is_youtube_rate_limit(message):
        task.warning_message = YOUTUBE_RATE_LIMIT_WARNING


def _uses_spotify_collection_confirmation(request: DownloadRequest) -> bool:
    source_url = urlparse(str(request.spotify_url))
    return (
        request.media_type == "audio"
        and (source_url.hostname or "").lower().endswith("spotify.com")
        and bool({"album", "playlist"} & set(source_url.path.split("/")))
    )


async def _park_for_youtube_confirmation(
    task_id: str,
    track_number: int,
    title: str,
    video_url: str,
    channel_name: str,
) -> None:
    task = tasks[task_id]
    task.status = "awaiting_confirmation"
    task.confirmation_required = True
    task.confirmation_title = title
    task.confirmation_url = video_url
    task.confirmation_channel = channel_name
    task.confirmation_track_number = track_number
    task.confirmation_source = "matched"
    if 0 < track_number <= len(task.tracks):
        track = task.tracks[track_number - 1]
        track.youtube_title = title
        track.youtube_url = video_url
        track.youtube_channel = channel_name
        track.confirmation_required = True
        task.track_updates.append(
            {
                "track_number": track_number,
                "track_name": track.name,
                "status": "awaiting_confirmation",
            }
        )
    async with queue_lock:
        active.discard(task_id)
    await _try_start_next()
    await _publish_download_confirmation(task)


# In-memory task storage (in production, use Redis or database)
tasks: Dict[str, DownloadStatus] = {}
requests_by_task: Dict[str, DownloadRequest] = {}
cancellation_events: Dict[str, Event] = {}
download_archives: Dict[str, str] = {}

# Server-owned download queue: the server decides when each task actually
# starts running, so downloads keep progressing even if every browser tab
# is closed. `order` tracks display/queue order; `pending` holds task ids
# that are waiting for a free execution slot; `active` holds task ids that
# are currently executing.
order: list[str] = []
pending: list[str] = []
active: set[str] = set()
paused: bool = False
# Tasks whose active download was interrupted because the queue was paused
# (as opposed to being cancelled/removed by the user); these get put back
# at the front of the queue instead of being marked as cancelled.
paused_task_ids: set[str] = set()
# Paused tasks cleared from the queue remain alive until their worker exits.
discard_after_stop: set[str] = set()
# Partially completed paused tasks remain in history after their worker exits.
preserve_after_stop: set[str] = set()
confirmation_resume_events: Dict[str, Event] = {}
confirmation_approved: set[str] = set()
track_confirmation_events: Dict[tuple[str, int], Event] = {}
queue_lock = asyncio.Lock()
jellyfin_library_scan_service: JellyfinLibraryScanService | None = None
notification_hub: NotificationHub | None = None
storage_protection_service: StorageProtectionService | None = None
channel_watcher_store: ChannelWatcherStore | None = None


def set_notification_hub(hub: NotificationHub | None) -> None:
    """Attach the application-scoped live notification broadcaster."""
    global notification_hub
    notification_hub = hub


def set_storage_protection_service(
    service: StorageProtectionService | None,
) -> None:
    """Attach the application-scoped storage reserve checker."""
    global storage_protection_service
    storage_protection_service = service


def set_channel_watcher_store(store: ChannelWatcherStore | None) -> None:
    """Attach the persistent video-state store used by Channel Watcher tasks."""
    global channel_watcher_store
    channel_watcher_store = store


def _reset_watched_video_status(task: DownloadStatus) -> None:
    if (
        channel_watcher_store is not None
        and task.channel_id
        and task.channel_video_id
        and task.confirmation_source == "channel_watcher"
    ):
        channel_watcher_store.update_video_status(
            task.channel_id,
            task.channel_video_id,
            "discovered",
            utc_now(),
        )


def get_known_download_paths() -> list[dict[str, str]]:
    """Return paths used by queued and active work for storage reporting."""
    known_paths = []
    for task_id in order:
        task = tasks.get(task_id)
        request = requests_by_task.get(task_id)
        if (
            task is None
            or request is None
            or task.status not in {
                "queued",
                "processing",
                "cancelling",
                "awaiting_confirmation",
            }
        ):
            continue
        known_paths.append(
            {
                "label": task.title or f"Queued download {task_id[:8]}",
                "path": request.output_dir or APIConfig.get_output_dir(),
            }
        )
    return known_paths


async def _prepare_playlist_confirmation(
    task_id: str,
    track_number: int,
    track_title: str,
    video_url: str,
    youtube_title: str,
    channel_name: str,
) -> None:
    task = tasks.get(task_id)
    if task is None or not 0 < track_number <= len(task.tracks):
        return
    track = task.tracks[track_number - 1]
    track.youtube_title = youtube_title
    track.youtube_url = video_url
    track.youtube_channel = channel_name
    track.confirmation_required = True
    task.track_updates.append(
        {
            "track_number": track_number,
            "track_name": track_title,
            "status": "awaiting_confirmation",
        }
    )
    if notification_hub is not None:
        await notification_hub.publish(
            "download_confirmation",
            f"{youtube_title} is waiting for your approval to download from {channel_name}.",
            title=youtube_title,
            task_id=task_id,
        )


async def _publish_download_attention(task: DownloadStatus) -> None:
    if notification_hub is None:
        return
    if task.warning_message:
        message = task.warning_message
    elif task.status in {"failed", "completed_with_errors"}:
        message = task.error_message or f"Download {task.status.replace('_', ' ')}."
    else:
        return
    title = task.title or task.current_track or "Download"
    await notification_hub.publish(
        "download_attention",
        f"{title}: {message}",
        title=title,
        task_id=task.task_id,
    )


async def _publish_download_confirmation(task: DownloadStatus) -> None:
    if notification_hub is None or not task.confirmation_required:
        return
    title = task.confirmation_title or task.title or task.current_track or "Download"
    channel = task.confirmation_channel or "YouTube"
    await notification_hub.publish(
        "download_confirmation",
        f"{title} is waiting for your approval to download from {channel}.",
        title=title,
        task_id=task.task_id,
    )


def set_jellyfin_library_scan_service(
    service: JellyfinLibraryScanService | None,
) -> None:
    """Attach the application-scoped Jellyfin scheduler to the download queue."""
    global jellyfin_library_scan_service
    jellyfin_library_scan_service = service


def is_queue_idle() -> bool:
    """Return whether all queued and active downloads have finished."""
    return (
        not active
        and not pending
        and not any(task.status == "awaiting_confirmation" for task in tasks.values())
    )


def _content_type_for_url(spotify_url: str) -> str:
    if YouTubeDownloader.is_youtube_collection_url(spotify_url):
        return "playlist"
    if (
        YouTubeDownloader.is_youtube_track_url(spotify_url)
        or SoundCloudDownloader.is_track_url(spotify_url)
    ):
        return "track"
    if "track" in spotify_url:
        return "track"
    if "album" in spotify_url:
        return "album"
    if "playlist" in spotify_url:
        return "playlist"
    raise HTTPException(
        status_code=400,
        detail=(
            "URL must be a Spotify track, album, or playlist; a YouTube track, "
            "album, or playlist; or a SoundCloud track."
        ),
    )


def _queue_snapshot() -> list[DownloadStatus]:
    """Return all known tasks in queue order, annotated with queue position."""
    snapshot = []
    for index, task_id in enumerate(pending):
        task = tasks.get(task_id)
        if task is None:
            continue
        task.queue_position = index + 1
        snapshot.append(task)
    for task_id in order:
        if task_id in pending:
            continue
        task = tasks.get(task_id)
        if task is None:
            continue
        task.queue_position = 0 if task_id in active else None
        snapshot.append(task)
    # Preserve original enqueue order for the combined list.
    position = {task_id: i for i, task_id in enumerate(order)}
    snapshot.sort(key=lambda task: position.get(task.task_id, len(order)))
    return snapshot


def _retain_finished_tracks(task: DownloadStatus) -> None:
    """Keep completed/failed playlist rows after clearing its remaining tracks."""
    finished_updates = [
        update
        for update in task.track_updates
        if update.get("status") in {"completed", "error"}
    ]
    finished_numbers = {update.get("track_number") for update in finished_updates}
    task.tracks = [
        track
        for track in task.tracks
        if (track.playlist_position or track.number) in finished_numbers
    ]
    task.completed_tracks = sum(
        update.get("status") == "completed" for update in finished_updates
    )
    task.failed_tracks = sum(
        update.get("status") == "error" for update in finished_updates
    )
    task.failed_track_names = [
        str(update["track_name"])
        for update in finished_updates
        if update.get("status") == "error" and update.get("track_name")
    ]


async def _try_start_next() -> None:
    """Start queued tasks while there is a free execution slot."""
    async with queue_lock:
        if paused:
            return
        while pending and len(active) < APIConfig.MAX_ACTIVE_DOWNLOAD_JOBS:
            task_id = None
            request = None
            for candidate_id in list(pending):
                candidate_request = requests_by_task.get(candidate_id)
                candidate_task = tasks.get(candidate_id)
                if (
                    candidate_request is None
                    or candidate_task is None
                    or candidate_task.status == "cancelled"
                ):
                    pending.remove(candidate_id)
                    continue
                if storage_protection_service is not None:
                    was_storage_blocked = candidate_task.storage_blocked
                    blocked, reason = await storage_protection_service.check_destination(
                        candidate_request.output_dir or APIConfig.get_output_dir()
                    )
                    candidate_task.storage_blocked = blocked
                    if blocked:
                        candidate_task.error_message = reason
                        continue
                    if was_storage_blocked:
                        candidate_task.error_message = None
                pending.remove(candidate_id)
                task_id = candidate_id
                request = candidate_request
                break
            if task_id is None or request is None:
                break
            active.add(task_id)
            if task_id in confirmation_resume_events:
                if task_id not in confirmation_approved:
                    active.discard(task_id)
                    continue
                confirmation_approved.discard(task_id)
                tasks[task_id].status = "processing"
                confirmation_resume_events[task_id].set()
                continue
            cancellation_events[task_id] = Event()
            asyncio.create_task(_run_task(task_id, request))


async def _run_task(task_id: str, request: DownloadRequest) -> None:
    try:
        await download_task(task_id, request)
    finally:
        active.discard(task_id)
        task = tasks.get(task_id)
        if task is not None and task.status == "cancelled":
            _reset_watched_video_status(task)
        if task_id in discard_after_stop:
            discard_after_stop.discard(task_id)
            paused_task_ids.discard(task_id)
            tasks.pop(task_id, None)
            requests_by_task.pop(task_id, None)
            cancellation_events.pop(task_id, None)
            download_archives.pop(task_id, None)
        elif task_id in preserve_after_stop:
            preserve_after_stop.discard(task_id)
            paused_task_ids.discard(task_id)
            if task is not None:
                _retain_finished_tracks(task)
        elif task is not None and task.status == "queued":
            # Interrupted by a queue pause; keep it first in line to resume.
            async with queue_lock:
                if task_id not in pending:
                    pending.insert(0, task_id)
        await _try_start_next()
        confirmation_resume_events.pop(task_id, None)
        confirmation_approved.discard(task_id)
        for confirmation_key in [
            key for key in track_confirmation_events if key[0] == task_id
        ]:
            track_confirmation_events.pop(confirmation_key, None)
        if jellyfin_library_scan_service is not None:
            content_downloaded = bool(
                task
                and task.status in {"completed", "completed_with_errors"}
                and task.completed_tracks > 0
            )
            await jellyfin_library_scan_service.queue_changed(content_downloaded)


@router.post("/download", response_model=DownloadResponse)
async def start_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    """Enqueue a download task for supported Spotify, YouTube, or SoundCloud URLs.

    The task is handed off to the server-owned download queue and starts
    automatically as soon as a slot is free. The download continues on the
    server even if the client disconnects or the page is closed.
    """
    try:
        # Generate unique task ID
        task_id = str(uuid.uuid4())
        spotify_url = str(request.spotify_url)
        if request.media_type == "video" and not YouTubeDownloader.is_youtube_url(spotify_url):
            raise HTTPException(
                status_code=400,
                detail="Video downloads require a YouTube video or playlist URL.",
            )
        content_type = _content_type_for_url(spotify_url)

        # Create initial task status
        task_status = DownloadStatus(
            task_id=task_id,
            status="queued",
            progress=0,
            total_tracks=0,
            completed_tracks=0,
            failed_tracks=0,
            started_at=datetime.now().isoformat(),
            output_format=request.output_format,
            bit_rate=request.bit_rate,
            spotify_url=spotify_url,
            output_dir=request.output_dir,
            content_type=content_type,
            media_type=request.media_type,
        )
        tasks[task_id] = task_status
        requests_by_task[task_id] = request
        order.append(task_id)
        async with queue_lock:
            pending.append(task_id)

        background_tasks.add_task(_try_start_next)

        logger.info(f"Enqueued download task {task_id} for {spotify_url}")

        return DownloadResponse(
            task_id=task_id,
            status="queued",
            spotify_url=spotify_url,
            content_type=content_type,
            message=f"Download task queued for {content_type}",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting download: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download/{task_id}/status", response_model=DownloadStatus)
async def get_download_status(task_id: str):
    """Get the current status of a download task."""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    return tasks[task_id]


@router.post("/download/{task_id}/retry", status_code=202)
async def retry_download(task_id: str):
    """Requeue a failed download using its original request settings."""
    async with queue_lock:
        task = tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status not in {"failed", "completed_with_errors"}:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot retry download with status: {task.status}",
            )
        if task_id not in requests_by_task:
            raise HTTPException(
                status_code=409, detail="Original download request is unavailable"
            )
        if task_id in active:
            raise HTTPException(status_code=409, detail="Download is still stopping")
        task.status = "queued"
        task.progress = 0
        task.current_track = None
        task.current_track_number = None
        task.current_track_status = None
        task.track_updates = []
        task.total_tracks = 0
        task.completed_tracks = 0
        task.failed_tracks = 0
        task.failed_track_names = []
        task.warning_message = None
        task.output_directory = None
        task.download_url = None
        task.confirmation_required = False
        task.confirmation_title = None
        task.confirmation_url = None
        task.confirmation_track_number = None
        task.confirmation_source = None
        task.error_message = None
        task.started_at = datetime.now().isoformat()
        task.completed_at = None
        task.playlist_verification = None
        task.tracks = []
        task.queue_position = None
        cancellation_events[task_id] = Event()
        paused_task_ids.discard(task_id)
        discard_after_stop.discard(task_id)
        preserve_after_stop.discard(task_id)
        if task_id not in order:
            order.append(task_id)
        if task_id not in pending:
            pending.append(task_id)

    await _try_start_next()
    return {"task_id": task_id, "status": "queued", "message": "Download retry queued"}


@router.get("/download/{task_id}/cancel")
@router.post("/download/{task_id}/cancel")
async def cancel_download(task_id: str):
    """Cancel a download task, whether it is queued or actively downloading."""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = tasks[task_id]
    if task.status in ["completed", "completed_with_errors", "failed", "cancelled"]:
        raise HTTPException(
            status_code=400, detail=f"Cannot cancel task with status: {task.status}"
        )

    if task.status == "awaiting_confirmation" and task_id not in confirmation_resume_events:
        task.status = "cancelled"
        task.confirmation_required = False
        task.error_message = "Cancelled by user before it started"
        task.completed_at = datetime.now().isoformat()
        _reset_watched_video_status(task)
        return {"message": "Queued download removed"}

    if task_id in pending and task_id not in active:
        async with queue_lock:
            if task_id in pending:
                pending.remove(task_id)
        task.status = "cancelled"
        task.error_message = "Cancelled by user before it started"
        task.completed_at = datetime.now().isoformat()
        _reset_watched_video_status(task)
        download_archives.pop(task_id, None)
        return {"message": "Queued download removed"}

    cancellation_event = cancellation_events.get(task_id)
    if cancellation_event is None:
        raise HTTPException(status_code=409, detail="Download task is no longer active")

    paused_task_ids.discard(task_id)
    cancellation_event.set()
    task.status = "cancelling"
    task.error_message = "Cancellation requested by user"

    return {"message": "Download cancellation requested"}


@router.post("/download/{task_id}/confirm-youtube", status_code=202)
async def confirm_youtube_download(
    task_id: str,
    track_number: int | None = None,
):
    """Approve the matched YouTube video currently awaiting user confirmation."""
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if track_number is not None:
        if track_number < 1:
            raise HTTPException(status_code=422, detail="track_number must be at least 1")
        if task.status != "processing" or track_number > len(task.tracks):
            raise HTTPException(
                status_code=409, detail="This playlist track is not awaiting confirmation"
            )
        track = task.tracks[track_number - 1]
        approval_event = track_confirmation_events.get((task_id, track_number))
        if not track.confirmation_required or approval_event is None:
            raise HTTPException(
                status_code=409, detail="This playlist track is not awaiting confirmation"
            )
        track.confirmation_required = False
        approval_event.set()
        return {
            "task_id": task_id,
            "track_number": track_number,
            "status": task.status,
        }
    if task.status != "awaiting_confirmation" or not task.confirmation_required:
        raise HTTPException(
            status_code=409, detail="This download is not awaiting confirmation"
        )

    task.confirmation_required = False
    if (
        task.confirmation_source == "matched"
        and task.confirmation_track_number
        and task.confirmation_track_number <= len(task.tracks)
    ):
        task.tracks[task.confirmation_track_number - 1].confirmation_required = False

    task.status = "queued"
    if (
        task.confirmation_source == "channel_watcher"
        and channel_watcher_store is not None
        and task.channel_id
        and task.channel_video_id
    ):
        channel_watcher_store.update_video_status(
            task.channel_id, task.channel_video_id, "queued", utc_now()
        )
    if task_id in confirmation_resume_events:
        confirmation_approved.add(task_id)
    async with queue_lock:
        if task_id not in pending:
            pending.append(task_id)
    await _try_start_next()
    return {"task_id": task_id, "status": task.status}


@router.delete("/download/{task_id}")
async def remove_download(task_id: str):
    """Remove a finished task from the server's history."""
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in [
        "completed",
        "completed_with_errors",
        "failed",
        "cancelled",
    ]:
        raise HTTPException(
            status_code=400, detail=f"Cannot remove task with status: {task.status}"
        )

    tasks.pop(task_id, None)
    requests_by_task.pop(task_id, None)
    cancellation_events.pop(task_id, None)
    download_archives.pop(task_id, None)
    if task_id in order:
        order.remove(task_id)
    return {"message": "Task removed"}


@router.get("/downloads")
async def list_downloads():
    """List all download tasks with their statuses.
    Returns a dictionary with keys for completed, pending, and processing tasks.
    """
    if not tasks:
        return JSONResponse(status_code=204, content={"message": "No download tasks found"})
    tasks_completed = [
        task for task in tasks.values()
        if task.status in ["completed", "completed_with_errors", "failed", "cancelled"]
    ]
    tasks_pending = [
        task
        for task in tasks.values()
        if task.status in {"queued", "awaiting_confirmation"}
    ]
    tasks_processing = [
        task for task in tasks.values() if task.status in ["processing", "cancelling"]
    ]

    return {
        "completed": tasks_completed,
        "pending": tasks_pending,
        "processing": tasks_processing,
    }


@router.get("/queue")
async def get_queue():
    """Return every known download task in queue order.

    This is the source of truth for clients: it reflects the state of the
    server-owned queue regardless of which browser tab (if any) enqueued
    each item, and whether the queue is globally paused.
    """
    return {
        "paused": paused,
        "tasks": _queue_snapshot(),
    }


@router.post("/queue/pause")
async def pause_queue():
    """Pause the download queue.

    Stops any queued download from starting, and interrupts the currently
    active one(s) so they can be resumed later from the front of the queue.
    """
    global paused
    paused = True

    for task_id in list(active):
        task = tasks.get(task_id)
        cancellation_event = cancellation_events.get(task_id)
        if task is None or cancellation_event is None:
            continue
        if task.status == "processing":
            paused_task_ids.add(task_id)
            task.status = "queued"
            task.error_message = "Paused by user"
            cancellation_event.set()

    return {"message": "Queue paused", "paused": paused}


@router.post("/queue/resume")
async def resume_queue(background_tasks: BackgroundTasks):
    """Resume the download queue, restarting queued downloads automatically."""
    global paused
    paused = False
    background_tasks.add_task(_try_start_next)
    return {"message": "Queue resumed", "paused": paused}


@router.delete("/queue/completed")
async def clear_completed():
    """Remove every finished (completed/failed/cancelled) task from history."""
    removed = 0
    for task_id in list(tasks.keys()):
        task = tasks[task_id]
        if task.status in ["completed", "completed_with_errors", "failed", "cancelled"]:
            tasks.pop(task_id, None)
            requests_by_task.pop(task_id, None)
            cancellation_events.pop(task_id, None)
            download_archives.pop(task_id, None)
            if task_id in order:
                order.remove(task_id)
            removed += 1
    return {"message": f"Removed {removed} finished task(s)", "removed": removed}


@router.delete("/queue")
async def clear_queue():
    """Clear queued work and the remaining tracks of active playlist downloads."""
    removed = 0

    def has_completed_tracks(task: DownloadStatus) -> bool:
        return task.completed_tracks > 0 or any(
            update.get("status") == "completed" for update in task.track_updates
        )

    def is_playlist_download(task_id: str, task: DownloadStatus) -> bool:
        request = requests_by_task.get(task_id)
        return (
            request is not None
            and request.media_type == "audio"
            and task.content_type in {"playlist", "album"}
            and task.total_tracks > 1
        )

    async with queue_lock:
        for task_id in list(pending):
            pending.remove(task_id)
            task = tasks.get(task_id)
            if (
                task is not None
                and task.error_message == "Paused by user"
                and has_completed_tracks(task)
            ):
                task.status = "cancelled"
                task.error_message = "Remaining tracks cleared by user"
                task.completed_at = datetime.now().isoformat()
                _retain_finished_tracks(task)
                cancellation_events.pop(task_id, None)
                paused_task_ids.discard(task_id)
                removed += 1
                continue

            tasks.pop(task_id, None)
            if task is not None:
                _reset_watched_video_status(task)
            requests_by_task.pop(task_id, None)
            cancellation_events.pop(task_id, None)
            download_archives.pop(task_id, None)
            if task_id in order:
                order.remove(task_id)
            if task is not None:
                removed += 1
        for task_id, task in list(tasks.items()):
            if task.status != "awaiting_confirmation":
                continue
            if task_id in confirmation_resume_events:
                cancellation_event = cancellation_events.get(task_id)
                if cancellation_event is not None:
                    cancellation_event.set()
                task.status = "cancelling"
                task.error_message = "Cancellation requested by user"
                continue
            tasks.pop(task_id, None)
            _reset_watched_video_status(task)
            requests_by_task.pop(task_id, None)
            cancellation_events.pop(task_id, None)
            download_archives.pop(task_id, None)
            if task_id in order:
                order.remove(task_id)
            removed += 1
        for task_id in list(active - paused_task_ids):
            task = tasks.get(task_id)
            if task is None or task.status != "processing":
                continue
            if not is_playlist_download(task_id, task):
                continue

            cancellation_event = cancellation_events.get(task_id)
            if cancellation_event is None:
                continue
            cancellation_event.set()
            task.status = "cancelling"
            task.error_message = "Remaining tracks cleared by user"
            if has_completed_tracks(task):
                preserve_after_stop.add(task_id)
                _retain_finished_tracks(task)
            else:
                discard_after_stop.add(task_id)
                if task_id in order:
                    order.remove(task_id)
            removed += 1
        for task_id in list(active & paused_task_ids):
            task = tasks.get(task_id)
            if task is None or task.status != "queued":
                continue
            paused_task_ids.discard(task_id)
            if has_completed_tracks(task):
                preserve_after_stop.add(task_id)
                task.status = "cancelling"
                task.error_message = "Remaining tracks cleared by user"
                _retain_finished_tracks(task)
            else:
                discard_after_stop.add(task_id)
                if task_id in order:
                    order.remove(task_id)
            removed += 1
    return {"message": f"Removed {removed} queued task(s)", "removed": removed}


@router.get("/inspect")
async def inspect_spotify_url(spotify_url: str):
    """Inspect a Spotify URL to get metadata without downloading."""
    try:
        spotify = SpotifyAPI()
        if "track" in spotify_url:
            track = spotify.get_track(spotify_url)
            return TrackInfo(
                name=track.name,
                artists=track.artists,
                album_name=track.album_name,
                duration=track.duration,
                number=track.number if hasattr(track, "number") else 1,
                uri=track.uri,
            )

        elif "album" in spotify_url:
            album = spotify.get_album(spotify_url)
            tracks = [
                TrackInfo(
                    name=t.name,
                    artists=t.artists,
                    album_name=t.album_name,
                    duration=t.duration,
                    number=t.number,
                    uri=t.uri,
                )
                for t in album.tracks
            ]
            return AlbumInfo(
                name=album.name,
                artists=album.artists,
                release_date=album.release_date,
                total_tracks=len(album.tracks),
                cover_url=album.cover_url,
                tracks=tracks,
            )

        elif "playlist" in spotify_url:
            playlist = spotify.get_playlist(spotify_url)
            tracks = [
                TrackInfo(
                    name=t.name,
                    artists=t.artists,
                    album_name=t.album_name,
                    duration=t.duration,
                    number=t.number,
                    uri=t.uri,
                )
                for t in playlist.tracks
            ]
            return PlaylistInfo(
                name=playlist.name,
                owner=playlist.owner,
                description=playlist.description,
                total_tracks=len(playlist.tracks),
                cover_url=playlist.cover_url,
                tracks=tracks,
            )

        else:
            raise HTTPException(status_code=400, detail="Invalid Spotify URL")

    except Exception as e:
        logger.error(f"Error inspecting URL {spotify_url}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


async def download_task(task_id: str, request: DownloadRequest):
    """Background task for handling downloads."""
    cancellation_event = cancellation_events[task_id]

    def mark_interrupted(task: DownloadStatus) -> None:
        if task_id in paused_task_ids:
            paused_task_ids.discard(task_id)
            task.status = "queued"
            task.error_message = "Paused by user"
        else:
            task.status = "cancelled"
            task.error_message = (
                "Remaining tracks cleared by user"
                if task_id in preserve_after_stop
                else "Task cancelled by user"
            )
        task.completed_at = datetime.now().isoformat()

    try:
        task = tasks[task_id]
        loop = asyncio.get_running_loop()

        def confirm_youtube_item(track_number: int, _source_title: str, youtube_url: str) -> bool:
            metadata_options = {
                "quiet": True,
                "skip_download": True,
                "cookiefile": (
                    str(Config.YTDLP_COOKIES_PATH)
                    if Config.YTDLP_COOKIES_PATH
                    else None
                ),
            }
            with yt_dlp.YoutubeDL(metadata_options) as ydl:
                video_info = ydl.extract_info(youtube_url, download=False)
            youtube_title = str(video_info.get("title") or "").strip()
            if not youtube_title:
                raise ValueError(
                    f"YouTube did not provide a title for {youtube_url}"
                )
            channel_name = str(
                video_info.get("channel")
                or video_info.get("uploader")
                or "Unknown channel"
            ).strip()

            resumed = Event()
            confirmation_resume_events[task_id] = resumed
            asyncio.run_coroutine_threadsafe(
                _park_for_youtube_confirmation(
                    task_id,
                    track_number,
                    youtube_title,
                    youtube_url,
                    channel_name,
                ),
                loop,
            ).result()
            while not resumed.wait(0.2):
                if cancellation_event.is_set():
                    return False
            return not cancellation_event.is_set()

        def expose_playlist_match(
            track_number: int,
            track: object,
            youtube_url: str,
            video_info: dict,
        ) -> None:
            channel_name = str(
                video_info.get("channel")
                or video_info.get("uploader")
                or "Unknown channel"
            ).strip()
            approval_event = Event()
            track_confirmation_events[(task_id, track_number)] = approval_event
            asyncio.run_coroutine_threadsafe(
                _prepare_playlist_confirmation(
                    task_id,
                    track_number,
                    str(getattr(track, "name", "Track")),
                    youtube_url,
                    str(video_info["title"]).strip(),
                    channel_name,
                ),
                loop,
            ).result()

        def wait_for_playlist_approval(track: object, _youtube_url: str) -> bool:
            track_number = int(
                getattr(track, "playlist_position", None)
                or getattr(track, "number", 0)
                or 0
            )
            approval_event = track_confirmation_events.get((task_id, track_number))
            if approval_event is None:
                raise RuntimeError(
                    f"No YouTube approval state exists for playlist track {track_number}"
                )
            while not approval_event.wait(0.2):
                if cancellation_event.is_set():
                    return False
            return not cancellation_event.is_set()

        def playlist_approval_is_ready(track: object, _youtube_url: str) -> bool:
            track_number = int(
                getattr(track, "playlist_position", None)
                or getattr(track, "number", 0)
                or 0
            )
            approval_event = track_confirmation_events.get((task_id, track_number))
            if approval_event is None:
                raise RuntimeError(
                    f"No YouTube approval state exists for playlist track {track_number}"
                )
            return approval_event.is_set()

        confirm_selected_youtube = (
            APIConfig.CONFIRM_MATCHED_YOUTUBE_DOWNLOADS
            and not YouTubeDownloader.is_youtube_track_url(str(request.spotify_url))
        )
        confirm_spotify_collection = (
            confirm_selected_youtube
            and _uses_spotify_collection_confirmation(request)
        )
        if cancellation_event.is_set():
            mark_interrupted(task)
            return
        task.status = "processing"
        is_youtube_collection = (
            request.media_type == "audio"
            and YouTubeDownloader.is_youtube_collection_url(str(request.spotify_url))
        )
        if is_youtube_collection:
            task.playlist_verification = (
                "rechecking" if task.tracks else "checking"
            )

        # Initialize the download service
        if request.media_type == "video":
            download_service = VideoDownloadService(
                output_dir=request.output_dir,
                video_format=request.video_format,
                video_resolution=request.video_resolution,
                include_subtitles=request.include_subtitles,
                overwrite_existing=request.overwrite_existing,
                download_archive=download_archives.get(task_id),
            )
            if confirm_selected_youtube:
                download_service.youtube_confirmation_callback = (
                    confirm_youtube_item
                )
            download_service.ydl_error_callback = (
                lambda message: _report_yt_dlp_error(task, message)
            )
        else:
            download_service = DownloadService(
                output_dir=request.output_dir,
                download_lyrics=request.download_lyrics,
                download_cover=request.download_cover,
                generate_nfo=request.generate_nfo,
                output_format=request.output_format,
                bit_rate=request.bit_rate,
                overwrite_existing=request.overwrite_existing or bool(request.download_files),
                trim_silence=request.trim_silence,
            )
            downloader = getattr(download_service, "downloader", None)
            if downloader is not None:
                downloader.ydl_error_callback = (
                    lambda message: _report_yt_dlp_error(task, message)
                )
            soundcloud_downloader = getattr(
                download_service, "soundcloud_downloader", None
            )
            if soundcloud_downloader is not None:
                soundcloud_downloader.ydl_error_callback = (
                    lambda message: _report_yt_dlp_error(task, message)
                )
            if confirm_selected_youtube:
                download_service.youtube_confirmation_callback = (
                    wait_for_playlist_approval
                    if confirm_spotify_collection
                    else confirm_youtube_item
                )
                if confirm_spotify_collection:
                    download_service.youtube_confirmation_ready_callback = (
                        playlist_approval_is_ready
                    )
                    download_service.youtube_confirmation_metadata_callback = (
                        expose_playlist_match
                    )
                if downloader is not None and not confirm_spotify_collection:
                    downloader.youtube_confirmation_callback = (
                        lambda track, youtube_url: confirm_youtube_item(
                            int(
                                task.current_track_number
                                or getattr(track, "number", 0)
                                or 1
                            ),
                            track.name,
                            youtube_url,
                        )
                    )

        # Progress callback
        started_track_numbers = set()
        track_progress_lock = Lock()
        active_track_fractions: Dict[int, float] = {}

        def progress_callback(current: int, total: int, track_name: str):
            with track_progress_lock:
                task.playlist_verification = None
                task.current_track = track_name
                task.current_track_number = current
                task.current_track_status = "downloading"
                task.total_tracks = total
                active_track_fractions.setdefault(current, 0)
                task.progress = _download_progress_percent(
                    task.completed_tracks,
                    task.failed_tracks,
                    total,
                    sum(active_track_fractions.values()),
                )
                if current not in started_track_numbers:
                    started_track_numbers.add(current)
                    task.track_updates.append(
                        {
                            "track_number": current,
                            "track_name": track_name,
                            "status": "downloading",
                        }
                    )

        # Each item's share of the bar: download is 50%, ffmpeg post-processing 50%
        def fraction_callback(current: int, total: int, fraction: float):
            if total > 0:
                with track_progress_lock:
                    active_track_fractions[current] = fraction
                    task.progress = _download_progress_percent(
                        task.completed_tracks,
                        task.failed_tracks,
                        total,
                        sum(active_track_fractions.values()),
                    )

        download_service.progress_fraction_callback = fraction_callback

        def track_result_callback(current: int, track_name: str, result: str):
            with track_progress_lock:
                active_track_fractions.pop(current, None)
                task.current_track = track_name
                task.current_track_number = current
                task.current_track_status = result
                task.track_updates.append(
                    {
                        "track_number": current,
                        "track_name": track_name,
                        "status": result,
                    }
                )
                if result == "error" and track_name not in task.failed_track_names:
                    task.failed_track_names.append(track_name)
                    task.failed_tracks += 1
                elif result == "completed":
                    task.completed_tracks += 1
                task.progress = _download_progress_percent(
                    task.completed_tracks,
                    task.failed_tracks,
                    task.total_tracks,
                    sum(active_track_fractions.values()),
                )

        def title_callback(title: str):
            task.title = title

        def tracks_callback(track_list: list):
            task.tracks = [TrackInfo(**track) for track in track_list]
            task.total_tracks = len(task.tracks)
            task.playlist_verification = None

        # Perform the download
        result = await download_service.download_from_url(
            str(request.spotify_url),
            progress_callback=progress_callback,
            track_result_callback=track_result_callback,
            cancellation_event=cancellation_event,
            title_callback=title_callback,
            tracks_callback=tracks_callback,
        )

        if cancellation_event.is_set():
            mark_interrupted(task)
            return

        # A task with no successful tracks is a failure; partial results remain completed.
        completed_tracks = result.get("completed_tracks", 0)
        failed_tracks = result.get("failed_tracks", 0)
        failed_track_names = result.get("failed_track_names", [])
        if failed_tracks and completed_tracks:
            task.status = "completed_with_errors"
        elif failed_tracks:
            task.status = "failed"
        else:
            task.status = "completed"
        task.progress = 100
        task.completed_tracks = completed_tracks
        task.failed_tracks = failed_tracks
        task.failed_track_names = failed_track_names
        task.output_directory = result.get("output_directory")
        task.download_url = result.get("download_url")
        if not task.title:
            # YouTube collections only expose their resolved name in the final result.
            task.title = result.get("collection_name")
        if failed_tracks:
            failed_summary = ", ".join(failed_track_names) or f"{failed_tracks} track(s)"
            task.error_message = (
                f"Downloaded {completed_tracks}/{task.total_tracks} tracks. "
                f"Failed: {failed_summary}."
            )
        task.completed_at = datetime.now().isoformat()

        logger.info(f"Download task {task_id} completed successfully")

    except Exception as e:
        task = tasks[task_id]
        if cancellation_event.is_set():
            mark_interrupted(task)
        else:
            logger.error(f"Download task {task_id} failed: {str(e)}", exc_info=True)
            if task.completed_tracks > 0:
                task.status = "completed"
                task.error_message = (
                    f"Download finished with an error after {task.completed_tracks} "
                    f"track(s): {e}"
                )
            else:
                task.status = "failed"
                task.error_message = str(e)
        task.completed_at = datetime.now().isoformat()
    finally:
        task = tasks.get(task_id)
        if task is not None:
            task.playlist_verification = None
        archive_path = download_archives.get(task_id)
        request = requests_by_task.get(task_id)
        if archive_path and request is not None and task is not None:
            video_id = str(request.spotify_url).rsplit("=", 1)[-1].split("&", 1)[0]
            archived = False
            archive = Path(archive_path)
            if archive.is_file():
                try:
                    archived = any(
                        line.strip().endswith(f" {video_id}")
                        for line in archive.read_text(encoding="utf-8").splitlines()
                    )
                except OSError:
                    logger.exception("Could not read watched-video archive %s", archive)
            if task.status in {
                "completed",
                "completed_with_errors",
                "failed",
                "cancelled",
            }:
                status = (
                    "downloaded"
                    if archived
                    else ("skipped" if task.status == "cancelled" else "failed")
                )
                try:
                    ChannelWatcherStore().update_video_status(
                        archive.stem,
                        video_id,
                        status,
                        utc_now(),
                        task.error_message,
                    )
                except Exception:
                    logger.exception("Could not persist watched-video task result for %s", video_id)
        if task is not None and task.status != "queued":
            cancellation_events.pop(task_id, None)
            download_archives.pop(task_id, None)
            await _publish_download_attention(task)


async def enqueue_watched_video(channel: dict, video_url: str) -> str:
    """Place a watched upload into the same server-owned queue as manual downloads."""
    video_id = video_url.rsplit("=", 1)[-1].split("&", 1)[0]
    for task_id, request in requests_by_task.items():
        if (
            request.media_type == "video"
            and str(request.spotify_url).endswith(video_id)
            and download_archives.get(task_id) == channel["download_archive"]
            and tasks.get(task_id)
            and tasks[task_id].status
            in {"queued", "processing", "cancelling", "awaiting_confirmation"}
        ):
            return (
                "awaiting_confirmation"
                if tasks[task_id].status == "awaiting_confirmation"
                else "queued"
            )

    request = DownloadRequest(
        spotify_url=video_url,
        output_dir=channel["output_dir"],
        media_type="video",
        video_format=channel["video_format"],
        video_resolution=channel["video_resolution"],
        include_subtitles=channel["include_subtitles"],
        overwrite_existing=False,
    )
    task_id = str(uuid.uuid4())
    content_type = "track"
    require_confirmation = APIConfig.CONFIRM_CHANNEL_WATCHER_DOWNLOADS
    tasks[task_id] = DownloadStatus(
        task_id=task_id,
        status="awaiting_confirmation" if require_confirmation else "queued",
        progress=0,
        total_tracks=0,
        completed_tracks=0,
        failed_tracks=0,
        started_at=datetime.now().isoformat(),
        output_format=request.output_format,
        bit_rate=request.bit_rate,
        spotify_url=video_url,
        output_dir=request.output_dir,
        content_type=content_type,
        media_type="video",
        title=str(channel.get("video_title") or "Watched channel upload"),
        confirmation_required=require_confirmation,
        confirmation_title=(
            str(channel.get("video_title") or video_id)
            if require_confirmation
            else None
        ),
        confirmation_url=video_url if require_confirmation else None,
        confirmation_channel=(
            str(channel.get("title") or "Unknown channel")
            if require_confirmation
            else None
        ),
        confirmation_source="channel_watcher",
        channel_id=str(channel["id"]),
        channel_video_id=video_id,
    )
    requests_by_task[task_id] = request
    download_archives[task_id] = channel["download_archive"]
    order.append(task_id)
    if not require_confirmation:
        async with queue_lock:
            pending.append(task_id)
        asyncio.create_task(_try_start_next())
    else:
        await _publish_download_confirmation(tasks[task_id])
    return "awaiting_confirmation" if require_confirmation else "queued"


@router.get("/config/output_dir")
async def get_default_output_dir():
    """Returns the default value of the output directory."""
    return {"output_dir": APIConfig.get_output_dir()}


class CreateFolderRequest(BaseModel):
    root: str
    name: str


@router.post("/config/output_dirs")
async def create_output_directory(request: CreateFolderRequest):
    """Creates a new folder directly inside the given base media path."""
    name = request.name.strip()
    if (
        not name
        or name in (".", "..")
        or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
        or name.endswith(".")
    ):
        raise HTTPException(status_code=400, detail="Invalid folder name")

    output_root = Path(request.root).expanduser().resolve()
    if not output_root.is_dir():
        raise HTTPException(
            status_code=400,
            detail="Base media path must be an existing directory",
        )

    new_directory = output_root / name
    try:
        new_directory.mkdir()
    except FileExistsError:
        raise HTTPException(status_code=409, detail="A folder with that name already exists")
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Could not create folder: {e}")
    return {"name": new_directory.name, "path": str(new_directory)}


@router.get("/config/output_dirs")
async def get_output_directories(root: str | None = None, media_type: str = "audio"):
    """Returns immediate subdirectories of the configured audio or video directory."""
    if media_type not in ("audio", "video"):
        raise HTTPException(status_code=400, detail="media_type must be 'audio' or 'video'")
    if root:
        output_root = Path(root).expanduser().resolve()
    elif media_type == "video":
        output_root = Path(APIConfig.get_video_dir()).expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=True)
    else:
        output_root = Path(APIConfig.get_output_dir()).expanduser().resolve()
    if not output_root.is_dir():
        raise HTTPException(
            status_code=400,
            detail="Base media path must be an existing directory",
        )

    directories = sorted(
        (
            {"name": directory.name, "path": str(directory)}
            for directory in output_root.iterdir()
            if directory.is_dir()
        ),
        key=lambda directory: directory["name"].casefold(),
    )
    return {"root": str(output_root), "directories": directories}
