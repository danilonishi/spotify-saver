import os
import asyncio
import xml.etree.ElementTree as ET
from threading import Event
from pathlib import Path

import pytest

from spotifysaver.api.schemas import DownloadRequest, DownloadStatus
from spotifysaver.downloader.youtube_downloader import YouTubeDownloader
from spotifysaver.downloader.youtube_downloader_for_cli import YouTubeDownloaderForCLI
from spotifysaver.enums import AudioFormat
from spotifysaver.models.album import Album
from spotifysaver.models.playlist import Playlist
from spotifysaver.models.track import Track
from spotifysaver.metadata import NFOGenerator
from spotifysaver.api.services.download_service import DownloadService


def test_download_request_defaults_to_disabled():
    request = DownloadRequest(
        spotify_url="https://open.spotify.com/track/2t9DE7p2wx4McTXQm0y2Fe?si=278359903d454ee1",
        output_format="mp3",
    )

    assert request.overwrite_existing is False


def test_download_request_accepts_opus_for_youtube_audio():
    request = DownloadRequest(
        spotify_url="https://www.youtube.com/watch?v=3TNpOD6bov8",
        output_format="opus",
        bit_rate=256,
        download_lyrics=True,
        generate_nfo=True,
        overwrite_existing=False,
    )

    assert request.output_format == "opus"
    assert request.bit_rate == 256
    assert request.download_lyrics is True
    assert request.generate_nfo is True
    assert request.overwrite_existing is False


def test_youtube_audio_options_do_not_enable_unplayable_formats(tmp_path):
    downloader = YouTubeDownloader(base_dir=str(tmp_path))

    options = downloader._get_ydl_opts(tmp_path / "audio")

    assert "allow_unplayable_formats" not in options


@pytest.mark.parametrize("output_format", list(AudioFormat))
@pytest.mark.parametrize("bitrate", [96, 128, 192, 256])
def test_youtube_audio_options_pass_selected_format_and_bitrate(
    tmp_path, output_format, bitrate
):
    downloader = YouTubeDownloader(base_dir=str(tmp_path))

    options = downloader._get_ydl_opts(
        tmp_path / "audio", output_format, downloader.int_to_bitrate(bitrate)
    )
    extract_audio = next(
        processor
        for processor in options["postprocessors"]
        if processor["key"] == "FFmpegExtractAudio"
    )

    assert extract_audio["preferredcodec"] == output_format.value
    assert extract_audio["preferredquality"] == str(bitrate)


def test_download_status_defaults_to_m4a():
    status = DownloadStatus(task_id="task-1", status="queued", progress=0)

    assert status.output_format == "m4a"


def test_download_request_allows_enabling_downloads():
    request = DownloadRequest(
        spotify_url="https://open.spotify.com/track/2t9DE7p2wx4McTXQm0y2Fe?si=278359903d454ee1",
        output_format="mp3",
        overwrite_existing=True,
    )

    assert request.overwrite_existing is True


def test_download_request_defaults_to_256_kbps_and_accepts_256():
    default_request = DownloadRequest(
        spotify_url="https://open.spotify.com/track/2t9DE7p2wx4McTXQm0y2Fe?si=278359903d454ee1",
    )
    explicit_request = DownloadRequest(
        spotify_url="https://open.spotify.com/track/2t9DE7p2wx4McTXQm0y2Fe?si=278359903d454ee1",
        bit_rate=256,
    )

    assert default_request.bit_rate == 256
    assert explicit_request.bit_rate == 256
    assert default_request.output_format == "m4a"


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


def test_cli_existing_track_skips_search_when_overwrite_is_disabled(tmp_path, monkeypatch):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))
    track = _make_track()
    existing_path = downloader._get_output_path(track, output_format=AudioFormat.MP3)
    existing_path.write_text("existing audio", encoding="utf-8")

    monkeypatch.setattr(
        downloader.searcher,
        "search_track",
        lambda _: pytest.fail("YouTube search should not run for an existing track"),
    )

    audio_path, updated_track = downloader.download_track_cli(
        track,
        output_format=AudioFormat.MP3,
        overwrite_existing=False,
    )

    assert audio_path == existing_path
    assert updated_track == track


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


