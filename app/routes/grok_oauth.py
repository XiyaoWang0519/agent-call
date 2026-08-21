from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from mcp.server.auth.provider import AuthorizeError

from app.grok_oauth.consent import (
    CONSENT_SECURITY_HEADERS,
    consent_error_page,
    consent_form,
    denied_redirect,
    render_consent,
    secure_html,
)
from app.grok_oauth.constants import GROK_OAUTH_CONSENT_PATH, GROK_OAUTH_REVOKE_ALL_PATH
from app.grok_oauth.provider import GENERIC_FAILURE, GrokOAuthProvider, client_limiter_key
from app.security import require_debug_token

router = APIRouter(tags=["grok-oauth"])


def _provider(request: Request) -> GrokOAuthProvider:
    provider = getattr(request.app.state, "grok_oauth", None)
    if not isinstance(provider, GrokOAuthProvider):
        raise HTTPException(status_code=404, detail="not found")
    return provider


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("fly-client-ip") or request.headers.get("x-real-ip")
    host = request.client.host if request.client else None
    return client_limiter_key(host, forwarded)


@router.get(GROK_OAUTH_CONSENT_PATH, include_in_schema=False)
async def grok_oauth_consent(request: Request) -> Response:
    provider = _provider(request)
    return await render_consent(provider, request.query_params.get("tx"))


@router.post(GROK_OAUTH_CONSENT_PATH, include_in_schema=False)
async def grok_oauth_consent_submit(request: Request) -> Response:
    provider = _provider(request)
    key = _client_key(request)
    if provider.is_rate_limited(key):
        return secure_html(consent_error_page(GENERIC_FAILURE), status_code=429)

    form = await request.form()
    transaction_id = str(form.get("tx") or "")
    action = str(form.get("action") or "")
    csrf_token = str(form.get("csrf_token") or "")
    owner_secret = str(form.get("owner_secret") or "")
    transaction = await provider.load_transaction(transaction_id)
    if transaction is None or not provider.verify_csrf(transaction, csrf_token):
        return secure_html(consent_error_page(GENERIC_FAILURE), status_code=400)

    if action == "deny":
        denied = await provider.deny_transaction(transaction_id)
        if denied is None:
            return secure_html(consent_error_page(GENERIC_FAILURE), status_code=400)
        return denied_redirect(denied)

    if action != "approve" or not provider.owner_secret_matches(owner_secret):
        provider.record_failed_attempt(key)
        return secure_html(
            consent_form(transaction=transaction, error=GENERIC_FAILURE),
            status_code=401,
        )

    try:
        redirect_to = await provider.approve_transaction(transaction_id)
    except AuthorizeError:
        provider.record_failed_attempt(key)
        return secure_html(consent_error_page(GENERIC_FAILURE), status_code=400)
    provider.clear_failed_attempts(key)
    return RedirectResponse(
        redirect_to,
        status_code=302,
        headers={**CONSENT_SECURITY_HEADERS, "Cache-Control": "no-store"},
    )


@router.post(GROK_OAUTH_REVOKE_ALL_PATH, include_in_schema=False)
async def grok_oauth_revoke_all(
    request: Request, _: None = Depends(require_debug_token)
) -> dict[str, int]:
    provider = _provider(request)
    revoked = await provider.revoke_all()
    return {"revoked_families": revoked}
