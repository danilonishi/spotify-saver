import asyncio
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from types import SimpleNamespace

import spotifysaver.api.services.channel_watcher as channel_watcher_service
from spotifysaver.api.routers import download as download_router
from spotifysaver.api.routers.channel_watcher import (
    check_watched_channel_now,
    create_watched_channel,
    delete_watched_channel,
    get_channel_watcher_status,
    list_watched_channels,
    set_watched_channel_active,
    update_watched_channel,
)
from spotifysaver.api.schemas import (
    DownloadRequest,
    DownloadStatus,
    WatchedChannelActiveRequest,
    WatchedChannelRequest,
)
from spotifysaver.api.services.channel_watcher import (
    MAX_CHANNEL_VIDEOS_PER_CHECK,
    CHANNEL_DISCOVERY_OVERLAP,
    ChannelScan,
    ChannelWatcherService,
    ChannelWatcherStore,
    YouTubeChannelProvider,
    next_check_time,
)

UTC = timezone.utc


class FakeChannelProvider:
    def __init__(self, entries=None, error=None, metadata=None):
        self.entries = entries or []
        self.error = error
        self.metadata = metadata or {}
        self.scan_calls = []

    def scan_videos(self, _channel_url, cursor_video_id, known_video_ids, limit):
        self.scan_calls.append((cursor_video_id, known_video_ids, limit))
        if self.error:
            raise self.error
        newest_video_id = str(self.entries[0]["id"]) if self.entries else None
        if cursor_video_id is None:
            return ChannelScan("Test channel", self.entries[:limit], newest_video_id, True)

        discovered = []
        cursor_found = False
        cursor_reached = False
        overlap_count = 0
        for index, entry in enumerate(self.entries):
            if str(entry.get("id")) == cursor_video_id:
                cursor_found = True
                continue
            if cursor_found:
                overlap_count += 1
            if str(entry.get("id")) not in known_video_ids:
                discovered.append(entry)
            if cursor_found and overlap_count >= CHANNEL_DISCOVERY_OVERLAP:
                cursor_reached = True
                break
            if len(discovered) >= limit:
                break
        else:
            cursor_reached = cursor_found
        return ChannelScan("Test channel", discovered, newest_video_id, cursor_reached)

    def video_metadata(self, video_url):
        return self.metadata.get(video_url, {})


def _add_channel(store, output_dir, content_filters=None):
    settings = {
        "platform": "youtube",
        "url": "https://www.youtube.com/@test/videos",
        "output_dir": str(output_dir),
        "interval": "daily",
        "check_hour": 9,
        "check_minute": 30,
        "check_weekday": None,
        "video_format": "mp4",
        "video_resolution": 1080,
        "include_subtitles": False,
    }
    settings.update(content_filters or {})
    store.initialize()
    return store.create_channel(settings, datetime(2026, 10, 1, 12, tzinfo=UTC))


