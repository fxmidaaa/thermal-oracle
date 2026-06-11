from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация бэкенда. Переопределяется THERMAL_* или backend/.env."""

    model_config = SettingsConfigDict(env_prefix="THERMAL_", env_file=".env", extra="ignore")

    # 5433 — порт TimescaleDB из infra/docker-compose.yml
    database_url: str = "postgresql://postgres:postgres@localhost:5433/thermal"
    db_pool_min: int = 1
    db_pool_max: int = 10
    log_json: bool = False

    # user-auth; в проде ОБЯЗАТЕЛЬНО задать THERMAL_JWT_SECRET (≥ 32 байт)
    jwt_secret: str = "dev-secret-change-me-in-production-0000"
    jwt_ttl_hours: int = 72
    pairing_ttl_minutes: int = 10
