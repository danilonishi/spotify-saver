"""Persistent server-wide download concurrency and bandwidth preferences."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..config import APIConfig


class DownloadLimitsStore:
    """Persist limits that apply to the server-managed download queue."""

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
                CREATE TABLE IF NOT EXISTS download_limits (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    max_concurrent_downloads INTEGER NOT NULL,
                    bandwidth_limit_mib_per_second REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO download_limits (
                    id, max_concurrent_downloads, bandwidth_limit_mib_per_second
                ) VALUES (1, ?, ?)
                """,
                (
                    APIConfig.DEFAULT_MAX_CONCURRENT_DOWNLOADS,
                    APIConfig.DEFAULT_BANDWIDTH_LIMIT_MIB_PER_SECOND,
                ),
            )

    def get_limits(self) -> dict[str, int | float]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT max_concurrent_downloads, bandwidth_limit_mib_per_second
                FROM download_limits
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("Download limit settings have not been initialized")
        return {
            "max_concurrent_downloads": int(row[0]),
            "bandwidth_limit_mib_per_second": float(row[1]),
        }

    def set_limits(
        self, max_concurrent_downloads: int, bandwidth_limit_mib_per_second: float
    ) -> dict[str, int | float]:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE download_limits
                SET max_concurrent_downloads = ?,
                    bandwidth_limit_mib_per_second = ?
                WHERE id = 1
                """,
                (max_concurrent_downloads, bandwidth_limit_mib_per_second),
            )
        return self.get_limits()
