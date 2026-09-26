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
cancellation_events: Dict[str, Event] = {}


@router.post("/download", response_model=DownloadResponse)
async def start_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    """Start a download task for a Spotify URL or YouTube video/collection URL.

    This endpoint initiates the download process and returns a task ID
    that can be used to track the progress of the download.
    """
    try:
        # Generate unique task ID
        task_id = str(uuid.uuid4())

        # Determine content type from URL
        spotify_url = str(request.spotify_url)
        if YouTubeDownloader.is_youtube_collection_url(spotify_url):
            content_type = "playlist"
        elif YouTubeDownloader.is_youtube_track_url(spotify_url):
            content_type = "track"
        elif "track" in spotify_url:
            content_type = "track"
        elif "album" in spotify_url:
            content_type = "album"
        elif "playlist" in spotify_url:
            content_type = "playlist"
        else:
            raise HTTPException(
                status_code=400,
                detail="URL must be a Spotify track, album, or playlist, or a YouTube track, album, or playlist.",
            )

        # Create initial task status
        task_status = DownloadStatus(
            task_id=task_id,
            status="pending",
            progress=0,
            total_tracks=0,
            completed_tracks=0,
            failed_tracks=0,
            started_at=datetime.now().isoformat(),
            output_format=request.output_format,
            bit_rate=request.bit_rate,
        )
        tasks[task_id] = task_status
        cancellation_events[task_id] = Event()

        # Start background download task
        background_tasks.add_task(download_task, task_id, request)

        logger.info(f"Started download task {task_id} for {spotify_url}")

        return DownloadResponse(
            task_id=task_id,
            status="pending",
            spotify_url=spotify_url,
            content_type=content_type,
            message=f"Download task started for {content_type}",
        )

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
    """Cancel a download task."""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = tasks[task_id]
    if task.status in ["completed", "failed", "cancelled"]:
        raise HTTPException(
            status_code=400, detail=f"Cannot cancel task with status: {task.status}"
        )

    cancellation_event = cancellation_events.get(task_id)
    if cancellation_event is None:
        raise HTTPException(status_code=409, detail="Download task is no longer active")

    cancellation_event.set()
    task.status = "cancelling"
    task.error_message = "Cancellation requested by user"

    return {"message": "Download cancellation requested"}


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
    tasks_pending = [task for task in tasks.values() if task.status == "pending"]
    tasks_processing = [task for task in tasks.values() if task.status == "processing"]

    return {
        "completed": tasks_completed,
        "pending": tasks_pending,
        "processing": tasks_processing,
    }


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
    try:
        task = tasks[task_id]
        if cancellation_event.is_set():
            task.status = "cancelled"
            task.error_message = "Task cancelled by user"
            task.completed_at = datetime.now().isoformat()
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

        # Perform the download
        result = await download_service.download_from_url(
            str(request.spotify_url),
            progress_callback=progress_callback,
            track_result_callback=track_result_callback,
            cancellation_event=cancellation_event,
        )

        if cancellation_event.is_set():
            task.status = "cancelled"
            task.error_message = "Task cancelled by user"
            task.completed_at = datetime.now().isoformat()
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
            task.status = "cancelled"
            task.error_message = "Task cancelled by user"
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
        cancellation_events.pop(task_id, None)


@router.get("/config/output_dir")
async def get_default_output_dir():
    """Returns the default value of the output directory."""
    return {"output_dir": APIConfig.get_output_dir()}


@router.get("/config/output_dirs")
async def get_output_directories():
    """Returns immediate subdirectories of the configured music directory."""
    output_root = Path(APIConfig.get_output_dir())
    if not output_root.is_dir():
        return {"directories": []}

    directories = sorted(
        (
            {"name": directory.name, "path": str(directory)}
            for directory in output_root.iterdir()
            if directory.is_dir()
        ),
        key=lambda directory: directory["name"].casefold(),
    )
    return {"directories": directories}
