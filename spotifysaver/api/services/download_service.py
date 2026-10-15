"""Download service for API operations"""

import asyncio
from pathlib import Path
from threading import Event
from typing import Dict, Optional, Callable, Any

from yt_dlp.utils import DownloadCancelled

from ...services import SpotifyAPI, YoutubeMusicSearcher
from ...downloader import YouTubeDownloader, YouTubeDownloaderForCLI
from ...enums import AudioFormat, Bitrate
from ...metadata import NFOGenerator
from ...models.album import Album
from ...spotlog import get_logger
from ..config import APIConfig

logger = get_logger("DownloadService")


def _track_to_dict(track: Any, number: int) -> dict:
    """Convert a Track model into the plain dict shape used by the API schema."""
    return {
        "name": track.name,
        "artists": track.artists,
        "album_name": getattr(track, "album_name", None),
        "duration": track.duration,
        "number": number,
        "uri": track.uri,
    }


class DownloadService:
    """Service class for handling download operations via API."""

    def __init__(
        self,
        output_dir: Optional[str] = None,
        download_lyrics: bool = False,
        download_cover: bool = True,
        generate_nfo: bool = False,
        output_format: str = APIConfig.DEFAULT_FORMAT,
        bit_rate: int = 256,
        overwrite_existing: bool = False,
        trim_silence: bool = False,
    ):
        """Initialize the download service.

        Args:
            output_dir: Custom output directory
            download_lyrics: Whether to download lyrics
            download_cover: Whether to download cover art
            generate_nfo: Whether to generate NFO files
            output_format: Audio format for downloads
        """
        self.output_dir = output_dir or APIConfig.get_output_dir()
        self.download_lyrics = download_lyrics
        self.download_cover = download_cover
        self.generate_nfo = generate_nfo
        self.overwrite_existing = overwrite_existing
        self.trim_silence = trim_silence
        self.cancellation_event: Optional[Event] = None
        # Convert string format to enum for internal use
        self.output_format = YouTubeDownloader.string_to_audio_format(output_format)
        self.bit_rate = YouTubeDownloader.int_to_bitrate(bit_rate)

        # Spotify clients are initialized only for Spotify source URLs.
        self.spotify = None
        self.searcher = None
        self.downloader = YouTubeDownloaderForCLI(base_dir=self.output_dir)
        self.downloader.trim_silence = trim_silence
        # Called as (current, total, fraction 0-1 of the current item)
        self.progress_fraction_callback: Optional[Callable[[int, int, float], None]] = None
        self._position = (1, 1)
        self.downloader.progress_fraction_callback = self._report_fraction

    def _report_fraction(self, fraction: float, info: dict) -> None:
        if not self.progress_fraction_callback:
            return
        index, total = self._position
        if info.get("playlist_index") and info.get("playlist_count"):
            index, total = int(info["playlist_index"]), int(info["playlist_count"])
        self.progress_fraction_callback(index, total, fraction)

    async def download_from_url(
        self,
        spotify_url: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        track_result_callback: Optional[Callable[[int, str, str], None]] = None,
        cancellation_event: Optional[Event] = None,
        title_callback: Optional[Callable[[str], None]] = None,
        tracks_callback: Optional[Callable[[list], None]] = None,
    ) -> Dict[str, Any]:
        """Download content from a Spotify URL or a YouTube video/collection URL.

        Args:
            spotify_url: Source URL to download
            progress_callback: Optional callback for progress updates
            title_callback: Optional callback invoked with the display name
                (album/playlist/track title) as soon as it is known
            tracks_callback: Optional callback invoked with the full track
                listing (as plain dicts) as soon as it is known

        Returns:
            Dict containing download results and statistics
        """
        self.cancellation_event = cancellation_event
        user_progress_callback = progress_callback

        def progress_callback(idx: int, total: int, name: str):
            self._position = (idx, total)
            if user_progress_callback:
                user_progress_callback(idx, total, name)

        try:
            if YouTubeDownloader.is_youtube_collection_url(spotify_url):
                return await self._download_youtube_playlist(
                    spotify_url,
                    progress_callback,
                    track_result_callback,
                    title_callback,
                    tracks_callback,
                )
            if YouTubeDownloader.is_youtube_track_url(spotify_url):
                return await self._download_youtube_track(
                    spotify_url, progress_callback, title_callback
                )

            self.spotify = SpotifyAPI()
            self.searcher = YoutubeMusicSearcher()
            if "track" in spotify_url:
                return await self._download_track(
                    spotify_url, progress_callback, title_callback, tracks_callback
                )
            elif "album" in spotify_url:
                return await self._download_album(
                    spotify_url, progress_callback, track_result_callback,
                    title_callback, tracks_callback
                )
            elif "playlist" in spotify_url:
                return await self._download_playlist(
                    spotify_url, progress_callback, track_result_callback,
                    title_callback, tracks_callback
                )
            else:
                raise ValueError("Invalid Spotify URL type")

        except DownloadCancelled:
            raise
        except Exception as e:
            logger.error(f"Error downloading from {spotify_url}: {str(e)}", exc_info=True)
            raise

    async def _download_youtube_playlist(
        self,
        playlist_url: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        track_result_callback: Optional[Callable[[int, str, str], None]] = None,
        title_callback: Optional[Callable[[str], None]] = None,
        tracks_callback: Optional[Callable[[list], None]] = None,
    ) -> Dict[str, Any]:
        """Download a YouTube playlist or YouTube Music album collection."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            self.downloader.download_youtube_playlist_cli,
            playlist_url,
            self.output_format,
            self.bit_rate,
            self.download_cover,
            self.overwrite_existing,
            progress_callback,
            track_result_callback,
            False,
            self.cancellation_event,
            title_callback,
            tracks_callback,
            getattr(self, "generate_nfo", False),
        )
        return {"content_type": "playlist", **result}

    async def _download_youtube_track(
        self,
        track_url: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        title_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Download one YouTube video as the selected audio format."""
        def titled_progress(idx: int, total: int, name: str):
            if title_callback:
                title_callback(name)
            if progress_callback:
                progress_callback(idx, total, name)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            self.downloader.download_youtube_track_cli,
            track_url,
            self.output_format,
            self.bit_rate,
            self.overwrite_existing,
            titled_progress,
            self.cancellation_event,
            SpotifyAPI.resolve_youtube_track_title,
            getattr(self, "generate_nfo", False),
        )
        return {"content_type": "track", **result}

    async def _download_track(
        self,
        track_url: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        title_callback: Optional[Callable[[str], None]] = None,
        tracks_callback: Optional[Callable[[list], None]] = None,
    ) -> Dict[str, Any]:
        """Download a single track."""
        track = self.spotify.get_track(track_url)
        if title_callback:
            title_callback(track.name)
        if tracks_callback:
            tracks_callback([_track_to_dict(track, getattr(track, "number", 1) or 1)])

        self._position = (1, 1)
        if progress_callback:
            progress_callback(1, 1, track.name)

        # Run download in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        audio_path, updated_track = await loop.run_in_executor(
            None, self._download_track_sync, track, self.cancellation_event, track_url
        )

        return {
            "content_type": "track",
            "completed_tracks": 1 if audio_path else 0,
            "failed_tracks": 0 if audio_path else 1,
            "failed_track_names": [] if audio_path else [track.name],
            "total_tracks": 1,
            "output_directory": str(audio_path.parent) if audio_path else None,
        }

    async def _download_album(
        self,
        album_url: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        track_result_callback: Optional[Callable[[int, str, str], None]] = None,
        title_callback: Optional[Callable[[str], None]] = None,
        tracks_callback: Optional[Callable[[list], None]] = None,
    ) -> Dict[str, Any]:
        """Download an entire album."""
        album = self.spotify.get_album(album_url)
        if title_callback:
            title_callback(album.name)
        if tracks_callback:
            tracks_callback([_track_to_dict(t, t.number) for t in album.tracks])

        # Create a wrapper for the progress callback
        def sync_progress_callback(idx: int, total: int, name: str):
            self._position = (idx, total)
            if progress_callback:
                progress_callback(idx, total, name)

        def sync_track_result_callback(idx: int, name: str, result: str):
            if track_result_callback:
                track_result_callback(idx, name, result)

        # Run download in thread pool
        loop = asyncio.get_event_loop()
        success, total, failed_track_names = await loop.run_in_executor(
            None,
            self.downloader.download_album_cli,
            album,
            self.download_lyrics,
            self.output_format,
            self.bit_rate,
            self.generate_nfo,
            self.download_cover,
            self.overwrite_existing,
            sync_progress_callback,
            sync_track_result_callback,
            self.cancellation_event,
            album_url,
        )

        output_dir = self.downloader._get_album_dir(album)

        return {
            "content_type": "album",
            "completed_tracks": success,
            "failed_tracks": total - success,
            "failed_track_names": failed_track_names,
            "total_tracks": total,
            "output_directory": str(output_dir),
        }

    async def _download_playlist(
        self,
        playlist_url: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        track_result_callback: Optional[Callable[[int, str, str], None]] = None,
        title_callback: Optional[Callable[[str], None]] = None,
        tracks_callback: Optional[Callable[[list], None]] = None,
    ) -> Dict[str, Any]:
        """Download an entire playlist."""
        playlist = self.spotify.get_playlist(playlist_url)
        if title_callback:
            title_callback(playlist.name)
        if tracks_callback:
            tracks_callback([_track_to_dict(t, t.number) for t in playlist.tracks])

        # Create a wrapper for the progress callback
        def sync_progress_callback(idx: int, total: int, name: str):
            self._position = (idx, total)
            if progress_callback:
                progress_callback(idx, total, name)

        def sync_track_result_callback(idx: int, name: str, result: str):
            if track_result_callback:
                track_result_callback(idx, name, result)

        # Run download in thread pool
        loop = asyncio.get_event_loop()
        success, total, failed_track_names = await loop.run_in_executor(
            None,
            self.downloader.download_playlist_cli,
            playlist,
            self.output_format,
            self.bit_rate,
            self.download_lyrics,
            self.download_cover,
            self.overwrite_existing,
            sync_progress_callback,
            self.generate_nfo,
            sync_track_result_callback,
            self.cancellation_event,
            playlist_url,
        )

        output_dir = self.downloader.get_playlist_dir(playlist)

        return {
            "content_type": "playlist",
            "completed_tracks": success,
            "failed_tracks": total - success,
            "failed_track_names": failed_track_names,
            "total_tracks": total,
            "output_directory": str(output_dir),
        }

    def _download_track_sync(self, track, cancellation_event=None, source_url=None):
        """Synchronous track download helper."""
        result = self.downloader.download_track_cli(
            track, 
            output_format=self.output_format, 
            bitrate=self.bit_rate,
            download_lyrics=self.download_lyrics,
            overwrite_existing=self.overwrite_existing,
            cancellation_event=cancellation_event,
        )
        audio_path, updated_track = result
        if audio_path and self.generate_nfo:
            nfo_track = updated_track or track
            album = Album(
                name=nfo_track.album_name or nfo_track.name,
                artists=nfo_track.album_artist or nfo_track.artists or ["Unknown Artist"],
                release_date=nfo_track.release_date or "Unknown",
                genres=nfo_track.genres or [],
                cover_url=nfo_track.cover_url or "",
                tracks=[nfo_track],
            )
            NFOGenerator.generate(album, audio_path.parent, source_url)
        return result
