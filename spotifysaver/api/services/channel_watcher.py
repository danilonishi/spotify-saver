"""Persistent, platform-aware scheduling of channel checks."""

import asyncio
import sqlite3
import threading
import time as time_module
import uuid
from datetime import date, datetime, time, timedelta, timezone
from itertools import islice
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yt_dlp

from ...config import Config
from ...spotlog import get_logger
from ..config import APIConfig

logger = get_logger("uvicorn.error.ChannelWatcher")
UTC = timezone.utc
SCHEDULER_POLL_SECONDS = 15
CHANNEL_LIST_TIMEOUT_SECONDS = 90
MAX_CHANNEL_VIDEOS_PER_CHECK = 20
YOUTUBE_VIDEO_METADATA_INTERVAL_SECONDS = 5
YOUTUBE_RATE_LIMIT_COOLDOWN_SECONDS = 60 * 60


def utc_now() -> datetime:
    return datetime.now(UTC)


def _is_youtube_rate_limited(error: Exception) -> bool:
    message = str(error).lower().replace("-", " ")
    return "rate limit" in message or "too many requests" in message


class YouTubeCooldownActive(RuntimeError):
    def __init__(self, cooldown_until: str):
        super().__init__(cooldown_until)
        self.cooldown_until = cooldown_until


def _get_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"Unknown timezone: {timezone_name}") from error


def next_check_time(
    interval: str,
    hour: int,
    minute: int,
    weekday: Optional[int],
    after: datetime,
    timezone_name: str = "UTC",
) -> datetime:
    """Return the next local schedule occurrence as a UTC datetime."""
    after = after.astimezone(UTC)
    if interval == "minutely":
        return after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    zone = _get_timezone(timezone_name)

    def valid_instants(local_time: datetime) -> list[datetime]:
        for offset in range(181):
            candidate_local = local_time + timedelta(minutes=offset)
            instants = set()
            for fold in (0, 1):
                instant = candidate_local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                if instant.astimezone(zone).replace(tzinfo=None) == candidate_local:
                    instants.add(instant)
            if instants:
                return sorted(instants)
        raise ValueError(f"Could not resolve local schedule time in {timezone_name}")

    local_after = after.astimezone(zone).replace(tzinfo=None)
    if interval == "hourly":
        candidate_hour = local_after.replace(minute=0, second=0, microsecond=0)
        for _ in range(49):
            candidate = candidate_hour.replace(minute=minute)
            future = [instant for instant in valid_instants(candidate) if instant > after]
            if future:
                return future[0]
            candidate_hour += timedelta(hours=1)
        raise ValueError("Could not find the next hourly schedule occurrence")

    if interval not in {"daily", "weekly"} or (interval == "weekly" and weekday is None):
        raise ValueError("Weekly schedules require a weekday")
    for days_ahead in range(8 if interval == "weekly" else 2):
        candidate_date = local_after.date() + timedelta(days=days_ahead)
        if interval == "weekly" and candidate_date.weekday() != weekday:
            continue
        candidate = datetime.combine(candidate_date, time(hour, minute))
        instants = valid_instants(candidate)
        if instants and instants[0] > after:
            return instants[0]
    raise ValueError("Could not find the next schedule occurrence")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_channel_url(url: str) -> str:
    parsed = urlparse(url.strip())
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in {"http", "https"} or hostname not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
    }:
        raise ValueError("Only YouTube channel URLs are supported")
    if not any(parsed.path.startswith(prefix) for prefix in ("/@", "/channel/", "/c/", "/user/")):
        raise ValueError("Enter a YouTube channel URL, not a video or playlist URL")
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts[0].startswith("@"):
        channel_path = f"/{path_parts[0]}"
    else:
        channel_path = f"/{path_parts[0]}/{path_parts[1]}"
    return f"https://www.youtube.com{channel_path}"


def _sanitize_filename(name: str) -> str:
    for character in '<>:"/\\|?*':
        name = name.replace(character, "_")
    return " ".join(name.split()).strip(". ")[:150] or "Untitled"


def _video_url(entry: dict[str, Any]) -> Optional[str]:
    url = entry.get("webpage_url")
    if url:
        return str(url)
    video_id = entry.get("id")
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else None


