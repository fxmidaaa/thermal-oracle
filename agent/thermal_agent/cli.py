"""CLI агента: pair | detect-sensors | run | status (+ низкоуровневый register)."""
import datetime as dt
import logging
import threading
import time

import httpx
import typer

from thermal_agent import __version__
from thermal_agent.config import CONFIG_PATH, load_config, save_config
from thermal_agent.spool import Spool

cli = typer.Typer(no_args_is_help=True, help=f"ThermalOracle agent v{__version__}")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@cli.command()
def pair(
    code: str = typer.Option(..., prompt="Pairing-код из дашборда"),
    api_url: str = typer.Option("http://127.0.0.1:8000"),
    name: str = typer.Option(None, help="имя устройства (по умолчанию — hostname)"),
    device_class: str = typer.Option("laptop", help="laptop | desktop"),
) -> None:
    """Сопрячь агента с аккаунтом по короткому коду — без ручных токенов."""
    from thermal_agent.pairing import PairingError
    from thermal_agent.pairing import pair as do_pair

    try:
        result = do_pair(api_url, code, name=name, device_class=device_class)
    except PairingError as exc:
        typer.echo(f"Сопряжение не удалось: {exc}", err=True)
        typer.echo("Код живёт 10 минут и одноразовый — запросите новый в дашборде.", err=True)
        raise typer.Exit(1) from exc
    except httpx.HTTPError as exc:
        typer.echo(f"Бэкенд недоступен ({api_url}): {exc}", err=True)
        raise typer.Exit(1) from exc

    cfg = load_config()
    cfg.api_url = api_url
    cfg.device_token = result["device_token"]
    save_config(cfg)
    typer.echo(f"Устройство сопряжено: {result['device_id']}")
    typer.echo(f"Токен сохранён в {CONFIG_PATH}")
    typer.echo("Дальше: thermal-agent detect-sensors, затем thermal-agent run")


@cli.command()
def register(
    token: str = typer.Option(..., help="device token (выдаёт backend: app.cli create-device)"),
    api_url: str = typer.Option("http://127.0.0.1:8000"),
    lhm_url: str = typer.Option(None, help="переопределить URL LibreHardwareMonitor"),
) -> None:
    """[низкоуровневый] Вписать готовый токен в конфиг; обычно нужен `pair`."""
    cfg = load_config()
    cfg.device_token = token
    cfg.api_url = api_url
    if lhm_url:
        cfg.lhm_url = lhm_url
    save_config(cfg)
    typer.echo(f"Сохранено в {CONFIG_PATH}")
    typer.echo("Дальше: thermal-agent detect-sensors, затем thermal-agent run")


@cli.command("detect-sensors")
def detect_sensors(lhm_url: str = typer.Option(None)) -> None:
    """Показать, какие сенсоры находят правила выбора (dry-run сбора)."""
    from thermal_agent.collectors.windows_lhm import LhmCollector

    cfg = load_config()
    url = lhm_url or cfg.lhm_url
    collector = LhmCollector(url, timeout=3.0)
    try:
        report = collector.detect()
    except httpx.HTTPError as exc:
        typer.echo(f"Не удалось прочитать {url}: {exc}", err=True)
        typer.echo(
            "Проверьте: LibreHardwareMonitor запущен (от администратора), "
            "Options → Remote Web Server → Run, порт 8085.",
            err=True,
        )
        raise typer.Exit(1) from exc
    finally:
        collector.close()

    reading = report["reading"]
    typer.echo(f"LHM: {url}\n")
    rows = [
        ("cpu_temp", reading.cpu_temp, "°C"),
        ("cpu_power", reading.cpu_power, "W"),
        ("gpu_temp", reading.gpu_temp, "°C"),
        ("gpu_power", reading.gpu_power, "W"),
        ("fan_rpm", reading.fan_rpm, "RPM"),
    ]
    for field, value, unit in rows:
        source = reading.details.get(field, "— не найден —")
        shown = f"{value:.1f} {unit}" if isinstance(value, float) else (
            f"{value} {unit}" if value is not None else "n/a")
        typer.echo(f"  {field:<10} {shown:>12}   {source}")
    fans = reading.details.get("fans_found")
    if fans:
        typer.echo(f"\n  все вентиляторы: {', '.join(fans)} (берётся max)")
    typer.echo(f"\ncapabilities: {report['capabilities']}")


@cli.command()
def run(
    no_ship: bool = typer.Option(False, help="только сбор в спул, без отправки"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Запустить агента: сэмплер 1 Гц (+ шиппер раз в 30 с). Ctrl+C — стоп."""
    from thermal_agent.collectors.windows_lhm import LhmCollector
    from thermal_agent.sampler import Sampler
    from thermal_agent.shipper import Shipper

    _setup_logging(verbose)
    log = logging.getLogger("agent")
    cfg = load_config()

    shipper = None
    stop = threading.Event()
    if not no_ship:
        if not cfg.device_token or not cfg.api_url:
            typer.echo(
                "Не настроен токен/адрес бэкенда: thermal-agent pair --code XXXX-XXXX "
                "(или запустите с --no-ship — только локальный сбор)", err=True)
            raise typer.Exit(2)
        shipper = Shipper(cfg, stop)
        shipper.start()

    collector = LhmCollector(cfg.lhm_url)
    log.info("агент v%s: сбор %ss → %s%s", __version__, cfg.sample_interval_s,
             cfg.spool_path, " (без отправки)" if no_ship else f", шиппинг → {cfg.api_url}")
    try:
        Sampler(cfg, collector, stop).loop()
    except KeyboardInterrupt:
        typer.echo("\nостанавливаюсь…")
    finally:
        stop.set()
        collector.close()
        if shipper is not None:
            shipper.join(timeout=10)


@cli.command()
def status() -> None:
    """Конфиг, доступность LHM, глубина спула."""
    cfg = load_config()
    token = f"{cfg.device_token[:12]}…" if cfg.device_token else "— не задан —"
    typer.echo(f"конфиг:     {CONFIG_PATH} ({'есть' if CONFIG_PATH.exists() else 'нет'})")
    typer.echo(f"api_url:    {cfg.api_url or '— не задан —'}")
    typer.echo(f"token:      {token}")
    typer.echo(f"lhm_url:    {cfg.lhm_url}")

    try:
        httpx.get(cfg.lhm_url, timeout=1.0).raise_for_status()
        typer.echo("LHM:        доступен")
    except httpx.HTTPError as exc:
        typer.echo(f"LHM:        НЕдоступен ({exc.__class__.__name__})")

    spool = Spool(cfg.spool_path, cfg.spool_max_hours)
    try:
        total, claimed = spool.counts()
        oldest = spool.oldest_ts_ms()
        age = ""
        if oldest:
            minutes = (time.time() - oldest / 1000) / 60
            age = f", старейшая запись {minutes:.0f} мин назад"
        typer.echo(f"спул:       {total} строк (в полёте {claimed}{age})")
        if total:
            typer.echo(f"            ≈{dt.timedelta(seconds=total)} телеметрии")
    finally:
        spool.close()


if __name__ == "__main__":
    cli()