def test_active_track_download_stops_from_yt_dlp_progress_hook(tmp_path, monkeypatch):
    downloader = YouTubeDownloader(base_dir=str(tmp_path))
    track = _make_track()
    cancellation_event = Event()
    monkeypatch.setattr(
        downloader.searcher,
        "search_track",
        lambda _: "https://music.youtube.com/watch?v=test",
    )
    monkeypatch.setattr(downloader, "_get_ydl_opts", lambda *args: {})

    class CancellingYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def download(self, _urls):
            cancellation_event.set()
            self.options["progress_hooks"][0]({"status": "downloading"})

    monkeypatch.setattr(
        "spotifysaver.downloader.youtube_downloader.yt_dlp.YoutubeDL",
        CancellingYoutubeDL,
    )

    result = downloader.download_track(track, cancellation_event=cancellation_event)

    assert result == (None, None)


@pytest.mark.parametrize(
    ("metadata", "expected_artist"),
    [
        (
            {
                "title": "帝国のテーマ",
                "fulltitle": "帝国のテーマ",
                "track": "帝国のテーマ",
                "artist": "Hitoshi Sakimoto, Hitoshi Sakimoto, Hitoshi Sakimoto",
                "channel": "Hitoshi Sakimoto - Topic",
                "uploader": "Hitoshi Sakimoto - Topic",
                "ext": "webm",
            },
            "Hitoshi Sakimoto",
        ),
        (
            {
                "title": "Empire Theme",
                "channel": "Hitoshi Sakimoto",
                "uploader": "Hitoshi Sakimoto - Topic",
                "ext": "webm",
            },
            "Hitoshi Sakimoto",
        ),
    ],
)
def test_direct_youtube_track_uses_artist_or_channel_folder(
    tmp_path, monkeypatch, metadata, expected_artist
):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))

    class FakeYoutubeDL:
        options = None
        downloaded_path = None

        def __init__(self, options):
            self.params = options
            self.params["outtmpl"] = {"default": self.params["outtmpl"]}
            FakeYoutubeDL.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download):
            assert url == "https://music.youtube.com/watch?v=YPnMd26jwrU"
            assert download is False
            self.info = metadata
            return self.info

        def prepare_filename(self, info):
            return self.params["outtmpl"]["default"] % info

        def download(self, urls):
            assert urls == ["https://music.youtube.com/watch?v=YPnMd26jwrU"]
            output_path = Path(self.prepare_filename(self.info)).with_suffix(".mp3")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"mp3")
            FakeYoutubeDL.downloaded_path = output_path

    monkeypatch.setattr(
        "spotifysaver.downloader.youtube_downloader_for_cli.yt_dlp.YoutubeDL",
        FakeYoutubeDL,
    )

    result = downloader.download_youtube_track_cli(
        "https://music.youtube.com/watch?v=YPnMd26jwrU",
        output_format=AudioFormat.MP3,
        title_resolver=(
            lambda _info: "The Archadian Empire" if metadata.get("fulltitle") else None
        ),
    )

    assert result["completed_tracks"] == 1
    assert Path(result["output_directory"]) == tmp_path / expected_artist
    assert "YouTube" not in Path(result["output_directory"]).parts
    if metadata.get("fulltitle"):
        assert FakeYoutubeDL.downloaded_path.name == "帝国のテーマ - The Archadian Empire.mp3"
    assert FakeYoutubeDL.options["postprocessors"][0]["preferredcodec"] == "mp3"
    assert any(
        processor.get("key") == "FFmpegMetadata"
        for processor in FakeYoutubeDL.options["postprocessors"]
    )


