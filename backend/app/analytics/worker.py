"""Analytics worker — отдельный процесс (architecture.md §5.6).

    python -m app.analytics.worker            # планировщик, навсегда
    python -m app.analytics.worker --once detect_windows   # один джоб и выход

| Джоб             | Период  | Что делает                                  |
|------------------|---------|---------------------------------------------|
| detect_windows   | 5 мин   | инкрементальные окна → rth_windows           |
| estimate_ambient | 1 ч     | idle-эпизоды → ambient_estimates (idempotent)|
| update_trends    | 6 ч     | Theil–Sen, CUSUM, диагноз → device_health    |
| reprocess        | 30 мин  | опоздавшие данные из reprocess_queue         |
| cleanup          | 24 ч    | DELETE старых ingest_batches/reprocess_queue |

Джобы идемпотентны: пропуск тика или повторный запуск безопасны, поэтому
Celery/брокер не нужны (решение §8).
"""
import asyncio

import asyncpg
import structlog
import typer
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.analytics import jobs
from app.analytics.params import AnalysisParams
from app.logging import configure_logging
from app.settings import Settings

log = structlog.get_logger(__name__)

JOBS = {
    "detect_windows": (jobs.detect_windows_job, 5 * 60),
    "estimate_ambient": (jobs.estimate_ambient_job, 60 * 60),
    "update_trends": (jobs.update_trends_job, 6 * 60 * 60),
    "reprocess": (jobs.reprocess_job, 30 * 60),
    "cleanup": (jobs.cleanup_job, 24 * 60 * 60),
}

cli = typer.Typer()


async def _run_forever(settings: Settings) -> None:
    pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=settings.db_pool_max)
    params = AnalysisParams()
    scheduler = AsyncIOScheduler(timezone="UTC")
    for name, (fn, interval_s) in JOBS.items():
        scheduler.add_job(
            fn, "interval", seconds=interval_s, args=(pool, params), id=name,
            coalesce=True, max_instances=1, misfire_grace_time=interval_s,
        )
    scheduler.start()
    log.info("worker.started", jobs=list(JOBS))
    try:
        await asyncio.Event().wait()  # навсегда (Ctrl+C / SIGTERM завершают процесс)
    finally:
        scheduler.shutdown(wait=False)
        await pool.close()


async def _run_once(settings: Settings, job_name: str) -> None:
    fn, _ = JOBS[job_name]
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        await fn(pool, AnalysisParams())
    finally:
        await pool.close()


@cli.command()
def main(
    once: str = typer.Option(None, help=f"выполнить один джоб и выйти: {', '.join(JOBS)}"),
) -> None:
    settings = Settings()
    configure_logging(settings.log_json)
    if once is not None:
        if once not in JOBS:
            typer.echo(f"нет такого джоба: {once}; есть {', '.join(JOBS)}", err=True)
            raise typer.Exit(2)
        asyncio.run(_run_once(settings, once))
        return
    asyncio.run(_run_forever(settings))


if __name__ == "__main__":
    cli()
