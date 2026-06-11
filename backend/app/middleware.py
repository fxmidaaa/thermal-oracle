"""ASGI-middleware распаковки gzip-тел запросов.

Агент шлёт батчи с Content-Encoding: gzip (architecture.md §3). Starlette из
коробки сжимает только ответы, поэтому распаковка запросов — своя. Лимиты
защищают от zip-бомб: вход ≤ 1 МиБ, распакованное ≤ 5 МиБ (штатный батч из
120 сэмплов — единицы КиБ).
"""
import gzip
import json

MAX_COMPRESSED = 1 * 2**20
MAX_INFLATED = 5 * 2**20


class GzipRequestMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope["headers"]}
        if headers.get(b"content-encoding", b"").strip().lower() != b"gzip":
            return await self.app(scope, receive, send)

        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get("body", b""))
            if len(body) > MAX_COMPRESSED:
                return await self._reject(send, 413, "compressed body too large")
            if not message.get("more_body"):
                break
        try:
            inflated = gzip.decompress(bytes(body))
        except (OSError, EOFError):
            return await self._reject(send, 400, "invalid gzip body")
        if len(inflated) > MAX_INFLATED:
            return await self._reject(send, 413, "inflated body too large")

        scope = dict(scope)
        scope["headers"] = [
            (k, v)
            for k, v in scope["headers"]
            if k.lower() not in (b"content-encoding", b"content-length")
        ] + [(b"content-length", str(len(inflated)).encode())]

        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": inflated, "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send, status: int, detail: str) -> None:
        payload = json.dumps({"detail": detail}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": payload})
