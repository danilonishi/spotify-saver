"""Youtube Downloader Module"""

from pathlib import Path
from typing import Optional

from spotifysaver.services import YoutubeMusicSearcher, LrclibAPI
from spotifysaver.metadata import NFOGenerator
from spotifysaver.downloader.youtube_downloader import YouTubeDownloader
from spotifysaver.downloader.image_downloader import ImageDownloader
from spotifysaver.models import Track, Album, Playlist
from spotifysaver.enums import AudioFormat, Bitrate
from spotifysaver.spotlog import get_logger


class YouTubeDownloaderForCLI(YouTubeDownloader):
    """Downloads tracks from YouTube Music and adds Spotify metadata.

    This class handles the complete download process including audio download,
    metadata injection, lyrics fetching, and file organization.

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


    def download_track_cli(
        self, 
        track: Track, 
        output_format: AudioFormat = AudioFormat.M4A, 
        bitrate: Bitrate = Bitrate.B128,
        album_artist: str = None,
        download_lyrics: bool = False,
        progress_callback: Optional[callable] = None,
        overwrite_existing: bool = False,
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
            expected_paths = [
                self._get_output_path(
                    track,
                    album_artist=album_artist,
                    output_format=output_format,
                )
            ]
            if track.album_artist:
                expected_paths.append(
                    self._get_output_path(
                        track,
                        album_artist=track.album_artist[0],
                        output_format=output_format,
                    )
                )

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
            if not yt_url:
                raise ValueError(f"No se encontró en YouTube Music: {track.name}")

            audio_path, updated_track = self.download_track(
                track=track,
                album_artist=album_artist,
                download_lyrics=download_lyrics,
                output_format=output_format,
                bitrate=bitrate,
                overwrite_existing=overwrite_existing,
            )

            if audio_path:
                self.logger.info(f"Track descargado exitosamente: {track.name}")
                return audio_path, updated_track
            else:
                self.logger.warning(f"No se pudo descargar el track: {track.name}")
                return None, None

        except Exception as e:
            self.logger.error(f"Error al descargar el track {track.name}: {str(e)}", exc_info=True)
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
            self.logger.error("Álbum no contiene tracks.")
            return 0, 0, []

        success = 0
        failed_tracks = []
        for idx, track in enumerate(album.tracks, 1):
            try:
                if progress_callback:
                    progress_callback(idx, len(album.tracks), track.name)

                expected_path = self._get_output_path(
                    track, album_artist=album.artists[0], output_format=output_format
                )
                track_artist_path = None
                if track.album_artist:
                    track_artist_path = self._get_output_path(
                        track,
                        album_artist=track.album_artist[0],
                        output_format=output_format,
                    )
                audio_path, _ = self.download_track(
                    track=track,
                    album_artist=album.artists[0],
                    download_lyrics=download_lyrics,
                    output_format=output_format,
                    bitrate=bitrate,
                    overwrite_existing=overwrite_existing,
                )
                if audio_path or expected_path.exists() or (
                    track_artist_path and track_artist_path.exists()
                ):
                    success += 1
                else:
                    failed_tracks.append(track.name)
            except Exception as e:
                self.logger.error(f"Error en track {track.name}: {str(e)}")
                failed_tracks.append(track.name)

        # Generar metadatos solo si hay éxitos
        if success > 0:
            output_dir = self._get_album_dir(album)
            if nfo:
                NFOGenerator.generate(album, output_dir)
            if cover and album.cover_url:
                self._save_cover_album(album.cover_url, output_dir / "cover.jpg")

            # Guarda el cover del artista
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

        Returns:
            tuple: (successful_downloads, total_tracks)
        """
        if not playlist.name or not playlist.tracks:
            self.logger.error("Playlist inválida: sin nombre o tracks vacíos")
            return 0, 0, []

        output_dir = self.get_playlist_dir(playlist)
        output_dir.mkdir(parents=True, exist_ok=True)
        success = 0
        failed_tracks = []

        for idx, track in enumerate(playlist.tracks, 1):
            try:
                # Notificar progreso (si hay callback)
                if progress_callback:
                    progress_callback(idx, len(playlist.tracks), track.name)

                _, updated_track = self.download_track(
                    track,
                    album_artist=(
                        track.album_artist[0]
                        if track.album_artist
                        else None
                    ),
                    output_format=output_format,
                    bitrate=bitrate,
                    overwrite_existing=overwrite_existing,
                    download_lyrics=download_lyrics,
                )
                if updated_track:
                    success += 1
                else:
                    failed_tracks.append(track.name)
            except Exception as e:
                self.logger.error(f"Error en {track.name}: {str(e)}")
                failed_tracks.append(track.name)

        if success > 0 and cover and playlist.cover_url:
            try:
                self._save_cover_album(playlist.cover_url, output_dir / "cover.jpg")
            except Exception as e:
                self.logger.error(f"Error downloading playlist cover: {str(e)}")

        self.ensure_playlist_m3u(playlist, output_format=output_format)

        return success, len(playlist.tracks), failed_tracks
