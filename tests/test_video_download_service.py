import xml.etree.ElementTree as ET
from pathlib import Path

from spotifysaver.api.services.video_download_service import VideoDownloadService


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
