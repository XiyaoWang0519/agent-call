from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from mcp.server.auth.provider import AuthorizeError

from app.mcp_oauth.consent import (
    consent_error_page,
    consent_form,
    consent_security_headers,
    denied_redirect,
    render_consent,
    secure_html,
)
from app.mcp_oauth.constants import MCP_OAUTH_CONSENT_PATH, MCP_OAUTH_REVOKE_ALL_PATH
from app.mcp_oauth.provider import GENERIC_FAILURE, MCPOAuthProvider, client_limiter_key
from app.security import require_debug_token

router = APIRouter(tags=["mcp-oauth"])


def _provider(request: Request) -> MCPOAuthProvider:
    provider = getattr(request.app.state, "mcp_oauth", None)
    if not isinstance(provider, MCPOAuthProvider):
        raise HTTPException(status_code=404, detail="not found")
    return provider


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("fly-client-ip") or request.headers.get("x-real-ip")
    host = request.client.host if request.client else None
    return client_limiter_key(host, forwarded)


@router.get(MCP_OAUTH_CONSENT_PATH, include_in_schema=False)
async def mcp_oauth_consent(request: Request) -> Response:
    provider = _provider(request)
    return await render_consent(provider, request.query_params.get("tx"))


@router.post(MCP_OAUTH_CONSENT_PATH, include_in_schema=False)
async def mcp_oauth_consent_submit(request: Request) -> Response:
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
            redirect_uri=str(transaction["redirect_uri"]),
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
        headers=consent_security_headers(str(transaction["redirect_uri"])),
    )


@router.post(MCP_OAUTH_REVOKE_ALL_PATH, include_in_schema=False)
async def mcp_oauth_revoke_all(
    request: Request, _: None = Depends(require_debug_token)
) -> dict[str, int]:
    provider = _provider(request)
    revoked = await provider.revoke_all()
    return {"revoked_families": revoked}
