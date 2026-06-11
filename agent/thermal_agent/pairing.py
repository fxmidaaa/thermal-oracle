"""Сопряжение агента с аккаунтом по pairing-коду из дашборда.

Пользователь не трогает токены руками: код с экрана → POST
/v1/telemetry/pair → постоянный device token → конфиг агента.
Зеркало backend-схемы PairRequest (contract-тест держит синхрон).
"""
import platform as platform_mod
import sys

import httpx

from thermal_agent import __version__ as AGENT_VERSION


class PairingError(RuntimeError):
    pass


def default_device_name() -> str:
    return platform_mod.node() or "my-pc"


def default_platform() -> str:
    return "macos" if sys.platform == "darwin" else "windows"


def build_pair_payload(
    code: str,
    name: str,
    platform: str,
    device_class: str = "laptop",
    agent_version: str = AGENT_VERSION,
) -> dict:
    return {
        "code": code.strip(),
        "name": name,
        "platform": platform,
        "device_class": device_class,
        "agent_version": agent_version,
    }


def pair(
    api_url: str,
    code: str,
    name: str | None = None,
    device_class: str = "laptop",
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """→ {"device_id": ..., "device_token": ...}; PairingError при отказе,
    httpx.HTTPError при сетевых проблемах."""
    payload = build_pair_payload(
        code, name or default_device_name(), default_platform(), device_class
    )
    with httpx.Client(timeout=10.0, transport=transport) as client:
        response = client.post(api_url.rstrip("/") + "/v1/telemetry/pair", json=payload)
    if response.status_code == 201:
        return response.json()
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    raise PairingError(f"сервер отказал ({response.status_code}): {detail}")
