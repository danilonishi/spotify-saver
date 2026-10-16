"""Persistent server-wide YouTube download confirmation preferences."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..config import APIConfig


class YouTubeConfirmationStore:
    """Persist whether automatically selected YouTube downloads need approval."""

    def __init__(self, database_path: str):
        self.database_path = Path(database_path).expanduser()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS youtube_confirmation_preferences (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    confirm_matched_downloads INTEGER NOT NULL,
                    confirm_channel_watcher_downloads INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO youtube_confirmation_preferences (
                    id, confirm_matched_downloads, confirm_channel_watcher_downloads
                ) VALUES (1, 0, 0)
                """
            )

    def get_preferences(self) -> dict[str, bool]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT confirm_matched_downloads, confirm_channel_watcher_downloads
                FROM youtube_confirmation_preferences
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("YouTube confirmation preferences are not initialized")
        return {
            "confirm_matched_downloads": bool(row[0]),
            "confirm_channel_watcher_downloads": bool(row[1]),
        }

    def set_preferences(
        self,
        confirm_matched_downloads: bool,
        confirm_channel_watcher_downloads: bool,
    ) -> dict[str, bool]:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE youtube_confirmation_preferences
                SET confirm_matched_downloads = ?,
                    confirm_channel_watcher_downloads = ?
                WHERE id = 1
                """,
                (int(confirm_matched_downloads), int(confirm_channel_watcher_downloads)),
            )
        return self.get_preferences()
