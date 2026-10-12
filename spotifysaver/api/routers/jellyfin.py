"""Settings endpoints for Jellyfin library scans."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..schemas import JellyfinLibraryScanSettingsUpdate
from ..services.jellyfin_library_scan import JellyfinLibraryScanService

router = APIRouter()


def _service(request: Request) -> JellyfinLibraryScanService:
    service: Optional[JellyfinLibraryScanService] = getattr(
        request.app.state, "jellyfin_library_scan", None
    )
    if service is None:
        raise HTTPException(
            status_code=503, detail="Jellyfin library scan settings are unavailable"
        )
    return service


@router.get("/preferences/jellyfin-library-scan")
async def get_jellyfin_library_scan_settings(request: Request):
    """Return Jellyfin settings without exposing the stored API key."""
    return _service(request).get_status()


@router.put("/preferences/jellyfin-library-scan")
async def set_jellyfin_library_scan_settings(
    settings: JellyfinLibraryScanSettingsUpdate,
    request: Request,
):
    """Persist Jellyfin connection settings and the scan cooldown."""
    try:
        return _service(request).save_settings(
            server_url=settings.server_url,
            api_key=settings.api_key,
            clear_api_key=settings.clear_api_key,
            scan_interval_minutes=settings.scan_interval_minutes,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/preferences/jellyfin-library-scan/api-key")
async def remove_jellyfin_api_key(request: Request):
    """Remove the saved Jellyfin API key without changing other settings."""
    return _service(request).remove_api_key()


@router.post("/jellyfin-library-scan")
async def request_jellyfin_library_scan(request: Request):
    """Queue a manual Jellyfin library refresh, bypassing the cooldown."""
    try:
        return _service(request).request_manual_scan()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
