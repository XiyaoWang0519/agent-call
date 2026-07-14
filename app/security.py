from __future__ import annotations

import secrets

from fastapi import HTTPException, Request
from starlette.datastructures import FormData
from starlette.types import ASGIApp, Receive, Scope, Send
from twilio.request_validator import RequestValidator

from app.settings import Settings


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
        poke_user_id = headers.get(b"x-poke-user-id", b"").decode("latin-1")
        expected_auth = f"Bearer {Settings.reveal(self.settings.mcp_bearer_token)}"
        auth_matches = constant_time_equal(authorization, expected_auth)
        user_matches = not poke_user_id or constant_time_equal(
            poke_user_id, self.settings.allowed_poke_user_id or ""
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