def _published_datetime(entry: dict[str, Any]) -> tuple[Optional[datetime], Optional[date]]:
    for key in ("release_timestamp", "timestamp"):
        timestamp = entry.get(key)
        if isinstance(timestamp, (int, float)):
            return datetime.fromtimestamp(timestamp, UTC), None

    for key in ("upload_date", "release_date"):
        value = entry.get(key)
        if isinstance(value, str):
            try:
                parsed_date = datetime.strptime(value[:8], "%Y%m%d").date()
            except ValueError:
                try:
                    parsed_date = date.fromisoformat(value[:10])
                except ValueError:
                    continue
            return None, parsed_date
    return None, None


class ChannelProvider(Protocol):
    """Platform adapter used by the scheduler to enumerate published content."""

    def list_videos(
        self, channel_url: str, published_after: Optional[datetime] = None
    ) -> tuple[str, list[dict[str, Any]]]:
        ...

    def video_metadata(self, video_url: str) -> dict[str, Any]:
        ...


class YouTubeChannelProvider:
    """YouTube channel adapter backed by yt-dlp."""

    def __init__(self):
        self._metadata_lock = threading.Lock()
        self._last_metadata_request_at: Optional[float] = None

    @staticmethod
    def _options() -> dict[str, Any]:
        return {
            "quiet": True,
            "extract_flat": "in_playlist",
            "extractor_args": {"youtube": {"approximate_date": [""]}},
            "skip_download": True,
            "socket_timeout": 30,
            "sleep_interval_requests": 1,
            "cookiefile": (str(Config.YTDLP_COOKIES_PATH) if Config.YTDLP_COOKIES_PATH else None),
        }

    @staticmethod
    def _normalize_upload_dates(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for entry in entries:
            item = dict(entry)
            timestamp = item.get("timestamp")
            if (
                "upload_date" not in item
                and isinstance(timestamp, (int, float))
                and item.get("release_timestamp") is None
            ):
                item["upload_date"] = datetime.fromtimestamp(timestamp, UTC).strftime("%Y%m%d")
                item.pop("timestamp", None)
            normalized.append(item)
        return normalized

    @classmethod
    def _recent_entries(
        cls,
        playlist: dict[str, Any],
        published_after: datetime,
        limit: int = MAX_CHANNEL_VIDEOS_PER_CHECK,
    ) -> list[dict[str, Any]]:
        recent = []
        for entry in islice(playlist.get("entries") or [], limit):
            if not entry:
                continue
            if entry.get("_type") in {"playlist", "multi_video"}:
                recent.extend(
                    cls._recent_entries(
                        entry,
                        published_after,
                        limit - len(recent),
                    )
                )
                continue
            item = cls._normalize_upload_dates([entry])[0]
            upload_date = item.get("upload_date")
            if (
                isinstance(upload_date, str)
                and len(upload_date) == 8
                and upload_date.isdigit()
                and datetime.strptime(upload_date, "%Y%m%d").date() < published_after.date()
            ):
                break
            recent.append(item)
        return recent

    def list_videos(
        self, channel_url: str, published_after: Optional[datetime] = None
    ) -> tuple[str, list[dict[str, Any]]]:
        with yt_dlp.YoutubeDL(self._options()) as ydl:
            info = ydl.extract_info(
                channel_url,
                download=False,
                process=False,
            )
            if not info or info.get("entries") is None:
                raise ValueError("YouTube did not return a channel video list")
            entries = (
                self._recent_entries(info, published_after) if published_after is not None else []
            )
        title = info.get("channel") or info.get("uploader") or info.get("title")
        return str(title or channel_url), entries

    def video_metadata(self, video_url: str) -> dict[str, Any]:
        with self._metadata_lock:
            now = time_module.monotonic()
            if self._last_metadata_request_at is not None:
                wait = YOUTUBE_VIDEO_METADATA_INTERVAL_SECONDS - (
                    now - self._last_metadata_request_at
                )
                if wait > 0:
                    time_module.sleep(wait)
            self._last_metadata_request_at = time_module.monotonic()
            with yt_dlp.YoutubeDL(self._options()) as ydl:
                info = ydl.extract_info(video_url, download=False)
        if not info:
            raise ValueError(f"YouTube returned no metadata for {video_url}")
        return info


class ChannelWatcherStore:
    """SQLite persistence for watched channels and their next scheduled check."""

    def __init__(self, database_path: Optional[str] = None):
        self.database_path = Path(
            database_path or APIConfig.get_channel_watcher_db_path()
        ).expanduser()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS watched_channels (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT,
                    output_dir TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    check_hour INTEGER NOT NULL,
                    check_minute INTEGER NOT NULL,
                    check_weekday INTEGER,
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    video_format TEXT NOT NULL,
                    video_resolution INTEGER NOT NULL,
                    include_subtitles INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    check_started_at TEXT,
                    next_check_at TEXT NOT NULL,
                    last_error TEXT,
                    UNIQUE (platform, normalized_url)
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(watched_channels)")}
            if "timezone" not in columns:
                connection.execute(
                    "ALTER TABLE watched_channels "
                    "ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'"
                )
            if "check_started_at" not in columns:
                connection.execute("ALTER TABLE watched_channels ADD COLUMN check_started_at TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_watcher_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    youtube_cooldown_until TEXT
                )
                """
            )
            connection.execute("INSERT OR IGNORE INTO channel_watcher_state (id) VALUES (1)")
            legacy_cooldown = connection.execute(
                """
                SELECT MAX(next_check_at)
                FROM watched_channels
                WHERE next_check_at > ?
                    AND (
                        lower(replace(last_error, '-', ' ')) LIKE '%rate limit%'
                        OR lower(last_error) LIKE '%too many requests%'
                    )
                """,
                (_iso(utc_now()),),
            ).fetchone()[0]
            if legacy_cooldown is not None:
                current_cooldown = connection.execute(
                    "SELECT youtube_cooldown_until FROM channel_watcher_state WHERE id = 1"
                ).fetchone()[0]
                if current_cooldown is None or legacy_cooldown > current_cooldown:
                    connection.execute(
                        """
                        UPDATE channel_watcher_state
                        SET youtube_cooldown_until = ?
                        WHERE id = 1
                        """,
                        (legacy_cooldown,),
                    )
            connection.commit()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item.pop("normalized_url", None)
        item["include_subtitles"] = bool(item["include_subtitles"])
        return item

    def list_channels(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM watched_channels ORDER BY created_at, id"
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_channel(self, channel_id: str) -> Optional[dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM watched_channels WHERE id = ?", (channel_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def create_channel(self, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        normalized_url = _normalize_channel_url(settings["url"])
        _get_timezone(settings.get("timezone", "UTC"))
        if settings["interval"] == "weekly" and settings.get("check_weekday") is None:
            raise ValueError("Weekly schedules require a weekday")
        channel_id = str(uuid.uuid4())
        next_check = now
        values = (
            channel_id,
            settings.get("platform", "youtube"),
            normalized_url,
            settings["url"].strip(),
            None,
            settings["output_dir"],
            settings["interval"],
            settings["check_hour"],
            settings["check_minute"],
            settings.get("check_weekday"),
            settings.get("timezone", "UTC"),
            settings.get("video_format", "mp4"),
            settings.get("video_resolution", 0),
            int(settings.get("include_subtitles", False)),
            _iso(now),
            None,
            _iso(next_check),
            None,
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO watched_channels (
                    id, platform, normalized_url, url, title, output_dir, interval,
                    check_hour, check_minute, check_weekday, timezone, video_format,
                    video_resolution, include_subtitles, created_at,
                    last_checked_at, next_check_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            connection.commit()
        created = self.get_channel(channel_id)
        if created is None:
            raise RuntimeError("Could not load newly created watched channel")
        return created

    def update_channel(
        self, channel_id: str, settings: dict[str, Any], now: datetime
    ) -> Optional[dict[str, Any]]:
        existing = self.get_channel(channel_id)
        if existing is None:
            return None
        normalized_url = _normalize_channel_url(settings["url"])
        _get_timezone(settings.get("timezone", "UTC"))
        if settings["interval"] == "weekly" and settings.get("check_weekday") is None:
            raise ValueError("Weekly schedules require a weekday")
        url_changed = normalized_url != _normalize_channel_url(existing["url"])
        next_check = next_check_time(
            settings["interval"],
            settings["check_hour"],
            settings["check_minute"],
            settings.get("check_weekday"),
            now,
            settings.get("timezone", "UTC"),
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE watched_channels
                SET platform = ?, normalized_url = ?, url = ?, title = ?,
                    output_dir = ?, interval = ?, check_hour = ?, check_minute = ?,
                    check_weekday = ?, timezone = ?, video_format = ?, video_resolution = ?,
                    include_subtitles = ?, last_checked_at = ?, check_started_at = NULL,
                    next_check_at = ?,
                    last_error = NULL
                WHERE id = ?
                """,
                (
                    settings.get("platform", existing["platform"]),
                    normalized_url,
                    settings["url"].strip(),
                    None if url_changed else existing["title"],
                    settings["output_dir"],
                    settings["interval"],
                    settings["check_hour"],
                    settings["check_minute"],
                    settings.get("check_weekday"),
                    settings.get("timezone", "UTC"),
                    settings.get("video_format", "mp4"),
                    settings.get("video_resolution", 0),
                    int(settings.get("include_subtitles", False)),
                    None if url_changed else existing["last_checked_at"],
                    _iso(next_check),
                    channel_id,
                ),
            )
            connection.commit()
        return self.get_channel(channel_id)

    def delete_channel(self, channel_id: str) -> bool:
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.execute("DELETE FROM watched_channels WHERE id = ?", (channel_id,))
            connection.commit()
        return cursor.rowcount > 0

    def due_channels(self, now: datetime) -> list[dict[str, Any]]:
        if self.get_youtube_cooldown_until(now) is not None:
            return []
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM watched_channels WHERE next_check_at <= ?",
                (_iso(now),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_youtube_cooldown_until(self, now: Optional[datetime] = None) -> Optional[str]:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT youtube_cooldown_until FROM channel_watcher_state WHERE id = 1"
            ).fetchone()
        if row is None or row[0] is None:
            return None
        cooldown_until = _parse_datetime(row[0])
        if cooldown_until <= (now or utc_now()):
            return None
        return row[0]

    def set_youtube_cooldown_until(self, cooldown_until: datetime) -> None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT youtube_cooldown_until FROM channel_watcher_state WHERE id = 1"
            ).fetchone()
            existing = _parse_datetime(row[0]) if row and row[0] else None
            if existing is None or cooldown_until > existing:
                connection.execute(
                    """
                    UPDATE channel_watcher_state
                    SET youtube_cooldown_until = ?
                    WHERE id = 1
                    """,
                    (_iso(cooldown_until),),
                )
            connection.commit()

    def record_success(
        self, channel_id: str, title: str, checked_at: datetime, next_check: datetime
    ) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE watched_channels
                SET title = ?, last_checked_at = ?, check_started_at = NULL,
                    next_check_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (title, _iso(checked_at), _iso(next_check), channel_id),
            )
            connection.commit()

    def record_check_started(self, channel_id: str, started_at: datetime) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE watched_channels SET check_started_at = ? WHERE id = ?",
                (_iso(started_at), channel_id),
            )
            connection.commit()

    def defer_check_until(self, channel_id: str, check_at: str) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE watched_channels
                SET check_started_at = NULL, next_check_at = ?
                WHERE id = ?
                """,
                (check_at, channel_id),
            )
            connection.commit()

    def clear_check_started(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("UPDATE watched_channels SET check_started_at = NULL")
            connection.commit()

    def record_failure(self, channel_id: str, error: str, next_check: datetime) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE watched_channels
                SET last_error = ?, check_started_at = NULL, next_check_at = ?
                WHERE id = ?
                """,
                (error[:1000], _iso(next_check), channel_id),
            )
            connection.commit()


