from typing import Callable, Optional

from spotifysaver.spotlog.logger import get_logger


class SDLogger:
    def __init__(
        self, error_callback: Optional[Callable[[str], None]] = None
    ):
        self.logger = get_logger("SpotifyDownloader")
        self.error_callback = error_callback

    def debug(self, msg):
        self.logger.debug(f"[SpotifyDownloader] {msg}")

    def info(self, msg):
        self.logger.info(f"[SpotifyDownloader] {msg}")

    def warning(self, msg):
        self.logger.warning(f"[SpotifyDownloader] {msg}")

    def error(self, msg):
        self.logger.error(f"[SpotifyDownloader] {msg}")
        if self.error_callback:
            self.error_callback(str(msg))