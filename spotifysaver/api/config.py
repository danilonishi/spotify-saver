"""API Configuration Settings"""

import os
from typing import List


class APIConfig:
    """Configuration settings for the FastAPI application."""

    # CORS settings
    ALLOWED_ORIGINS: List[str] = ["*"]

    # API settings
    DEFAULT_OUTPUT_DIR: str = "Music"
    DEFAULT_VIDEO_DIR: str = "Videos"
    # Number of downloads the server queue runs at the same time; the rest
    # wait in the queue and are started automatically as slots free up.
    MAX_CONCURRENT_DOWNLOADS: int = 1

    # File settings
    ALLOWED_FORMATS: List[str] = ["m4a", "mp3"]
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
