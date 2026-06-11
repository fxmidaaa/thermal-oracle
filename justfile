set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

# Поднять/остановить локальную TimescaleDB
up:
    docker compose -f infra/docker-compose.yml up -d

down:
    docker compose -f infra/docker-compose.yml down

# Применить миграции
migrate:
    cd backend; alembic upgrade head

# Запустить API в dev-режиме
api:
    cd backend; uvicorn app.main:app --reload

# Тесты и линт
test:
    cd backend; pytest

lint:
    cd backend; ruff check .
