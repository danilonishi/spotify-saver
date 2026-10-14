"""Persistent server-wide download-directory preferences."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from ..config import APIConfig


class DownloadDirectoryStore:
    """Reads and writes the shared audio, video, and watcher folders."""

    COLUMNS = {
        "audio": "audio_output_dir",
        "video": "video_output_dir",
        "watcher": "watcher_output_dir",
    }

    def __init__(self, database_path: Optional[str] = None):
        self.database_path = Path(
            database_path or APIConfig.get_channel_watcher_db_path()
        ).expanduser()

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
                CREATE TABLE IF NOT EXISTS server_download_directories (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    audio_output_dir TEXT,
                    video_output_dir TEXT,
                    watcher_output_dir TEXT
                )
                """
            )
            exists = connection.execute(
                "SELECT 1 FROM server_download_directories WHERE id = 1"
            ).fetchone()
            if exists is not None:
                return

            legacy_table_exists = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'user_download_directories'
                """
            ).fetchone()
            if legacy_table_exists:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO server_download_directories (
                        id, audio_output_dir, video_output_dir, watcher_output_dir
                    )
                    SELECT
                        1,
                        (SELECT audio_output_dir FROM user_download_directories
                         WHERE audio_output_dir IS NOT NULL ORDER BY rowid ASC LIMIT 1),
                        (SELECT video_output_dir FROM user_download_directories
                         WHERE video_output_dir IS NOT NULL ORDER BY rowid ASC LIMIT 1),
                        (SELECT watcher_output_dir FROM user_download_directories
                         WHERE watcher_output_dir IS NOT NULL ORDER BY rowid ASC LIMIT 1)
                    """
                )
            connection.execute(
                "INSERT OR IGNORE INTO server_download_directories (id) VALUES (1)"
            )

    @staticmethod
    def _nearest_existing_directory(directory: str) -> Optional[str]:
        candidate = Path(directory).expanduser()
        while not candidate.is_dir():
            parent = candidate.parent
            if parent == candidate:
                return None
            candidate = parent
        return str(candidate)

    def get_download_directories(self) -> dict[str, Optional[str]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT audio_output_dir, video_output_dir, watcher_output_dir
                FROM server_download_directories
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return {"audio": None, "video": None, "watcher": None}

        stored = dict(zip(self.COLUMNS, row))
        preferences = {
            kind: self._nearest_existing_directory(stored[kind])
            if stored[kind]
            else None
            for kind in self.COLUMNS
        }
        with self._connect() as connection:
            for kind, column in self.COLUMNS.items():
                if preferences[kind] != stored[kind]:
                    connection.execute(
                        f"UPDATE server_download_directories SET {column} = ? WHERE id = 1",
                        (preferences[kind],),
                    )
        return preferences

    def set_download_directory(
        self, kind: str, directory: str
    ) -> dict[str, Optional[str]]:
        column = self.COLUMNS[kind]
        with self._connect() as connection:
            connection.execute(
                f"UPDATE server_download_directories SET {column} = ? WHERE id = 1",
                (directory,),
            )
        return self.get_download_directories()

    def restore_download_directories(
        self, directories: dict[str, Optional[str]]
    ) -> None:
        """Persist imported paths verbatim; they may need adapting on a new host."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO server_download_directories (
                    id, audio_output_dir, video_output_dir, watcher_output_dir
                ) VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    audio_output_dir = excluded.audio_output_dir,
                    video_output_dir = excluded.video_output_dir,
                    watcher_output_dir = excluded.watcher_output_dir
                """,
                (
                    directories.get("audio"),
                    directories.get("video"),
                    directories.get("watcher"),
                ),
            )
