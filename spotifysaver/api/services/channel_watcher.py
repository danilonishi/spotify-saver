"""Persistent, platform-aware scheduling of channel checks."""

import asyncio
import json
import sqlite3
import threading
import time as time_module
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Optional, Protocol
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
CHANNEL_DISCOVERY_OVERLAP = 20


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
    if interval == "manual":
        return datetime.max.replace(tzinfo=UTC)

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


CONTENT_FILTER_KEYS = (
    "include_keywords",
    "exclude_keywords",
    "min_duration_seconds",
    "max_duration_seconds",
    "published_within_days",
    "published_after",
    "published_before",
    "exclude_shorts",
    "exclude_livestreams",
)


def _content_filter_settings(settings: dict[str, Any]) -> dict[str, Any]:
    filters = {
        "include_keywords": [
            term.strip() for term in settings.get("include_keywords", []) if term.strip()
        ],
        "exclude_keywords": [
            term.strip() for term in settings.get("exclude_keywords", []) if term.strip()
        ],
    }
    for key in CONTENT_FILTER_KEYS[2:]:
        value = settings.get(key)
        filters[key] = value.isoformat() if isinstance(value, date) else value

    minimum = filters["min_duration_seconds"]
    maximum = filters["max_duration_seconds"]
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("Minimum duration cannot be greater than maximum duration")
    start = filters["published_after"]
    end = filters["published_before"]
    if start is not None and end is not None and start > end:
        raise ValueError("Upload date start cannot be after upload date end")
    return filters


def _shorts_url(video_url: str) -> bool:
    return "/shorts/" in urlparse(video_url).path.casefold()


def _needs_filter_metadata(
    filters: dict[str, Any], entry: dict[str, Any], video_url: str
) -> bool:
    published_at, published_date = _published_datetime(entry)
    if published_at is None and published_date is None:
        return True
    if filters.get("published_within_days") is not None and published_at is None:
        return True
    if (
        (filters.get("min_duration_seconds") is not None
         or filters.get("max_duration_seconds") is not None)
        and not isinstance(entry.get("duration"), (int, float))
    ):
        return True
    if (
        filters.get("exclude_shorts")
        and entry.get("is_short") is None
        and not _shorts_url(video_url)
    ):
        return True
    if filters.get("exclude_livestreams") and not (
        isinstance(entry.get("is_live"), bool)
        or isinstance(entry.get("was_live"), bool)
        or (isinstance(entry.get("live_status"), str) and entry["live_status"])
    ):
        return True
    return False


def _content_filter_reason(
    filters: dict[str, Any],
    entry: dict[str, Any],
    video_url: str,
    now: datetime,
) -> Optional[str]:
    title = str(entry.get("title") or "").casefold()
    include_keywords = filters.get("include_keywords", [])
    if include_keywords and not any(term.casefold() in title for term in include_keywords):
        return "Title does not match any included keyword"
    excluded_term = next(
        (term for term in filters.get("exclude_keywords", []) if term.casefold() in title),
        None,
    )
    if excluded_term:
        return f"Title contains excluded term: {excluded_term}"

    duration = entry.get("duration")
    if filters.get("min_duration_seconds") is not None:
        if not isinstance(duration, (int, float)):
            raise ValueError("YouTube did not provide video duration")
        if duration < filters["min_duration_seconds"]:
            return "Video is shorter than the minimum duration"
    if filters.get("max_duration_seconds") is not None:
        if not isinstance(duration, (int, float)):
            raise ValueError("YouTube did not provide video duration")
        if duration > filters["max_duration_seconds"]:
            return "Video is longer than the maximum duration"

    published_at, published_date = _published_datetime(entry)
    if published_at is None and published_date is None:
        raise ValueError("YouTube did not provide a publish date")
    published_day = published_at.date() if published_at else published_date
    if filters.get("published_within_days") is not None:
        cutoff = now - timedelta(days=filters["published_within_days"])
        if (
            published_at < cutoff
            if published_at
            else published_day < cutoff.date()
        ):
            return "Video is older than the selected upload window"
    if filters.get("published_after") and published_day < date.fromisoformat(
        filters["published_after"]
    ):
        return "Video was uploaded before the selected start date"
    if filters.get("published_before") and published_day > date.fromisoformat(
        filters["published_before"]
    ):
        return "Video was uploaded after the selected end date"

    if filters.get("exclude_shorts"):
        is_short = entry.get("is_short")
        if is_short is True or _shorts_url(video_url):
            return "Shorts are excluded"
    if filters.get("exclude_livestreams"):
        live_status = str(entry.get("live_status") or "").casefold()
        if (
            entry.get("is_live") is True
            or entry.get("was_live") is True
            or live_status in {"is_live", "is_upcoming", "was_live", "post_live"}
        ):
            return "Livestreams are excluded"
    return None


