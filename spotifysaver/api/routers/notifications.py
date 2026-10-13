"""Server-Sent Events endpoint for live notifications."""

from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..services.notification_hub import NotificationHub, encode_sse_event

router = APIRouter()


@router.get("/notifications/events")
async def notification_events(request: Request, after: str = ""):
    hub: NotificationHub | None = getattr(request.app.state, "notification_hub", None)
    if hub is None:
        raise HTTPException(status_code=503, detail="Notifications are unavailable")
    last_event_header = request.headers.get("last-event-id")
    last_event_id = last_event_header if last_event_header is not None else after

    async def stream() -> AsyncIterator[str]:
        async for event in hub.subscribe(last_event_id):
            if await request.is_disconnected():
                break
            yield encode_sse_event(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
