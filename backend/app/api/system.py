from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["system"])


@router.get("/healthz")
async def healthz(request: Request, response: Response) -> dict:
    pool = getattr(request.app.state, "pool", None)
    db_ok = False
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                db_ok = await conn.fetchval("SELECT 1") == 1
        except Exception:  # noqa: BLE001 — healthz не должен падать 500
            db_ok = False
    if not db_ok:
        response.status_code = 503
    return {"status": "ok" if db_ok else "degraded", "db": db_ok}
