import asyncio

import pytest

from spotifysaver.api.routers import download as download_router
from spotifysaver.api.schemas import DownloadStatus
from spotifysaver.api.services.notification_hub import (
    NotificationHub,
    encode_sse_event,
)


def test_notification_hub_replays_events_after_last_seen_id():
    async def run_test():
        hub = NotificationHub()
        await hub.publish("new_content", "One upload found", channel_id="channel-1")
        await hub.publish("check_failed", "Channel check failed", channel_id="channel-2")
        first_event_id = hub._history[0]["id"]
        stream = hub.subscribe(last_event_id=first_event_id)
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(run_test())

    assert event["category"] == "check_failed"
    assert event["category"] == "check_failed"
    assert event["channel_id"] == "channel-2"


def test_notification_hub_replays_after_server_restart():
    async def run_test():
        previous_server = NotificationHub()
        await previous_server.publish("new_content", "Previous server event")
        previous_event_id = previous_server._history[0]["id"]
        restarted_server = NotificationHub()
        await restarted_server.publish("check_failed", "Current server event")
        stream = restarted_server.subscribe(last_event_id=previous_event_id)
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(run_test())

    assert event["category"] == "check_failed"


def test_notification_hub_broadcasts_to_connected_subscribers():
    async def run_test():
        hub = NotificationHub()
        stream = hub.subscribe()
        pending_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await hub.publish("download_attention", "Download failed", task_id="task-1")
        event = await asyncio.wait_for(pending_event, timeout=1)
        await stream.aclose()
        return event

    event = asyncio.run(run_test())

    assert event["category"] == "download_attention"
    assert event["task_id"] == "task-1"


def test_notification_event_is_encoded_as_server_sent_event():
    encoded = encode_sse_event(
        {"id": "instance:3", "category": "new_content", "message": "Found uploads"}
    )

    assert encoded.startswith("id: instance:3\nevent: notification\n")
    assert (
        'data: {"id": "instance:3", "category": "new_content", "message": "Found uploads"}'
        in encoded
    )


@pytest.mark.parametrize(
    ("status", "warning", "expected_message"),
    [
        ("failed", None, "network unavailable"),
        ("completed", "YouTube is rate limited", "YouTube is rate limited"),
    ],
)
def test_download_attention_is_published_for_failures_and_warnings(
    monkeypatch, status, warning, expected_message
):
    async def run_test():
        hub = NotificationHub()
        monkeypatch.setattr(download_router, "notification_hub", hub)
        task = DownloadStatus(
            task_id="task-1",
            status=status,
            progress=0,
            title="Test download",
            error_message="network unavailable" if status == "failed" else None,
            warning_message=warning,
        )
        await download_router._publish_download_attention(task)
        stream = hub.subscribe()
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(run_test())

    assert event["category"] == "download_attention"
    assert event["task_id"] == "task-1"
    assert expected_message in event["message"]
