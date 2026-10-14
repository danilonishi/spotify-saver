"""Storage reserve settings and filesystem-level free-space reporting."""

import asyncio
import os
import shutil
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional

from .notification_hub import NotificationHub

GB = 1_000_000_000
STORAGE_CHECK_INTERVAL_SECONDS = 30


class StorageProtectionStore:
    """Persist the minimum free-space reserve used by download preflight checks."""

    def __init__(self, database_path: str):
        self.database_path = Path(database_path).expanduser()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
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
                CREATE TABLE IF NOT EXISTS storage_protection_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    minimum_free_bytes INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute("INSERT OR IGNORE INTO storage_protection_settings (id) VALUES (1)")

    def get_minimum_free_bytes(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT minimum_free_bytes FROM storage_protection_settings WHERE id = 1"
            ).fetchone()
        return int(row[0]) if row else 0

    def set_minimum_free_bytes(self, minimum_free_bytes: int) -> int:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO storage_protection_settings (id, minimum_free_bytes)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    minimum_free_bytes = excluded.minimum_free_bytes
                """,
                (minimum_free_bytes,),
            )
        return minimum_free_bytes


class StorageProtectionService:
    """Inspect destinations and prevent new work from crossing a free-space reserve."""

    def __init__(
        self,
        store: StorageProtectionStore,
        paths_provider: Callable[[], list[dict[str, str]]],
        notification_hub: NotificationHub,
        queue_kick: Callable[[], Awaitable[None]],
    ):
        self.store = store
        self.paths_provider = paths_provider
        self.notification_hub = notification_hub
        self.queue_kick = queue_kick
        self._warned_filesystems: set[int] = set()
        self._notification_lock = asyncio.Lock()
        self._monitor_task: Optional[asyncio.Task[None]] = None

    @staticmethod
    def _existing_path(path: str) -> Path:
        candidate = Path(path).expanduser()
        while not candidate.exists():
            parent = candidate.parent
            if parent == candidate:
                raise OSError(f"No existing parent directory for {path}")
            candidate = parent
        if not candidate.is_dir():
            candidate = candidate.parent
        return candidate.resolve()

    def _filesystem_report(
        self, paths: list[dict[str, str]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        filesystems: dict[int, dict[str, Any]] = {}
        errors: list[dict[str, str]] = []
        for item in paths:
            path = item["path"]
            try:
                existing_path = self._existing_path(path)
                usage = shutil.disk_usage(existing_path)
                device_id = os.stat(existing_path).st_dev
            except OSError as error:
                errors.append(
                    {
                        "label": item["label"],
                        "path": path,
                        "error": str(error),
                    }
                )
                continue

            filesystem = filesystems.setdefault(
                device_id,
                {
                    "device_id": device_id,
                    "paths": [],
                    "free_bytes": usage.free,
                    "total_bytes": usage.total,
                },
            )
            filesystem["free_bytes"] = min(filesystem["free_bytes"], usage.free)
            filesystem["total_bytes"] = max(filesystem["total_bytes"], usage.total)
            filesystem["paths"].append({"label": item["label"], "path": path})

        return list(filesystems.values()), errors

    @staticmethod
    def _public_report(
        filesystems: list[dict[str, Any]],
        errors: list[dict[str, str]],
        minimum_free_bytes: int,
    ) -> dict[str, Any]:
        public_filesystems = []
        for index, filesystem in enumerate(filesystems, start=1):
            free_bytes = filesystem["free_bytes"]
            public_filesystems.append(
                {
                    "label": f"Storage {index}",
                    "paths": filesystem["paths"],
                    "free_bytes": free_bytes,
                    "total_bytes": filesystem["total_bytes"],
                    "minimum_free_bytes": minimum_free_bytes,
                    "below_reserve": (minimum_free_bytes > 0 and free_bytes < minimum_free_bytes),
                }
            )
        return {
            "minimum_free_space_gb": round(minimum_free_bytes / GB, 2),
            "protection_enabled": minimum_free_bytes > 0,
            "filesystems": public_filesystems,
            "errors": errors,
        }

    def get_report(self) -> dict[str, Any]:
        minimum_free_bytes = self.store.get_minimum_free_bytes()
        filesystems, errors = self._filesystem_report(self.paths_provider())
        return self._public_report(filesystems, errors, minimum_free_bytes)

    async def check_destination(self, path: str) -> tuple[bool, Optional[str]]:
        minimum_free_bytes = self.store.get_minimum_free_bytes()
        if minimum_free_bytes == 0:
            return False, None

        filesystems, errors = self._filesystem_report(
            [{"label": "Download destination", "path": path}]
        )
        await self._publish_storage_state(filesystems)

        if errors:
            return True, f"Waiting for storage check: {errors[0]['error']}"
        if not filesystems:
            return True, "Waiting for storage check: destination filesystem is unavailable"

        filesystem = filesystems[0]
        free_bytes = filesystem["free_bytes"]
        if free_bytes < minimum_free_bytes:
            return (
                True,
                "Waiting for free space: "
                f"{free_bytes / GB:.2f} GB available; "
                f"{minimum_free_bytes / GB:.2f} GB reserve required.",
            )
        return False, None

    async def refresh(self) -> None:
        minimum_free_bytes = self.store.get_minimum_free_bytes()
        if minimum_free_bytes == 0:
            async with self._notification_lock:
                self._warned_filesystems.clear()
            return
        filesystems, _ = self._filesystem_report(self.paths_provider())
        await self._publish_storage_state(filesystems)

    async def _publish_storage_state(self, filesystems: list[dict[str, Any]]) -> None:
        async with self._notification_lock:
            minimum_free_bytes = self.store.get_minimum_free_bytes()
            observed_devices = {item["device_id"] for item in filesystems}
            low_filesystems = {
                item["device_id"] for item in filesystems if item["free_bytes"] < minimum_free_bytes
            }

            for filesystem in filesystems:
                device_id = filesystem["device_id"]
                if device_id not in low_filesystems or device_id in self._warned_filesystems:
                    continue
                labels = ", ".join(dict.fromkeys(path["label"] for path in filesystem["paths"]))
                await self.notification_hub.publish(
                    "low_storage",
                    f"{labels or 'A download filesystem'} has "
                    f"{filesystem['free_bytes'] / GB:.2f} GB free, below the "
                    f"{minimum_free_bytes / GB:.2f} GB reserve.",
                    filesystem=f"device-{device_id}",
                    free_space_gb=f"{filesystem['free_bytes'] / GB:.2f}",
                    reserve_gb=f"{minimum_free_bytes / GB:.2f}",
                )
                self._warned_filesystems.add(device_id)
            self._warned_filesystems.difference_update(observed_devices - low_filesystems)

    async def start(self) -> None:
        await self.refresh()
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor())

    async def stop(self) -> None:
        if self._monitor_task is None:
            return
        self._monitor_task.cancel()
        try:
            await self._monitor_task
        except asyncio.CancelledError:
            pass
        self._monitor_task = None

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(STORAGE_CHECK_INTERVAL_SECONDS)
            await self.refresh()
            await self.queue_kick()
