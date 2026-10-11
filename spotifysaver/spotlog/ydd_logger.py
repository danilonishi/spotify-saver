from typing import Callable, Optional

from spotifysaver.spotlog.logger import get_logger


class YDLLogger:
    def __init__(
        self, error_callback: Optional[Callable[[str], None]] = None
    ):
        self.logger = get_logger("YT-DLP")
        self.error_callback = error_callback

    def debug(self, msg):
        self.logger.debug(f"[yt-dlp] {msg}")

    def info(self, msg):
        self.logger.info(f"[yt-dlp] {msg}")

    def warning(self, msg):
        self.logger.warning(f"[yt-dlp] {msg}")

    def error(self, msg):
        self.logger.error(f"[yt-dlp] {msg}")
        if self.error_callback:
            self.error_callback(str(msg))