class ChannelProvider(Protocol):
    """Platform adapter used by the scheduler to enumerate published content."""

    def scan_videos(
        self,
        channel_url: str,
        cursor_video_id: Optional[str],
        known_video_ids: set[str],
        limit: int,
    ) -> "ChannelScan":
        ...

    def video_metadata(self, video_url: str) -> dict[str, Any]:
        ...


@dataclass
class ChannelScan:
    title: str
    entries: list[dict[str, Any]]
    newest_video_id: Optional[str]
    cursor_reached: bool


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

    def scan_videos(
        self,
        channel_url: str,
        cursor_video_id: Optional[str],
        known_video_ids: set[str],
        limit: int,
    ) -> ChannelScan:
        with yt_dlp.YoutubeDL(self._options()) as ydl:
            channel_info = ydl.extract_info(
                channel_url,
                download=False,
                process=False,
            )
            if not channel_info:
                raise ValueError("YouTube did not return channel metadata")
            channel_id = str(channel_info.get("channel_id") or channel_info.get("id") or "")
            if not channel_id.startswith("UC"):
                raise ValueError("YouTube did not return a channel ID for its uploads playlist")
            uploads_playlist_id = f"UU{channel_id[2:]}"
            playlist = ydl.extract_info(
                f"https://www.youtube.com/playlist?list={uploads_playlist_id}",
                download=False,
                process=False,
            )
            if not playlist or playlist.get("entries") is None:
                raise ValueError("YouTube did not return an uploads playlist")

            entries: list[dict[str, Any]] = []
            newest_video_id = None
            cursor_found = cursor_video_id is None
            cursor_reached = cursor_video_id is None
            overlap_count = 0
            for entry in playlist["entries"]:
                if not entry or not entry.get("id"):
                    continue
                video_id = str(entry["id"])
                if newest_video_id is None:
                    newest_video_id = video_id
                if cursor_video_id is not None and video_id == cursor_video_id:
                    cursor_found = True
                    continue

                if cursor_found and cursor_video_id is not None:
                    overlap_count += 1
                if video_id not in known_video_ids:
                    entries.append(self._normalize_upload_dates([entry])[0])
                if cursor_found and cursor_video_id is not None:
                    if overlap_count >= CHANNEL_DISCOVERY_OVERLAP:
                        cursor_reached = True
                        break
                if len(entries) >= limit:
                    break
            else:
                cursor_reached = cursor_found

        title = (
            channel_info.get("channel")
            or channel_info.get("uploader")
            or channel_info.get("title")
            or channel_url
        )
        return ChannelScan(str(title), entries, newest_video_id, cursor_reached)

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
                    filters_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    check_started_at TEXT,
                    discovery_cursor_video_id TEXT,
                    discovery_initialized INTEGER NOT NULL DEFAULT 0,
                    next_check_at TEXT NOT NULL,
                    last_error TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
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
            if "discovery_cursor_video_id" not in columns:
                connection.execute(
                    "ALTER TABLE watched_channels ADD COLUMN discovery_cursor_video_id TEXT"
                )
            if "discovery_initialized" not in columns:
                connection.execute(
                    "ALTER TABLE watched_channels "
                    "ADD COLUMN discovery_initialized INTEGER NOT NULL DEFAULT 0"
                )
            if "active" not in columns:
                connection.execute(
                    "ALTER TABLE watched_channels "
                    "ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )
            if "filters_json" not in columns:
                connection.execute(
                    "ALTER TABLE watched_channels "
                    "ADD COLUMN filters_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.execute(
                """
                UPDATE watched_channels
                SET interval = 'hourly'
                WHERE interval = 'minutely'
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS watched_channel_videos (
                    channel_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    video_url TEXT NOT NULL,
                    title TEXT,
                    entry_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel_id, video_id)
                )
                """
            )
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
        filters = json.loads(item.pop("filters_json", "{}"))
        for key in CONTENT_FILTER_KEYS:
            item[key] = filters.get(key)
        item["include_keywords"] = item["include_keywords"] or []
        item["exclude_keywords"] = item["exclude_keywords"] or []
        item["exclude_shorts"] = bool(item["exclude_shorts"])
        item["exclude_livestreams"] = bool(item["exclude_livestreams"])
        item["include_subtitles"] = bool(item["include_subtitles"])
        item["active"] = bool(item["active"])
        return item

    def list_channels(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM watched_channels ORDER BY created_at, id"
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def replace_channels(self, channels: list[dict[str, Any]]) -> None:
        """Replace watched-channel settings while preserving discovery cursors."""
        values = []
        for channel in channels:
            normalized_url = _normalize_channel_url(channel["url"])
            filters = _content_filter_settings(channel)
            _get_timezone(channel["timezone"])
            if channel["interval"] == "weekly" and channel["check_weekday"] is None:
                raise ValueError("Weekly schedules require a weekday")
            values.append(
                (
                    channel["id"],
                    channel["platform"],
                    normalized_url,
                    channel["url"].strip(),
                    channel.get("title"),
                    channel["output_dir"],
                    channel["interval"],
                    channel["check_hour"],
                    channel["check_minute"],
                    channel.get("check_weekday"),
                    channel["timezone"],
                    channel["video_format"],
                    channel["video_resolution"],
                    int(channel["include_subtitles"]),
                    json.dumps(filters),
                    channel["created_at"],
                    channel.get("last_checked_at"),
                    None,
                    channel.get("discovery_cursor_video_id"),
                    int(channel["discovery_initialized"]),
                    channel["next_check_at"],
                    None,
                    int(channel["active"]),
                )
            )

        with self._connect() as connection:
            connection.execute("DELETE FROM watched_channel_videos")
            connection.execute("DELETE FROM watched_channels")
            connection.executemany(
                """
                INSERT INTO watched_channels (
                    id, platform, normalized_url, url, title, output_dir, interval,
                    check_hour, check_minute, check_weekday, timezone, video_format,
                    video_resolution, include_subtitles, filters_json, created_at,
                    last_checked_at, check_started_at, discovery_cursor_video_id,
                    discovery_initialized, next_check_at, last_error, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?)
                """,
                values,
            )
            connection.commit()

    def get_channel(self, channel_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM watched_channels WHERE id = ?", (channel_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def set_channel_active(self, channel_id: str, active: bool) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE watched_channels SET active = ? WHERE id = ?",
                (int(active), channel_id),
            )
            if cursor.rowcount == 0:
                return None
            connection.commit()
        return self.get_channel(channel_id)

    def create_channel(self, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        normalized_url = _normalize_channel_url(settings["url"])
        filters = _content_filter_settings(settings)
        _get_timezone(settings.get("timezone", "UTC"))
        if settings["interval"] == "weekly" and settings.get("check_weekday") is None:
            raise ValueError("Weekly schedules require a weekday")
        channel_id = str(uuid.uuid4())
        next_check = datetime.max.replace(tzinfo=UTC) if settings["interval"] == "manual" else now
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
            json.dumps(filters),
            _iso(now),
            None,
            _iso(next_check),
            None,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO watched_channels (
                    id, platform, normalized_url, url, title, output_dir, interval,
                    check_hour, check_minute, check_weekday, timezone, video_format,
                    video_resolution, include_subtitles, filters_json, created_at,
                    last_checked_at, next_check_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        filters = _content_filter_settings(settings)
        _get_timezone(settings.get("timezone", "UTC"))
        if settings["interval"] == "weekly" and settings.get("check_weekday") is None:
            raise ValueError("Weekly schedules require a weekday")
        url_changed = normalized_url != _normalize_channel_url(existing["url"])
        source_changed = (
            url_changed or settings.get("platform", existing["platform"]) != existing["platform"]
        )
        next_check = next_check_time(
            settings["interval"],
            settings["check_hour"],
            settings["check_minute"],
            settings.get("check_weekday"),
            now,
            settings.get("timezone", "UTC"),
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE watched_channels
                SET platform = ?, normalized_url = ?, url = ?, title = ?,
                    output_dir = ?, interval = ?, check_hour = ?, check_minute = ?,
                    check_weekday = ?, timezone = ?, video_format = ?, video_resolution = ?,
                    include_subtitles = ?, filters_json = ?, last_checked_at = ?, check_started_at = NULL,
                    discovery_cursor_video_id = ?, discovery_initialized = ?,
                    next_check_at = ?,
                    last_error = NULL
                WHERE id = ?
                """,
                (
                    settings.get("platform", existing["platform"]),
                    normalized_url,
                    settings["url"].strip(),
                    None if source_changed else existing["title"],
                    settings["output_dir"],
                    settings["interval"],
                    settings["check_hour"],
                    settings["check_minute"],
                    settings.get("check_weekday"),
                    settings.get("timezone", "UTC"),
                    settings.get("video_format", "mp4"),
                    settings.get("video_resolution", 0),
                    int(settings.get("include_subtitles", False)),
                    json.dumps(filters),
                    None if source_changed else existing["last_checked_at"],
                    None if source_changed else existing["discovery_cursor_video_id"],
                    0 if source_changed else existing["discovery_initialized"],
                    _iso(next_check),
                    channel_id,
                ),
            )
            if source_changed:
                connection.execute(
                    "DELETE FROM watched_channel_videos WHERE channel_id = ?", (channel_id,)
                )
            connection.commit()
        return self.get_channel(channel_id)

    def delete_channel(self, channel_id: str) -> bool:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM watched_channel_videos WHERE channel_id = ?", (channel_id,)
            )
            cursor = connection.execute("DELETE FROM watched_channels WHERE id = ?", (channel_id,))
            connection.commit()
        return cursor.rowcount > 0

    def known_video_ids(self, channel_id: str) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT video_id FROM watched_channel_videos WHERE channel_id = ?",
                (channel_id,),
            ).fetchall()
        return {row[0] for row in rows}

    def save_discovery_scan(
        self,
        channel_id: str,
        entries: list[dict[str, Any]],
        now: datetime,
        cursor_video_id: Optional[str] = None,
        baseline_entries: Optional[list[dict[str, Any]]] = None,
        advance_cursor: bool = False,
    ) -> None:
        values = []
        for items, status, error in (
            (entries, "discovered", None),
            (baseline_entries or [], "skipped", "Initial baseline"),
        ):
            for entry in items:
                video_id = str(entry.get("id") or "")
                video_url = _video_url(entry)
                if not video_id or not video_url:
                    continue
                values.append(
                    (
                        channel_id,
                        video_id,
                        video_url,
                        entry.get("title"),
                        json.dumps(entry, separators=(",", ":"), default=str),
                        status,
                        error,
                        _iso(now),
                        _iso(now),
                    )
                )
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO watched_channel_videos (
                    channel_id, video_id, video_url, title, entry_json, status,
                    error, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            if advance_cursor:
                connection.execute(
                    """
                    UPDATE watched_channels
                    SET discovery_cursor_video_id = ?, discovery_initialized = 1
                    WHERE id = ?
                    """,
                    (cursor_video_id, channel_id),
                )
            connection.commit()

    def pending_videos(self, channel_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT video_id, video_url, title, entry_json, status
                FROM watched_channel_videos
                WHERE channel_id = ?
                  AND status IN ('discovered', 'failed', 'awaiting_confirmation')
                ORDER BY rowid
                LIMIT ?
                """,
                (channel_id, MAX_CHANNEL_VIDEOS_PER_CHECK),
            ).fetchall()
        return [
            {
                "id": row["video_id"],
                "webpage_url": row["video_url"],
                "title": row["title"],
                "entry": json.loads(row["entry_json"]),
                "status": row["status"],
            }
            for row in rows
        ]

    def reset_queued_video_statuses(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE watched_channel_videos
                SET status = 'discovered', updated_at = ?
                WHERE status IN ('queued', 'awaiting_confirmation')
                """,
                (_iso(utc_now()),),
            )
            connection.commit()

    def update_video_entry(
        self, channel_id: str, video_id: str, entry: dict[str, Any], now: datetime
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE watched_channel_videos
                SET title = ?, entry_json = ?, updated_at = ?
                WHERE channel_id = ? AND video_id = ?
                """,
                (
                    entry.get("title"),
                    json.dumps(entry, separators=(",", ":"), default=str),
                    _iso(now),
                    channel_id,
                    video_id,
                ),
            )
            connection.commit()

    def update_video_status(
        self,
        channel_id: str,
        video_id: str,
        status: str,
        now: datetime,
        error: Optional[str] = None,
        expected_status: Optional[str] = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE watched_channel_videos
                SET status = ?, error = ?, updated_at = ?
                WHERE channel_id = ? AND video_id = ?
                    {"AND status = ?" if expected_status is not None else ""}
                """,
                (status, error, _iso(now), channel_id, video_id)
                + ((expected_status,) if expected_status is not None else ()),
            )
            connection.commit()

    def due_channels(self, now: datetime) -> list[dict[str, Any]]:
        if self.get_youtube_cooldown_until(now) is not None:
            return []
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM watched_channels
                WHERE active = 1 AND interval != 'manual' AND next_check_at <= ?
                """,
                (_iso(now),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_youtube_cooldown_until(self, now: Optional[datetime] = None) -> Optional[str]:
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
            connection.execute(
                "UPDATE watched_channels SET check_started_at = ? WHERE id = ?",
                (_iso(started_at), channel_id),
            )
            connection.commit()

    def defer_check_until(self, channel_id: str, check_at: str) -> None:
        with self._connect() as connection:
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
        with self._connect() as connection:
            connection.execute("UPDATE watched_channels SET check_started_at = NULL")
            connection.commit()

    def record_failure(self, channel_id: str, error: str, next_check: datetime) -> None:
        with self._connect() as connection:
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
        enqueue_video: Optional[
            Callable[[dict[str, Any], str], Awaitable[Optional[str]]]
        ] = None,
        providers: Optional[dict[str, ChannelProvider]] = None,
        archive_dir: Optional[str] = None,
        channel_list_timeout_seconds: float = CHANNEL_LIST_TIMEOUT_SECONDS,
        publish_notification: Optional[
            Callable[..., Awaitable[None]]
        ] = None,
    ):
        self.store = store or ChannelWatcherStore()
        self.enqueue_video = enqueue_video
        self.providers = providers or {"youtube": YouTubeChannelProvider()}
        self.archive_dir = Path(
            archive_dir or Path.home() / ".spotify-saver" / "channel-watch-archives"
        )
        self.channel_list_timeout_seconds = channel_list_timeout_seconds
        self.publish_notification = publish_notification
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._check_tasks: dict[str, asyncio.Task[None]] = {}
        self._youtube_metadata_request_lock = asyncio.Lock()

    def start(self) -> None:
        self.store.initialize()
        self.store.clear_check_started()
        self.store.reset_queued_video_statuses()
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

    def start_manual_check(self, channel_id: str) -> bool:
        channel = self.store.get_channel(channel_id)
        if channel is None:
            return False
        cooldown_until = self.store.get_youtube_cooldown_until()
        if cooldown_until is not None:
            raise YouTubeCooldownActive(cooldown_until)
        if channel_id in self._check_tasks or channel["check_started_at"]:
            return False
        self._check_tasks[channel_id] = asyncio.create_task(
            self._run_channel_check(channel_id, channel["url"]),
            name=f"manual-channel-check-{channel_id}",
        )
        return True

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
        discovery_initialized = bool(channel["discovery_initialized"])
        cursor_video_id = channel["discovery_cursor_video_id"]
        is_initial_baseline = not discovery_initialized and previous_check is None
        check_started_at = utc_now()
        discovered_count = 0
        self.store.record_check_started(channel_id, check_started_at)
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
                scan = await asyncio.wait_for(
                    asyncio.to_thread(
                        provider.scan_videos,
                        channel["url"],
                        cursor_video_id,
                        self.store.known_video_ids(channel_id),
                        MAX_CHANNEL_VIDEOS_PER_CHECK
                        + (CHANNEL_DISCOVERY_OVERLAP if not discovery_initialized else 0),
                    ),
                    timeout=self.channel_list_timeout_seconds,
                )
            except asyncio.TimeoutError as error:
                raise TimeoutError(
                    "YouTube channel listing exceeded "
                    f"{self.channel_list_timeout_seconds:g} seconds"
                ) from error
            logger.info(
                "Scanned %d uploads from watched channel %s",
                len(scan.entries),
                channel["url"],
            )
            current_channel = self.store.get_channel(channel_id)
            if current_channel is None or (
                current_channel["platform"] != channel["platform"]
                or current_channel["url"] != channel["url"]
            ):
                return
            channel = current_channel
            advance_cursor = is_initial_baseline or not discovery_initialized or scan.cursor_reached
            if is_initial_baseline:
                new_entries = []
                baseline_entries = scan.entries
            elif not discovery_initialized:
                new_entries = scan.entries[:MAX_CHANNEL_VIDEOS_PER_CHECK]
                baseline_entries = scan.entries[MAX_CHANNEL_VIDEOS_PER_CHECK:]
            else:
                new_entries = scan.entries
                baseline_entries = []
            discovered_count = len(new_entries)
            self.store.save_discovery_scan(
                channel_id,
                new_entries,
                utc_now(),
                cursor_video_id=scan.newest_video_id if advance_cursor else None,
                baseline_entries=baseline_entries,
                advance_cursor=advance_cursor,
            )
            logger.info(
                "Persisted %d new upload candidates for watched channel %s",
                discovered_count,
                channel["url"],
            )
            if is_initial_baseline:
                logger.info(
                    "Established initial upload baseline for watched channel %s at video %s",
                    channel["url"],
                    scan.newest_video_id or "none",
                )
                queued_count = 0
            else:
                queued_count = await self._queue_pending_videos(channel, provider)
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
            if self.publish_notification:
                await self.publish_notification(
                    "check_failed",
                    f"Could not check {current_channel.get('title') or current_channel['url']}: {error}",
                    source=current_channel.get("title") or current_channel["url"],
                    channel_id=channel_id,
                )
            return

        completed_at = utc_now()
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
            completed_at,
            current_channel["timezone"],
        )
        self.store.record_success(channel_id, scan.title, completed_at, next_at)
        logger.info(
            "Watched channel %s check completed: %d new uploads discovered, "
            "%d new downloads queued; "
            "next check at %s",
            channel["url"],
            discovered_count,
            queued_count,
            next_at.isoformat(),
        )
        if queued_count and self.publish_notification:
            source = scan.title or channel.get("title") or channel["url"]
            await self.publish_notification(
                "new_content",
                f"Queued {queued_count} new video(s) from {source} for download.",
                source=source,
                channel_id=channel_id,
                count=str(queued_count),
            )

    async def _queue_pending_videos(
        self,
        channel: dict[str, Any],
        provider: ChannelProvider,
    ) -> int:
        if self.enqueue_video is None:
            raise RuntimeError("Channel watcher download queue is not configured")

        queued_count = 0
        failures = []
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self.archive_dir / f"{channel['id']}.txt"
        archived_ids = set()
        if archive_path.exists():
            archived_ids = {
                line.rsplit(" ", 1)[-1].strip()
                for line in archive_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }

        for candidate in self.store.pending_videos(channel["id"]):
            video_id = candidate["id"]
            entry = candidate["entry"]
            video_url = candidate["webpage_url"]
            if video_id in archived_ids:
                self.store.update_video_status(channel["id"], video_id, "downloaded", utc_now())
                continue

            if self._video_file_exists(channel["output_dir"], entry):
                self.store.update_video_status(
                    channel["id"], video_id, "skipped", utc_now(), "Already present on disk"
                )
                continue

            if _needs_filter_metadata(channel, entry, video_url):
                try:
                    details = await self._youtube_request(provider.video_metadata, video_url)
                except YouTubeCooldownActive:
                    raise
                except Exception as error:
                    self.store.update_video_status(
                        channel["id"], video_id, "failed", utc_now(), str(error)
                    )
                    failures.append(f"Could not read video details for {video_url}: {error}")
                    continue
                entry = {**entry, **details}
                self.store.update_video_entry(channel["id"], video_id, entry, utc_now())

            published_at, published_date = _published_datetime(entry)
            if published_date is None and published_at is None:
                error = f"YouTube did not provide a publish date for {video_url}"
                self.store.update_video_status(
                    channel["id"], video_id, "failed", utc_now(), error
                )
                failures.append(error)
                continue

            if published_at is not None and published_at > utc_now():
                continue
            if published_date is not None and published_date > utc_now().date():
                continue

            try:
                filter_reason = _content_filter_reason(
                    channel, entry, video_url, utc_now()
                )
            except ValueError as error:
                self.store.update_video_status(
                    channel["id"], video_id, "failed", utc_now(), str(error)
                )
                failures.append(f"Could not apply content filters to {video_url}: {error}")
                continue
            if filter_reason:
                self.store.update_video_status(
                    channel["id"], video_id, "filtered", utc_now(), filter_reason
                )
                continue

            if candidate["status"] == "failed":
                self.store.update_video_status(
                    channel["id"], video_id, "discovered", utc_now(), expected_status="failed"
                )
            try:
                queue_status = await self.enqueue_video(
                    {
                        **channel,
                        "download_archive": str(archive_path),
                        "video_title": entry.get("title") or video_id,
                    },
                    video_url,
                )
            except Exception as error:
                self.store.update_video_status(
                    channel["id"], video_id, "failed", utc_now(), str(error)
                )
                failures.append(f"Could not enqueue {video_url}: {error}")
                continue
            self.store.update_video_status(
                channel["id"],
                video_id,
                queue_status or "queued",
                utc_now(),
                expected_status="discovered",
            )
            queued_count += 1

        if failures:
            raise RuntimeError("; ".join(failures))
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