class ChannelWatcherService:
    """Checks due channels and queues only videos newer than the last success."""

    def __init__(
        self,
        store: Optional[ChannelWatcherStore] = None,
        enqueue_video: Optional[Callable[[dict[str, Any], str], Awaitable[None]]] = None,
        providers: Optional[dict[str, ChannelProvider]] = None,
        archive_dir: Optional[str] = None,
        channel_list_timeout_seconds: float = CHANNEL_LIST_TIMEOUT_SECONDS,
    ):
        self.store = store or ChannelWatcherStore()
        self.enqueue_video = enqueue_video
        self.providers = providers or {"youtube": YouTubeChannelProvider()}
        self.archive_dir = Path(
            archive_dir or Path.home() / ".spotify-saver" / "channel-watch-archives"
        )
        self.channel_list_timeout_seconds = channel_list_timeout_seconds
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._check_tasks: dict[str, asyncio.Task[None]] = {}
        self._youtube_metadata_request_lock = asyncio.Lock()

    def start(self) -> None:
        self.store.initialize()
        self.store.clear_check_started()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="channel-watcher")
        logger.info("Channel watcher scheduler started")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            await self._task
            self._task = None
        if self._check_tasks:
            check_tasks = tuple(self._check_tasks.values())
            for task in check_tasks:
                task.cancel()
            await asyncio.gather(*check_tasks, return_exceptions=True)
        self.store.clear_check_started()
        logger.info("Channel watcher scheduler stopped")

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._start_due_checks()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Channel watcher scheduler iteration failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=SCHEDULER_POLL_SECONDS)
            except asyncio.TimeoutError:
                continue

    def _start_due_checks(self) -> None:
        for channel in self.store.due_channels(utc_now()):
            channel_id = channel["id"]
            if channel_id in self._check_tasks:
                continue
            logger.info("Scheduling due check for watched channel %s", channel["url"])
            self._check_tasks[channel_id] = asyncio.create_task(
                self._run_channel_check(channel_id, channel["url"]),
                name=f"channel-check-{channel_id}",
            )

    async def _run_channel_check(self, channel_id: str, channel_url: str) -> None:
        try:
            await self.check_channel(channel_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected failure checking watched channel %s", channel_url)
        finally:
            self._check_tasks.pop(channel_id, None)

    async def run_due_checks(self, now: Optional[datetime] = None) -> None:
        for channel in self.store.due_channels(now or utc_now()):
            try:
                await self.check_channel(channel["id"])
            except Exception:
                logger.exception("Unexpected failure checking watched channel %s", channel["url"])

    async def check_channel(self, channel_id: str) -> None:
        cooldown_until = self.store.get_youtube_cooldown_until()
        if cooldown_until is not None:
            logger.info(
                "Skipping watched channel check during global YouTube cooldown until %s",
                cooldown_until,
            )
            return
        channel = self.store.get_channel(channel_id)
        if channel is None:
            return
        provider = self.providers.get(channel["platform"])
        if provider is None:
            error = f"Unsupported channel platform: {channel['platform']}"
            retry_at = next_check_time(
                channel["interval"],
                channel["check_hour"],
                channel["check_minute"],
                channel["check_weekday"],
                utc_now(),
                channel["timezone"],
            )
            self.store.record_failure(channel_id, error, retry_at)
            logger.error(error)
            return

        previous_check = (
            _parse_datetime(channel["last_checked_at"]) if channel["last_checked_at"] else None
        )
        self.store.record_check_started(channel_id, utc_now())
        logger.info(
            "Starting check for watched channel %s; last successful check: %s",
            channel["url"],
            previous_check.isoformat() if previous_check else "none (baseline)",
        )
        try:
            try:
                cooldown_until = self.store.get_youtube_cooldown_until()
                if cooldown_until is not None:
                    raise YouTubeCooldownActive(cooldown_until)
                title, entries = await asyncio.wait_for(
                    asyncio.to_thread(
                        provider.list_videos,
                        channel["url"],
                        published_after=previous_check,
                    ),
                    timeout=self.channel_list_timeout_seconds,
                )
            except asyncio.TimeoutError as error:
                raise TimeoutError(
                    "YouTube channel listing exceeded "
                    f"{self.channel_list_timeout_seconds:g} seconds"
                ) from error
            logger.info(
                "Fetched %d videos from watched channel %s",
                len(entries),
                channel["url"],
            )
            current_channel = self.store.get_channel(channel_id)
            if current_channel is None or (
                current_channel["platform"] != channel["platform"]
                or current_channel["url"] != channel["url"]
            ):
                return
            channel = current_channel
            queued_count = 0
            if previous_check is not None:
                queued_count = await self._queue_new_videos(
                    channel, provider, entries, previous_check
                )
        except YouTubeCooldownActive as error:
            current_channel = self.store.get_channel(channel_id)
            if current_channel is not None:
                self.store.defer_check_until(channel_id, error.cooldown_until)
            return
        except Exception as error:
            current_channel = self.store.get_channel(channel_id)
            if current_channel is None or (
                current_channel["platform"] != channel["platform"]
                or current_channel["url"] != channel["url"]
            ):
                return
            if _is_youtube_rate_limited(error):
                retry_at = utc_now() + timedelta(seconds=YOUTUBE_RATE_LIMIT_COOLDOWN_SECONDS)
                self.store.set_youtube_cooldown_until(retry_at)
                logger.warning(
                    "YouTube rate limit detected; pausing all channel watcher checks until %s",
                    retry_at.isoformat(),
                )
            else:
                retry_at = next_check_time(
                    current_channel["interval"],
                    current_channel["check_hour"],
                    current_channel["check_minute"],
                    current_channel["check_weekday"],
                    utc_now(),
                    current_channel["timezone"],
                )
            self.store.record_failure(channel_id, str(error), retry_at)
            logger.error("Could not check watched channel %s: %s", current_channel["url"], error)
            return

        checked_at = utc_now()
        current_channel = self.store.get_channel(channel_id)
        if current_channel is None or (
            current_channel["platform"] != channel["platform"]
            or current_channel["url"] != channel["url"]
        ):
            return
        next_at = next_check_time(
            current_channel["interval"],
            current_channel["check_hour"],
            current_channel["check_minute"],
            current_channel["check_weekday"],
            checked_at,
            current_channel["timezone"],
        )
        self.store.record_success(channel_id, title, checked_at, next_at)
        logger.info(
            "Watched channel %s check completed: %d videos found, %d new downloads queued; "
            "next check at %s",
            channel["url"],
            len(entries),
            queued_count,
            next_at.isoformat(),
        )

    async def _queue_new_videos(
        self,
        channel: dict[str, Any],
        provider: ChannelProvider,
        entries: list[dict[str, Any]],
        previous_check: datetime,
    ) -> int:
        if self.enqueue_video is None:
            raise RuntimeError("Channel watcher download queue is not configured")

        queued_count = 0
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self.archive_dir / f"{channel['id']}.txt"
        archived_ids = set()
        if archive_path.exists():
            archived_ids = {
                line.rsplit(" ", 1)[-1].strip()
                for line in archive_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }

        for entry in entries:
            video_id = str(entry.get("id") or "")
            video_url = _video_url(entry)
            if not video_id or not video_url or video_id in archived_ids:
                continue
            published_at, published_date = _published_datetime(entry)
            metadata_loaded = False
            if published_date is None and published_at is None:
                try:
                    details = await self._youtube_request(provider.video_metadata, video_url)
                except YouTubeCooldownActive:
                    raise
                except Exception as error:
                    raise RuntimeError(
                        f"Could not read publish time for {video_url}: {error}"
                    ) from error
                published_at, published_date = _published_datetime(details)
                metadata_loaded = True
                if published_date is None and published_at is None:
                    raise ValueError(f"YouTube did not provide a publish date for {video_url}")

            if published_date is not None:
                today = utc_now().date()
                if published_date < previous_check.date() or published_date > today:
                    continue
                needs_precise_time = (
                    published_date == previous_check.date() or published_date == today
                )
                if needs_precise_time and not metadata_loaded:
                    details = await self._youtube_request(provider.video_metadata, video_url)
                    published_at, _ = _published_datetime(details)
                if needs_precise_time and published_at is None:
                    raise ValueError(
                        "YouTube did not provide a precise publish time for "
                        f"recent upload {video_url}"
                    )
                if published_at is None:
                    published_at = datetime.combine(published_date, time.min, tzinfo=UTC)
            if published_at is not None and published_at > utc_now():
                continue
            if published_at is None or published_at <= previous_check:
                continue
            if self._video_file_exists(channel["output_dir"], entry):
                with archive_path.open("a", encoding="utf-8") as archive:
                    archive.write(f"youtube {video_id}\n")
                archived_ids.add(video_id)
                continue
            await self.enqueue_video(
                {
                    **channel,
                    "download_archive": str(archive_path),
                    "video_title": entry.get("title") or video_id,
                },
                video_url,
            )
            queued_count += 1
            archived_ids.add(video_id)
        return queued_count

    async def _youtube_request(
        self, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        async with self._youtube_metadata_request_lock:
            cooldown_until = self.store.get_youtube_cooldown_until()
            if cooldown_until is not None:
                raise YouTubeCooldownActive(cooldown_until)
            return await asyncio.to_thread(callback, *args, **kwargs)

    @staticmethod
    def _video_file_exists(output_dir: str, entry: dict[str, Any]) -> bool:
        title = _sanitize_filename(str(entry.get("title") or entry.get("id") or ""))
        if not title:
            return False
        root = Path(output_dir)
        if not root.is_dir():
            return False
        directories = [root]
        try:
            directories.extend(directory for directory in root.iterdir() if directory.is_dir())
        except OSError:
            logger.warning("Could not inspect existing video files in %s", root)
            return False
        for directory in directories:
            try:
                if any(
                    file.is_file() and file.stem.casefold() == title.casefold()
                    for file in directory.iterdir()
                ):
                    return True
            except OSError:
                logger.warning("Could not inspect existing video files in %s", directory)
        return False
