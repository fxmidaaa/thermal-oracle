import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.schemas.telemetry import IngestResponse, TelemetryBatch
from app.services.ingest_service import ingest_batch
from app.services.token_service import authenticate_device

router = APIRouter(tags=["ingest"])
_bearer = HTTPBearer(auto_error=False)


async def get_device_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> uuid.UUID:
    if credentials is None:
        raise HTTPException(status_code=401, detail="device token required")
    async with request.app.state.pool.acquire() as conn:
        device_id = await authenticate_device(conn, credentials.credentials)
    if device_id is None:
        raise HTTPException(status_code=401, detail="invalid device token")
    return device_id


@router.post("/v1/telemetry", response_model=IngestResponse)
async def post_telemetry(
    batch: TelemetryBatch,
    request: Request,
    device_id: uuid.UUID = Depends(get_device_id),
) -> IngestResponse:
    async with request.app.state.pool.acquire() as conn:
        result = await ingest_batch(conn, device_id, batch)
    if result.duplicate_batch:
        # Повтор уже обработанного батча — успех для агента: спул можно чистить.
        return IngestResponse(accepted=0, duplicates=0, rejected=0, status="duplicate")
    return IngestResponse(
        accepted=result.accepted,
        duplicates=result.duplicates,
        rejected=result.rejected,
    )
