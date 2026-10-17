"""Download service for API operations"""

import asyncio
from pathlib import Path
from threading import Event, local
from typing import Dict, Optional, Callable, Any

from yt_dlp.utils import DownloadCancelled

from ...services import SpotifyAPI, YoutubeMusicSearcher
from ...downloader import (
    SoundCloudDownloader,
    YouTubeDownloader,
    YouTubeDownloaderForCLI,
)
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
        "playlist_position": getattr(track, "playlist_position", None),
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
        self.youtube_confirmation_callback = None
        self.youtube_confirmation_ready_callback = None
        self.youtube_confirmation_metadata_callback = None
        self.soundcloud_downloader = SoundCloudDownloader(base_dir=self.output_dir)
        self.downloader.rate_limit_provider = (
            APIConfig.get_download_rate_limit_bytes_per_second
        )
        self.soundcloud_downloader.rate_limit_provider = (
            APIConfig.get_download_rate_limit_bytes_per_second
        )
        self.downloader.trim_silence = trim_silence
        # Called as (current, total, fraction 0-1 of the current item)
        self.progress_fraction_callback: Optional[Callable[[int, int, float], None]] = None
        self._position = local()
        self.downloader.progress_fraction_callback = self._report_fraction
        self.soundcloud_downloader.progress_fraction_callback = self._report_fraction

    def _report_fraction(self, fraction: float, info: dict) -> None:
        if not self.progress_fraction_callback:
            return
        index, total = getattr(self._position, "value", (1, 1))
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
            self._position.value = (idx, total)
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
            if SoundCloudDownloader.is_track_url(spotify_url):
                return await self._download_soundcloud_track(
                    spotify_url,
                    progress_callback,
                    cancellation_event,
                    title_callback,
                    tracks_callback,
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

    async def _download_soundcloud_track(
        self,
        track_url: str,
        progress_callback: Optional[Callable[[int, int, str], None]],
        cancellation_event: Optional[Event],
        title_callback: Optional[Callable[[str], None]],
        tracks_callback: Optional[Callable[[list], None]],
    ) -> Dict[str, Any]:
        loop = asyncio.get_event_loop()
        available, track, output_path = await loop.run_in_executor(
            None,
            self.soundcloud_downloader.download_track,
            track_url,
            self.output_format,
            self.bit_rate,
            self.download_lyrics,
            self.download_cover,
            self.overwrite_existing,
            cancellation_event,
            progress_callback,
            title_callback,
            tracks_callback,
            self.generate_nfo,
        )
        if cancellation_event and cancellation_event.is_set():
            return {
                "content_type": "track",
                "completed_tracks": 0,
                "failed_tracks": 0,
                "failed_track_names": [],
                "total_tracks": 1,
                "output_directory": str(self.output_dir),
            }
        if track is None:
            raise ValueError("Could not read SoundCloud track metadata")

        if available:
            if output_path is None:
                raise RuntimeError("SoundCloud download was interrupted")
            return {
                "content_type": "track",
                "download_url": track_url,
                "completed_tracks": 1,
                "failed_tracks": 0,
                "failed_track_names": [],
                "total_tracks": 1,
                "output_directory": str(output_path.parent),
            }

        logger.info(
            "SoundCloud track is not available for direct download; falling back to YouTube Music: %s",
            track.name,
        )
        return await self._download_track(
            track_url,
            progress_callback,
            title_callback,
            tracks_callback,
            track=track,
        )

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
        self.downloader.youtube_confirmation_callback = getattr(
            self, "youtube_confirmation_callback", None
        )
        self.downloader.youtube_confirmation_metadata_callback = getattr(
            self, "youtube_confirmation_metadata_callback", None
        )
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
        return {
            "content_type": "playlist",
            "download_url": playlist_url,
            **result,
        }

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
        return {"content_type": "track", "download_url": track_url, **result}

    async def _download_track(
        self,
        track_url: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        title_callback: Optional[Callable[[str], None]] = None,
        tracks_callback: Optional[Callable[[list], None]] = None,
        track: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Download a single track."""
        track = track or self.spotify.get_track(track_url)
        if title_callback:
            title_callback(track.name)
        if tracks_callback:
            tracks_callback([_track_to_dict(track, getattr(track, "number", 1) or 1)])

        self._position.value = (1, 1)

        # Run download in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        audio_path, updated_track = await loop.run_in_executor(
            None,
            self._download_track_sync,
            track,
            self.cancellation_event,
            track_url,
            progress_callback,
        )

        return {
            "content_type": "track",
            "download_url": (
                updated_track.youtube_url if updated_track else track.youtube_url
            ),
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
            tracks_callback(
                [
                    {
                        **_track_to_dict(track, track.number),
                        "disc_number": track.disc_number,
                    }
                    for track in album.tracks
                ]
            )

        # Create a wrapper for the progress callback
        def sync_progress_callback(idx: int, total: int, name: str):
            self._position.value = (idx, total)
            if progress_callback:
                progress_callback(idx, total, name)

        def sync_track_result_callback(idx: int, name: str, result: str):
            if track_result_callback:
                track_result_callback(idx, name, result)

        # Run download in thread pool
        loop = asyncio.get_event_loop()
        self.downloader.youtube_confirmation_callback = getattr(
            self, "youtube_confirmation_callback", None
        )
        self.downloader.youtube_confirmation_ready_callback = getattr(
            self, "youtube_confirmation_ready_callback", None
        )
        self.downloader.youtube_confirmation_metadata_callback = getattr(
            self, "youtube_confirmation_metadata_callback", None
        )
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
            self._position.value = (idx, total)
            if progress_callback:
                progress_callback(idx, total, name)

        def sync_track_result_callback(idx: int, name: str, result: str):
            if track_result_callback:
                track_result_callback(idx, name, result)

        # Run download in thread pool
        loop = asyncio.get_event_loop()
        self.downloader.youtube_confirmation_callback = getattr(
            self, "youtube_confirmation_callback", None
        )
        self.downloader.youtube_confirmation_ready_callback = getattr(
            self, "youtube_confirmation_ready_callback", None
        )
        self.downloader.youtube_confirmation_metadata_callback = getattr(
            self, "youtube_confirmation_metadata_callback", None
        )
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

    def _download_track_sync(
        self,
        track,
        cancellation_event=None,
        source_url=None,
        progress_callback=None,
    ):
        """Synchronous track download helper."""
        result = self.downloader.download_track_cli(
            track, 
            output_format=self.output_format, 
            bitrate=self.bit_rate,
            download_lyrics=self.download_lyrics,
            progress_callback=progress_callback,
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
