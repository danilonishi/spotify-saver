"""Persist Jellyfin scan settings and schedule library refreshes."""

import asyncio
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ..config import APIConfig
from ...spotlog import get_logger

logger = get_logger("JellyfinLibraryScan")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class JellyfinLibraryScanStore:
    """Stores Jellyfin configuration and pending scan state in SQLite."""

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
                CREATE TABLE IF NOT EXISTS jellyfin_library_scan_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    server_url TEXT,
                    api_key TEXT,
                    scan_interval_minutes INTEGER NOT NULL DEFAULT 5,
                    scan_pending INTEGER NOT NULL DEFAULT 0,
                    scan_generation INTEGER NOT NULL DEFAULT 0,
                    scan_force_pending INTEGER NOT NULL DEFAULT 0,
                    last_scan_at TEXT,
                    last_attempt_at TEXT,
                    last_error TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO jellyfin_library_scan_settings (id)
                VALUES (1)
                """
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(jellyfin_library_scan_settings)"
                )
            }
            if "scan_generation" not in columns:
                connection.execute(
                    """
                    ALTER TABLE jellyfin_library_scan_settings
                    ADD COLUMN scan_generation INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "scan_force_pending" not in columns:
                connection.execute(
                    """
                    ALTER TABLE jellyfin_library_scan_settings
                    ADD COLUMN scan_force_pending INTEGER NOT NULL DEFAULT 0
                    """
                )

    def get_settings(self) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT server_url, api_key, scan_interval_minutes, scan_pending,
                       scan_generation, scan_force_pending, last_scan_at,
                       last_attempt_at, last_error
                FROM jellyfin_library_scan_settings
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return {
                "server_url": None,
                "api_key": None,
                "scan_interval_minutes": 5,
                "scan_pending": False,
                "scan_generation": 0,
                "scan_force_pending": False,
                "last_scan_at": None,
                "last_attempt_at": None,
                "last_error": None,
            }
        return {
            "server_url": row[0],
            "api_key": row[1],
            "scan_interval_minutes": row[2],
            "scan_pending": bool(row[3]),
            "scan_generation": row[4],
            "scan_force_pending": bool(row[5]),
            "last_scan_at": row[6],
            "last_attempt_at": row[7],
            "last_error": row[8],
        }

    def save_settings(
        self,
        server_url: Optional[str],
        api_key: Optional[str],
        clear_api_key: bool,
        scan_interval_minutes: int,
    ) -> None:
        current = self.get_settings()
        stored_api_key = None if clear_api_key else current["api_key"]
        if api_key:
            stored_api_key = api_key
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jellyfin_library_scan_settings
                SET server_url = ?, api_key = ?, scan_interval_minutes = ?
                WHERE id = 1
                """,
                (server_url, stored_api_key, scan_interval_minutes),
            )

    def clear_api_key(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jellyfin_library_scan_settings
                SET api_key = NULL
                WHERE id = 1
                """
            )

    def request_scan(self, force: bool = False) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jellyfin_library_scan_settings
                SET scan_pending = 1,
                    scan_generation = scan_generation + 1,
                    scan_force_pending = CASE WHEN ? THEN 1 ELSE scan_force_pending END
                WHERE id = 1
                """,
                (force,),
            )

    def record_attempt(self, attempted_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jellyfin_library_scan_settings
                SET last_attempt_at = ?, last_error = NULL, scan_force_pending = 0
                WHERE id = 1
                """,
                (attempted_at,),
            )

    def record_success(self, completed_at: str, generation: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jellyfin_library_scan_settings
                SET scan_pending = CASE
                        WHEN scan_generation = ? THEN 0
                        ELSE 1
                    END,
                    last_scan_at = ?, last_error = NULL
                WHERE id = 1
                """,
                (generation, completed_at),
            )

    def record_failure(self, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jellyfin_library_scan_settings
                SET last_error = ?
                WHERE id = 1
                """,
                (error[:500],),
            )


def _validate_server_url(server_url: str) -> str:
    normalized = server_url.strip().rstrip("/")
    if normalized and "://" not in normalized:
        normalized = f"http://{normalized}"
    try:
        parsed = urlsplit(normalized)
        parsed.port
    except ValueError as error:
        raise ValueError("Enter a Jellyfin URL with a valid host and port") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a Jellyfin server URL starting with http:// or https://")
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in parsed.netloc)
    ):
        raise ValueError("The Jellyfin server URL cannot contain credentials or a query")
    return normalized


