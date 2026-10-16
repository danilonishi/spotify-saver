"""SpotifySaver Downloader Module"""

from spotifysaver.downloader.youtube_downloader import YouTubeDownloader
from spotifysaver.downloader.youtube_downloader_for_cli import YouTubeDownloaderForCLI
from spotifysaver.downloader.image_downloader import ImageDownloader
from spotifysaver.downloader.soundcloud_downloader import SoundCloudDownloader

__all__ = [
    "YouTubeDownloader",
    "YouTubeDownloaderForCLI",
    "SoundCloudDownloader",
    "ImageDownloader",
]
