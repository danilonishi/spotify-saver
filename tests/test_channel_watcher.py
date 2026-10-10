import asyncio
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from types import SimpleNamespace

import spotifysaver.api.services.channel_watcher as channel_watcher_service
from spotifysaver.api.routers.channel_watcher import (
    create_watched_channel,
    delete_watched_channel,
    get_channel_watcher_status,
    list_watched_channels,
    update_watched_channel,
)
from spotifysaver.api.schemas import WatchedChannelRequest
from spotifysaver.api.services.channel_watcher import (
    MAX_CHANNEL_VIDEOS_PER_CHECK,
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
        self.published_after_calls = []

    def list_videos(self, _channel_url, published_after=None):
        self.published_after_calls.append(published_after)
        if self.error:
            raise self.error
        return "Test channel", self.entries if published_after is not None else []

    def video_metadata(self, video_url):
        return self.metadata.get(video_url, {})


def _add_channel(store, output_dir):
    store.initialize()
    return store.create_channel(
        {
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
        },
        datetime(2026, 10, 1, 12, tzinfo=UTC),
    )


def test_next_check_time_uses_utc_hourly_minute_and_weekly_day():
    after = datetime(2026, 10, 7, 14, 35, tzinfo=UTC)

    assert next_check_time(
        "minutely",
        9,
        15,
        None,
        datetime(2026, 10, 7, 14, 35, 42, 123456, tzinfo=UTC),
        "America/Los_Angeles",
    ) == datetime(2026, 10, 7, 14, 36, tzinfo=UTC)
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


def test_youtube_listing_stops_at_the_last_check_date_in_each_tab():
    cutoff = datetime(2026, 10, 10, 19, 30, tzinfo=UTC)
    timestamp_today = datetime(2026, 10, 10, 19, 0, tzinfo=UTC).timestamp()
    timestamp_old = datetime(2026, 10, 9, 23, 0, tzinfo=UTC).timestamp()
    playlist = {
        "entries": [
            {
                "_type": "playlist",
                "entries": [
                    {"id": "new-upload", "timestamp": timestamp_today},
                    {"id": "old-upload", "timestamp": timestamp_old},
                    {"id": "should-not-be-read", "timestamp": timestamp_today},
                ],
            },
            {
                "_type": "playlist",
                "entries": [{"id": "new-short", "timestamp": timestamp_today}],
            },
        ]
    }

    entries = YouTubeChannelProvider._recent_entries(playlist, cutoff)

    assert [entry["id"] for entry in entries] == ["new-upload", "new-short"]


def test_youtube_listing_is_capped_at_latest_video_limit_without_dates():
    cutoff = datetime(2026, 10, 10, 19, 30, tzinfo=UTC)
    consumed = []

    def entries():
        for index in range(MAX_CHANNEL_VIDEOS_PER_CHECK + 1):
            consumed.append(index)
            yield {"id": f"video-{index}"}

    playlist = {"entries": entries()}

    recent = YouTubeChannelProvider._recent_entries(playlist, cutoff)

    assert len(recent) == MAX_CHANNEL_VIDEOS_PER_CHECK
    assert len(consumed) == MAX_CHANNEL_VIDEOS_PER_CHECK
    assert recent[-1]["id"] == f"video-{MAX_CHANNEL_VIDEOS_PER_CHECK - 1}"


def test_youtube_video_limit_is_shared_across_channel_tabs():
    cutoff = datetime(2026, 10, 10, 19, 30, tzinfo=UTC)
    playlist = {
        "entries": [
            {"_type": "playlist", "entries": [{"id": f"video-{i}"} for i in range(75)]},
            {"_type": "playlist", "entries": [{"id": f"short-{i}"} for i in range(75)]},
        ]
    }

    recent = YouTubeChannelProvider._recent_entries(playlist, cutoff)

    assert len(recent) == MAX_CHANNEL_VIDEOS_PER_CHECK
    assert recent[-1]["id"] == f"video-{MAX_CHANNEL_VIDEOS_PER_CHECK - 1}"


def test_youtube_baseline_does_not_iterate_channel_uploads(monkeypatch):
    calls = []

    class UnreadEntries:
        def __iter__(self):
            raise AssertionError("Baseline checks must not enumerate upload history")

    class FakeYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download, process):
            calls.append((download, process))
            return {"channel": "Test channel", "entries": UnreadEntries()}

    monkeypatch.setattr(channel_watcher_service.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    assert YouTubeChannelProvider().list_videos(
        "https://www.youtube.com/@test",
        published_after=None,
    ) == ("Test channel", [])
    assert calls == [(False, False)]


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
    assert any("0 videos found, 0 new downloads queued" in message for message in messages)


def test_timed_out_channel_listing_records_failure_and_clears_in_progress(tmp_path):
    store = ChannelWatcherStore(str(tmp_path / "watcher.sqlite3"))
    channel = _add_channel(store, tmp_path / "videos")

    class SlowProvider(FakeChannelProvider):
        def list_videos(self, _channel_url, published_after=None):
            time.sleep(0.1)
            return "Test channel", []

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
        def list_videos(self, _channel_url, published_after=None):
            started.set()
            resume.wait(timeout=5)
            return "Test channel", []

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
        def list_videos(self, channel_url, published_after=None):
            if "@second" in channel_url:
                second_finished.set()
            else:
                first_started.set()
                resume_first.wait(timeout=5)
            return "Test channel", []

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
        def list_videos(self, channel_url, published_after=None):
            started.set()
            resume.wait()
            return super().list_videos(channel_url, published_after)

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
    assert provider.published_after_calls == [None]
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
    provider = FakeChannelProvider(
        [
            {
                "id": "old-video",
                "title": "Old upload",
                "timestamp": int(datetime(2026, 10, 1, 11, tzinfo=UTC).timestamp()),
            },
            {
                "id": "existing-video",
                "title": "Existing upload",
                "timestamp": int(datetime(2026, 10, 2, 8, tzinfo=UTC).timestamp()),
            },
            {
                "id": "new-video",
                "title": "New upload",
                "upload_date": "20261002",
            },
            {
                "id": "scheduled-video",
                "title": "Scheduled upload",
                "timestamp": int((datetime.now(UTC) + timedelta(days=1)).timestamp()),
            },
        ]
    )
    queued = []

    async def enqueue(watched_channel, video_url):
        queued.append((watched_channel, video_url))

    service = ChannelWatcherService(
        store=store,
        enqueue_video=enqueue,
        providers={"youtube": provider},
        archive_dir=str(tmp_path / "archives"),
    )
    asyncio.run(service.check_channel(channel["id"]))

    assert [url for _, url in queued] == ["https://www.youtube.com/watch?v=new-video"]
    assert queued[0][0]["download_archive"].endswith(f"{channel['id']}.txt")
    assert store.get_channel(channel["id"])["last_error"] is None


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
                "upload_date": previous_check.strftime("%Y%m%d"),
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
    assert "precise publish time" in checked["last_error"]


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
        {**channel, "interval": "minutely"},
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
            "interval": "minutely",
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
    assert provider.published_after_calls == [previous_check]
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

        listed = await list_watched_channels(request)
        assert listed[0]["id"] == channel["id"]
        assert await get_channel_watcher_status(request) == {"youtube_cooldown_until": None}

        payload["video_resolution"] = 1080
        updated = await update_watched_channel(
            channel["id"], WatchedChannelRequest.model_validate(payload), request
        )
        assert updated["video_resolution"] == 1080

        removed = await delete_watched_channel(channel["id"], request)
        assert removed["message"] == "Watched channel removed"
        assert await list_watched_channels(request) == []

    asyncio.run(exercise_routes())


def test_channel_watcher_api_accepts_minutely_interval(tmp_path):
    output_dir = tmp_path / "videos"
    output_dir.mkdir()
    payload = {
        "url": "https://www.youtube.com/@test",
        "output_dir": str(output_dir),
        "interval": "minutely",
        "check_hour": 9,
        "check_minute": 30,
    }

    settings = WatchedChannelRequest.model_validate(payload)

    assert settings.interval == "minutely"
