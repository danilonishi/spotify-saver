"""NFO Generator for Jellyfin-compatible XML metadata files.

This module generates XML metadata files (.nfo) that are compatible with Jellyfin
media server for organizing music libraries with proper metadata.
"""

from typing import Optional
from pathlib import Path
from datetime import datetime
from xml.etree import ElementTree as ET
from xml.dom import minidom

from spotifysaver.models.album import Album
from spotifysaver.models.playlist import Playlist
from spotifysaver.services.schemas import AlbumADBResponse
from spotifysaver.services import TheAudioDBService


class NFOGenerator:
    """Generator for Jellyfin-compatible NFO metadata files.
    
    This class provides static methods to generate XML metadata files for albums
    that are compatible with Jellyfin media server. These files contain detailed
    information about albums, tracks, artists, and other metadata.
    """

    @staticmethod
    def _get_theaudiodb_data(album: Album) -> Optional[AlbumADBResponse]:
        service = TheAudioDBService()
        return service.get_album_metadata(album.artists[0], album.name)

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """Convert seconds to MM:SS format.
        
        Args:
            seconds: Duration in seconds
            
        Returns:
            str: Formatted duration string in MM:SS format
        """
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _web_url(source_url: Optional[str]) -> Optional[str]:
        if not source_url:
            return None
        if source_url.startswith("spotify:"):
            parts = source_url.split(":")
            if len(parts) == 3:
                return f"https://open.spotify.com/{parts[1]}/{parts[2]}"
        return source_url

    @staticmethod
    def generate(album: Album, output_dir: Path, source_url: Optional[str] = None):
        """Generate an album.nfo file in the specified directory.
        
        Creates a Jellyfin-compatible XML metadata file containing album information,
        track listings, artist details, genres, and other metadata required for
        proper media library organization.

        Args:
            album: Album object containing the album information and tracks
            output_dir: Directory where the album.nfo file will be saved
            source_url: URL used to request the album or its containing playlist
        """
        # Root element
        root = ET.Element("album")

        # Datos base de Spotify
        ET.SubElement(root, "title").text = album.name
        album_url = source_url or getattr(album, "uri", None)
        if not album_url and album.tracks:
            album_url = album.tracks[0].uri
        album_url = NFOGenerator._web_url(album_url)
        if album_url:
            ET.SubElement(root, "url").text = album_url
        if album.release_date:
            ET.SubElement(root, "year").text = album.release_date[:4]
            ET.SubElement(root, "premiered").text = album.release_date
            ET.SubElement(root, "releasedate").text = album.release_date

        total_seconds = sum(t.duration for t in album.tracks)
        runtime_minutes = total_seconds // 60
        ET.SubElement(root, "runtime").text = str(runtime_minutes)

        if album.genres:
            for genre in album.genres:
                ET.SubElement(root, "genre").text = genre

        for artist in album.artists:
            ET.SubElement(root, "artist").text = artist
        ET.SubElement(root, "albumartist").text = ", ".join(album.artists)

        # Tracks
        for track in album.tracks:
            track_elem = ET.SubElement(root, "track")
            ET.SubElement(track_elem, "position").text = str(track.number)
            ET.SubElement(track_elem, "title").text = track.name
            ET.SubElement(track_elem, "duration").text = NFOGenerator._format_duration(track.duration)
            if track.youtube_url:
                ET.SubElement(track_elem, "url").text = track.youtube_url

        # Datos extra de TheAudioDB
        adb_data = NFOGenerator._get_theaudiodb_data(album)
        if adb_data:
            if adb_data.genre:
                ET.SubElement(root, "genre").text = adb_data.genre
            if adb_data.description:
                # description is a list of AlbumDescription objects; use the first one
                desc = adb_data.description[0].description if adb_data.description else ""
                ET.SubElement(root, "review").text = desc
                ET.SubElement(root, "outline").text = desc
            if adb_data.id:
                ET.SubElement(root, "audiodbalbumid").text = str(adb_data.id)
            if adb_data.artist_id:
                ET.SubElement(root, "audiodbartistid").text = str(adb_data.artist_id)
            if adb_data.musicbrainz_id:
                ET.SubElement(root, "musicbrainzalbumid").text = adb_data.musicbrainz_id
            if adb_data.artist_musicbrainz_id:
                ET.SubElement(root, "musicbrainzalbumartistid").text = adb_data.artist_musicbrainz_id

        # Static fields
        ET.SubElement(root, "lockdata").text = "false"
        ET.SubElement(root, "dateadded").text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Pretty XML
        xml_str = ET.tostring(root, encoding="utf-8", method="xml")
        pretty_xml = minidom.parseString(xml_str).toprettyxml(indent="  ")

        nfo_path = output_dir / "album.nfo"
        with open(nfo_path, "w", encoding="utf-8") as f:
            f.write(pretty_xml)

    @staticmethod
    def generate_playlist(
        playlist: Playlist, output_dir: Path, source_url: Optional[str] = None
    ) -> None:
        """Generate a playlist.nfo file with its source URL."""
        root = ET.Element("playlist")
        ET.SubElement(root, "title").text = playlist.name
        if playlist.description:
            ET.SubElement(root, "plot").text = playlist.description
        if playlist.owner:
            ET.SubElement(root, "owner").text = playlist.owner
        for track in playlist.tracks:
            track_elem = ET.SubElement(root, "track")
            ET.SubElement(track_elem, "position").text = str(track.playlist_position or track.number)
            ET.SubElement(track_elem, "title").text = track.name
            if track.youtube_url:
                ET.SubElement(track_elem, "url").text = track.youtube_url
        playlist_url = NFOGenerator._web_url(source_url or playlist.uri)
        if playlist_url:
            ET.SubElement(root, "url").text = playlist_url
        ET.SubElement(root, "dateadded").text = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(root).write(
            output_dir / "playlist.nfo", encoding="utf-8", xml_declaration=True
        )
