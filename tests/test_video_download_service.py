from threading import Event
import asyncio
import xml.etree.ElementTree as ET
from pathlib import Path

from spotifysaver.api.services.video_download_service import VideoDownloadService
from yt_dlp.utils import DownloadCancelled


def test_video_cancellation_hook_uses_yt_dlp_cancel_signal():
    cancellation_event = Event()
    service = VideoDownloadService()
    options = {}
    service.cancellation_event = cancellation_event
    service._cancel_hook(options)
    cancellation_event.set()

    try:
        options["progress_hooks"][0]({})
    except DownloadCancelled:
        pass
    else:
        raise AssertionError("Expected yt-dlp's dedicated cancellation exception")


def test_video_playlist_reports_entries_to_queue_callback(monkeypatch, tmp_path):
    entries = [
        {
            "id": "video-1",
            "title": "First Video",
            "webpage_url": "https://www.youtube.com/watch?v=video-1",
            "channel": "Sample Channel",
            "channel_url": "https://www.youtube.com/@sample",
            "duration": 91,
        },
        {
            "id": "video-2",
            "title": "Second Video",
            "webpage_url": "https://www.youtube.com/watch?v=video-2",
            "channel": "Sample Channel",
            "channel_url": "https://www.youtube.com/@sample",
            "duration": 123,
        },
    ]
    queue_tracks = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def download(self, urls):
            assert len(urls) == 1
            output_path = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            output_path.touch()

    service = VideoDownloadService(output_dir=str(tmp_path))
    monkeypatch.setattr(
        service,
        "_resolve_entries",
        lambda _url: ("Sample Playlist", entries),
    )
    monkeypatch.setattr(service, "_save_channel_cover", lambda *_args: None)
    monkeypatch.setattr(
        "spotifysaver.api.services.video_download_service.yt_dlp.YoutubeDL",
        FakeYoutubeDL,
    )

    result = asyncio.run(
        service.download_from_url(
            "https://www.youtube.com/playlist?list=playlist-id",
            tracks_callback=queue_tracks.extend,
        )
    )

    assert result["completed_tracks"] == 2
    assert queue_tracks == [
        {
            "name": "First Video",
            "artists": ["Sample Channel"],
            "album_name": "Sample Playlist",
            "duration": 91,
            "number": 1,
            "uri": "https://www.youtube.com/watch?v=video-1",
        },
        {
            "name": "Second Video",
            "artists": ["Sample Channel"],
            "album_name": "Sample Playlist",
            "duration": 123,
            "number": 2,
            "uri": "https://www.youtube.com/watch?v=video-2",
        },
    ]


def test_video_download_writes_description_to_jellyfin_nfo(monkeypatch, tmp_path):
    description = "A description with <tags> & special characters."
    entry = {
        "id": "video-id",
        "title": "Sample Video",
        "webpage_url": "https://www.youtube.com/watch?v=video-id",
        "channel": "Sample Channel",
        "channel_url": "https://www.youtube.com/@sample",
        "description": "",
    }

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def download(self, urls):
            assert urls == [entry["webpage_url"]]
            for hook in self.options.get("progress_hooks", []):
                hook({"info_dict": {"description": description}})
            video_path = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            video_path.touch()

    service = VideoDownloadService(output_dir=str(tmp_path))
    monkeypatch.setattr(service, "_resolve_entries", lambda _url: ("", [entry]))
    monkeypatch.setattr(service, "_save_channel_cover", lambda *_args: None)
    monkeypatch.setattr(
        "spotifysaver.api.services.video_download_service.yt_dlp.YoutubeDL",
        FakeYoutubeDL,
    )

    result = service._download_sync(entry["webpage_url"], None, None, None)

    assert result["completed_tracks"] == 1
    nfo_path = tmp_path / "Sample Channel" / "Sample Video.nfo"
    root = ET.parse(nfo_path).getroot()
    assert root.tag == "movie"
    assert root.findtext("title") == "Sample Video"
    assert root.findtext("plot") == description


def test_video_download_fetches_missing_description_for_nfo(monkeypatch, tmp_path):
    description = "Description fetched separately."
    entry = {
        "id": "video-id",
        "title": "Sample Video",
        "webpage_url": "https://www.youtube.com/watch?v=video-id",
        "channel": "Sample Channel",
        "channel_url": "https://www.youtube.com/@sample",
        "description": "",
    }
    metadata_requests = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            assert download is False
            metadata_requests.append(_url)
            return {**entry, "description": description}

        def download(self, urls):
            video_path = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            video_path.touch()

    service = VideoDownloadService(output_dir=str(tmp_path))
    monkeypatch.setattr(service, "_resolve_entries", lambda _url: ("", [entry]))
    monkeypatch.setattr(service, "_save_channel_cover", lambda *_args: None)
    monkeypatch.setattr(
        "spotifysaver.api.services.video_download_service.yt_dlp.YoutubeDL",
        FakeYoutubeDL,
    )

    result = service._download_sync(entry["webpage_url"], None, None, None)

    assert result["completed_tracks"] == 1
    assert metadata_requests == [entry["webpage_url"]]
    root = ET.parse(tmp_path / "Sample Channel" / "Sample Video.nfo").getroot()
    assert root.findtext("plot") == description
