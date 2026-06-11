# ThermalOracle backend: ОДИН образ — три режима запуска (команда задаётся в compose):
#   API:      uvicorn app.main:app --host 0.0.0.0 --port 8000   (CMD по умолчанию)
#   Worker:   python -m app.analytics.worker
#   Миграции: alembic upgrade head
#
# Multi-stage: builder собирает wheel'ы (свой + зависимости), рантайм ставит их
# офлайн — итоговый слой без pip-кэша и build-окружения. Все зависимости
# (asyncpg, bcrypt, numpy, psycopg[binary]) имеют manylinux-колёса — компиляторы
# в образе не нужны.
FROM python:3.13-slim AS builder
WORKDIR /src
COPY . .
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels thermal-oracle-backend \
    && rm -rf /wheels
# wheel содержит только пакет app* — миграции кладём рядом с alembic.ini
COPY alembic.ini ./
COPY alembic ./alembic
RUN useradd --create-home --uid 1000 appuser
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
