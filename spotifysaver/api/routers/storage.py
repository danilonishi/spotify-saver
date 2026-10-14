"""Storage status and minimum-free-space reserve endpoints."""

from fastapi import APIRouter, HTTPException, Request

from ..schemas import StorageReserveUpdate
from ..services.storage_protection import GB, StorageProtectionService

router = APIRouter()


def _service(request: Request) -> StorageProtectionService:
    service = getattr(request.app.state, "storage_protection", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Storage protection is unavailable")
    return service


@router.get("/storage")
async def get_storage_status(request: Request):
    """Report free space for each filesystem used by configured destinations."""
    return _service(request).get_report()


@router.put("/storage/reserve")
async def set_storage_reserve(settings: StorageReserveUpdate, request: Request):
    """Set the minimum free-space reserve; zero disables download blocking."""
    service = _service(request)
    minimum_free_bytes = round(settings.minimum_free_space_gb * GB)
    service.store.set_minimum_free_bytes(minimum_free_bytes)
    await service.refresh()
    await service.queue_kick()
    return service.get_report()