def test_content_filters_persist_and_validate(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(
        store,
        tmp_path / "videos",
        {
            "include_keywords": [" review ", "tutorial"],
            "exclude_keywords": ["spoiler"],
            "min_duration_seconds": 120,
            "max_duration_seconds": 3600,
            "published_within_days": 30,
            "published_after": "2026-09-01",
            "published_before": "2026-10-01",
            "exclude_shorts": True,
            "exclude_livestreams": True,
        },
    )

    assert channel["include_keywords"] == ["review", "tutorial"]
    assert channel["exclude_keywords"] == ["spoiler"]
    assert channel["min_duration_seconds"] == 120
    assert channel["max_duration_seconds"] == 3600
    assert channel["published_within_days"] == 30
    assert channel["published_after"] == "2026-09-01"
    assert channel["published_before"] == "2026-10-01"
    assert channel["exclude_shorts"] is True
    assert channel["exclude_livestreams"] is True
    assert store.list_channels()[0]["exclude_shorts"] is True

    updated_settings = {
        **channel,
        "include_keywords": ["podcast"],
        "exclude_keywords": [],
        "min_duration_seconds": None,
        "max_duration_seconds": 1800,
        "published_within_days": None,
        "published_after": None,
        "published_before": None,
        "exclude_shorts": False,
        "exclude_livestreams": True,
    }
    updated = store.update_channel(
        channel["id"], updated_settings, datetime(2026, 10, 2, 12, tzinfo=UTC)
    )
    restored = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    restored.initialize()
    persisted_update = restored.get_channel(channel["id"])
    assert updated["include_keywords"] == ["podcast"]
    assert persisted_update["include_keywords"] == ["podcast"]
    assert persisted_update["max_duration_seconds"] == 1800
    assert persisted_update["exclude_shorts"] is False

    with pytest.raises(ValueError, match="Minimum duration"):
        _add_channel(
            store,
            tmp_path / "videos",
            {"min_duration_seconds": 600, "max_duration_seconds": 300},
        )


def test_channel_filter_predicates_cover_keyword_duration_dates_and_types():
    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    filters = {
        "include_keywords": ["review", "tutorial"],
        "exclude_keywords": ["spoiler"],
        "min_duration_seconds": 60,
        "max_duration_seconds": 600,
        "published_within_days": 30,
        "published_after": "2026-10-01",
        "published_before": "2026-10-31",
        "exclude_shorts": True,
        "exclude_livestreams": True,
    }
    eligible = {
        "title": "New Tutorial: Review",
        "duration": 120,
        "upload_date": "20261010",
        "is_short": False,
        "live_status": "not_live",
    }

    assert channel_watcher_service._content_filter_reason(
        filters, eligible, "https://www.youtube.com/watch?v=regular", now
    ) is None
    assert channel_watcher_service._needs_filter_metadata(
        {"published_within_days": 1},
        eligible,
        "https://www.youtube.com/watch?v=regular",
    )
    assert not channel_watcher_service._needs_filter_metadata(
        {"published_within_days": 1},
        {**eligible, "timestamp": now.timestamp()},
        "https://www.youtube.com/watch?v=regular",
    )
    assert "included keyword" in channel_watcher_service._content_filter_reason(
        filters, {**eligible, "title": "New album"}, "https://youtube.com/watch?v=x", now
    )
    assert "excluded term" in channel_watcher_service._content_filter_reason(
        filters, {**eligible, "title": "Tutorial spoiler"}, "https://youtube.com/watch?v=x", now
    )
    assert "minimum duration" in channel_watcher_service._content_filter_reason(
        filters, {**eligible, "duration": 30}, "https://youtube.com/watch?v=x", now
    )
    assert "maximum duration" in channel_watcher_service._content_filter_reason(
        filters, {**eligible, "duration": 700}, "https://youtube.com/watch?v=x", now
    )
    assert "upload window" in channel_watcher_service._content_filter_reason(
        filters, {**eligible, "upload_date": "20260801"}, "https://youtube.com/watch?v=x", now
    )
    date_filters = {**filters, "published_within_days": None}
    assert "start date" in channel_watcher_service._content_filter_reason(
        date_filters,
        {**eligible, "upload_date": "20260915"},
        "https://youtube.com/watch?v=x",
        now,
    )
    assert "end date" in channel_watcher_service._content_filter_reason(
        date_filters,
        {**eligible, "upload_date": "20261101"},
        "https://youtube.com/watch?v=x",
        now,
    )
    assert "Shorts" in channel_watcher_service._content_filter_reason(
        filters, eligible, "https://www.youtube.com/shorts/short-id", now
    )
    assert "Livestreams" in channel_watcher_service._content_filter_reason(
        filters,
        {**eligible, "live_status": "was_live"},
        "https://www.youtube.com/watch?v=stream",
        now,
    )


def test_pending_shorts_and_livestreams_are_filtered_using_video_metadata(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    output_dir = tmp_path / "videos"
    output_dir.mkdir()
    channel = _add_channel(
        store,
        output_dir,
        {"exclude_shorts": True, "exclude_livestreams": True},
    )
    entries = [
        {
            "id": "short-upload",
            "title": "Short",
            "upload_date": datetime.now(UTC).strftime("%Y%m%d"),
        },
        {
            "id": "stream-upload",
            "title": "Stream",
            "upload_date": datetime.now(UTC).strftime("%Y%m%d"),
        },
    ]
    store.save_discovery_scan(channel["id"], entries, datetime.now(UTC))
    short_url = "https://www.youtube.com/watch?v=short-upload"
    stream_url = "https://www.youtube.com/watch?v=stream-upload"
    provider = FakeChannelProvider(
        metadata={
            short_url: {"is_short": True},
            stream_url: {"live_status": "was_live"},
        }
    )
    queued = []

    async def enqueue(_channel, video_url):
        queued.append(video_url)

    service = ChannelWatcherService(
        store=store,
        enqueue_video=enqueue,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
    )
    asyncio.run(service._queue_pending_videos(channel, provider))

    assert queued == []
    assert len(provider.metadata) == 2
    assert store.pending_videos(channel["id"]) == []


def test_duration_filter_enriches_metadata_before_enqueue(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos", {"min_duration_seconds": 60})
    video_url = "https://www.youtube.com/watch?v=duration-upload"
    store.save_discovery_scan(
        channel["id"],
        [{
            "id": "duration-upload",
            "title": "Long enough upload",
            "upload_date": datetime.now(UTC).strftime("%Y%m%d"),
        }],
        datetime.now(UTC),
    )
    provider = FakeChannelProvider(metadata={video_url: {"duration": 120}})
    queued = []

    async def enqueue(_channel, candidate_url):
        queued.append(candidate_url)

    service = ChannelWatcherService(
        store=store,
        enqueue_video=enqueue,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
    )

    asyncio.run(service._queue_pending_videos(channel, provider))

    assert queued == [video_url]
    assert len(provider.metadata) == 1


def test_store_connections_close_after_success_and_failure(monkeypatch, tmp_path):
    class FakeConnection:
        def __init__(self):
            self.closed = False
            self.exit_exception = None

        def __enter__(self):
            return self

        def __exit__(self, exception_type, _exception, _traceback):
            self.exit_exception = exception_type

        def close(self):
            self.closed = True

    connections = []

    def connect(_database_path):
        connection = FakeConnection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(channel_watcher_service.sqlite3, "connect", connect)
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))

    with store._connect() as connection:
        assert connection is connections[-1]

    assert connections[-1].closed
    assert connections[-1].exit_exception is None

    try:
        with store._connect():
            raise RuntimeError("query failed")
    except RuntimeError as error:
        assert str(error) == "query failed"
    else:
        raise AssertionError("Store connection should propagate query errors")

    assert connections[-1].closed
    assert connections[-1].exit_exception is RuntimeError


def test_next_check_time_uses_utc_hourly_minute_and_weekly_day():
    after = datetime(2026, 10, 7, 14, 35, tzinfo=UTC)

    assert next_check_time(
        "manual", 9, 15, None, after, "America/Los_Angeles"
    ) == datetime.max.replace(tzinfo=UTC)
    assert next_check_time("hourly", 9, 15, None, after) == datetime(
        2026, 10, 7, 15, 15, tzinfo=UTC
    )
    assert next_check_time("daily", 9, 30, None, after) == datetime(2026, 10, 8, 9, 30, tzinfo=UTC)
    assert next_check_time("weekly", 9, 30, 0, after) == datetime(2026, 10, 12, 9, 30, tzinfo=UTC)


def test_next_check_time_preserves_local_time_and_handles_dst():
    zone = "America/Los_Angeles"
    before_spring_forward = datetime(2026, 3, 7, 12, tzinfo=UTC)
    after_spring_forward = datetime(2026, 3, 8, 16, 30, tzinfo=UTC)
    during_fall_back = datetime(2026, 11, 1, 8, 45, tzinfo=UTC)

    assert next_check_time("daily", 9, 0, None, before_spring_forward, zone) == datetime(
        2026, 3, 7, 17, 0, tzinfo=UTC
    )
    assert next_check_time("daily", 9, 0, None, after_spring_forward, zone) == datetime(
        2026, 3, 9, 16, 0, tzinfo=UTC
    )
    assert next_check_time("hourly", 0, 15, None, during_fall_back, zone) == datetime(
        2026, 11, 1, 9, 15, tzinfo=UTC
    )
    assert next_check_time("daily", 1, 30, None, during_fall_back, zone) == datetime(
        2026, 11, 2, 9, 30, tzinfo=UTC
    )


def test_youtube_channel_listing_uses_approximate_dates_without_video_detail_lookups():
    timestamp = datetime(2026, 10, 10, 19, 30, tzinfo=UTC).timestamp()

    entries = YouTubeChannelProvider._normalize_upload_dates(
        [
            {"id": "video", "timestamp": timestamp},
            {"id": "scheduled", "timestamp": timestamp, "release_timestamp": timestamp},
            {"id": "dated", "timestamp": timestamp, "upload_date": "20261009"},
        ]
    )

    assert YouTubeChannelProvider._options()["extractor_args"] == {
        "youtube": {"approximate_date": [""]}
    }
    assert YouTubeChannelProvider._options()["socket_timeout"] == 30
    assert YouTubeChannelProvider._options()["sleep_interval_requests"] == 1
    assert entries[0] == {"id": "video", "upload_date": "20261010"}
    assert entries[1]["timestamp"] == timestamp
    assert entries[2]["upload_date"] == "20261009"


def test_youtube_video_metadata_requests_are_spaced(monkeypatch):
    clock = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    class FakeYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            assert download is False
            return {"id": "video"}

    monkeypatch.setattr(channel_watcher_service.time_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(channel_watcher_service.time_module, "sleep", sleep)
    monkeypatch.setattr(channel_watcher_service.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    provider = YouTubeChannelProvider()

    provider.video_metadata("https://www.youtube.com/watch?v=first")
    provider.video_metadata("https://www.youtube.com/watch?v=second")

    assert sleeps == [5]


def test_youtube_scan_reads_uploads_playlist_until_cursor_and_overlap(monkeypatch):
    old_entries = [{"id": f"old-{index}"} for index in range(CHANNEL_DISCOVERY_OVERLAP)]
    playlist_entries = [
        {"id": "new-upload"},
        {"id": "pending-upload"},
        {"id": "cursor"},
        *old_entries,
    ]
    urls = []

    class FakeYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download, process):
            assert download is False
            assert process is False
            urls.append(url)
            if "playlist?list=" not in url:
                return {"channel_id": "UC123", "channel": "Test channel"}
            return {"entries": playlist_entries}

    monkeypatch.setattr(channel_watcher_service.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    scan = YouTubeChannelProvider().scan_videos(
        "https://www.youtube.com/@test",
        "cursor",
        {"pending-upload", *(entry["id"] for entry in old_entries)},
        MAX_CHANNEL_VIDEOS_PER_CHECK,
    )

    assert urls == [
        "https://www.youtube.com/@test",
        "https://www.youtube.com/playlist?list=UU123",
    ]
    assert scan.title == "Test channel"
    assert scan.newest_video_id == "new-upload"
    assert scan.cursor_reached is True
    assert [entry["id"] for entry in scan.entries] == ["new-upload"]


def test_youtube_scan_stops_at_safety_limit_before_cursor(monkeypatch):
    consumed = []
    playlist_entries = [
        {"id": f"video-{index}"} for index in range(MAX_CHANNEL_VIDEOS_PER_CHECK + 1)
    ]
    playlist_entries.append({"id": "cursor"})

    class FakeYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download, process):
            if "playlist?list=" in url:
                return {
                    "entries": (consumed.append(entry["id"]) or entry for entry in playlist_entries)
                }
            return {"channel_id": "UC123"}

    monkeypatch.setattr(channel_watcher_service.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    scan = YouTubeChannelProvider().scan_videos(
        "https://www.youtube.com/@test",
        "cursor",
        set(),
        MAX_CHANNEL_VIDEOS_PER_CHECK,
    )

    assert len(scan.entries) == MAX_CHANNEL_VIDEOS_PER_CHECK
    assert len(consumed) == MAX_CHANNEL_VIDEOS_PER_CHECK
    assert scan.cursor_reached is False


def test_youtube_baseline_scan_is_capped_to_recent_uploads(monkeypatch):
    calls = []
    consumed = []

    class FakeYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download, process):
            calls.append((url, download, process))
            if "playlist?list=" in url:
                return {
                    "entries": (
                        consumed.append(index) or {"id": f"video-{index}"}
                        for index in range(MAX_CHANNEL_VIDEOS_PER_CHECK + 1)
                    )
                }
            return {"channel": "Test channel", "channel_id": "UC123"}

    monkeypatch.setattr(channel_watcher_service.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    scan = YouTubeChannelProvider().scan_videos(
        "https://www.youtube.com/@test",
        None,
        set(),
        MAX_CHANNEL_VIDEOS_PER_CHECK,
    )
    assert scan.title == "Test channel"
    assert len(scan.entries) == MAX_CHANNEL_VIDEOS_PER_CHECK
    assert scan.newest_video_id == "video-0"
    assert consumed == list(range(MAX_CHANNEL_VIDEOS_PER_CHECK))
    assert calls == [
        ("https://www.youtube.com/@test", False, False),
        ("https://www.youtube.com/playlist?list=UU123", False, False),
    ]


def test_nonexistent_daily_wall_time_advances_to_valid_time():
    after = datetime(2026, 3, 8, 8, 0, tzinfo=UTC)

    assert next_check_time("daily", 2, 30, None, after, "America/Los_Angeles") == datetime(
        2026, 3, 8, 10, 0, tzinfo=UTC
    )


def test_updated_schedule_is_not_due_until_its_next_local_occurrence(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    updated_at = datetime(2026, 10, 10, 12, tzinfo=UTC)
    settings = {
        **channel,
        "interval": "daily",
        "check_hour": 9,
        "check_minute": 30,
        "check_weekday": None,
        "timezone": "America/Los_Angeles",
    }
    updated = store.update_channel(channel["id"], settings, updated_at)
    expected_check = datetime(2026, 10, 10, 16, 30, tzinfo=UTC)
    provider = FakeChannelProvider()
    service = ChannelWatcherService(
        store=store,
        enqueue_video=None,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
    )

    assert updated["next_check_at"] == expected_check.isoformat()
    asyncio.run(service.run_due_checks(expected_check - timedelta(seconds=1)))
    assert store.get_channel(channel["id"])["last_checked_at"] is None

    asyncio.run(service.run_due_checks(expected_check))
    assert store.get_channel(channel["id"])["last_checked_at"] is not None


def test_successful_check_logs_when_no_new_videos_are_queued(tmp_path, caplog):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    service = ChannelWatcherService(
        store=store,
        enqueue_video=None,
        providers={"youtube": FakeChannelProvider()},
        archive_dir=str(tmp_path / "archives"),
    )
    caplog.set_level(logging.INFO, logger="uvicorn.error.ChannelWatcher")

    asyncio.run(service.check_channel(channel["id"]))

    messages = [record.getMessage() for record in caplog.records]
    assert any("Starting check for watched channel" in message for message in messages)
    assert any(
        "0 new uploads discovered, 0 new downloads queued" in message for message in messages
    )


def test_timed_out_channel_listing_records_failure_and_clears_in_progress(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")

    class SlowProvider(FakeChannelProvider):
        def scan_videos(self, _channel_url, cursor_video_id, known_video_ids, limit):
            time.sleep(0.1)
            return ChannelScan("Test channel", [], None, True)

    service = ChannelWatcherService(
        store=store,
        enqueue_video=None,
        providers={"youtube": SlowProvider()},
        archive_dir=str(tmp_path / "archives"),
        channel_list_timeout_seconds=0.01,
    )

    asyncio.run(service.check_channel(channel["id"]))

    checked = store.get_channel(channel["id"])
    assert checked["last_checked_at"] is None
    assert checked["check_started_at"] is None
    assert "channel listing exceeded 0.01 seconds" in checked["last_error"]


def test_scheduler_task_runs_due_channels_until_stopped(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    service = ChannelWatcherService(
        store=store,
        enqueue_video=None,
        providers={"youtube": FakeChannelProvider()},
        archive_dir=str(tmp_path / "archives"),
    )

    async def exercise():
        service.start()

        async def wait_for_check():
            while store.get_channel(channel["id"])["last_checked_at"] is None:
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(wait_for_check(), timeout=2)
        finally:
            await service.stop()

    asyncio.run(exercise())
    assert store.get_channel(channel["id"])["last_checked_at"] is not None


def test_stopping_scheduler_cancels_a_hung_channel_check(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    started = threading.Event()
    resume = threading.Event()

    class WaitingProvider(FakeChannelProvider):
        def scan_videos(self, _channel_url, cursor_video_id, known_video_ids, limit):
            started.set()
            resume.wait(timeout=5)
            return ChannelScan("Test channel", [], None, True)

    service = ChannelWatcherService(
        store=store,
        enqueue_video=None,
        providers={"youtube": WaitingProvider()},
        archive_dir=str(tmp_path / "archives"),
    )

    async def exercise():
        service.start()
        try:
            await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=2)
            await asyncio.wait_for(service.stop(), timeout=1)
        finally:
            resume.set()

    asyncio.run(exercise())

    assert store.get_channel(channel["id"])["check_started_at"] is None


def test_slow_channel_check_does_not_block_scheduler_from_checking_other_channels(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    first_channel = _add_channel(store, tmp_path / "videos")
    second_channel = store.create_channel(
        {
            "platform": "youtube",
            "url": "https://www.youtube.com/@second/videos",
            "output_dir": str(tmp_path / "videos"),
            "interval": "daily",
            "check_hour": 9,
            "check_minute": 30,
            "check_weekday": None,
            "video_format": "mp4",
            "video_resolution": 1080,
            "include_subtitles": False,
        },
        datetime(2026, 10, 1, 12, tzinfo=UTC),
    )
    first_started = threading.Event()
    resume_first = threading.Event()
    second_finished = threading.Event()

    class DelayedProvider(FakeChannelProvider):
        def scan_videos(self, channel_url, cursor_video_id, known_video_ids, limit):
            if "@second" in channel_url:
                second_finished.set()
            else:
                first_started.set()
                resume_first.wait(timeout=5)
            return ChannelScan("Test channel", [], None, True)

    service = ChannelWatcherService(
        store=store,
        enqueue_video=None,
        providers={"youtube": DelayedProvider()},
        archive_dir=str(tmp_path / "archives"),
    )

    async def exercise():
        service.start()

        async def wait_for_second_success():
            await asyncio.to_thread(second_finished.wait)
            while store.get_channel(second_channel["id"])["last_checked_at"] is None:
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(wait_for_second_success(), timeout=2)
            assert first_started.is_set()
            assert store.get_channel(first_channel["id"])["check_started_at"] is not None
        finally:
            resume_first.set()
            await service.stop()

    asyncio.run(exercise())


def test_in_flight_check_uses_updated_schedule_when_saving_next_check(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    started = threading.Event()
    resume = threading.Event()

    class WaitingProvider(FakeChannelProvider):
        def scan_videos(self, channel_url, cursor_video_id, known_video_ids, limit):
            started.set()
            resume.wait()
            return super().scan_videos(channel_url, cursor_video_id, known_video_ids, limit)

    async def exercise():
        service = ChannelWatcherService(
            store=store,
            enqueue_video=None,
            providers={"youtube": WaitingProvider()},
            archive_dir=str(tmp_path / "archives"),
        )
        check = asyncio.create_task(service.check_channel(channel["id"]))
        await asyncio.to_thread(started.wait)
        assert store.get_channel(channel["id"])["check_started_at"] is not None

        updated_at = datetime.now(UTC)
        updated_settings = {
            **store.get_channel(channel["id"]),
            "interval": "daily",
            "check_hour": (updated_at.hour + 2) % 24,
            "check_minute": 15,
            "check_weekday": None,
            "timezone": "UTC",
        }
        store.update_channel(channel["id"], updated_settings, updated_at)
        resume.set()
        await check

        checked = store.get_channel(channel["id"])
        assert checked["check_started_at"] is None
        assert (
            checked["next_check_at"]
            == next_check_time(
                "daily",
                updated_settings["check_hour"],
                15,
                None,
                datetime.fromisoformat(checked["last_checked_at"]),
            ).isoformat()
        )

    asyncio.run(exercise())


def test_existing_watched_channel_database_defaults_timezone_to_utc(tmp_path):
    database_path = tmp_path / "legacy-watcher.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE watched_channels (
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
                video_format TEXT NOT NULL,
                video_resolution INTEGER NOT NULL,
                include_subtitles INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_checked_at TEXT,
                next_check_at TEXT NOT NULL,
                last_error TEXT,
                UNIQUE (platform, normalized_url)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO watched_channels VALUES (
                'legacy-id', 'youtube', 'https://youtube.com/@old',
                'https://youtube.com/@old', 'Old channel', ?, 'daily',
                9, 30, NULL, 'mp4', 0, 0, ?, NULL, ?, NULL
            )
            """,
            (
                str(tmp_path),
                datetime(2026, 10, 1, tzinfo=UTC).isoformat(),
                datetime(2026, 10, 1, tzinfo=UTC).isoformat(),
            ),
        )

    store = ChannelWatcherStore(str(database_path))
    store.initialize()

    assert store.get_channel("legacy-id")["timezone"] == "UTC"
    assert store.get_channel("legacy-id")["check_started_at"] is None
    assert store.get_channel("legacy-id")["active"] is True


def test_inactive_channel_is_not_due_and_can_be_reactivated(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)

    assert [item["id"] for item in store.due_channels(now)] == [channel["id"]]
    paused = store.set_channel_active(channel["id"], False)
    assert paused["active"] is False
    assert store.due_channels(now) == []
    reloaded_store = ChannelWatcherStore(store.database_path)
    assert reloaded_store.get_channel(channel["id"])["active"] is False

    resumed = store.set_channel_active(channel["id"], True)
    assert resumed["active"] is True
    assert [item["id"] for item in store.due_channels(now)] == [channel["id"]]


def test_first_success_establishes_baseline_without_downloading(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    provider = FakeChannelProvider(
        [{"id": "old-video", "title": "Old video", "timestamp": 1_790_000_000}]
    )
    queued = []

    async def enqueue(_channel, video_url):
        queued.append(video_url)

    service = ChannelWatcherService(
        store=store,
        enqueue_video=enqueue,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
    )
    asyncio.run(service.check_channel(channel["id"]))

    checked = store.get_channel(channel["id"])
    assert queued == []
    assert provider.scan_calls[0][0] is None
    assert store.pending_videos(channel["id"]) == []
    assert checked["discovery_cursor_video_id"] == "old-video"
    assert checked["last_checked_at"] is not None
    assert checked["last_error"] is None


def test_success_queues_only_new_videos_not_already_on_disk(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    output_dir = tmp_path / "videos"
    output_dir.mkdir(parents=True)
    (output_dir / "Existing upload.mp4").write_text("video")
    channel = _add_channel(store, output_dir)
    previous_check = datetime(2026, 10, 1, 12, tzinfo=UTC)
    store.record_success(
        channel["id"],
        "Test channel",
        previous_check,
        datetime(2026, 10, 2, 9, 30, tzinfo=UTC),
    )
    store.save_discovery_scan(
        channel["id"],
        [],
        previous_check,
        cursor_video_id="cursor",
        baseline_entries=[{"id": "cursor", "title": "Previously seen"}],
        advance_cursor=True,
    )
    provider = FakeChannelProvider(
        [
            {
                "id": "existing-video",
                "title": "Existing upload",
                "upload_date": "20261002",
            },
            {
                "id": "new-video",
                "title": "New upload",
                "upload_date": "20251002",
            },
            {
                "id": "scheduled-video",
                "title": "Scheduled upload",
                "timestamp": int((datetime.now(UTC) + timedelta(days=1)).timestamp()),
            },
            {"id": "cursor", "title": "Previously seen"},
        ]
    )
    queued = []

    async def enqueue(watched_channel, video_url):
        queued.append((watched_channel, video_url))

    notifications = []

    async def publish_notification(category, message, **details):
        notifications.append((category, message, details))

    service = ChannelWatcherService(
        store=store,
        enqueue_video=enqueue,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
        publish_notification=publish_notification,
    )
    asyncio.run(service.check_channel(channel["id"]))

    assert [url for _, url in queued] == ["https://www.youtube.com/watch?v=new-video"]
    assert queued[0][0]["download_archive"].endswith(f"{channel['id']}.txt")
    assert store.get_channel(channel["id"])["last_error"] is None
    assert notifications[0][0] == "new_content"
    assert notifications[0][1] == "Queued 1 new video(s) from Test channel for download."
    assert notifications[0][2]["count"] == "1"


def test_failed_channel_check_publishes_notification(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    notifications = []

    async def publish_notification(category, message, **details):
        notifications.append((category, message, details))

    service = ChannelWatcherService(
        store=store,
        providers={"youtube": FakeChannelProvider(error=RuntimeError("upstream unavailable"))},
        publish_notification=publish_notification,
    )

    asyncio.run(service.check_channel(channel["id"]))

    assert len(notifications) == 1
    assert notifications[0][0] == "check_failed"
    assert "upstream unavailable" in notifications[0][1]
    assert notifications[0][2]["channel_id"] == channel["id"]


def test_newly_discovered_upload_is_not_filtered_by_check_timestamp(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    previous_check = datetime(2026, 10, 10, 23, 1, 42, tzinfo=UTC)
    store.record_success(
        channel["id"],
        "Test channel",
        previous_check,
        datetime(2026, 10, 11, 1, 0, tzinfo=UTC),
    )
    store.save_discovery_scan(
        channel["id"],
        [],
        previous_check,
        cursor_video_id="cursor",
        baseline_entries=[{"id": "cursor", "title": "Previously seen"}],
        advance_cursor=True,
    )
    provider = FakeChannelProvider(
        [
            {
                "id": "late-visible-upload",
                "title": "Late visible upload",
                "upload_date": "20261010",
            },
            {"id": "cursor", "title": "Previously seen"},
        ]
    )
    queued = []

    async def enqueue(_channel, video_url):
        queued.append(video_url)

    service = ChannelWatcherService(
        store=store,
        enqueue_video=enqueue,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
    )

    asyncio.run(service.check_channel(channel["id"]))

    assert queued == ["https://www.youtube.com/watch?v=late-visible-upload"]
    assert store.get_channel(channel["id"])["discovery_cursor_video_id"] == ("late-visible-upload")


def test_upload_discovery_resumes_after_safety_cap_without_skipping_candidates(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    previous_check = datetime.now(UTC) - timedelta(hours=1)
    store.record_success(
        channel["id"],
        "Test channel",
        previous_check,
        datetime.now(UTC),
    )
    store.save_discovery_scan(
        channel["id"],
        [],
        previous_check,
        cursor_video_id="old-cursor",
        baseline_entries=[{"id": "old-cursor", "title": "Previously seen"}],
        advance_cursor=True,
    )
    upload_date = datetime.now(UTC).strftime("%Y%m%d")
    uploads = [
        {"id": f"upload-{index}", "title": f"Upload {index}", "upload_date": upload_date}
        for index in range(MAX_CHANNEL_VIDEOS_PER_CHECK + 5)
    ]
    provider = FakeChannelProvider([*uploads, {"id": "old-cursor"}])
    queued = []

    async def enqueue(_channel, video_url):
        queued.append(video_url)

    service = ChannelWatcherService(
        store=store,
        enqueue_video=enqueue,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
    )

    asyncio.run(service.check_channel(channel["id"]))
    assert len(queued) == MAX_CHANNEL_VIDEOS_PER_CHECK
    assert store.get_channel(channel["id"])["discovery_cursor_video_id"] == "old-cursor"

    asyncio.run(service.check_channel(channel["id"]))

    assert len(queued) == MAX_CHANNEL_VIDEOS_PER_CHECK + 5
    assert store.get_channel(channel["id"])["discovery_cursor_video_id"] == "upload-0"
    assert len({url for url in queued}) == MAX_CHANNEL_VIDEOS_PER_CHECK + 5


def test_existing_channel_migration_recovers_recent_ids_and_preserves_baseline(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    last_checked = datetime.now(UTC) - timedelta(minutes=1)
    store.record_success(
        channel["id"],
        "Test channel",
        last_checked,
        datetime.now(UTC),
    )
    old_upload_date = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y%m%d")
    entries = [
        {"id": f"upload-{index}", "title": f"Upload {index}", "upload_date": old_upload_date}
        for index in range(MAX_CHANNEL_VIDEOS_PER_CHECK + 5)
    ]
    provider = FakeChannelProvider(entries)
    queued = []

    async def enqueue(_channel, video_url):
        queued.append(video_url)

    service = ChannelWatcherService(
        store=store,
        enqueue_video=enqueue,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
    )

    asyncio.run(service.check_channel(channel["id"]))

    assert len(queued) == MAX_CHANNEL_VIDEOS_PER_CHECK
    assert store.get_channel(channel["id"])["discovery_cursor_video_id"] == "upload-0"

    asyncio.run(service.check_channel(channel["id"]))

    assert len(queued) == MAX_CHANNEL_VIDEOS_PER_CHECK


def test_discovered_upload_is_retried_after_enqueue_failure(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    previous_check = datetime.now(UTC) - timedelta(hours=1)
    store.record_success(
        channel["id"],
        "Test channel",
        previous_check,
        datetime.now(UTC),
    )
    store.save_discovery_scan(
        channel["id"],
        [],
        previous_check,
        cursor_video_id="old-cursor",
        baseline_entries=[{"id": "old-cursor"}],
        advance_cursor=True,
    )
    provider = FakeChannelProvider(
        [
            {
                "id": "retry-upload",
                "title": "Retry upload",
                "upload_date": datetime.now(UTC).strftime("%Y%m%d"),
            },
            {"id": "old-cursor"},
        ]
    )
    attempts = []

    async def enqueue(_channel, video_url):
        attempts.append(video_url)
        if len(attempts) == 1:
            raise RuntimeError("Queue temporarily unavailable")

    service = ChannelWatcherService(
        store=store,
        enqueue_video=enqueue,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
    )

    asyncio.run(service.check_channel(channel["id"]))
    failed_check = store.get_channel(channel["id"])
    assert "Queue temporarily unavailable" in failed_check["last_error"]
    assert failed_check["discovery_cursor_video_id"] == "retry-upload"

    asyncio.run(service.check_channel(channel["id"]))

    assert attempts == [
        "https://www.youtube.com/watch?v=retry-upload",
        "https://www.youtube.com/watch?v=retry-upload",
    ]
    assert store.get_channel(channel["id"])["last_error"] is None


def test_watched_video_download_updates_persistent_video_state(tmp_path, monkeypatch):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    video_id = "downloaded-upload"
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    archive_path = tmp_path / "archives" / f"{channel['id']}.txt"
    archive_path.parent.mkdir()
    archive_path.write_text(f"youtube {video_id}\n", encoding="utf-8")
    store.save_discovery_scan(
        channel["id"],
        [{"id": video_id, "title": "Downloaded upload"}],
        datetime.now(UTC),
    )

    class SuccessfulVideoDownloadService:
        def __init__(self, **_kwargs):
            pass

        async def download_from_url(self, _url, **_kwargs):
            return {
                "completed_tracks": 1,
                "failed_tracks": 0,
                "failed_track_names": [],
                "output_directory": str(tmp_path / "videos"),
            }

    monkeypatch.setattr(download_router, "VideoDownloadService", SuccessfulVideoDownloadService)
    monkeypatch.setattr(download_router, "ChannelWatcherStore", lambda: store)

    task_id = "watched-video-task"
    request = DownloadRequest(
        spotify_url=video_url,
        output_dir=str(tmp_path / "videos"),
        media_type="video",
    )
    monkeypatch.setitem(
        download_router.tasks,
        task_id,
        DownloadStatus(task_id=task_id, status="queued", progress=0),
    )
    monkeypatch.setitem(download_router.requests_by_task, task_id, request)
    monkeypatch.setitem(download_router.cancellation_events, task_id, threading.Event())
    monkeypatch.setitem(download_router.download_archives, task_id, str(archive_path))

    asyncio.run(download_router.download_task(task_id, request))

    with sqlite3.connect(store.database_path) as connection:
        status = connection.execute(
            """
            SELECT status FROM watched_channel_videos
            WHERE channel_id = ? AND video_id = ?
            """,
            (channel["id"], video_id),
        ).fetchone()[0]
    assert status == "downloaded"


def test_same_day_upload_uses_exact_video_publish_timestamp(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    previous_check = datetime(2026, 10, 1, 12, tzinfo=UTC)
    store.record_success(
        channel["id"],
        "Test channel",
        previous_check,
        datetime(2026, 10, 2, 9, 30, tzinfo=UTC),
    )
    video_url = "https://www.youtube.com/watch?v=same-day-video"
    provider = FakeChannelProvider(
        [{"id": "same-day-video", "title": "Same day upload", "upload_date": "20261001"}],
        metadata={video_url: {"timestamp": int(datetime(2026, 10, 1, 13, tzinfo=UTC).timestamp())}},
    )
    queued = []

    async def enqueue(_channel, url):
        queued.append(url)

    service = ChannelWatcherService(
        store=store,
        enqueue_video=enqueue,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
    )
    asyncio.run(service.check_channel(channel["id"]))

    assert queued == [video_url]


def test_missing_same_day_publish_time_does_not_advance_watermark(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    previous_check = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    store.record_success(
        channel["id"],
        "Test channel",
        previous_check,
        previous_check + timedelta(days=1),
    )
    provider = FakeChannelProvider(
        [
            {
                "id": "unknown-time",
                "title": "Unknown time",
            }
        ]
    )

    async def enqueue(_channel, _url):
        raise AssertionError("Video without an exact publish time must not be queued")

    service = ChannelWatcherService(
        store=store,
        enqueue_video=enqueue,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
    )

    asyncio.run(service.check_channel(channel["id"]))

    checked = store.get_channel(channel["id"])
    assert checked["last_checked_at"] == previous_check.isoformat()
    assert "did not provide a publish date" in checked["last_error"]


def test_failed_fetch_preserves_last_successful_check(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    last_check = datetime(2026, 10, 1, 12, tzinfo=UTC)
    store.record_success(
        channel["id"],
        "Test channel",
        last_check,
        datetime(2026, 10, 2, 9, 30, tzinfo=UTC),
    )
    service = ChannelWatcherService(
        store=store,
        enqueue_video=None,
        providers={"youtube": FakeChannelProvider(error=RuntimeError("YouTube unavailable"))},
        archive_dir=str(tmp_path / "archives"),
    )

    asyncio.run(service.check_channel(channel["id"]))

    checked = store.get_channel(channel["id"])
    assert checked["last_checked_at"] == last_check.isoformat()
    assert checked["last_error"] == "YouTube unavailable"


def test_youtube_rate_limit_delays_retry_for_one_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(channel_watcher_service, "_get_timezone", lambda _name: UTC)
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    channel = store.update_channel(
        channel["id"],
        {**channel, "interval": "hourly"},
        datetime.now(UTC),
    )
    previous_check = datetime.now(UTC) - timedelta(minutes=5)
    store.record_success(
        channel["id"],
        "Test channel",
        previous_check,
        previous_check + timedelta(minutes=1),
    )
    error = (
        "This content isn't available. The current session has been rate-limited by YouTube "
        "for up to an hour."
    )

    class RateLimitedProvider(FakeChannelProvider):
        def video_metadata(self, _video_url):
            raise RuntimeError(error)

    async def enqueue(_channel, _video_url):
        raise AssertionError("Rate-limited video must not be queued")

    provider = RateLimitedProvider(entries=[{"id": "rate-limited-video"}])
    service = ChannelWatcherService(
        store=store,
        enqueue_video=enqueue,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
    )
    check_started_at = datetime.now(UTC)

    asyncio.run(service.check_channel(channel["id"]))

    checked = store.get_channel(channel["id"])
    retry_at = datetime.fromisoformat(checked["next_check_at"])
    assert retry_at >= check_started_at + timedelta(minutes=59)
    assert retry_at <= check_started_at + timedelta(minutes=61)
    assert "rate-limited by YouTube" in checked["last_error"]
    cooldown_until = store.get_youtube_cooldown_until()
    assert cooldown_until is not None
    assert datetime.fromisoformat(cooldown_until) == retry_at
    assert store.due_channels(datetime.now(UTC)) == []
    assert store.get_youtube_cooldown_until(retry_at) is None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(channel_watcher=service)))
    assert asyncio.run(get_channel_watcher_status(request)) == {
        "youtube_cooldown_until": cooldown_until
    }

    second_channel = store.create_channel(
        {
            "platform": "youtube",
            "url": "https://www.youtube.com/@second/videos",
            "output_dir": str(tmp_path / "videos"),
            "interval": "hourly",
            "check_hour": 9,
            "check_minute": 30,
            "check_weekday": None,
            "video_format": "mp4",
            "video_resolution": 1080,
            "include_subtitles": False,
        },
        datetime.now(UTC),
    )
    asyncio.run(service.check_channel(second_channel["id"]))
    assert len(provider.scan_calls) == 1
    assert len(store.due_channels(retry_at)) == 2

    reloaded_store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    assert reloaded_store.get_youtube_cooldown_until() == cooldown_until


def test_existing_per_channel_rate_limit_cooldown_migrates_globally(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")
    legacy_cooldown = datetime.now(UTC) + timedelta(minutes=30)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            UPDATE watched_channels
            SET last_error = ?, next_check_at = ?
            WHERE id = ?
            """,
            ("YouTube rate-limited this session", legacy_cooldown.isoformat(), channel["id"]),
        )

    store.initialize()

    assert store.get_youtube_cooldown_until() == legacy_cooldown.isoformat()
    assert store.due_channels(datetime.now(UTC)) == []


def test_channel_watcher_api_persists_settings_and_supports_crud(tmp_path):
    output_dir = tmp_path / "videos"
    output_dir.mkdir()
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    store.initialize()
    service = ChannelWatcherService(store=store)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(channel_watcher=service)))
    payload = {
        "platform": "youtube",
        "url": "https://www.youtube.com/@test/videos",
        "output_dir": str(output_dir),
        "interval": "weekly",
        "check_hour": 7,
        "check_minute": 45,
        "check_weekday": 2,
        "timezone": "America/Los_Angeles",
        "video_format": "webm",
        "video_resolution": 720,
        "include_subtitles": True,
    }

    async def exercise_routes():
        settings = WatchedChannelRequest.model_validate(payload)
        channel = await create_watched_channel(settings, request)
        assert channel["url"] == payload["url"]
        assert channel["video_format"] == "webm"
        assert channel["include_subtitles"] is True
        assert channel["timezone"] == "America/Los_Angeles"
        assert channel["active"] is True

        listed = await list_watched_channels(request)
        assert listed[0]["id"] == channel["id"]
        assert await get_channel_watcher_status(request) == {"youtube_cooldown_until": None}

        payload["video_resolution"] = 1080
        updated = await update_watched_channel(
            channel["id"], WatchedChannelRequest.model_validate(payload), request
        )
        assert updated["video_resolution"] == 1080
        paused = await set_watched_channel_active(
            channel["id"], WatchedChannelActiveRequest(active=False), request
        )
        assert paused["active"] is False
        assert store.get_channel(channel["id"])["active"] is False
        resumed = await set_watched_channel_active(
            channel["id"], WatchedChannelActiveRequest(active=True), request
        )
        assert resumed["active"] is True

        removed = await delete_watched_channel(channel["id"], request)
        assert removed["message"] == "Watched channel removed"
        assert await list_watched_channels(request) == []

    asyncio.run(exercise_routes())


def test_channel_watcher_api_accepts_manual_interval_and_rejects_minutely(tmp_path):
    output_dir = tmp_path / "videos"
    output_dir.mkdir()
    payload = {
        "url": "https://www.youtube.com/@test",
        "output_dir": str(output_dir),
        "interval": "manual",
        "check_hour": 9,
        "check_minute": 30,
    }

    settings = WatchedChannelRequest.model_validate(payload)

    assert settings.interval == "manual"
    try:
        WatchedChannelRequest.model_validate({**payload, "interval": "minutely"})
    except ValueError:
        pass
    else:
        raise AssertionError("The removed minutely interval must be rejected")


def test_manual_channel_is_not_due_and_check_now_starts_only_once(tmp_path):
    output_dir = tmp_path / "videos"
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    store.initialize()
    provider = FakeChannelProvider(entries=[{"id": "existing-video"}])
    service = ChannelWatcherService(
        store=store,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
    )
    channel = store.create_channel(
        {
            "platform": "youtube",
            "url": "https://www.youtube.com/@manual",
            "output_dir": str(output_dir),
            "interval": "manual",
            "check_hour": 9,
            "check_minute": 30,
        },
        datetime.now(UTC),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(channel_watcher=service)))

    async def exercise():
        assert channel["next_check_at"] == datetime.max.replace(tzinfo=UTC).isoformat()
        assert store.due_channels(datetime.max.replace(tzinfo=UTC)) == []
        response = await check_watched_channel_now(channel["id"], request)
        assert response == {"message": "Channel check started"}
        try:
            await check_watched_channel_now(channel["id"], request)
        except Exception as error:
            assert getattr(error, "status_code", None) == 409
        else:
            raise AssertionError("A duplicate check request should be rejected")
        await asyncio.gather(*service._check_tasks.values())

    asyncio.run(exercise())
    checked = store.get_channel(channel["id"])
    assert checked["last_checked_at"] is not None
    assert checked["interval"] == "manual"
    assert provider.scan_calls
