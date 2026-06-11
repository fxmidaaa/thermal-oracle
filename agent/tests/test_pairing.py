"""Паринг агента против httpx.MockTransport."""
import httpx
import pytest

from thermal_agent.pairing import PairingError, build_pair_payload, pair

OK = {"device_id": "8c5e9d6a-0000-0000-0000-000000000001", "device_token": "to_secret"}


def make_transport(status: int, body: dict, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def test_pair_success_returns_token():
    seen = []
    result = pair(
        "http://backend.test/", "AB23-CD45", name="My Legion",
        transport=make_transport(201, OK, seen),
    )
    assert result["device_token"] == "to_secret"
    request = seen[0]
    assert request.url.path == "/v1/telemetry/pair"   # хвостовой слэш api_url не ломает путь
    assert b'"code": "AB23-CD45"' in request.content or b'"code":"AB23-CD45"' in request.content


def test_pair_invalid_code_raises_with_detail():
    with pytest.raises(PairingError, match="invalid or expired"):
        pair(
            "http://backend.test", "XXXX-XXXX", name="x",
            transport=make_transport(400, {"detail": "invalid or expired pairing code"}, []),
        )


def test_payload_shape():
    payload = build_pair_payload("AB23-CD45", "Test PC", "windows")
    assert set(payload) == {"code", "name", "platform", "device_class", "agent_version"}
    assert payload["device_class"] == "laptop"
    assert payload["agent_version"]


def test_default_name_is_hostname():
    seen = []
    pair("http://b.test", "AB23-CD45", transport=make_transport(201, OK, seen))
    import json

    body = json.loads(seen[0].content)
    assert body["name"]                                # hostname подставился сам
    assert body["platform"] in ("windows", "macos")
