from __future__ import annotations

import time
from typing import Any

from joserfc import jwk, jwt
from joserfc.errors import JoseError

from app.grok_oauth.constants import GROK_OAUTH_SUBJECT


class AccessTokenIssuer:
    """Issue and verify HS256 access tokens for the Grok MCP resource."""

    def __init__(self, *, issuer: str, audience: str, signing_key: bytes) -> None:
        self.issuer = issuer
        self.audience = audience
        self.signing_key = signing_key
        self._jwt_key = jwk.import_key(signing_key, "oct")

    def issue(
        self,
        *,
        client_id: str,
        scopes: list[str],
        jti: str,
        family_id: str,
        expires_in: int,
        subject: str = GROK_OAUTH_SUBJECT,
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": self.issuer,
            "sub": subject,
            "aud": self.audience,
            "client_id": client_id,
            "scope": " ".join(scopes),
            "exp": now + expires_in,
            "iat": now,
            "jti": jti,
            "family_id": family_id,
            "token_use": "access",
        }
        return jwt.encode(
            {"alg": "HS256", "typ": "JWT"}, payload, self._jwt_key, algorithms=["HS256"]
        )

    def verify(self, token: str) -> dict[str, Any] | None:
        try:
            claims = jwt.decode(token, self._jwt_key, algorithms=["HS256"]).claims
        except (JoseError, ValueError, TypeError):
            return None
        if claims.get("token_use", "access") != "access":
            return None
        exp = claims.get("exp")
        if not isinstance(exp, int) or exp < int(time.time()):
            return None
        if claims.get("iss") != self.issuer:
            return None
        if claims.get("aud") != self.audience:
            return None
        if claims.get("sub") != GROK_OAUTH_SUBJECT:
            return None
        return claims