def _request_library_scan(server_url: str, api_key: str) -> None:
    request = Request(
        f"{server_url.rstrip('/')}/Library/Refresh",
        data=b"",
        headers={
            "Authorization": f'MediaBrowser Token="{api_key}"',
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(
                    f"Jellyfin returned HTTP {response.status} when requesting a library scan"
                )
    except HTTPError as error:
        raise RuntimeError(
            f"Jellyfin returned HTTP {error.code} when requesting a library scan"
        ) from error
    except URLError as error:
        raise RuntimeError(f"Could not reach Jellyfin: {error.reason}") from error


class JellyfinLibraryScanService:
    """Coalesces completed downloads into rate-limited Jellyfin library scans."""

    def __init__(
        self,
        store: JellyfinLibraryScanStore,
        is_queue_idle: Callable[[], bool],
    ):
        self.store = store
        self.is_queue_idle = is_queue_idle
        self._wake = asyncio.Event()
        self._worker: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self.store.initialize()
        self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._worker = None

    def get_status(self) -> dict:
        settings = self.store.get_settings()
        configured = bool(settings["server_url"] and settings["api_key"])
        last_attempt = _as_datetime(settings["last_attempt_at"])
        last_scan = _as_datetime(settings["last_scan_at"])
        last_activity = max(
            (timestamp for timestamp in (last_attempt, last_scan) if timestamp),
            default=None,
        )
        next_scan_at = None
        if settings["scan_pending"] and configured:
            due_at = (
                last_activity
                + timedelta(minutes=settings["scan_interval_minutes"])
                if last_activity
                else _utc_now()
            )
            next_scan_at = due_at.isoformat()
        return {
            "server_url": settings["server_url"],
            "api_key_configured": bool(settings["api_key"]),
            "configured": configured,
            "queue_idle": self.is_queue_idle(),
            "scan_interval_minutes": settings["scan_interval_minutes"],
            "scan_pending": settings["scan_pending"],
            "manual_scan_pending": settings["scan_force_pending"],
            "last_scan_at": settings["last_scan_at"],
            "next_scan_at": next_scan_at,
            "last_error": settings["last_error"],
        }

    def save_settings(
        self,
        server_url: str,
        api_key: Optional[str],
        clear_api_key: bool,
        scan_interval_minutes: int,
    ) -> dict:
        normalized_url = _validate_server_url(server_url) if server_url.strip() else None
        self.store.save_settings(
            normalized_url,
            api_key.strip() if api_key and api_key.strip() else None,
            clear_api_key,
            scan_interval_minutes,
        )
        self._wake.set()
        return self.get_status()

    def remove_api_key(self) -> dict:
        self.store.clear_api_key()
        self._wake.set()
        return self.get_status()

    async def queue_changed(self, content_downloaded: bool) -> None:
        if content_downloaded:
            self.store.request_scan()
        self._wake.set()

    def request_manual_scan(self) -> dict:
        settings = self.store.get_settings()
        if not settings["server_url"] or not settings["api_key"]:
            raise ValueError("Configure a Jellyfin server URL and API key before scanning")
        self.store.request_scan(force=True)
        self._wake.set()
        return self.get_status()

    async def _wait(self, seconds: Optional[float]) -> None:
        try:
            if seconds is None:
                await self._wake.wait()
            else:
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.1, seconds))
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    async def _run(self) -> None:
        while True:
            settings = self.store.get_settings()
            configured = bool(settings["server_url"] and settings["api_key"])
            if not settings["scan_pending"] or not configured:
                await self._wait(None)
                continue

            last_attempt = _as_datetime(settings["last_attempt_at"])
            last_scan = _as_datetime(settings["last_scan_at"])
            last_activity = max(
                (timestamp for timestamp in (last_attempt, last_scan) if timestamp),
                default=None,
            )
            due_at = (
                last_activity
                + timedelta(minutes=settings["scan_interval_minutes"])
                if last_activity
                else _utc_now()
            )
            seconds_until_due = (due_at - _utc_now()).total_seconds()
            if seconds_until_due > 0 and not settings["scan_force_pending"]:
                await self._wait(seconds_until_due)
                continue
            if not self.is_queue_idle():
                await self._wait(1)
                continue

            attempted_at = _utc_now().isoformat()
            request_generation = settings["scan_generation"]
            self.store.record_attempt(attempted_at)
            try:
                await asyncio.to_thread(
                    _request_library_scan,
                    settings["server_url"],
                    settings["api_key"],
                )
            except Exception as error:
                self.store.record_failure(str(error))
                logger.error("Jellyfin library scan request failed: %s", error)
            else:
                self.store.record_success(
                    _utc_now().isoformat(), request_generation
                )
                logger.info("Jellyfin library scan requested successfully")
