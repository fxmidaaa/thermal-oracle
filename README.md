# ThermalOracle

Micro-SaaS предиктивного мониторинга деградации термоинтерфейсов (термопаста,
жидкий металл, PTM7950) для высокопроизводительных ноутбуков и ПК. Агент
собирает телеметрию (температуры, мощности, RPM) с частотой 1 Гц, бэкенд
считает тепловое сопротивление Rth = (T_die − T_ambient) / P на стабильных
окнах нагрузки и строит тренд деградации.

**Архитектура:** [docs/architecture.md](docs/architecture.md) ·
решения: [docs/adr/](docs/adr/)

## Состав репозитория

| Каталог | Что это |
|---|---|
| `backend/` | FastAPI ingest API + схема БД (alembic) + dev-CLI; позже — analytics worker |
| `agent/` | Windows CLI-агент: LHM-коллектор, SQLite-спул, шиппер — [agent/README.md](agent/README.md) |
| `infra/` | docker-compose: TimescaleDB |
| `docs/` | архитектура и ADR |

## Быстрый старт: весь стек в Docker

```powershell
docker compose -f infra/docker-compose.yml up -d --build
```

Поднимет TimescaleDB → миграции (one-shot) → API (`:8000`) → analytics worker.
Swagger: http://127.0.0.1:8000/docs · здоровье: `/healthz`. Дальше: аккаунт и
pairing-код в Swagger, затем агент (раздел ниже). Полный чек-лист первого
запуска на своём железе — [docs/smoke-test.md](docs/smoke-test.md).

## Dev-режим (бэкенд локально, БД в Docker)

Требуется: Python ≥ 3.11, Docker Desktop.

```powershell
# 1. База
docker compose -f infra/docker-compose.yml up -d timescaledb

# 2. Бэкенд
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
alembic upgrade head            # вся схема: гипертаблицы, CAggs, политики

# 3. Аккаунт и pairing-код — через Swagger UI на http://127.0.0.1:8000/docs
#    (после запуска API из шага 4):
#    POST /api/v1/auth/register → токен → Authorize → POST /api/v1/devices/pairing-code
#    Затем на машине с агентом: thermal-agent pair --code XXXX-XXXX
#    (низкоуровневый обход без UI: python -m app.cli create-device …)

# 4. API (отдельный терминал)
uvicorn app.main:app --reload

# 4b. Analytics worker (ещё один терминал) — окна, T_ambient, тренды
python -m app.analytics.worker
#     для отладки можно гнать джобы по одному:
python -m app.analytics.worker --once detect_windows
python -m app.analytics.worker --once estimate_ambient
python -m app.analytics.worker --once update_trends

# 5. Синтетический батч телеметрии (отдельный терминал)
python -m app.cli send-test-batch --token <токен из шага 3>

# Тесты (интеграционные сами скипаются, если БД не поднята)
pytest
```

## Агент на своей машине

Установка, настройка LibreHardwareMonitor, `detect-sensors`, `register`, `run`
и автозапуск — в [agent/README.md](agent/README.md). Краткая версия:

```powershell
cd agent
pip install -e .
thermal-agent detect-sensors      # LHM должен быть запущен (admin + web server)
thermal-agent pair --code XXXX-XXXX --api-url http://127.0.0.1:8000
thermal-agent run                 # или run --no-ship: сбор без бэкенда
```

База слушает на `localhost:5433` (не 5432 — чтобы не конфликтовать с локальным
PostgreSQL). Строка подключения переопределяется переменной
`THERMAL_DATABASE_URL` или файлом `backend/.env`.
