"""API Configuration Settings"""

import os
from typing import List


class APIConfig:
    """Configuration settings for the FastAPI application."""

    # Cross-origin access is opt-in; the bundled UI uses same-origin requests.
    ALLOWED_ORIGINS: List[str] = [
        origin.strip()
        for origin in os.getenv("SPOTIFYSAVER_CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]

    # API settings
    DEFAULT_OUTPUT_DIR: str = "Music"
    DEFAULT_VIDEO_DIR: str = "Videos"
    # Number of downloads the server queue runs at the same time; the rest
    # wait in the queue and are started automatically as slots free up.
    DEFAULT_MAX_CONCURRENT_DOWNLOADS: int = 3
    DEFAULT_BANDWIDTH_LIMIT_MIB_PER_SECOND: float = 0
    MAX_CONCURRENT_DOWNLOADS: int = DEFAULT_MAX_CONCURRENT_DOWNLOADS
    BANDWIDTH_LIMIT_MIB_PER_SECOND: float = DEFAULT_BANDWIDTH_LIMIT_MIB_PER_SECOND
    ACTIVE_DOWNLOADS: int = 0
    CONFIRM_MATCHED_YOUTUBE_DOWNLOADS: bool = False
    CONFIRM_CHANNEL_WATCHER_DOWNLOADS: bool = False

    @classmethod
    def set_download_limits(
        cls, max_concurrent_downloads: int, bandwidth_limit_mib_per_second: float
    ) -> None:
        """Update the server-wide download limits used by the queue and yt-dlp."""
        cls.MAX_CONCURRENT_DOWNLOADS = max_concurrent_downloads
        cls.BANDWIDTH_LIMIT_MIB_PER_SECOND = bandwidth_limit_mib_per_second

    @classmethod
    def set_active_downloads(cls, active_downloads: int) -> None:
        """Track active jobs so bandwidth remains capped after lowering concurrency."""
        cls.ACTIVE_DOWNLOADS = active_downloads

    @classmethod
    def set_youtube_confirmation_preferences(
        cls,
        confirm_matched_downloads: bool,
        confirm_channel_watcher_downloads: bool,
    ) -> None:
        """Apply the persisted YouTube download confirmation preferences."""
        cls.CONFIRM_MATCHED_YOUTUBE_DOWNLOADS = confirm_matched_downloads
        cls.CONFIRM_CHANNEL_WATCHER_DOWNLOADS = confirm_channel_watcher_downloads

    @classmethod
    def get_download_rate_limit_bytes_per_second(cls) -> int | None:
        """Return a per-job yt-dlp rate limit derived from queue concurrency."""
        if cls.BANDWIDTH_LIMIT_MIB_PER_SECOND <= 0:
            return None
        total_bytes_per_second = int(
            cls.BANDWIDTH_LIMIT_MIB_PER_SECOND * 1024 * 1024
        )
        concurrent_downloads = max(
            cls.MAX_CONCURRENT_DOWNLOADS, cls.ACTIVE_DOWNLOADS
        )
        return max(1, total_bytes_per_second // concurrent_downloads)

    # File settings
    ALLOWED_FORMATS: List[str] = ["m4a", "mp3", "opus"]
    DEFAULT_FORMAT: str = "m4a"

    # Service settings
    API_PORT: int = 8000
    API_HOST: str = "0.0.0.0"
    LOG_LEVEL: str = "info"

    @classmethod
    def get_output_dir(cls) -> str:
        """Get the output directory from environment or default."""
        return os.getenv("SPOTIFYSAVER_OUTPUT_DIR", cls.DEFAULT_OUTPUT_DIR)

    @classmethod
    def get_video_dir(cls) -> str:
        """Get the video output directory from environment or default."""
        return os.getenv("SPOTIFYSAVER_VIDEO_DIR", cls.DEFAULT_VIDEO_DIR)

    @classmethod
    def get_channel_watcher_db_path(cls) -> str:
        """Get the persistent database path for watched channels."""
        return os.getenv(
            "SPOTIFYSAVER_CHANNEL_WATCHER_DB",
            os.path.join(
                os.path.expanduser("~"),
                ".spotify-saver",
                "channel-watcher.sqlite3",
            ),
        )

    @classmethod
    def get_security_db_path(cls) -> str:
        """Get the persistent database path for authentication and credentials."""
        return os.getenv(
            "SPOTIFYSAVER_SECURITY_DB",
            os.path.join(
                os.path.expanduser("~"),
                ".spotify-saver",
                "security.sqlite3",
            ),
        )
