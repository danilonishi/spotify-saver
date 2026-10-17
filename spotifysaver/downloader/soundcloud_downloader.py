"""Download SoundCloud tracks when the artist enables original downloads."""

import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable, Optional
from urllib.parse import urlparse

import yt_dlp

from spotifysaver.config import Config
from spotifysaver.download_limiter import track_download_limiter
from spotifysaver.downloader.image_downloader import ImageDownloader
from spotifysaver.enums import AudioFormat, Bitrate
from spotifysaver.metadata import MusicFileMetadata, NFOGenerator
from spotifysaver.models import Album, Track
from spotifysaver.services import LrclibAPI
from spotifysaver.spotlog import SDLogger, get_logger


class SoundCloudDownloader:
    """Download SoundCloud tracks and preserve their SoundCloud metadata."""

    def __init__(self, base_dir: str = "Music"):
        self.logger = get_logger(self.__class__.__name__)
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.image_downloader = ImageDownloader()
        self.lrc_client = LrclibAPI()
        self.rate_limit_provider: Optional[Callable[[], Optional[int]]] = None
        self.ydl_error_callback: Optional[Callable[[str], None]] = None
        self.progress_fraction_callback: Optional[Callable[[float, dict], None]] = None

    @staticmethod
    def is_track_url(url: str) -> bool:
        """Return whether a URL points to a SoundCloud track page."""
        try:
            parsed_url = urlparse(url)
            hostname = (parsed_url.hostname or "").lower()
            if (
                parsed_url.scheme not in {"http", "https"}
                or parsed_url.username is not None
                or parsed_url.password is not None
                or hostname
                not in {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}
            ):
                return False
        except ValueError:
            return False

        path_parts = [part for part in parsed_url.path.split("/") if part]
        if len(path_parts) == 3 and path_parts[2].startswith("s-"):
            path_parts = path_parts[:2]
        reserved_paths = {
            "albums",
            "comments",
            "discover",
            "likes",
            "playlists",
            "reposts",
            "sets",
            "spotlight",
            "stations",
            "tracks",
        }
        return (
            len(path_parts) == 2
            and path_parts[0].lower() not in reserved_paths
            and path_parts[1].lower() not in reserved_paths
        )

    @staticmethod
    def _artist_names(info: dict) -> list[str]:
        artist_metadata = info.get("artists") or (
            info.get("artist") or info.get("channel") or info.get("uploader")
        )
        if isinstance(artist_metadata, dict):
            artist_values = [artist_metadata.get("name", "")]
        elif isinstance(artist_metadata, list):
            artist_values = artist_metadata
        else:
            artist_values = str(artist_metadata or "").split(",")

        artists = []
        for artist_value in artist_values:
            if isinstance(artist_value, dict):
                artist_value = artist_value.get("name", "")
            artist_name = str(artist_value).strip()
            if artist_name and artist_name.casefold() not in {
                name.casefold() for name in artists
            }:
                artists.append(artist_name)
        return artists or ["Unknown Artist"]

    @staticmethod
    def _release_date(info: dict) -> str:
        release_timestamp = info.get("release_timestamp") or info.get("timestamp")
        if release_timestamp:
            return datetime.fromtimestamp(
                release_timestamp, timezone.utc
            ).strftime("%Y-%m-%d")
        release_date = str(info.get("release_date") or info.get("upload_date") or "")
        if len(release_date) == 8 and release_date.isdigit():
            return f"{release_date[:4]}-{release_date[4:6]}-{release_date[6:8]}"
        return release_date

    @staticmethod
    def _safe_filename(value: str) -> str:
        value = re.sub(r'[<>:"/\\|?*]', "_", value)
        value = value.replace("–", "-").replace("—", "-")
        value = re.sub(r"\s+", " ", value).strip(". ")
        return value[:200].strip() or "Unknown"

    def _track_from_info(self, info: dict, source_url: str) -> Track:
        title = str(info.get("title") or info.get("id") or "SoundCloud track").strip()
        artists = self._artist_names(info)
        return Track(
            number=1,
            total_tracks=1,
            name=title,
            duration=int(info.get("duration") or 0),
            uri=source_url,
            artists=artists,
            album_artist=artists,
            release_date=self._release_date(info),
            source_type="soundcloud",
            genres=info.get("genres") or [],
            album_name=str(info.get("album") or title),
            cover_url=info.get("thumbnail") or "",
        )

    def _get_ydl_opts(
        self,
        output_path: Path,
        output_format: AudioFormat,
        bitrate: Bitrate,
        overwrite_existing: bool,
    ) -> dict:
        is_verbose = self.logger.getEffectiveLevel() <= 10
        options = {
            "format": "bestaudio/best",
            "outtmpl": str(output_path.with_suffix(".%(ext)s")),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": output_format.value,
                    "preferredquality": str(bitrate.value),
                }
            ],
            "quiet": not is_verbose,
            "verbose": is_verbose,
            "logger": SDLogger(self.ydl_error_callback),
            "cookiefile": (
                str(Config.YTDLP_COOKIES_PATH) if Config.YTDLP_COOKIES_PATH else None
            ),
            "retries": 5,
            "fragment_retries": 5,
            "skip_unavailable_fragments": True,
            "windowsfilenames": True,
            "overwrites": overwrite_existing,
        }
        if self.rate_limit_provider is not None:
            rate_limit = self.rate_limit_provider()
            if rate_limit is not None:
                options["ratelimit"] = rate_limit
        self._add_progress_split_hooks(options)
        return options

    def _add_progress_split_hooks(self, options: dict) -> None:
        def report(fraction: float, info: dict) -> None:
            if self.progress_fraction_callback:
                self.progress_fraction_callback(fraction, info or {})

        completed_postprocessors: dict = {}

        def on_download(status: dict) -> None:
            if status.get("status") == "finished":
                fraction = 1.0
            else:
                downloaded = status.get("downloaded_bytes") or 0
                size = (
                    status.get("total_bytes")
                    or status.get("total_bytes_estimate")
                    or 0
                )
                fraction = min(downloaded / size, 1.0) if size else 0.0
            report(0.5 * fraction, status.get("info_dict"))

        def on_postprocess(status: dict) -> None:
            info = status.get("info_dict") or {}
            key = info.get("id")
            if status.get("status") == "finished":
                completed_postprocessors[key] = completed_postprocessors.get(key, 0) + 1
            expected = max(len(options.get("postprocessors") or []), 1)
            fraction = min(completed_postprocessors.get(key, 0) / expected, 1.0)
            report(0.5 + 0.5 * fraction, info)

        options.setdefault("progress_hooks", []).append(on_download)
        options.setdefault("postprocessor_hooks", []).append(on_postprocess)

    @staticmethod
    def _add_cancellation_hook(
        options: dict, cancellation_event: Optional[Event]
    ) -> None:
        if cancellation_event is None:
            return

        def cancel_if_requested(_status):
            if cancellation_event.is_set():
                raise yt_dlp.utils.DownloadCancelled("Download cancelled by user")

        options.setdefault("progress_hooks", []).append(cancel_if_requested)

    def _output_path(self, track: Track, output_format: AudioFormat) -> Path:
        artist = self._safe_filename(track.artists[0] if track.artists else "")
        album = self._safe_filename(track.album_name or "Unknown Album")
        year = track.release_date[:4] if track.release_date else "Unknown"
        directory = self.base_dir / artist / f"{album} ({year})"
        directory.mkdir(parents=True, exist_ok=True)
        title = self._safe_filename(track.name or "Unknown Track")
        return directory / f"1 - {artist} - {title}.{output_format.value}"

    def _save_cover(self, track: Track, output_path: Path) -> None:
        if not track.cover_url or output_path.exists():
            return
        self.image_downloader.download_image(track.cover_url, output_path)

    def _save_lyrics(self, track: Track, audio_path: Path) -> bool:
        try:
            lyrics = self.lrc_client.get_lyrics_with_fallback(track)
            if not lyrics or "[instrumental]" in lyrics.lower():
                return False
            lyrics_path = audio_path.with_suffix(".lrc")
            lyrics_path.write_text(lyrics, encoding="utf-8")
            return lyrics_path.stat().st_size > 0
        except Exception:
            self.logger.exception("Error saving SoundCloud track lyrics")
            return False

    def download_track(
        self,
        url: str,
        output_format: AudioFormat = AudioFormat.M4A,
        bitrate: Bitrate = Bitrate.B128,
        download_lyrics: bool = False,
        download_cover: bool = True,
        overwrite_existing: bool = False,
        cancellation_event: Optional[Event] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        title_callback: Optional[Callable[[str], None]] = None,
        tracks_callback: Optional[Callable[[list], None]] = None,
        generate_nfo: bool = False,
    ) -> tuple[bool, Optional[Track], Optional[Path]]:
        """Download only if the artist-authorized original format is available."""
        if not self.is_track_url(url):
            raise ValueError("URL is not a supported SoundCloud track")
        if cancellation_event and cancellation_event.is_set():
            return False, None, None

        output_path_template = self.base_dir / "%(title)s"
        options = self._get_ydl_opts(
            output_path_template, output_format, bitrate, overwrite_existing
        )
        self._add_cancellation_hook(options, cancellation_event)

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if not isinstance(info, dict):
                raise ValueError("SoundCloud did not return track metadata")

            track = self._track_from_info(info, url)
            if title_callback:
                title_callback(track.name)
            if tracks_callback:
                tracks_callback(
                    [
                        {
                            "name": track.name,
                            "artists": track.artists,
                            "album_name": track.album_name,
                            "duration": track.duration,
                            "number": 1,
                            "uri": track.uri,
                        }
                    ]
                )

            has_artist_download = any(
                item.get("format_id") == "download"
                for item in info.get("formats") or []
            )
            if not has_artist_download:
                return False, track, None

            output_path = self._output_path(track, output_format)
            if download_cover:
                self._save_cover(track, output_path.parent / "cover.jpg")
            if output_path.is_file() and not overwrite_existing:
                return True, track, output_path
            if cancellation_event and cancellation_event.is_set():
                return True, track, None
            ydl.params["format"] = "download"
            ydl.format_selector = ydl.build_format_selector("download")
            ydl.params["outtmpl"]["default"] = str(
                output_path.with_suffix(".%(ext)s")
            )
            with track_download_limiter.download_slot(
                cancellation_event
            ) as acquired:
                if not acquired:
                    return False, track, None
                if progress_callback:
                    progress_callback(1, 1, track.name)
                ydl.process_ie_result(info, download=True)
            if cancellation_event and cancellation_event.is_set():
                output_path.unlink(missing_ok=True)
                return True, track, None
            if not output_path.is_file():
                raise RuntimeError(
                    "SoundCloud reported a downloadable track but produced no audio file"
                )

        metadata = MusicFileMetadata(
            file_path=output_path,
            track=track,
            cover_data=(
                self.image_downloader.get_image_from_url(track.cover_url)
                if download_cover and track.cover_url
                else None
            ),
        )
        metadata.add_metadata()
        if download_lyrics:
            track = track.with_lyrics_status(self._save_lyrics(track, output_path))
        if generate_nfo:
            NFOGenerator.generate(
                Album(
                    name=track.album_name,
                    artists=track.album_artist,
                    release_date=track.release_date,
                    genres=track.genres or [],
                    cover_url=track.cover_url or "",
                    tracks=[track],
                    uri=url,
                ),
                output_path.parent,
                url,
            )
        return True, track, output_path
