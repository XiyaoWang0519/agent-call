from __future__ import annotations

import secrets
from collections.abc import Mapping

from fastapi import HTTPException, Request
from starlette.datastructures import FormData
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from twilio.request_validator import RequestValidator

from app.settings import Settings

OPENAI_WEBHOOK_BODY_MAX_BYTES = 256 * 1024
TWILIO_WEBHOOK_BODY_MAX_BYTES = 64 * 1024


class _RequestBodyTooLarge(Exception):
    pass


class WebhookBodyLimitMiddleware:
    """Reject oversized provider callbacks before Starlette buffers or parses them."""

    def __init__(self, app: ASGIApp):
        self.app = app

    @staticmethod
    def _limit_for_path(path: str) -> int | None:
        if path == "/webhooks/openai":
            return OPENAI_WEBHOOK_BODY_MAX_BYTES
        if path.startswith("/webhooks/twilio/"):
            return TWILIO_WEBHOOK_BODY_MAX_BYTES
        return None

    @staticmethod
    async def _send_error(send: Send, status: int, detail: str) -> None:
        body = f'{{"detail":"{detail}"}}'.encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for_path(str(scope.get("path", "")))
        if limit is None:
            await self.app(scope, receive, send)
            return

        headers: Mapping[bytes, bytes] = {
            key.lower(): value for key, value in scope.get("headers", [])
        }
        raw_content_length = headers.get(b"content-length")
        if raw_content_length is not None:
            try:
                content_length = int(raw_content_length)
            except ValueError:
                await self._send_error(send, 400, "invalid content-length")
                return
            if content_length < 0:
                await self._send_error(send, 400, "invalid content-length")
                return
            if content_length > limit:
                await self._send_error(send, 413, "request body too large")
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if isinstance(body, bytes):
                    received += len(body)
                if received > limit:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await self._send_error(send, 413, "request body too large")


def constant_time_equal(actual: str | None, expected: str) -> bool:
    return actual is not None and secrets.compare_digest(actual.encode(), expected.encode())


class MCPAuthMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        agent_user_id = headers.get(b"x-agent-user-id", b"").decode("latin-1")
        expected_auth = f"Bearer {Settings.reveal(self.settings.mcp_bearer_token)}"
        expected_user_id = (self.settings.allowed_agent_user_id or "").strip()
        auth_matches = constant_time_equal(authorization, expected_auth)
        user_matches = bool(expected_user_id) and constant_time_equal(
            agent_user_id, expected_user_id
        )
        authorized = auth_matches and user_matches
        if authorized:
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"unauthorized"}',
            }
        )


async def require_debug_token(request: Request) -> None:
    settings: Settings = request.app.state.settings
    token = request.headers.get("authorization")
    expected = f"Bearer {Settings.reveal(settings.debug_api_token)}"
    if not constant_time_equal(token, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


async def require_deploy_guard_token(request: Request) -> None:
    settings: Settings = request.app.state.settings
    token = request.headers.get("authorization")
    expected = f"Bearer {Settings.reveal(settings.deploy_guard_token)}"
    if not constant_time_equal(token, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def exact_public_url(request: Request, settings: Settings) -> str:
    base = (settings.public_base_url or "").rstrip("/")
    url = f"{base}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


async def verify_twilio_request(request: Request) -> FormData:
    settings: Settings = request.app.state.settings
    signature = request.headers.get("X-Twilio-Signature")
    if signature is None:
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    form = await request.form()
    params = {key: str(value) for key, value in form.multi_items()}
    validator = RequestValidator(Settings.reveal(settings.twilio_auth_token))
    if not validator.validate(exact_public_url(request, settings), params, signature):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    return form
