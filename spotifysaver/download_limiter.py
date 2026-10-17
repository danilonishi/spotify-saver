"""Global concurrency limiter for individual media transfers."""

from contextlib import contextmanager
from threading import Condition, Event
from typing import Iterator


class TrackDownloadLimiter:
    """Dynamically adjustable, cancellation-aware limit for track transfers."""

    def __init__(self, limit: int):
        self._condition = Condition()
        self._limit = limit
        self._active = 0

    @property
    def active_count(self) -> int:
        with self._condition:
            return self._active

    def set_limit(self, limit: int) -> None:
        with self._condition:
            self._limit = limit
            self._condition.notify_all()

    @contextmanager
    def download_slot(self, cancellation_event: Event | None = None) -> Iterator[bool]:
        acquired = False
        try:
            with self._condition:
                while self._active >= self._limit:
                    if cancellation_event is not None and cancellation_event.is_set():
                        break
                    self._condition.wait(timeout=0.2)
                if cancellation_event is None or not cancellation_event.is_set():
                    self._active += 1
                    acquired = True
            yield acquired
        finally:
            if acquired:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()


track_download_limiter = TrackDownloadLimiter(3)