def test_direct_youtube_opus_download_writes_title_and_artist_tags(
    tmp_path, monkeypatch
):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))
    saved_tags = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            self.params = options
            self.params["outtmpl"] = {"default": self.params["outtmpl"]}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            assert download is False
            return {"title": "Direct upload", "channel": "Test artist", "ext": "webm"}

        def prepare_filename(self, info):
            return self.params["outtmpl"]["default"] % info

        def download(self, _urls):
            output_path = Path(self.prepare_filename(
                {"title": "Direct upload", "channel": "Test artist", "ext": "webm"}
            )).with_suffix(".opus")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"opus")

    class FakeOggOpus:
        def __init__(self, _path):
            self.tags = saved_tags

        def __setitem__(self, key, value):
            self.tags[key] = value

        def save(self):
            pass

    monkeypatch.setattr(
        "spotifysaver.downloader.youtube_downloader_for_cli.yt_dlp.YoutubeDL",
        FakeYoutubeDL,
    )
    monkeypatch.setattr(
        "spotifysaver.downloader.youtube_downloader_for_cli.OggOpus",
        FakeOggOpus,
    )
    monkeypatch.setattr(downloader, "_save_youtube_cover", lambda *_args: None)

    result = downloader.download_youtube_track_cli(
        "https://www.youtube.com/watch?v=test",
        output_format=AudioFormat.OPUS,
    )

    assert result["completed_tracks"] == 1
    assert saved_tags == {"title": "Direct upload", "artist": "Test artist"}


def test_direct_youtube_track_generates_nfo_when_enabled(tmp_path, monkeypatch):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))
    metadata = {
        "title": "Direct upload",
        "channel": "Test artist",
        "duration": 185,
        "upload_date": "20241003",
        "ext": "webm",
    }
    generated = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.params = options
            self.params["outtmpl"] = {"default": self.params["outtmpl"]}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            assert download is False
            return metadata

        def prepare_filename(self, info):
            return self.params["outtmpl"]["default"] % info

        def download(self, _urls):
            output_path = Path(self.prepare_filename(metadata)).with_suffix(".m4a")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.touch()

    monkeypatch.setattr(
        "spotifysaver.downloader.youtube_downloader_for_cli.yt_dlp.YoutubeDL",
        FakeYoutubeDL,
    )
    monkeypatch.setattr(downloader, "_save_youtube_cover", lambda *_args: None)
    monkeypatch.setattr(
        NFOGenerator,
        "generate",
        lambda album, output_dir, source_url=None: generated.append(
            (album, output_dir, source_url)
        ),
    )

    result = downloader.download_youtube_track_cli(
        "https://www.youtube.com/watch?v=direct-track",
        generate_nfo=True,
    )

    assert result["completed_tracks"] == 1
    album, output_dir, source_url = generated[0]
    assert album.name == "Direct upload"
    assert album.artists == ["Test artist"]
    assert album.tracks[0].duration == 185
    assert album.tracks[0].release_date == "2024-10-03"
    assert album.tracks[0].youtube_url == source_url
    assert output_dir == Path(result["output_directory"])
    assert source_url == "https://www.youtube.com/watch?v=direct-track"


def test_download_service_passes_nfo_setting_to_direct_youtube_track(
    tmp_path, monkeypatch
):
    service = DownloadService(output_dir=str(tmp_path), generate_nfo=True)
    calls = []

    def download_youtube_track(*args):
        calls.append(args)
        return {"completed_tracks": 1}

    monkeypatch.setattr(
        service.downloader, "download_youtube_track_cli", download_youtube_track
    )

    result = asyncio.run(
        service._download_youtube_track(
            "https://www.youtube.com/watch?v=direct-track"
        )
    )

    assert result["completed_tracks"] == 1
    assert calls[0][-1] is True


