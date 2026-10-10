"""Video download service for API operations (YouTube videos and playlists)."""

import asyncio
import re
from pathlib import Path
from threading import Event
from typing import Any, Callable, Dict, Optional

import yt_dlp

from ...config import Config
from ...downloader import YouTubeDownloader
from ...spotlog import get_logger
from ..config import APIConfig

logger = get_logger("VideoDownloadService")


def _sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name or "")
    name = re.sub(r"\s+", " ", name).strip(". ")
    return name[:150] or "Untitled"


class VideoDownloadService:
    """Downloads YouTube videos (or playlists) as video files."""

    def __init__(
        self,
        output_dir: Optional[str] = None,
        video_format: str = "mp4",
        video_resolution: int = 0,
        include_subtitles: bool = False,
        overwrite_existing: bool = False,
    ):
        self.output_dir = Path(output_dir or APIConfig.get_output_dir())
        self.video_format = video_format
        self.video_resolution = video_resolution
        self.include_subtitles = include_subtitles
        self.overwrite_existing = overwrite_existing
        self.cancellation_event: Optional[Event] = None

    def _format_selector(self) -> str:
        height = f"[height<={self.video_resolution}]" if self.video_resolution else ""
        if self.video_format == "mp4":
            return (
                f"bestvideo{height}[ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo{height}+bestaudio/best{height}"
            )
        if self.video_format == "webm":
            return (
                f"bestvideo{height}[ext=webm]+bestaudio[ext=webm]/"
                f"bestvideo{height}+bestaudio/best{height}"
            )
        return f"bestvideo{height}+bestaudio/best{height}"

    def _ydl_opts(self, outtmpl: str) -> dict:
        opts: Dict[str, Any] = {
            "format": self._format_selector(),
            "merge_output_format": self.video_format,
            "outtmpl": outtmpl,
            "overwrites": self.overwrite_existing,
            "noplaylist": True,
            "quiet": True,
            "logger": None,
            "cookiefile": (
                str(Config.YTDLP_COOKIES_PATH) if Config.YTDLP_COOKIES_PATH else None
            ),
            "retries": 5,
            "fragment_retries": 5,
        }
        if self.include_subtitles:
            opts["writesubtitles"] = True
            opts["subtitleslangs"] = ["all", "-live_chat"]
            opts["postprocessors"] = [
                {"key": "FFmpegSubtitlesConvertor", "format": "srt"}
            ]
        return opts

    def _cancel_hook(self, opts: dict) -> None:
        event = self.cancellation_event
        if event is None:
            return

        def check(_status):
            if event.is_set():
                raise yt_dlp.utils.DownloadError("Download cancelled by user")

        opts.setdefault("progress_hooks", []).append(check)

    def _resolve_entries(self, url: str) -> tuple[str, list[dict]]:
        """Return (collection title, entries) without downloading anything."""
        is_collection = YouTubeDownloader.is_youtube_collection_url(url)
        opts = {
            "quiet": True,
            "extract_flat": "in_playlist",
            "noplaylist": not is_collection,
            "cookiefile": (
                str(Config.YTDLP_COOKIES_PATH) if Config.YTDLP_COOKIES_PATH else None
            ),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info.get("_type") == "playlist" or info.get("entries") is not None:
            entries = [e for e in (info.get("entries") or []) if e]
            return info.get("title") or "YouTube Playlist", entries
        return "", [info]

    def _download_sync(
        self,
        url: str,
        progress_callback: Optional[Callable[[int, int, str], None]],
        track_result_callback: Optional[Callable[[int, str, str], None]],
        title_callback: Optional[Callable[[str], None]],
    ) -> Dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        collection, entries = self._resolve_entries(url)
        total = len(entries)
        if title_callback:
            title_callback(collection or entries[0].get("title") or "YouTube video")

        output_dir = self.output_dir / _sanitize(collection) if collection else self.output_dir
        completed = 0
        failed_names: list[str] = []

        for index, entry in enumerate(entries, start=1):
            if self.cancellation_event and self.cancellation_event.is_set():
                break
            title = entry.get("title") or entry.get("id") or f"Video {index}"
            entry_url = entry.get("webpage_url") or entry.get("url") or (
                f"https://www.youtube.com/watch?v={entry['id']}" if entry.get("id") else url
            )
            if progress_callback:
                progress_callback(index, total, title)

            prefix = f"{index:02d} - " if collection else ""
            template = str(output_dir / f"{prefix}{_sanitize(title)}.%(ext)s")
            opts = self._ydl_opts(template)
            self._cancel_hook(opts)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([entry_url])
                completed += 1
                if track_result_callback:
                    track_result_callback(index, title, "completed")
            except Exception as error:
                if self.cancellation_event and self.cancellation_event.is_set():
                    break
                logger.error(f"Error downloading video {title}: {error}")
                reason = re.sub(r"\x1b\[[0-9;]*m", "", str(error)).strip()
                failed_names.append(f"{title} ({reason[:200]})")
                if track_result_callback:
                    track_result_callback(index, title, "error")

        return {
            "content_type": "playlist" if collection else "track",
            "collection_name": collection or None,
            "completed_tracks": completed,
            "failed_tracks": len(failed_names),
            "failed_track_names": failed_names,
            "total_tracks": total,
            "output_directory": str(output_dir),
        }

    async def download_from_url(
        self,
        url: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        track_result_callback: Optional[Callable[[int, str, str], None]] = None,
        cancellation_event: Optional[Event] = None,
        title_callback: Optional[Callable[[str], None]] = None,
        tracks_callback: Optional[Callable[[list], None]] = None,
    ) -> Dict[str, Any]:
        if not YouTubeDownloader.is_youtube_url(url):
            raise ValueError("Video downloads require a YouTube video or playlist URL")
        self.cancellation_event = cancellation_event
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._download_sync,
            url,
            progress_callback,
            track_result_callback,
            title_callback,
        )
