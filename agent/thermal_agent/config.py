"""Конфиг агента: ~/.thermal_oracle/config.toml (чтение — stdlib tomllib,
запись — свой минимальный сериализатор, чтобы не тянуть зависимость)."""
import json
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

CONFIG_DIR = Path.home() / ".thermal_oracle"
CONFIG_PATH = CONFIG_DIR / "config.toml"


@dataclass
class AgentConfig:
    api_url: str = ""
    device_token: str = ""
    lhm_url: str = "http://localhost:8085/data.json"
    sample_interval_s: float = 1.0
    ship_interval_s: float = 30.0
    spool_max_hours: float = 24.0
    spool_path: str = str(CONFIG_DIR / "spool.db")


def load_config(path: Path = CONFIG_PATH) -> AgentConfig:
    if not path.exists():
        return AgentConfig()
    with path.open("rb") as fh:
        data = tomllib.load(fh).get("agent", {})
    known = {f.name for f in fields(AgentConfig)}
    return AgentConfig(**{k: v for k, v in data.items() if k in known})


def save_config(cfg: AgentConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[agent]"]
    for key, value in asdict(cfg).items():
        if isinstance(value, str):
            # json.dumps даёт валидную TOML basic string (экранирует \ и ")
            lines.append(f"{key} = {json.dumps(value)}")
        else:
            lines.append(f"{key} = {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
