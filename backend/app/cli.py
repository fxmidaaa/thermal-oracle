"""Dev-CLI: сидирование пользователя/устройства и синтетическая телеметрия.

Пользовательский API (JWT, регистрация, pairing) — следующий шаг; до него
устройства заводятся отсюда напрямую в БД.

    python -m app.cli create-user --email dev@local
    python -m app.cli create-device --user-email dev@local --name "Test Legion"
    python -m app.cli send-test-batch --token to_...
"""
import asyncio
import datetime as dt
import gzip
import json
import math
import random
import uuid

import asyncpg
import typer

from app.settings import Settings

cli = typer.Typer(no_args_is_help=True)


def _run(coro):
    return asyncio.run(coro)


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(Settings().database_url)


@cli.command()
def create_user(email: str = typer.Option(...)) -> None:
    async def _do():
        conn = await _connect()
        try:
            user_id = await conn.fetchval(
                """INSERT INTO users (email) VALUES ($1)
                   ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
                   RETURNING id""",
                email,
            )
            typer.echo(f"user_id: {user_id}")
        finally:
            await conn.close()

    _run(_do())


@cli.command()
def create_device(
    user_email: str = typer.Option(...),
    name: str = typer.Option(...),
    platform: str = typer.Option("windows"),
    device_class: str = typer.Option("laptop"),
) -> None:
    from app.services.token_service import issue_device_token

    async def _do():
        conn = await _connect()
        try:
            user_id = await conn.fetchval("SELECT id FROM users WHERE email = $1", user_email)
            if user_id is None:
                typer.echo(f"Нет пользователя {user_email}; сначала create-user", err=True)
                raise typer.Exit(1)
            device_id = await conn.fetchval(
                """INSERT INTO devices (user_id, name, platform, device_class)
                   VALUES ($1, $2, $3, $4) RETURNING id""",
                user_id, name, platform, device_class,
            )
            token = await issue_device_token(conn, device_id)
            typer.echo(f"device_id: {device_id}")
            typer.echo(f"token:     {token}")
            typer.echo("Токен показан ОДИН раз: в БД хранится только хэш.")
        finally:
            await conn.close()

    _run(_do())


@cli.command()
def send_test_batch(
    token: str = typer.Option(...),
    api_url: str = typer.Option("http://127.0.0.1:8000"),
    samples: int = typer.Option(30, min=1, max=120),
    profile: str = typer.Option("load", help="load | idle"),
) -> None:
    """Сгенерировать и отправить синтетический батч (проверка полного пути)."""
    import httpx

    now = dt.datetime.now(dt.UTC)
    items = []
    for i in range(samples):
        ts = now - dt.timedelta(seconds=samples - 1 - i)
        wobble = math.sin(i / 5.0)
        if profile == "idle":
            item = dict(
                cpu_temp=46.0 + wobble * 0.6 + random.uniform(-0.2, 0.2),
                gpu_temp=42.0 + wobble * 0.4,
                cpu_power=3.2 + random.uniform(-0.5, 0.5),
                gpu_power=0.8,
                fan_rpm=0,
                process="explorer.exe",
            )
        else:
            item = dict(
                cpu_temp=87.5 + wobble * 0.5 + random.uniform(-0.3, 0.3),
                gpu_temp=68.0 + wobble * 0.4,
                cpu_power=62.0 + wobble * 2.0 + random.uniform(-1.0, 1.0),
                gpu_power=15.0,
                fan_rpm=4300 + int(wobble * 50),
                process="CinebenchR23.exe",
            )
        items.append({"ts": ts.isoformat(), **item})

    payload = {
        "schema_version": 1,
        "batch_id": str(uuid.uuid4()),
        "sent_at": now.isoformat(),
        "agent_version": "cli-dev",
        "samples": items,
    }
    body = gzip.compress(json.dumps(payload).encode())
    response = httpx.post(
        f"{api_url}/v1/telemetry",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
        timeout=10.0,
    )
    typer.echo(f"{response.status_code}: {response.text}")


if __name__ == "__main__":
    cli()
