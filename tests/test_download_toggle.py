from pathlib import Path

import pytest

from spotifysaver.api.schemas import DownloadRequest
from spotifysaver.downloader.youtube_downloader import YouTubeDownloader
from spotifysaver.enums import AudioFormat
from spotifysaver.models.playlist import Playlist
from spotifysaver.models.track import Track


def test_download_request_defaults_to_disabled():
    request = DownloadRequest(
        spotify_url="https://open.spotify.com/track/2t9DE7p2wx4McTXQm0y2Fe?si=278359903d454ee1",
        output_format="mp3",
    )

    assert request.overwrite_existing is False


def test_download_request_allows_enabling_downloads():
    request = DownloadRequest(
        spotify_url="https://open.spotify.com/track/2t9DE7p2wx4McTXQm0y2Fe?si=278359903d454ee1",
        output_format="mp3",
        overwrite_existing=True,
    )

    assert request.overwrite_existing is True


def _make_track():
    return Track(
        number=1,
        total_tracks=1,
        name="Track One",
        duration=180,
        uri="spotify:track:1",
        artists=["Artist One"],
        album_artist=["Artist One"],
        release_date="2024-01-01",
        album_name="Album One",
        source_type="playlist",
        playlist_name="My Playlist",
    )


def test_existing_track_is_skipped_when_overwrite_is_disabled(tmp_path, monkeypatch):
    downloader = YouTubeDownloader(base_dir=str(tmp_path))
    track = _make_track()
    existing_path = downloader._get_output_path(track, output_format=AudioFormat.MP3)
    existing_path.write_text("existing audio", encoding="utf-8")

    monkeypatch.setattr(
        downloader.searcher,
        "search_track",
        lambda _: pytest.fail("YouTube search should not run for an existing track"),
    )

    audio_path, updated_track = downloader.download_track(
        track, output_format=AudioFormat.MP3, overwrite_existing=False
    )

    assert audio_path == existing_path
    assert updated_track == track
    assert existing_path.read_text(encoding="utf-8") == "existing audio"


def test_missing_track_is_attempted_when_overwrite_is_disabled(tmp_path, monkeypatch):
    downloader = YouTubeDownloader(base_dir=str(tmp_path))
    track = _make_track()
    searched = []

    monkeypatch.setattr(
        downloader.searcher,
        "search_track",
        lambda value: searched.append(value) or "https://music.youtube.com/watch?v=test",
    )
    monkeypatch.setattr(downloader, "_get_ydl_opts", lambda *args: {})

    class FailingYoutubeDL:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def download(self, urls):
            raise RuntimeError("test download stop")

    monkeypatch.setattr(
        "spotifysaver.downloader.youtube_downloader.yt_dlp.YoutubeDL",
        lambda _: FailingYoutubeDL(),
    )

    downloader.download_track(
        track, output_format=AudioFormat.MP3, overwrite_existing=False
    )

    assert searched == [track]


def test_playlist_m3u_is_created_with_local_track_paths(tmp_path):
    downloader = YouTubeDownloader(base_dir=str(tmp_path))
    track = Track(
        number=1,
        total_tracks=1,
        name="Track One",
        duration=180,
        uri="spotify:track:1",
        artists=["Artist One"],
        album_artist=["Artist One"],
        release_date="2024-01-01",
        album_name="Album One",
        source_type="playlist",
        playlist_name="My Playlist",
    )
    playlist = Playlist(
        name="My Playlist",
        description="",
        owner="me",
        uri="spotify:playlist:1",
        cover_url="",
        tracks=[track],
    )

    local_track_dir = (
        tmp_path
        / "Artist One"
        / "Album One (2024)"
    )
    local_track_dir.mkdir(parents=True)
    local_file = local_track_dir / "1 - Artist One - Track One.mp3"
    local_file.write_text("audio", encoding="utf-8")
    m3u_path = downloader.get_playlist_m3u_path(playlist)
    m3u_path.write_text("stale absolute path\n", encoding="utf-8")

    m3u_path = downloader.ensure_playlist_m3u(playlist, output_format=AudioFormat.MP3)

    assert m3u_path is not None
    assert m3u_path.exists()
    playlist_contents = m3u_path.read_text(encoding="utf-8")
    assert "../Artist One/Album One (2024)/1 - Artist One - Track One.mp3" in playlist_contents
    assert str(local_file.resolve()) not in playlist_contents
    assert m3u_path.name == "My Playlist.m3u"