def test_direct_youtube_collection_generates_nfo_when_enabled(
    tmp_path, monkeypatch
):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))
    generated = []
    collection_url = "https://www.youtube.com/playlist?list=collection-id"
    collection = {
        "title": "Sample Collection",
        "playlist_count": 1,
        "entries": [
            {
                "id": "collection-track",
                "title": "Collection Track",
                "channel": "Sample Artist",
                "duration": 120,
                "playlist_index": 1,
            }
        ],
    }

    class FakeYoutubeDL:
        def __init__(self, options):
            self.params = options
            self.params["outtmpl"] = {"default": self.params["outtmpl"]}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            assert download is False
            return collection

        def prepare_filename(self, info):
            template = self.params["outtmpl"]["default"]
            return (
                template.replace("%(playlist_index)02d", "01")
                .replace("%(title)s", info["title"])
                .replace("%(ext)s", "webm")
            )

        def download(self, _urls):
            output_path = Path(
                self.params["outtmpl"]["default"]
                .replace("%(playlist_index)02d", "01")
                .replace("%(title)s", "Collection Track")
                .replace("%(ext)s", "m4a")
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.touch()

    monkeypatch.setattr(
        "spotifysaver.downloader.youtube_downloader_for_cli.yt_dlp.YoutubeDL",
        FakeYoutubeDL,
    )
    monkeypatch.setattr(
        NFOGenerator,
        "generate",
        lambda album, output_dir, source_url=None: generated.append(
            (album, output_dir, source_url)
        ),
    )

    result = downloader.download_youtube_playlist_cli(
        collection_url,
        download_cover=False,
        generate_nfo=True,
    )

    assert result["completed_tracks"] == 1
    album, output_dir, source_url = generated[0]
    assert album.name == "Sample Collection"
    assert album.tracks[0].name == "Collection Track"
    assert album.tracks[0].youtube_url == (
        "https://www.youtube.com/watch?v=collection-track"
    )
    assert output_dir == Path(result["output_directory"])
    assert source_url == collection_url


def test_direct_youtube_track_fails_when_download_creates_no_output(
    tmp_path, monkeypatch
):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))
    metadata = {
        "title": "Track One",
        "channel": "Artist One",
        "ext": "webm",
    }

    class FakeYoutubeDL:
        def __init__(self, options):
            self.params = options
            self.params["outtmpl"] = {"default": self.params["outtmpl"]}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            assert download is False
            return metadata

        def prepare_filename(self, info):
            return self.params["outtmpl"]["default"] % info

        def download(self, _urls):
            return 0

    monkeypatch.setattr(
        "spotifysaver.downloader.youtube_downloader_for_cli.yt_dlp.YoutubeDL",
        FakeYoutubeDL,
    )

    result = downloader.download_youtube_track_cli(
        "https://www.youtube.com/watch?v=3TNpOD6bov8",
        output_format=AudioFormat.OPUS,
    )

    assert result["completed_tracks"] == 0
    assert result["failed_tracks"] == 1
    assert result["failed_track_names"] == ["Track One"]


def test_album_counts_existing_output_when_download_returns_no_path(tmp_path, monkeypatch):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))
    track = _make_track()
    album = Album(
        name="Album One",
        artists=["Artist One"],
        release_date="2024-01-01",
        genres=[],
        cover_url="",
        tracks=[track],
    )
    expected_path = downloader._get_output_path(
        track, album_artist="Artist One", output_format=AudioFormat.MP3
    )
    expected_path.write_text("downloaded audio", encoding="utf-8")

    monkeypatch.setattr(
        downloader,
        "download_track",
        lambda **kwargs: (None, None),
    )

    success, total, failed_tracks = downloader.download_album_cli(
        album,
        output_format=AudioFormat.MP3,
        overwrite_existing=False,
    )

    assert (success, total, failed_tracks) == (1, 1, [])


def test_album_redownloads_when_canonical_output_is_missing(tmp_path, monkeypatch):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))
    track = _make_track()
    track = Track(
        **{
            **track.__dict__,
            "album_artist": ["Track Album Artist"],
        }
    )
    album = Album(
        name="Album One",
        artists=["Album Artist"],
        release_date="2024-01-01",
        genres=[],
        cover_url="",
        tracks=[track],
    )
    alternate_path = downloader._get_output_path(
        track,
        album_artist="Track Album Artist",
        output_format=AudioFormat.MP3,
    )
    alternate_path.write_text("downloaded audio", encoding="utf-8")

    downloaded = []

    monkeypatch.setattr(
        downloader,
        "download_track",
        lambda **kwargs: downloaded.append(kwargs) or (None, None),
    )

    success, total, failed_tracks = downloader.download_album_cli(
        album,
        output_format=AudioFormat.MP3,
        overwrite_existing=False,
    )

    assert (success, total, failed_tracks) == (0, 1, [track.name])
    assert downloaded
    assert downloaded[0]["album_artist"] == "Album Artist"


