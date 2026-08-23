from __future__ import annotations

import html
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.provider import construct_redirect_uri
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app.grok_oauth.constants import GROK_OAUTH_CONSENT_PATH, GROK_OAUTH_SCOPE
from app.grok_oauth.provider import GENERIC_FAILURE, GrokOAuthProvider

CONSENT_SECURITY_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; font-src 'none'; "
        "script-src 'none'; connect-src 'none'; form-action 'self'; frame-ancestors 'none'; "
        "base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}


def secure_html(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content=content, status_code=status_code, headers=CONSENT_SECURITY_HEADERS)


def _page(*, title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, sans-serif;
      background: #111;
      color: #f5f5f5;
    }}
    main {{
      max-width: 32rem;
      margin: 3rem auto;
      padding: 1.5rem;
      border: 1px solid #333;
      border-radius: 0.75rem;
      background: #1b1b1b;
    }}
    h1 {{ font-size: 1.25rem; margin-top: 0; }}
    dl {{ display: grid; grid-template-columns: 8rem 1fr; gap: 0.4rem 0.75rem; }}
    dt {{ color: #bbb; }}
    dd {{ margin: 0; word-break: break-word; }}
    label {{ display: block; margin: 1rem 0 0.4rem; }}
    input[type=password] {{
      width: 100%;
      box-sizing: border-box;
      padding: 0.6rem;
      border-radius: 0.4rem;
      border: 1px solid #555;
      background: #111;
      color: inherit;
    }}
    .actions {{ display: flex; gap: 0.75rem; margin-top: 1.25rem; }}
    button {{
      flex: 1;
      padding: 0.7rem 0.9rem;
      border-radius: 0.4rem;
      border: 0;
      font: inherit;
      cursor: pointer;
    }}
    button[value=approve] {{ background: #e8e8e8; color: #111; }}
    button[value=deny] {{ background: #333; color: #f5f5f5; }}
    .error {{ color: #f8b4b4; margin-top: 1rem; }}
    .unverified {{
      border: 1px solid #a66;
      background: #3a1f1f;
      padding: 0.75rem 0.9rem;
      border-radius: 0.4rem;
      margin: 0 0 1rem;
    }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    p {{ line-height: 1.45; }}
  </style>
</head>
<body>
  <main>
    {body}
  </main>
</body>
</html>
"""


def redirect_origin(redirect_uri: str) -> str:
    parts = urlsplit(redirect_uri)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return redirect_uri


def consent_form(*, transaction: dict[str, Any], error: str | None = None) -> str:
    client_name = str(transaction.get("client_name") or "Grok connector")
    client_id = str(transaction.get("client_id") or "")
    redirect_uri = str(transaction.get("redirect_uri") or "")
    origin = redirect_origin(redirect_uri)
    resource = str(transaction.get("resource") or "")
    scopes = " ".join(transaction.get("scopes") or [GROK_OAUTH_SCOPE])
    csrf = str(transaction.get("csrf_token") or "")
    tx = str(transaction.get("transaction_id") or "")
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return _page(
        title="Authorize Agent Call",
        body=f"""
    <h1>Authorize Agent Call</h1>
    <p class="unverified"><strong>Unverified client.</strong> This connector was
    dynamically registered and is not verified by this server. The displayed name
    is chosen by the registrant and can be spoofed. Confirm the exact client ID
    and the exact redirect URI/origin that will receive the authorization
    response before approving.</p>
    <p>Approving gives this connector access to Agent Call tools on this
    self-hosted deployment. This does not place a phone call by itself.</p>
    <dl>
      <dt>Displayed name</dt><dd>{html.escape(client_name)}</dd>
      <dt>Client ID</dt><dd class="mono">{html.escape(client_id)}</dd>
      <dt>Redirect URI</dt><dd class="mono">{html.escape(redirect_uri)}</dd>
      <dt>Redirect origin</dt><dd class="mono">{html.escape(origin)}</dd>
      <dt>Resource</dt><dd>{html.escape(resource)}</dd>
      <dt>Scope</dt><dd>{html.escape(scopes)}</dd>
    </dl>
    <form method="post" action="{html.escape(GROK_OAUTH_CONSENT_PATH)}" autocomplete="off">
      <input type="hidden" name="tx" value="{html.escape(tx)}">
      <input type="hidden" name="csrf_token" value="{html.escape(csrf)}">
      <label for="owner_secret">Owner authorization secret</label>
      <input id="owner_secret" name="owner_secret" type="password" required maxlength="1024">
      {error_html}
      <div class="actions">
        <button type="submit" name="action" value="approve">Approve</button>
        <button type="submit" name="action" value="deny">Deny</button>
      </div>
    </form>
""",
    )


def consent_error_page(message: str) -> str:
    return _page(
        title="Authorization failed",
        body=f"<h1>Authorization failed</h1><p class='error'>{html.escape(message)}</p>",
    )


def denied_redirect(transaction: dict[str, Any]) -> Response:
    url = construct_redirect_uri(
        str(transaction["redirect_uri"]),
        error="access_denied",
        state=transaction.get("state"),
    )
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})


async def render_consent(provider: GrokOAuthProvider, transaction_id: str | None) -> Response:
    if not transaction_id:
        return secure_html(consent_error_page(GENERIC_FAILURE), status_code=400)
    transaction = await provider.load_transaction(transaction_id)
    if transaction is None:
        return secure_html(consent_error_page(GENERIC_FAILURE), status_code=400)
    return secure_html(consent_form(transaction=transaction))
