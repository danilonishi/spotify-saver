"""Youtube Downloader Module"""

from pathlib import Path
from threading import Event
from typing import Optional

from mutagen.oggopus import OggOpus

from spotifysaver.services import YoutubeMusicSearcher, LrclibAPI
from spotifysaver.metadata import NFOGenerator
from spotifysaver.downloader.youtube_downloader import YouTubeDownloader, yt_dlp
from spotifysaver.downloader.image_downloader import ImageDownloader
from spotifysaver.models import Track, Album, Playlist
from spotifysaver.enums import AudioFormat, Bitrate
from spotifysaver.spotlog import get_logger


class YouTubeDownloaderForCLI(YouTubeDownloader):
    """Download Spotify-backed tracks and direct YouTube collections.

    This class adds CLI progress support to the downloader workflows.

    Attributes:
        base_dir: Base directory for music downloads
        searcher: YouTube Music searcher instance
        lrc_client: LRC Lib API client for lyrics
        image_downloader: Image downloader instance
    """

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

    @staticmethod
    def _youtube_artist_names(info: dict) -> list[str]:
        artist_metadata = (
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
        return artists

    @staticmethod
    def _youtube_release_date(info: dict) -> str:
        release_date = str(info.get("release_date") or info.get("upload_date") or "")
        if len(release_date) == 8 and release_date.isdigit():
            return (
                f"{release_date[:4]}-{release_date[4:6]}-{release_date[6:8]}"
            )
        return release_date

    def download_youtube_playlist_cli(
        self,
        url: str,
        output_format: AudioFormat = AudioFormat.M4A,
        bitrate: Bitrate = Bitrate.B128,
        download_cover: bool = True,
        overwrite_existing: bool = False,
        progress_callback: Optional[callable] = None,
        track_result_callback: Optional[callable] = None,
        dry_run: bool = False,
        cancellation_event: Optional[Event] = None,
        title_callback: Optional[callable] = None,
        tracks_callback: Optional[callable] = None,
        generate_nfo: bool = False,
    ) -> dict:
        """Download a YouTube playlist or YouTube Music album collection."""
        if not self.is_youtube_collection_url(url):
            raise ValueError("URL is not a supported YouTube playlist or album")

        output_template = (
            self.base_dir
            / "YouTube"
            / "%(playlist_title)s"
            / "%(playlist_index)02d - %(title)s"
        )
        ydl_opts = self._get_ydl_opts(output_template, output_format, bitrate)
        ydl_opts["outtmpl"] = str(output_template.with_suffix(".%(ext)s"))
        ydl_opts["windowsfilenames"] = True
        ydl_opts["overwrites"] = overwrite_existing
        ydl_opts["ignoreerrors"] = True
        self._add_cancellation_hook(ydl_opts, cancellation_event)
        ydl_opts["postprocessors"].append({"key": "FFmpegMetadata"})
        if download_cover:
            ydl_opts["writethumbnail"] = True
            ydl_opts["postprocessors"].append({"key": "EmbedThumbnail"})

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            if cancellation_event and cancellation_event.is_set():
                return {
                    "collection_name": "YouTube Collection",
                    "completed_tracks": 0,
                    "failed_tracks": 0,
                    "failed_track_names": [],
                    "total_tracks": 0,
                    "output_directory": str(self.base_dir / "YouTube"),
                    "dry_run": False,
                }
            collection = ydl.extract_info(url, download=False)
            if not isinstance(collection, dict) or not collection.get("entries"):
                raise ValueError("No tracks were found in the YouTube collection")

            collection_name = collection.get("title") or collection.get("id") or "YouTube Collection"
            output_dir = self.base_dir / "YouTube" / self._sanitize_filename(collection_name)
            ydl.params["outtmpl"]["default"] = str(
                output_dir / "%(playlist_index)02d - %(title)s.%(ext)s"
            )
            entries = collection["entries"]
            total = collection.get("playlist_count") or len(entries)
            total = int(total)
            display_number_by_playlist_index = {
                int((entry or {}).get("playlist_index") or display_number): display_number
                for display_number, entry in enumerate(entries, start=1)
            }
            if title_callback:
                title_callback(collection_name)
            if tracks_callback:
                tracks = []
                for index, entry in enumerate(entries, start=1):
                    entry = entry or {}
                    video_id = entry.get("id")
                    video_url = entry.get("webpage_url") or entry.get("original_url")
                    if not video_url:
                        source_url = entry.get("url")
                        if isinstance(source_url, str) and source_url.startswith(
                            ("http://", "https://")
                        ):
                            video_url = source_url
                        elif video_id:
                            video_url = f"https://www.youtube.com/watch?v={video_id}"
                    channel = entry.get("channel") or entry.get("uploader")
                    tracks.append(
                        {
                            "name": entry.get("title") or f"Track {index}",
                            "artists": [channel] if channel else [],
                            "album_name": collection_name,
                            "duration": int(entry.get("duration") or 0),
                            "number": index,
                            "uri": video_url or "",
                        }
                    )
                tracks_callback(tracks)

            if dry_run:
                return {
                    "collection_name": collection_name,
                    "completed_tracks": 0,
                    "failed_tracks": 0,
                    "failed_track_names": [],
                    "total_tracks": total,
                    "output_directory": str(output_dir),
                    "dry_run": True,
                }

            output_dir.mkdir(parents=True, exist_ok=True)
            failed_track_names = []
            expected_paths = {}
            missing_indexes = []
            reported_results = set()
            for index, entry in enumerate(entries, start=1):
                track_name = (entry or {}).get("title") or f"Track {index}"
                if not entry:
                    failed_track_names.append(track_name)
                    if track_result_callback:
                        track_result_callback(index, track_name, "error")
                        reported_results.add(index)
                    continue

                entry_info = dict(entry)
                entry_info.setdefault("playlist_title", collection_name)
                entry_info.setdefault("playlist_index", index)
                entry_info.setdefault("playlist_count", total)
                expected_path = Path(ydl.prepare_filename(entry_info)).with_suffix(
                    f".{output_format.value}"
                )
                expected_paths[index] = (track_name, expected_path)
                if expected_path.exists() and not overwrite_existing:
                    if track_result_callback:
                        track_result_callback(index, track_name, "completed")
                        reported_results.add(index)
                else:
                    missing_indexes.append(index)

            if missing_indexes:
                started_indexes = set()

                def report_completed_files():
                    for display_number in started_indexes:
                        if (
                            display_number in reported_results
                            or not expected_paths[display_number][1].exists()
                        ):
                            continue
                        track_name = expected_paths[display_number][0]
                        if track_result_callback:
                            track_result_callback(
                                display_number, track_name, "completed"
                            )
                        reported_results.add(display_number)

                def report_progress(download_info):
                    if download_info.get("status") != "downloading":
                        return
                    info = download_info.get("info_dict") or {}
                    index = info.get("playlist_index")
                    if index is None:
                        return
                    playlist_index = int(index)
                    display_number = display_number_by_playlist_index.get(playlist_index)
                    if display_number is None or display_number in started_indexes:
                        return
                    report_completed_files()
                    started_indexes.add(display_number)
                    if progress_callback:
                        progress_callback(
                            display_number,
                            total,
                            info.get("title") or f"Track {display_number}",
                        )

                if progress_callback:
                    ydl.add_progress_hook(report_progress)

                expected_postprocessors = {
                    postprocessor.get("key")
                    for postprocessor in ydl.params.get("postprocessors", [])
                    if postprocessor.get("key")
                }
                finished_postprocessors = {}

                def report_postprocessing(postprocess_info):
                    if postprocess_info.get("status") != "finished":
                        return
                    info = postprocess_info.get("info_dict") or {}
                    index = info.get("playlist_index")
                    if index is None:
                        return
                    display_number = display_number_by_playlist_index.get(int(index))
                    if display_number is None or display_number in reported_results:
                        return
                    finished = finished_postprocessors.setdefault(display_number, set())
                    finished.add(postprocess_info.get("postprocessor"))
                    if (
                        expected_postprocessors
                        and expected_postprocessors.issubset(finished)
                        and expected_paths[display_number][1].exists()
                    ):
                        track_name = expected_paths[display_number][0]
                        if track_result_callback:
                            track_result_callback(display_number, track_name, "completed")
                        reported_results.add(display_number)

                if track_result_callback:
                    ydl.add_postprocessor_hook(report_postprocessing)

                ydl.params["playlist_items"] = ",".join(
                    str(index) for index in missing_indexes
                )
                ydl.download([url])

                if cancellation_event and cancellation_event.is_set():
                    completed_tracks = sum(
                        expected_path.exists()
                        for _, expected_path in expected_paths.values()
                    )
                    return {
                        "collection_name": collection_name,
                        "completed_tracks": completed_tracks,
                        "failed_tracks": 0,
                        "failed_track_names": [],
                        "total_tracks": total,
                        "output_directory": str(output_dir),
                        "dry_run": False,
                    }

            completed_tracks = 0
            for index, (track_name, expected_path) in expected_paths.items():
                if expected_path.exists():
                    completed_tracks += 1
                    if track_result_callback and index not in reported_results:
                        track_result_callback(index, track_name, "completed")
                        reported_results.add(index)
                else:
                    self.logger.error(
                        f"YouTube download did not create the expected file: {expected_path}"
                    )
                    failed_track_names.append(track_name)
                    if track_result_callback and index not in reported_results:
                        track_result_callback(index, track_name, "error")
                        reported_results.add(index)

        if generate_nfo and completed_tracks:
            nfo_tracks = []
            for display_number, (track_name, expected_path) in expected_paths.items():
                if not expected_path.is_file():
                    continue
                entry = entries[display_number - 1] or {}
                video_url = entry.get("webpage_url") or entry.get("original_url")
                if not video_url and entry.get("id"):
                    video_url = f"https://www.youtube.com/watch?v={entry['id']}"
                artists = self._youtube_artist_names(entry)
                nfo_tracks.append(
                    Track(
                        number=display_number,
                        total_tracks=total,
                        name=track_name,
                        duration=int(entry.get("duration") or 0),
                        uri=video_url or "",
                        artists=artists,
                        album_artist=artists,
                        release_date=self._youtube_release_date(entry),
                        album_name=collection_name,
                        youtube_url=video_url,
                    )
                )
            if nfo_tracks:
                collection_artists = self._youtube_artist_names(collection)
                NFOGenerator.generate(
                    Album(
                        name=collection_name,
                        artists=collection_artists
                        or nfo_tracks[0].artists
                        or ["Unknown Artist"],
                        release_date=self._youtube_release_date(collection),
                        genres=collection.get("categories") or [],
                        cover_url="",
                        tracks=nfo_tracks,
                        uri=url,
                    ),
                    output_dir,
                    url,
                )

        return {
            "collection_name": collection_name,
            "completed_tracks": completed_tracks,
            "failed_tracks": len(failed_track_names),
            "failed_track_names": failed_track_names,
            "total_tracks": total,
            "output_directory": str(output_dir),
            "dry_run": False,
        }

    def download_youtube_track_cli(
        self,
        url: str,
        output_format: AudioFormat = AudioFormat.M4A,
        bitrate: Bitrate = Bitrate.B128,
        overwrite_existing: bool = False,
        progress_callback: Optional[callable] = None,
        cancellation_event: Optional[Event] = None,
        title_resolver: Optional[callable] = None,
        generate_nfo: bool = False,
    ) -> dict:
        """Download one YouTube video and extract it to the selected audio format."""
        if not self.is_youtube_track_url(url):
            raise ValueError("URL is not a supported YouTube video")

        output_directory = self.base_dir
        output_template = output_directory / "%(title)s"
        ydl_opts = self._get_ydl_opts(output_template, output_format, bitrate)
        ydl_opts["windowsfilenames"] = True
        ydl_opts["overwrites"] = overwrite_existing
        ydl_opts["postprocessors"].append({"key": "FFmpegMetadata"})
        self._add_cancellation_hook(ydl_opts, cancellation_event)

        def make_result(title: str, output_path: Optional[Path], completed: bool) -> dict:
            return {
                "completed_tracks": int(completed),
                "failed_tracks": int(not completed),
                "failed_track_names": [] if completed else [title],
                "total_tracks": 1,
                "output_directory": str(output_path.parent if output_path else output_directory),
            }

        if cancellation_event and cancellation_event.is_set():
            return make_result("YouTube track", None, False)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not isinstance(info, dict):
                raise ValueError("YouTube did not return track metadata")

            track_title = str(info.get("track") or "").strip()
            title = str(
                info.get("fulltitle")
                or info.get("title")
                or track_title
                or info.get("id")
                or "YouTube track"
            ).strip()
            if track_title and track_title.casefold() not in title.casefold():
                title = f"{track_title} - {title}"
            alternate_title = str(info.get("alt_title") or "").strip()
            if alternate_title and alternate_title.casefold() not in title.casefold():
                title = f"{title} - {alternate_title}"
            if title_resolver:
                try:
                    canonical_title = title_resolver(info)
                except Exception as error:
                    self.logger.warning(f"Could not resolve YouTube track title: {error}")
                    canonical_title = None
                if canonical_title:
                    canonical_title = str(canonical_title).strip()
                    if canonical_title and canonical_title.casefold() not in title.casefold():
                        title = f"{title} - {canonical_title}"
            artist_names = self._youtube_artist_names(info)
            artist_name = ", ".join(artist_names) or "Unknown Artist"
            artist_directory = output_directory / self._sanitize_filename(str(artist_name))
            artist_directory.mkdir(parents=True, exist_ok=True)
            filename_title = self._sanitize_filename(title)
            ydl.params["outtmpl"]["default"] = str(
                artist_directory / f"{filename_title}.%(ext)s"
            )
            output_path = Path(ydl.prepare_filename(info)).with_suffix(
                f".{output_format.value}"
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)

            if cancellation_event and cancellation_event.is_set():
                return make_result(title, output_path, False)

            if output_path.exists() and not overwrite_existing:
                if generate_nfo:
                    self._generate_youtube_track_nfo(
                        info, title, artist_names, url, output_path.parent
                    )
                if progress_callback:
                    progress_callback(1, 1, title)
                return make_result(title, output_path, True)

            if progress_callback:
                progress_callback(1, 1, title)
            ydl.download([url])

            if cancellation_event and cancellation_event.is_set():
                if output_path.exists():
                    output_path.unlink()
                return make_result(title, output_path, False)

        if output_path.is_file():
            if output_format == AudioFormat.OPUS:
                opus_audio = OggOpus(str(output_path))
                opus_audio["title"] = title
                opus_audio["artist"] = artist_name
                opus_audio.save()
            self._save_youtube_cover(info, output_path.parent / "cover.jpg")
            if generate_nfo:
                self._generate_youtube_track_nfo(
                    info, title, artist_names, url, output_path.parent
                )

        return make_result(title, output_path, output_path.is_file())

    @staticmethod
    def _generate_youtube_track_nfo(
        info: dict,
        title: str,
        artists: list[str],
        source_url: str,
        output_dir: Path,
    ) -> None:
        artist_names = artists or ["Unknown Artist"]
        track = Track(
            number=1,
            total_tracks=1,
            name=title,
            duration=int(info.get("duration") or 0),
            uri=source_url,
            artists=artist_names,
            album_artist=artist_names,
            release_date=YouTubeDownloaderForCLI._youtube_release_date(info),
            genres=info.get("categories") or [],
            album_name=info.get("album") or title,
            youtube_url=source_url,
        )
        NFOGenerator.generate(
            Album(
                name=track.album_name,
                artists=artist_names,
                release_date=track.release_date,
                genres=track.genres or [],
                cover_url="",
                tracks=[track],
                uri=source_url,
            ),
            output_dir,
            source_url,
        )

    def _save_youtube_cover(self, info: dict, output_path: Path) -> None:
        """Save the uploader's channel avatar as the artist folder cover."""
        if output_path.exists():
            return

        channel_url = info.get("channel_url") or info.get("uploader_url")
        if not channel_url:
            self.logger.warning("No channel URL available to fetch a cover")
            return

        try:
            with yt_dlp.YoutubeDL(
                {"quiet": True, "extract_flat": True, "playlistend": 0}
            ) as ydl:
                channel_info = ydl.extract_info(channel_url, download=False)
        except Exception as error:
            self.logger.warning(f"Could not fetch channel info: {error}")
            return

        thumbnails = [
            t for t in (channel_info or {}).get("thumbnails") or [] if t.get("url")
        ]
        avatars = [t for t in thumbnails if "avatar" in str(t.get("id", ""))]
        avatars.sort(
            key=lambda t: (t.get("width") or 0) * (t.get("height") or 0), reverse=True
        )
        # Square avatars are listed after banners; prefer the uncropped avatar
        avatars.sort(key=lambda t: "uncropped" not in str(t.get("id", "")))

        for thumbnail in avatars:
            if self.image_downloader.download_image(thumbnail["url"], output_path):
                self.logger.info(f"Cover saved in: {output_path}")
                return
        self.logger.warning("Could not download the channel image")

    def download_track_cli(
        self, 
        track: Track, 
        output_format: AudioFormat = AudioFormat.M4A, 
        bitrate: Bitrate = Bitrate.B128,
        album_artist: str = None,
        download_lyrics: bool = False,
        progress_callback: Optional[callable] = None,
        overwrite_existing: bool = False,
        cancellation_event: Optional[Event] = None,
    ) -> tuple[Optional[Path], Optional[Track]]:
        """
        Download a single track with CLI progress support.

        Args:
            track: Track object to download
            output_format: Audio format enum
            bitrate: Audio bitrate enum
            album_artist: Artist name for file organization
            download_lyrics: Whether to download lyrics
            progress_callback: Optional function for progress reporting. 
                            Example: lambda idx, total, name: print(f"{idx}/{total} {name}")

        Returns:
            tuple: (Downloaded file path, Updated track) or (None, None) on error
        """
        try:
            if cancellation_event and cancellation_event.is_set():
                return None, None

            expected_paths = [
                self._get_output_path(
                    track,
                    album_artist=album_artist,
                    output_format=output_format,
                )
            ]
            if not overwrite_existing:
                existing_path = next(
                    (path for path in expected_paths if path.exists()),
                    None,
                )
                if existing_path:
                    self._save_cover_album(
                        track.cover_url,
                        existing_path.parent / "cover.jpg",
                    )
                    self.logger.info(f"Skipping existing track: {existing_path}")
                    return existing_path, track

            if progress_callback:
                progress_callback(1, 1, track.name)

            yt_url = self.searcher.search_track(track)
            if cancellation_event and cancellation_event.is_set():
                return None, None
            if not yt_url:
                raise ValueError(f"Not found on YouTube Music: {track.name}")

            audio_path, updated_track = self.download_track(
                track=track,
                album_artist=album_artist,
                download_lyrics=download_lyrics,
                output_format=output_format,
                bitrate=bitrate,
                overwrite_existing=overwrite_existing,
                cancellation_event=cancellation_event,
                youtube_url=yt_url,
            )

            if audio_path:
                self.logger.info(f"Track downloaded successfully: {track.name}")
                return audio_path, (updated_track or track).with_youtube_url(yt_url)
            else:
                self.logger.warning(f"Could not download track: {track.name}")
                return None, None

        except Exception as e:
            self.logger.error(f"Error downloading track {track.name}: {str(e)}", exc_info=True)
            return None, None

    def download_album_cli(
        self,
        album: Album,
        download_lyrics: bool = False,
        output_format: AudioFormat = AudioFormat.M4A,
        bitrate: Bitrate = Bitrate.B128,
        nfo: bool = False,  # Generate NFO
        cover: bool = True,  # Download cover art
        overwrite_existing: bool = False,
        progress_callback: Optional[callable] = None,  # Progress callback
        track_result_callback: Optional[callable] = None,
        cancellation_event: Optional[Event] = None,
        source_url: Optional[str] = None,
    ) -> tuple[int, int, list[str]]:  # Returns (success, total, failed track names)
        """Download a complete album with progress support.

        Args:
            album: Album object to download
            download_lyrics: Whether to download lyrics
            output_format: Audio format enum
            bitrate: Audio bitrate enum
            nfo: Whether to generate NFO file
            cover: Whether to download cover art
            progress_callback: Function that receives (current_track, total_tracks, track_name).
                            Example: lambda idx, total, name: print(f"{idx}/{total} {name}")

        Returns:
            tuple: (successful_downloads, total_tracks)
        """
        if not album.tracks:
            self.logger.error("Album contains no tracks.")
            return 0, 0, []

        success = 0
        failed_tracks = []
        for idx, track in enumerate(album.tracks, 1):
            if cancellation_event and cancellation_event.is_set():
                break
            try:
                if progress_callback:
                    progress_callback(idx, len(album.tracks), track.name)

                expected_path = self._get_output_path(
                    track, album_artist=album.artists[0], output_format=output_format
                )
                audio_path, updated_track = self.download_track(
                    track=track,
                    album_artist=album.artists[0],
                    download_lyrics=download_lyrics,
                    output_format=output_format,
                    bitrate=bitrate,
                    overwrite_existing=overwrite_existing,
                    cancellation_event=cancellation_event,
                )
                if cancellation_event and cancellation_event.is_set():
                    break
                if audio_path or expected_path.exists():
                    success += 1
                    if updated_track:
                        album.tracks[idx - 1] = updated_track
                    if track_result_callback:
                        track_result_callback(idx, track.name, "completed")
                else:
                    failed_tracks.append(track.name)
                    if track_result_callback:
                        track_result_callback(idx, track.name, "error")
            except Exception as e:
                if cancellation_event and cancellation_event.is_set():
                    break
                self.logger.error(f"Error en track {track.name}: {str(e)}")
                failed_tracks.append(track.name)
                if track_result_callback:
                    track_result_callback(idx, track.name, "error")

        if cancellation_event and cancellation_event.is_set():
            return success, len(album.tracks), failed_tracks

        # Generate metadata only if at least one track succeeded
        if success > 0:
            output_dir = self._get_album_dir(album)
            if nfo:
                self._generate_album_nfos(album.tracks, output_format, source_url)
            if cover and album.cover_url:
                self._save_cover_album(album.cover_url, output_dir / "cover.jpg")

            # Save the artist cover
            # self._save_artist_cover()

        return success, len(album.tracks), failed_tracks

    def download_playlist_cli(
        self,
        playlist: Playlist,
        output_format: AudioFormat = AudioFormat.M4A,
        bitrate: Bitrate = Bitrate.B128,
        download_lyrics: bool = False,
        cover: bool = True,
        overwrite_existing: bool = False,
        progress_callback: Optional[callable] = None,
        nfo: bool = False,
        track_result_callback: Optional[callable] = None,
        cancellation_event: Optional[Event] = None,
        source_url: Optional[str] = None,
    ) -> tuple[int, int, list[str]]:
        """Download a complete playlist with progress bar support.

        Args:
            playlist: Playlist object to download
            output_format: Audio format enum
            bitrate: Audio bitrate enum
            download_lyrics: Whether to download lyrics
            cover: Whether to download playlist cover
            progress_callback: Function that receives (current_track, total_tracks, track_name).
                            Example: lambda idx, total, name: print(f"{idx}/{total} {name}")
            nfo: Whether to generate album NFO files in the album folders

        Returns:
            tuple: (successful_downloads, total_tracks, failed_track_names)
        """
        if not playlist.name or not playlist.tracks:
            self.logger.error("Invalid playlist: missing name or no tracks")
            return 0, 0, []

        output_dir = self.get_playlist_dir(playlist)
        output_dir.mkdir(parents=True, exist_ok=True)
        success = 0
        failed_tracks = []
        downloaded_tracks = []

        for idx, track in enumerate(playlist.tracks, 1):
            if cancellation_event and cancellation_event.is_set():
                break
            try:
                # Report progress if a callback is provided
                if progress_callback:
                    progress_callback(idx, len(playlist.tracks), track.name)

                _, updated_track = self.download_track(
                    track=track,
                    album_artist=(
                        track.album_artist[0]
                        if track.album_artist
                        else None
                    ),
                    output_format=output_format,
                    bitrate=bitrate,
                    overwrite_existing=overwrite_existing,
                    download_lyrics=download_lyrics,
                    cancellation_event=cancellation_event,
                )
                if cancellation_event and cancellation_event.is_set():
                    break
                if updated_track:
                    success += 1
                    downloaded_tracks.append(updated_track)
                    playlist.tracks[idx - 1] = updated_track
                    if track_result_callback:
                        track_result_callback(idx, track.name, "completed")
                else:
                    failed_tracks.append(track.name)
                    if track_result_callback:
                        track_result_callback(idx, track.name, "error")
            except Exception as e:
                if cancellation_event and cancellation_event.is_set():
                    break
                self.logger.error(f"Error processing {track.name}: {str(e)}")
                failed_tracks.append(track.name)
                if track_result_callback:
                    track_result_callback(idx, track.name, "error")

        if cancellation_event and cancellation_event.is_set():
            return success, len(playlist.tracks), failed_tracks

        if nfo:
            self._generate_album_nfos(downloaded_tracks, output_format, source_url)
            NFOGenerator.generate_playlist(playlist, output_dir, source_url)

        if success > 0 and cover and playlist.cover_url:
            try:
                self._save_cover_album(playlist.cover_url, output_dir / "cover.jpg")
            except Exception as e:
                self.logger.error(f"Error downloading playlist cover: {str(e)}")

        self.ensure_playlist_m3u(playlist, output_format=output_format)

        return success, len(playlist.tracks), failed_tracks
