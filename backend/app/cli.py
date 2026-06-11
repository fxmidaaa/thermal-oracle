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


@cli.command("diagnose-ambient")
def diagnose_ambient(
    device: str = typer.Option(None, help="UUID/префикс id; по умолчанию — последнее активное"),
    hours: float = typer.Option(24.0, help="окно анализа назад от текущего момента"),
) -> None:
    """Почему (не) считается T_ambient: эффективные пороги, профиль мощности,
    раны ниже порога, найденные эпизоды и скан порогов с рекомендацией."""
    import numpy as np

    from app.analytics.ambient import estimate_day_ambient, find_idle_episodes
    from app.analytics.params import AnalysisParams
    from app.analytics.series import bridge_short_gaps, rolling_mean, runs, span_s, split_on_gaps

    async def _do():
        conn = await _connect()
        try:
            row = await conn.fetchrow(
                """SELECT id, name, analysis_overrides FROM devices
                   WHERE $1::text IS NULL OR id::text LIKE $1 || '%'
                   ORDER BY last_seen_at DESC NULLS LAST LIMIT 1""",
                device,
            )
            if row is None:
                typer.echo("устройство не найдено", err=True)
                raise typer.Exit(1)
            params = AnalysisParams().with_overrides(row["analysis_overrides"])
            typer.echo(f"устройство: {row['name']} ({str(row['id'])[:8]}…)")
            typer.echo(
                "эффективные пороги: "
                f"idle_power_w={params.idle_power_w}  idle_power_max_w={params.idle_power_max_w}  "
                f"grace={params.idle_grace_s:.0f}с  min_dur={params.idle_min_duration_s:.0f}с  "
                f"discard_head={params.idle_discard_head_s:.0f}с  "
                f"min_tail={params.idle_min_tail_s:.0f}с  "
                # только cp1251-безопасные символы: консоль Windows
                f"p{params.ambient_percentile:.0f}  clamp<={params.ambient_clamp_high}°C"
            )

            rows = await conn.fetch(
                """SELECT extract(epoch FROM ts)::float8 AS ts, cpu_power, cpu_temp
                   FROM telemetry_raw
                   WHERE device_id = $1 AND ts > now() - make_interval(secs => $2)
                   ORDER BY ts""",
                row["id"], hours * 3600.0,
            )
            if not rows:
                typer.echo("данных нет — агент шлёт телеметрию?", err=True)
                raise typer.Exit(1)
            ts = np.array([r["ts"] for r in rows])
            power = np.array(
                [r["cpu_power"] if r["cpu_power"] is not None else np.nan for r in rows])
            temp = np.array(
                [r["cpu_temp"] if r["cpu_temp"] is not None else np.nan for r in rows])

            coverage = 100.0 * len(ts) / max(1.0, ts[-1] - ts[0] + 1.0)
            pcts = np.nanpercentile(power, [5, 10, 25, 50, 75, 90, 95])
            typer.echo(
                f"\nданные: {len(ts)} строк за {(ts[-1] - ts[0]) / 3600:.1f} ч "
                f"(покрытие {coverage:.0f}%)"
            )
            typer.echo(
                "мощность, Вт: "
                + "  ".join(f"p{p}={v:.1f}" for p, v in zip((5, 10, 25, 50, 75, 90, 95), pcts,
                                                            strict=True))
            )
            typer.echo(f"температура, °C: min={np.nanmin(temp):.1f} p10="
                       f"{np.nanpercentile(temp, 10):.1f} p50={np.nanpercentile(temp, 50):.1f}")

            def longest_runs(threshold: float) -> tuple[float, int]:
                """→ (самый длинный ран ниже порога, сколько ранов ≥ min_duration)."""
                best, qualifying = 0.0, 0
                for s0, s1 in split_on_gaps(ts, params.idle_gap_split_s):
                    smooth = rolling_mean(power[s0:s1], params.idle_rolling_s)
                    mask = np.where(np.isnan(smooth), False, smooth < threshold)
                    mask = bridge_short_gaps(mask, ts[s0:s1], params.idle_grace_s)
                    for r0, r1 in runs(mask):
                        duration = span_s(ts[s0:s1], r0, r1)
                        best = max(best, duration)
                        if duration >= params.idle_min_duration_s:
                            qualifying += 1
                return best, qualifying

            typer.echo("\nскан порогов (ран = непрерывно ниже порога, с грейсом):")
            scan = sorted({round(float(v), 1) for v in (*pcts[2:6], params.idle_power_w)})
            for threshold in scan:
                best, qualifying = longest_runs(threshold)
                marker = "  <- текущий" if threshold == round(params.idle_power_w, 1) else ""
                typer.echo(f"  <{threshold:>5.1f} Вт: макс. ран {best / 60:>5.1f} мин, "
                           f"эпизодов >={params.idle_min_duration_s / 60:.0f} мин: "
                           f"{qualifying}{marker}")

            episodes = find_idle_episodes(ts, power, temp, params)
            typer.echo(f"\nэпизоды при текущих порогах: {len(episodes)}")
            for e in episodes:
                est = f"{e.estimate:.1f}°C" if e.estimate is not None else "хвост короче min_tail"
                typer.echo(f"  {e.duration_s / 60:.1f} мин -> оценка: {est}")
            day = estimate_day_ambient(episodes, params)
            if day is not None:
                typer.echo(f"\nИТОГ: t_ambient={day.t_ambient:.1f}°C "
                           f"confidence={day.confidence:.2f} idle={day.idle_minutes} мин")
                if day.t_ambient >= params.ambient_clamp_high:
                    typer.echo("  ВНИМАНИЕ: оценка упёрлась в ambient_clamp_high — "
                               "поднимите его в analysis_overrides для горячего idle-профиля")
            else:
                typer.echo("\nИТОГ: оценки нет. Смотрите скан порогов выше: выбирайте "
                           "порог, дающий эпизоды, и/или уменьшайте idle_min_duration_s; "
                           "ключи overrides — как в строке «эффективные пороги».")
        finally:
            await conn.close()

    _run(_do())


if __name__ == "__main__":
    cli()
