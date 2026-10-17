"""Youtube Downloader Module"""

import logging
import os
import re
import requests
import yt_dlp
from threading import Event
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from spotifysaver.services import YoutubeMusicSearcher, LrclibAPI
from spotifysaver.metadata import NFOGenerator, MusicFileMetadata
from spotifysaver.downloader.image_downloader import ImageDownloader
from spotifysaver.downloader.trim_silence import FFmpegTrimSilencePP
from spotifysaver.models import Track, Album, Playlist
from spotifysaver.enums import AudioFormat, Bitrate
from spotifysaver.config import Config
from spotifysaver.download_limiter import track_download_limiter
from spotifysaver.spotlog import get_logger


class YouTubeDownloader:
    """Download YouTube matches for Spotify tracks and direct YouTube media.

    Spotify downloads receive Spotify metadata; direct YouTube collections use
    the metadata provided by YouTube and yt-dlp.

    Attributes:
        base_dir: Base directory for music downloads
        searcher: YouTube Music searcher instance
        lrc_client: LRC Lib API client for lyrics
        image_downloader: Image downloader instance
    """

    # Optional (fraction 0-1, yt-dlp info dict) callback: download = first 50%, ffmpeg = rest
    progress_fraction_callback: Optional[Callable[[float, dict], None]] = None

    def __init__(self, base_dir: str = "Music"):
        """Initialize the YouTube downloader.

        Args:
            base_dir: Base directory where music will be downloaded
        """
        self.logger = get_logger(f"{self.__class__.__name__}")
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(exist_ok=True)
        self.searcher = YoutubeMusicSearcher()
        self.lrc_client = LrclibAPI()
        self.image_downloader = ImageDownloader()
        self.ydl_error_callback: Optional[Callable[[str], None]] = None
        self.rate_limit_provider: Optional[Callable[[], Optional[int]]] = None
        self.youtube_confirmation_callback: Optional[
            Callable[[Track, str], bool]
        ] = None
        self.youtube_confirmation_metadata_callback: Optional[
            Callable[[int, Track, str, dict], None]
        ] = None
        self.trim_silence = False

    @staticmethod
    def is_youtube_collection_url(url: str) -> bool:
        """Return whether a URL points to a YouTube playlist or album collection."""
        parsed_url = urlparse(url)
        hostname = (parsed_url.hostname or "").lower()
        path = parsed_url.path.rstrip("/")
        playlist_ids = parse_qs(parsed_url.query).get("list", [])
        is_radio = (
            path == "/watch"
            and bool(playlist_ids)
            and playlist_ids[0].upper().startswith("RD")
        )
        collection_hosts = {
            "music.youtube.com",
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
        }
        return (
            hostname in collection_hosts
            and path in {"/playlist", "/watch"}
            and bool(playlist_ids)
            and not is_radio
        )

    @staticmethod
    def is_youtube_url(url: str) -> bool:
        """Return whether a URL is hosted by YouTube or YouTube Music."""
        hostname = (urlparse(url).hostname or "").lower()
        return hostname in {
            "music.youtube.com",
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "youtu.be",
        }

    @staticmethod
    def is_youtube_track_url(url: str) -> bool:
        """Return whether a URL identifies one YouTube video."""
        if not YouTubeDownloader.is_youtube_url(url):
            return False

        parsed_url = urlparse(url)
        path_parts = [part for part in parsed_url.path.split("/") if part]
        if parsed_url.hostname == "youtu.be":
            return bool(path_parts)
        if parsed_url.path.rstrip("/") == "/watch":
            return bool(parse_qs(parsed_url.query).get("v"))
        return len(path_parts) > 1 and path_parts[0] in {"shorts", "embed"}

    @staticmethod
    def string_to_audio_format(format_str: str) -> AudioFormat:
        """Convert string format to AudioFormat enum.

        Args:
            format_str: String format ("m4a", "mp3", "opus")

        Returns:
            AudioFormat enum value

        Raises:
            ValueError: If format string is not supported
        """
        format_map = {
            "m4a": AudioFormat.M4A,
            "mp3": AudioFormat.MP3,
            "opus": AudioFormat.OPUS,
        }

        if format_str.lower() not in format_map:
            raise ValueError(
                f"Unsupported format: {format_str}. Supported formats: {list(format_map.keys())}"
            )

        return format_map[format_str.lower()]

    @staticmethod
    def int_to_bitrate(bitrate_int: int) -> Bitrate:
        """Convert integer bitrate to Bitrate enum.

        Args:
            bitrate_int: Integer bitrate (96, 128, 192, 256)

        Returns:
            Bitrate enum value

        Raises:
            ValueError: If bitrate is not supported
        """
        bitrate_map = {
            96: Bitrate.B96,
            128: Bitrate.B128,
            192: Bitrate.B192,
            256: Bitrate.B256,
        }

        if bitrate_int not in bitrate_map:
            raise ValueError(
                f"Unsupported bitrate: {bitrate_int}. Supported bitrates: {list(bitrate_map.keys())}"
            )

        return bitrate_map[bitrate_int]

    def _get_ydl_opts(
        self,
        output_path: Path,
        output_format: AudioFormat = AudioFormat.M4A,
        bitrate: Bitrate = Bitrate.B256,
    ) -> dict:
        """Get robust yt-dlp configuration with cookie support.

        Args:
            output_path: Path where the file should be saved
            output_format: Audio format enum (M4A, MP3, OPUS). Default: M4A.
            bitrate: Bitrate enum (B96, B128, B192, B256). Default: B256.

        Returns:
            dict: yt-dlp configuration options
        """
        is_verbose = self.logger.getEffectiveLevel() <= logging.DEBUG
        ytm_base_url = "https://music.youtube.com"

        # Use enum values instead of hardcoded validation
        format_value = output_format.value
        bitrate_value = bitrate.value

        opts = {
            "format": "bestaudio/best",
            "outtmpl": str(output_path.with_suffix(f".%(ext)s")),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": format_value,
                    "preferredquality": str(bitrate_value),
                }
            ],
            "quiet": not is_verbose,
            "verbose": is_verbose,
            "extract_flat": False,
            "logger": self._get_ydl_logger(),
            # Cookie and header options to avoid blocks
            "cookiefile": (
                str(Config.YTDLP_COOKIES_PATH) if Config.YTDLP_COOKIES_PATH else None
            ),
            "referer": ytm_base_url,
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            ),
            "extractor_args": {
                "youtube": {
                    "player_client": ["android"],
                    "player_skip": ["configs"],
                }
            },
            "retries": 5,
            "fragment_retries": 5,
            "skip_unavailable_fragments": True,
            "force_keyframes_at_cuts": True,
        }

        rate_limit_provider = getattr(self, "rate_limit_provider", None)
        if rate_limit_provider is not None:
            rate_limit = rate_limit_provider()
            if rate_limit is not None:
                opts["ratelimit"] = rate_limit

        self._add_progress_split_hooks(opts)
        return opts

    def _add_trim_silence_postprocessor(
        self, ydl, output_format: AudioFormat, bitrate: Bitrate
    ) -> None:
        if getattr(self, "trim_silence", False):
            ydl.add_post_processor(
                FFmpegTrimSilencePP(
                    ydl,
                    output_format.value,
                    bitrate.value,
                )
            )

    def _add_progress_split_hooks(self, opts: dict) -> None:
        """Split per-item progress: downloading is 50%, ffmpeg post-processing the other 50%."""

        def report(fraction: float, info: dict) -> None:
            callback = self.progress_fraction_callback
            if callback:
                callback(fraction, info or {})

        pp_done: dict = {}

        def on_download(status: dict) -> None:
            if status.get("status") == "finished":
                fraction = 1.0
            else:
                done = status.get("downloaded_bytes") or 0
                size = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
                fraction = min(done / size, 1.0) if size else 0.0
            report(0.5 * fraction, status.get("info_dict"))

        def on_postprocess(status: dict) -> None:
            info = status.get("info_dict") or {}
            key = info.get("id")
            if status.get("status") == "finished":
                pp_done[key] = pp_done.get(key, 0) + 1
            expected = max(len(opts.get("postprocessors") or []), 1)
            report(0.5 + 0.5 * min(pp_done.get(key, 0) / expected, 1.0), info)

        opts.setdefault("progress_hooks", []).append(on_download)
        opts.setdefault("postprocessor_hooks", []).append(on_postprocess)

    def _get_ydl_logger(self):
        """Create a yt-dlp logger that integrates with the application logger.

        Returns:
            YDLLogger: Custom logger for yt-dlp integration
        """
        from spotifysaver.spotlog import SDLogger
        return SDLogger(getattr(self, "ydl_error_callback", None))

    def _add_cancellation_hook(self, ydl_opts: dict, cancellation_event: Optional[Event]):
        if cancellation_event is None:
            return

        def cancel_if_requested(_status):
            if cancellation_event.is_set():
                raise yt_dlp.utils.DownloadCancelled("Download cancelled by user")

        ydl_opts.setdefault("progress_hooks", []).append(cancel_if_requested)

    def _get_output_path(
        self,
        track: Track,
        album_artist: str = None,
        output_format: AudioFormat = AudioFormat.M4A,
    ) -> Path:
        """Generate output paths: Music/Artist/Album (Year)/Track.m4a.

        Args:
            track: Track object containing metadata
            album_artist: Artist name for album organization
            output_format: Audio format enum

        Returns:
            Path: Complete file path where the track should be saved
        """
        artist_name = (
            album_artist or track.artists[0] if track.artists else "Unknown Artist"
        )
        artist_name = self._sanitize_filename(artist_name)
        album_name = self._sanitize_filename(track.album_name or "Unknown Album")
        year = track.release_date[:4] if track.release_date else "Unknown"
        dir_path = self.base_dir / artist_name / f"{album_name} ({year})"

        dir_path.mkdir(parents=True, exist_ok=True)
        track_name = self._sanitize_filename(track.name or "Unknown Track")
        return dir_path / f"{track.number} - {artist_name} - {track_name}.{output_format.value}"

    def _download_cover(self, track: Track) -> Optional[bytes]:
        """Download cover art from Spotify.

        Args:
            track: Track object containing cover URL

        Returns:
            bytes: Cover art image data, or None if download failed
        """
        if not track.cover_url:
            return None
        return self.image_downloader.get_image_from_url(track.cover_url)

    def _save_lyrics(self, track: "Track", audio_path: Path) -> bool:
        """Save synchronized lyrics as .lrc file.

        Args:
            track: Track object for lyrics search
            audio_path: Path to the audio file (used to determine .lrc path)

        Returns:
            bool: True if lyrics were successfully saved, False otherwise
        """
        try:
            lyrics = self.lrc_client.get_lyrics_with_fallback(track)
            if not lyrics or "[instrumental]" in lyrics.lower():
                return False

            lrc_path = audio_path.with_suffix(".lrc")
            lrc_path.write_text(lyrics, encoding="utf-8")

            if lrc_path.stat().st_size > 0:
                self.logger.info(f"Lyrics saved in: {lrc_path}")
                return True

            return False

        except Exception as e:
            self.logger.error(f"Error saving song lyrics: {str(e)}", exc_info=True)
            return False

    def _get_album_dir(self, album: "Album") -> Path:
        """Get the album directory path.

        Args:
            album: Album object containing metadata

        Returns:
            Path: Directory path for the album
        """
        artist_dir = self.base_dir / album.artists[0]
        return artist_dir / f"{album.name} ({album.release_date[:4]})"

    def _save_artist_cover(self, url: str):
        """Download and save artist cover art.

        Args:
            url: URL of the cover image
        """
        if not url:
            return

        output_path = self.base_dir / "folder.jpg"
        try:
            image = self.image_downloader.download_image(url, output_path)
            if image:
                self.logger.info(f"Artist cover saved in: {output_path}")
        except Exception as e:
            self.logger.error(f"Error downloading artist cover: {e}")

    def _save_cover_album(self, url: str, output_path: Path):
        """Download and save album cover art.

        Args:
            url: URL of the cover image
            output_path: Path where the cover should be saved
        """
        if not url:
            return

        if output_path.exists():
            self.logger.info(f"Cover already exists at: {output_path}")
            return

        try:
            image = self.image_downloader.download_image(url, output_path)
            if image:
                self.logger.info(f"Cover saved in: {output_path}")
        except Exception as e:
            self.logger.error(f"Error downloading cover: {e}")

    def _generate_album_nfos(
        self,
        tracks: list[Track],
        output_format: AudioFormat,
        source_url: Optional[str] = None,
    ):
        albums = {}
        for track in tracks:
            album_key = (
                track.album_artist[0] if track.album_artist else track.artists[0],
                track.album_name,
                track.release_date,
            )
            albums.setdefault(album_key, []).append(track)

        for album_tracks in albums.values():
            album_tracks = [
                track
                if track.youtube_url
                else track.with_youtube_url(self.searcher.search_track(track))
                for track in album_tracks
            ]
            first_track = album_tracks[0]
            album = Album(
                name=first_track.album_name or "Unknown Album",
                artists=(first_track.album_artist or first_track.artists or ["Unknown Artist"]),
                release_date=first_track.release_date or "Unknown",
                genres=first_track.genres or [],
                cover_url=first_track.cover_url,
                tracks=album_tracks,
            )
            album_dir = self._get_output_path(
                first_track,
                album_artist=album.artists[0],
                output_format=output_format,
            ).parent
            NFOGenerator.generate(album, album_dir, source_url)

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for Windows compatibility.

        Args:
            filename: Original filename

        Returns:
            str: Sanitized filename safe for Windows
        """
        # Replace problematic characters
        filename = re.sub(r'[<>:"/\\|?*]', "_", filename)

        # Replace em dash and en dash with regular dash
        filename = filename.replace("–", "-").replace("—", "-")

        # Remove multiple spaces and replace with single space
        filename = re.sub(r"\s+", " ", filename)

        # Trim whitespace and dots from start/end
        filename = filename.strip(". ")

        # Limit length to 200 characters to avoid Windows path limits
        if len(filename) > 200:
            filename = filename[:200].strip()

        return filename

    def download_track(
        self,
        track: Track,
        output_format: AudioFormat = AudioFormat.M4A,
        bitrate: Bitrate = Bitrate.B128,
        album_artist: str = None,
        download_lyrics: bool = False,
        overwrite_existing: bool = True,
        cancellation_event: Optional[Event] = None,
        youtube_url: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> tuple[Optional[Path], Optional[Track]]:
        """Download a track from YouTube Music with Spotify metadata.

        Args:
            track: Track object with metadata
            yt_url: YouTube Music URL for the track
            album_artist: Artist name for file organization
            download_lyrics: Whether to download lyrics
            output_format: Audio format enum (M4A, MP3, OPUS).
            bitrate: Audio bitrate enum (B96, B128, B192, B256).

        Returns:
            tuple: (Downloaded file path, Updated track) or (None, None) on error
        """
        if cancellation_event and cancellation_event.is_set():
            return None, None

        output_path = self._get_output_path(track, album_artist, output_format)
        self._save_cover_album(track.cover_url, output_path.parent / "cover.jpg")
        if output_path.exists() and not overwrite_existing:
            self.logger.info(f"Skipping existing track: {output_path}")
            return output_path, track

        if cancellation_event and cancellation_event.is_set():
            return None, None

        yt_url = youtube_url or self.searcher.search_track(track)
        if cancellation_event and cancellation_event.is_set():
            return None, None

        ydl_opts = self._get_ydl_opts(output_path, output_format, bitrate)
        ydl_opts["overwrites"] = overwrite_existing
        self._add_cancellation_hook(ydl_opts, cancellation_event)

        if not yt_url:
            self.logger.error(f"No match found for: {track.name}")
            return None, None
        if (
            (confirmation_callback := getattr(self, "youtube_confirmation_callback", None))
            is not None
            and not confirmation_callback(track, yt_url)
        ):
            return None, None

        try:
            def download_audio() -> None:
                if progress_callback:
                    progress_callback(1, 1, track.name)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    self._add_trim_silence_postprocessor(
                        ydl, output_format, bitrate
                    )
                    ydl.download([yt_url])

            with track_download_limiter.download_slot(
                cancellation_event
            ) as acquired:
                if not acquired:
                    return None, None
                download_audio()

            if cancellation_event and cancellation_event.is_set():
                if output_path.exists():
                    output_path.unlink()
                return None, None

            # 2. Add metadata and cover art
            cover_data = self._download_cover(track)
            metadata = MusicFileMetadata(
                file_path=output_path, track=track, cover_data=cover_data
            )
            metadata.add_metadata()

            # 3. Lyrics handling
            updated_track = track.with_youtube_url(yt_url)
            if download_lyrics:
                success = self._save_lyrics(track, output_path)
                updated_track = updated_track.with_lyrics_status(success)

            self.logger.info(f"Download completed: {output_path}")
            return output_path, updated_track

        except Exception as e:
            self.logger.error(f"Error downloading {track.name}: {e}", exc_info=True)
            if output_path.exists():
                self.logger.debug(f"Removing corrupt file: {output_path}")
                output_path.unlink()
            return None, None
        
    def download_album(
        self,
        album: Album,
        output_format: AudioFormat = AudioFormat.M4A,
        bitrate: Bitrate = Bitrate.B128,
        download_lyrics: bool = False,
        nfo: bool = False,
        cover: bool = True,
        overwrite_existing: bool = False,
        source_url: Optional[str] = None,
    ):
        """Download a complete album and generate metadata.

        Args:
            album: Album object to download
            output_format: Audio format enum
            bitrate: Audio bitrate enum
            download_lyrics: Whether to download lyrics for tracks
            nfo: Whether to generate NFO metadata file
            cover: Whether to download album cover
        """
        for index, track in enumerate(album.tracks):
            _audio_path, updated_track = self.download_track(
                track=track,
                output_format=output_format,
                bitrate=bitrate,
                album_artist=album.artists[0],
                download_lyrics=download_lyrics,
                overwrite_existing=overwrite_existing,
            )
            if updated_track:
                album.tracks[index] = updated_track

        output_dir = self._get_album_dir(album)

        # Generate the NFO after downloading all tracks
        if nfo:
            self.logger.info(f"Generating NFO for album: {album.name}")
            NFOGenerator.generate(album, output_dir, source_url)

        # Download cover art
        if cover and album.cover_url:
            self.logger.info(f"Downloading cover for album: {album.name}")
            self._save_cover_album(album.cover_url, output_dir / "cover.jpg")

        pass

    def get_playlist_dir(self, playlist: Playlist) -> Path:
        """Return the dedicated playlist directory beside the music root."""
        playlist_dir = (
            self.base_dir.parent
            / "Playlists"
            / self._sanitize_filename(playlist.name)
        )
        playlist_dir.mkdir(parents=True, exist_ok=True)
        return playlist_dir

    def get_playlist_m3u_path(self, playlist: Playlist) -> Path:
        """Return the playlist M3U file path for a Spotify playlist."""
        playlist_dir = self.get_playlist_dir(playlist)
        return playlist_dir / f"{self._sanitize_filename(playlist.name)}.m3u"

    def ensure_playlist_m3u(
        self,
        playlist: Playlist,
        output_format: AudioFormat = AudioFormat.M4A,
    ) -> Optional[Path]:
        """Rewrite the playlist M3U file with the current local audio paths."""
        if not playlist.name:
            self.logger.error("Playlist name is empty. Cannot create playlist file.")
            return None

        m3u_path = self.get_playlist_m3u_path(playlist)

        playlist_entries: list[str] = []
        for track in playlist.tracks:
            try:
                artist_name = track.artists[0] if track.artists else "Unknown Artist"
                album_name = track.album_name or "Unknown Album"
                year = track.release_date[:4] if track.release_date else "Unknown"
                album_artist = (
                    track.album_artist[0]
                    if getattr(track, "album_artist", None)
                    and isinstance(track.album_artist, list)
                    and track.album_artist
                    else artist_name
                )
                local_path = self.base_dir / self._sanitize_filename(album_artist) / (
                    f"{self._sanitize_filename(album_name)} ({year})"
                ) / (
                    f"{track.number} - {self._sanitize_filename(album_artist)} - {self._sanitize_filename(track.name or 'Unknown Track')}.{output_format.value}"
                )
                if local_path.exists():
                    relative_path = os.path.relpath(
                        local_path, start=m3u_path.parent
                    )
                    playlist_entries.append(Path(relative_path).as_posix())
            except Exception as exc:
                self.logger.warning(f"Could not resolve local path for track {track.name}: {exc}")

        m3u_path.write_text("\n".join(playlist_entries) + ("\n" if playlist_entries else ""), encoding="utf-8")
        self.logger.info(f"Updated playlist file: {m3u_path}")
        return m3u_path

    def download_playlist(
        self,
        playlist: Playlist,
        output_format: AudioFormat = AudioFormat.M4A,
        bitrate: Bitrate = Bitrate.B128,
        download_lyrics: bool = False,
        cover: bool = True,
        nfo: bool = False,
        overwrite_existing: bool = False,
        source_url: Optional[str] = None,
    ):
        """Download a complete playlist and generate metadata.

        Args:
            playlist: Playlist object to download
            output_format: Audio format enum
            bitrate: Audio bitrate enum
            download_lyrics: Whether to download lyrics
            cover: Whether to download playlist cover
            nfo: Whether to generate NFO file

        Returns:
            bool: True if at least one track was successfully downloaded
        """

        # Basic validation
        if not playlist.name:
            self.logger.error("Playlist name is empty. Cannot create directory.")
            return False
        if not playlist.tracks:
            self.logger.warning(f"Playlist '{playlist.name}' has no tracks.")
            return False

        # Initial setup
        output_dir = self.get_playlist_dir(playlist)
        output_dir.mkdir(parents=True, exist_ok=True)
        success = False
        failed_tracks = []
        downloaded_tracks = []

        # Download tracks
        for track in playlist.tracks:
            try:
                # Download the YouTube URL
                _, updated_track = self.download_track(
                    track,
                    album_artist=(
                        track.album_artist[0]
                        if track.album_artist
                        else None
                    ),
                    output_format=output_format,
                    bitrate=bitrate,
                    download_lyrics=download_lyrics,
                    overwrite_existing=overwrite_existing,
                )
                if updated_track:
                    success = True
                    downloaded_tracks.append(updated_track)
            except Exception as e:
                failed_tracks.append(track.name)
                self.logger.error(
                    f"Error downloading track {track.name}: {e}"
                )  # Download cover art (only if successful)
        if success and playlist.cover_url and cover:
            self.logger.info(f"Downloading cover for playlist: {playlist.name}")
            self._save_cover_album(playlist.cover_url, output_dir / "cover.jpg")

        # Generate NFO (only if successful)
        if downloaded_tracks and nfo:
            self.logger.info(f"Generating album NFO files for playlist: {playlist.name}")
            self._generate_album_nfos(downloaded_tracks, output_format, source_url)
            NFOGenerator.generate_playlist(playlist, output_dir, source_url)

        self.ensure_playlist_m3u(playlist, output_format=output_format)

        # Log results
        if failed_tracks:
            self.logger.warning(
                f"Failed downloads in playlist '{playlist.name}': {len(failed_tracks)}/{len(playlist.tracks)}. "
                f"Ejemplos: {', '.join(failed_tracks[:3])}{'...' if len(failed_tracks) > 3 else ''}"
            )

        return success
