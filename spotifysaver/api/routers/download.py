"""Download endpoints for the SpotifySaver API"""

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Dict

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from ..schemas import (
    DownloadRequest,
    DownloadResponse,
    DownloadStatus,
    ErrorResponse,
    AlbumInfo,
    PlaylistInfo,
    TrackInfo,
)
from ..services import DownloadService
from ...downloader import YouTubeDownloader
from ...services import SpotifyAPI
from ...spotlog import get_logger
from ..config import APIConfig


logger = get_logger("API")
router = APIRouter()

# In-memory task storage (in production, use Redis or database)
tasks: Dict[str, DownloadStatus] = {}
requests_by_task: Dict[str, DownloadRequest] = {}
cancellation_events: Dict[str, Event] = {}

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
queue_lock = asyncio.Lock()


def _content_type_for_url(spotify_url: str) -> str:
    if YouTubeDownloader.is_youtube_collection_url(spotify_url):
        return "playlist"
    if YouTubeDownloader.is_youtube_track_url(spotify_url):
        return "track"
    if "track" in spotify_url:
        return "track"
    if "album" in spotify_url:
        return "album"
    if "playlist" in spotify_url:
        return "playlist"
    raise HTTPException(
        status_code=400,
        detail="URL must be a Spotify track, album, or playlist, or a YouTube track, album, or playlist.",
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


async def _try_start_next() -> None:
    """Start queued tasks while there is a free execution slot."""
    async with queue_lock:
        if paused:
            return
        while pending and len(active) < APIConfig.MAX_CONCURRENT_DOWNLOADS:
            task_id = pending.pop(0)
            request = requests_by_task.get(task_id)
            task = tasks.get(task_id)
            if request is None or task is None or task.status == "cancelled":
                continue
            active.add(task_id)
            cancellation_events[task_id] = Event()
            asyncio.create_task(_run_task(task_id, request))


async def _run_task(task_id: str, request: DownloadRequest) -> None:
    try:
        await download_task(task_id, request)
    finally:
        active.discard(task_id)
        task = tasks.get(task_id)
        if task is not None and task.status == "queued":
            # Interrupted by a queue pause; keep it first in line to resume.
            async with queue_lock:
                if task_id not in pending:
                    pending.insert(0, task_id)
        await _try_start_next()


@router.post("/download", response_model=DownloadResponse)
async def start_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    """Enqueue a download task for a Spotify URL or YouTube video/collection URL.

    The task is handed off to the server-owned download queue and starts
    automatically as soon as a slot is free. The download continues on the
    server even if the client disconnects or the page is closed.
    """
    try:
        # Generate unique task ID
        task_id = str(uuid.uuid4())
        spotify_url = str(request.spotify_url)
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


@router.get("/download/{task_id}/cancel")
@router.post("/download/{task_id}/cancel")
async def cancel_download(task_id: str):
    """Cancel a download task, whether it is queued or actively downloading."""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = tasks[task_id]
    if task.status in ["completed", "failed", "cancelled"]:
        raise HTTPException(
            status_code=400, detail=f"Cannot cancel task with status: {task.status}"
        )

    if task_id in pending and task_id not in active:
        async with queue_lock:
            if task_id in pending:
                pending.remove(task_id)
        task.status = "cancelled"
        task.error_message = "Cancelled by user before it started"
        task.completed_at = datetime.now().isoformat()
        return {"message": "Queued download removed"}

    cancellation_event = cancellation_events.get(task_id)
    if cancellation_event is None:
        raise HTTPException(status_code=409, detail="Download task is no longer active")

    paused_task_ids.discard(task_id)
    cancellation_event.set()
    task.status = "cancelling"
    task.error_message = "Cancellation requested by user"

    return {"message": "Download cancellation requested"}


@router.delete("/download/{task_id}")
async def remove_download(task_id: str):
    """Remove a finished task from the server's history."""
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in ["completed", "failed", "cancelled"]:
        raise HTTPException(
            status_code=400, detail=f"Cannot remove task with status: {task.status}"
        )

    tasks.pop(task_id, None)
    requests_by_task.pop(task_id, None)
    cancellation_events.pop(task_id, None)
    if task_id in order:
        order.remove(task_id)
    return {"message": "Task removed"}


@router.get("/downloads")
async def list_downloads():
    """List all download tasks with their statuses.
    Returns a dictionary with keys for completed, pending, and processing tasks.
    """
    if not tasks:
        return JSONResponse(
            status_code=204, content={"message": "No download tasks found"}
        )
    tasks_completed = [
        task
        for task in tasks.values()
        if task.status in ["completed", "failed", "cancelled"]
    ]
    tasks_pending = [task for task in tasks.values() if task.status == "queued"]
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
        if task.status in ["completed", "failed", "cancelled"]:
            tasks.pop(task_id, None)
            requests_by_task.pop(task_id, None)
            cancellation_events.pop(task_id, None)
            if task_id in order:
                order.remove(task_id)
            removed += 1
    return {"message": f"Removed {removed} finished task(s)", "removed": removed}


@router.delete("/queue")
async def clear_queue():
    """Remove queued (not yet started) tasks, keeping active downloads running."""
    removed = 0
    async with queue_lock:
        for task_id in list(pending):
            pending.remove(task_id)
            task = tasks.pop(task_id, None)
            requests_by_task.pop(task_id, None)
            cancellation_events.pop(task_id, None)
            if task_id in order:
                order.remove(task_id)
            if task is not None:
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
            task.error_message = "Task cancelled by user"
        task.completed_at = datetime.now().isoformat()

    try:
        task = tasks[task_id]
        if cancellation_event.is_set():
            mark_interrupted(task)
            return
        task.status = "processing"

        # Initialize the download service
        download_service = DownloadService(
            output_dir=request.output_dir,
            download_lyrics=request.download_lyrics,
            download_cover=request.download_cover,
            generate_nfo=request.generate_nfo,
            output_format=request.output_format,
            bit_rate=request.bit_rate,
            overwrite_existing=request.overwrite_existing or bool(request.download_files),
        )

        # Progress callback
        def progress_callback(current: int, total: int, track_name: str):
            task.current_track = track_name
            task.current_track_number = current
            task.current_track_status = "downloading"
            task.completed_tracks = current - 1  # current is 1-based
            task.total_tracks = total
            task.progress = int((current / total) * 100) if total > 0 else 0

        def track_result_callback(current: int, track_name: str, result: str):
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

        def title_callback(title: str):
            task.title = title

        def tracks_callback(track_list: list):
            task.tracks = [TrackInfo(**track) for track in track_list]
            task.total_tracks = len(task.tracks)

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
        task.status = "failed" if failed_tracks > 0 and completed_tracks == 0 else "completed"
        task.progress = 100
        task.completed_tracks = completed_tracks
        task.failed_tracks = failed_tracks
        task.failed_track_names = failed_track_names
        task.output_directory = result.get("output_directory")
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
        logger.error(f"Download task {task_id} failed: {str(e)}", exc_info=True)
        task = tasks[task_id]
        if cancellation_event.is_set():
            mark_interrupted(task)
        elif task.completed_tracks > 0:
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
        if tasks.get(task_id) is not None and tasks[task_id].status != "queued":
            cancellation_events.pop(task_id, None)


@router.get("/config/output_dir")
async def get_default_output_dir():
    """Returns the default value of the output directory."""
    return {"output_dir": APIConfig.get_output_dir()}


@router.get("/config/output_dirs")
async def get_output_directories(root: str | None = None):
    """Returns immediate subdirectories of the configured music directory."""
    output_root = Path(root or APIConfig.get_output_dir()).expanduser().resolve()
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
