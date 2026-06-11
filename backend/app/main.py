from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI

from app.api import ingest, pairing, system
from app.api import v1 as api_v1
from app.logging import configure_logging
from app.middleware import GzipRequestMiddleware
from app.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
        )
        yield
        await app.state.pool.close()

    app = FastAPI(title="ThermalOracle API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(GzipRequestMiddleware)
    app.include_router(system.router)
    app.include_router(ingest.router)      # агент: телеметрия (device token)
    app.include_router(pairing.router)     # агент: сопряжение (публичный)
    app.include_router(api_v1.router)      # пользователи/фронтенд (JWT)
    return app


app = create_app()