def test_album_retry_writes_matched_youtube_url_to_track_nfo(tmp_path, monkeypatch):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))
    track = _make_track()
    album = Album(
        name="Album One",
        artists=["Artist One"],
        release_date="2024-01-01",
        genres=[],
        cover_url="",
        tracks=[track],
    )
    youtube_url = "https://music.youtube.com/watch?v=qsound-logo"
    album_dir = downloader._get_album_dir(album)
    album_dir.mkdir(parents=True)
    monkeypatch.setattr(
        downloader,
        "download_track",
        lambda **_kwargs: (
            album_dir / "1 - Artist One - Track One.m4a",
            track.with_youtube_url(youtube_url),
        ),
    )
    monkeypatch.setattr(NFOGenerator, "_get_theaudiodb_data", lambda _album: None)

    success, total, failures = downloader.download_album_cli(album, nfo=True)

    assert (success, total, failures) == (1, 1, [])
    nfo = ET.parse(album_dir / "album.nfo")
    assert nfo.findtext("./track/url") == youtube_url


def test_album_cancellation_does_not_start_the_next_track(tmp_path, monkeypatch):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))
    first_track = _make_track()
    second_track = Track(
        **{
            **first_track.__dict__,
            "number": 2,
            "name": "Track Two",
            "uri": "spotify:track:2",
        }
    )
    album = Album(
        name="Album One",
        artists=["Artist One"],
        release_date="2024-01-01",
        genres=[],
        cover_url="",
        tracks=[first_track, second_track],
    )
    cancellation_event = Event()
    attempted = []

    def cancel_during_track(**kwargs):
        attempted.append(kwargs["track"].name)
        cancellation_event.set()
        return None, None

    monkeypatch.setattr(downloader, "download_track", cancel_during_track)

    result = downloader.download_album_cli(
        album, cancellation_event=cancellation_event
    )

    assert result == (0, 2, [])
    assert attempted == [first_track.name]


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
    expected_relative_path = Path(
        os.path.relpath(local_file, start=m3u_path.parent)
    ).as_posix()
    assert expected_relative_path in playlist_contents
    assert str(local_file.resolve()) not in playlist_contents
    assert m3u_path.name == "My Playlist.m3u"


def test_playlist_generates_album_nfo_in_album_directory(tmp_path, monkeypatch):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))
    track = _make_track()
    playlist = Playlist(
        name="My Playlist",
        description="",
        owner="me",
        uri="spotify:playlist:1",
        cover_url="",
        tracks=[track],
    )
    generated = []
    monkeypatch.setattr(
        downloader.searcher,
        "search_track",
        lambda _track: "https://music.youtube.com/watch?v=matched-video",
    )

    monkeypatch.setattr(
        downloader,
        "download_track",
        lambda **kwargs: (downloader._get_output_path(
            kwargs["track"],
            album_artist=kwargs["album_artist"],
            output_format=kwargs["output_format"],
        ), kwargs["track"]),
    )
    monkeypatch.setattr(
        "spotifysaver.downloader.youtube_downloader_for_cli.NFOGenerator.generate",
        lambda album, output_dir, source_url=None: generated.append(
            (album, output_dir, source_url)
        ),
    )

    result = downloader.download_playlist_cli(
        playlist,
        output_format=AudioFormat.MP3,
        cover=False,
        nfo=True,
    )

    assert result == (1, 1, [])
    assert len(generated) == 1
    assert generated[0][0].name == "Album One"
    assert generated[0][1] == tmp_path / "Artist One" / "Album One (2024)"
    assert generated[0][2] is None


def test_album_and_playlist_nfos_store_source_urls(tmp_path, monkeypatch):
    track = _make_track().with_youtube_url(
        "https://music.youtube.com/watch?v=matched-video"
    )
    album = Album(
        name="Album One",
        artists=["Artist One"],
        release_date="2024-01-01",
        genres=[],
        cover_url="",
        tracks=[track],
        uri="spotify:album:album-id",
    )
    playlist = Playlist(
        name="My Playlist",
        description="Playlist description",
        owner="me",
        uri="spotify:playlist:playlist-id",
        cover_url="",
        tracks=[track],
    )
    monkeypatch.setattr(NFOGenerator, "_get_theaudiodb_data", lambda _album: None)

    NFOGenerator.generate(album, tmp_path, "https://open.spotify.com/album/album-id")
    NFOGenerator.generate_playlist(
        playlist, tmp_path, "https://open.spotify.com/playlist/playlist-id"
    )

    assert ET.parse(tmp_path / "album.nfo").findtext("url") == (
        "https://open.spotify.com/album/album-id"
    )
    album_nfo = ET.parse(tmp_path / "album.nfo")
    assert album_nfo.findtext("./track/url") == (
        "https://music.youtube.com/watch?v=matched-video"
    )
    playlist_nfo = ET.parse(tmp_path / "playlist.nfo")
    assert playlist_nfo.findtext("url") == (
        "https://open.spotify.com/playlist/playlist-id"
    )
    assert playlist_nfo.findtext("plot") == "Playlist description"
    assert playlist_nfo.findtext("./track/url") == (
        "https://music.youtube.com/watch?v=matched-video"
    )


def test_single_track_audio_nfo_stores_source_url(tmp_path, monkeypatch):
    service = DownloadService(output_dir=str(tmp_path), generate_nfo=True)
    track = _make_track()
    audio_path = tmp_path / "Artist One" / "Album One" / "track.m4a"
    audio_path.parent.mkdir(parents=True)
    audio_path.touch()
    generated = []
    monkeypatch.setattr(
        service.downloader,
        "download_track_cli",
        lambda *_args, **_kwargs: (audio_path, track),
    )
    monkeypatch.setattr(
        NFOGenerator,
        "generate",
        lambda album, output_dir, source_url=None: generated.append(
            (album, output_dir, source_url)
        ),
    )

    service._download_track_sync(
        track, None, "https://open.spotify.com/track/track-id"
    )

    assert generated[0][0].name == "Album One"
    assert generated[0][1] == audio_path.parent
    assert generated[0][2] == "https://open.spotify.com/track/track-id"


def test_cli_track_download_keeps_matched_youtube_url(tmp_path, monkeypatch):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))
    track = _make_track()
    youtube_url = "https://music.youtube.com/watch?v=matched-video"
    audio_path = tmp_path / "track.m4a"
    monkeypatch.setattr(
        downloader.searcher, "search_track", lambda _track: youtube_url
    )
    monkeypatch.setattr(
        downloader,
        "download_track",
        lambda **kwargs: (audio_path, kwargs["track"]),
    )

    downloaded_path, downloaded_track = downloader.download_track_cli(track)

    assert downloaded_path == audio_path
    assert downloaded_track.youtube_url == youtube_url


def test_playlist_cancellation_does_not_start_the_next_track(tmp_path, monkeypatch):
    downloader = YouTubeDownloaderForCLI(base_dir=str(tmp_path))
    first_track = _make_track()
    second_track = Track(
        **{
            **first_track.__dict__,
            "number": 2,
            "name": "Track Two",
            "uri": "spotify:track:2",
        }
    )
    playlist = Playlist(
        name="My Playlist",
        description="",
        owner="me",
        uri="spotify:playlist:1",
        cover_url="",
        tracks=[first_track, second_track],
    )
    cancellation_event = Event()
    attempted = []

    def cancel_during_track(**kwargs):
        attempted.append(kwargs["track"].name)
        cancellation_event.set()
        return None, None

    monkeypatch.setattr(downloader, "download_track", cancel_during_track)

    result = downloader.download_playlist_cli(
        playlist, cancellation_event=cancellation_event
    )

    assert result == (0, 2, [])
    assert attempted == [first_track.name]


def test_playlist_track_uses_album_number_for_shared_output_path(tmp_path):
    downloader = YouTubeDownloader(base_dir=str(tmp_path))
    track = Track(
        number=6,
        total_tracks=10,
        name="Track One",
        duration=180,
        uri="spotify:track:1",
        artists=["Artist One"],
        album_artist=["Artist One"],
        release_date="2024-01-01",
        album_name="Album One",
        source_type="playlist",
        playlist_name="My Playlist",
        playlist_position=1,
    )

    output_path = downloader._get_output_path(track, output_format=AudioFormat.MP3)

    assert track.number == 6
    assert track.playlist_position == 1
    assert output_path.name == "6 - Artist One - Track One.mp3"
