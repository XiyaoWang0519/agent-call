from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from fastmcp.server.auth.auth import AccessToken, OAuthProvider
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from app.db import Database
from app.db.engine import _iso_now
from app.grok_oauth import constants as grok_oauth_constants
from app.grok_oauth.constants import (
    FAILED_ATTEMPT_LIMIT,
    FAILED_ATTEMPT_WINDOW_SECONDS,
    GROK_MCP_PATH,
    GROK_OAUTH_CONSENT_PATH,
    GROK_OAUTH_SCOPE,
    GROK_OAUTH_SUBJECT,
    OAUTH_STORAGE_KEY_SALT,
    OWNER_SECRET_RATE_LIMIT_KEY,
)
from app.grok_oauth.crypto import (
    decrypt_text,
    derive_mac_key,
    derive_signing_key,
    derive_storage_fernet,
    encrypt_sentinel,
    encrypt_text,
    fingerprint,
    keyed_hash,
    new_token,
    verify_owner_secret,
    verify_sentinel,
)
from app.grok_oauth.rate_limit import FailedAttemptLimiter
from app.grok_oauth.registration import (
    is_valid_pkce_s256_challenge,
    normalize_registered_client,
)
from app.grok_oauth.tokens import AccessTokenIssuer
from app.settings import Settings

logger = logging.getLogger(__name__)


def grok_oauth_issuer(public_base_url: str) -> str:
    return public_base_url.rstrip("/")


def grok_mcp_resource(public_base_url: str) -> str:
    return f"{grok_oauth_issuer(public_base_url)}{GROK_MCP_PATH}"


def normalize_resource(value: str) -> str:
    return value.rstrip("/")


class GrokOAuthProvider(OAuthProvider):
    """Self-hosted single-owner OAuth 2.1 provider for the Grok MCP endpoint."""

    def __init__(self, settings: Settings) -> None:
        if not settings.public_base_url:
            raise RuntimeError("PUBLIC_BASE_URL is required when Grok OAuth is enabled")
        issuer = grok_oauth_issuer(settings.public_base_url)
        resource = grok_mcp_resource(settings.public_base_url)
        super().__init__(
            base_url=issuer,
            resource_base_url=issuer,
            issuer_url=issuer,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[GROK_OAUTH_SCOPE],
                default_scopes=[GROK_OAUTH_SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[GROK_OAUTH_SCOPE],
        )
        self._settings = settings
        self.resource = resource
        owner_hash = Settings.reveal(settings.grok_mcp_oauth_owner_secret_hash)
        signing_material = Settings.reveal(settings.grok_mcp_oauth_signing_key)
        storage_material = Settings.reveal(settings.grok_mcp_oauth_storage_encryption_key)
        self._owner_secret_hash = owner_hash
        self._fernet = derive_storage_fernet(storage_material, salt=OAUTH_STORAGE_KEY_SALT)
        self._mac_key = derive_mac_key(storage_material, salt=OAUTH_STORAGE_KEY_SALT)
        self._issuer = AccessTokenIssuer(
            issuer=issuer,
            audience=resource,
            signing_key=derive_signing_key(signing_material, salt=issuer),
        )
        self._owner_fingerprint = fingerprint(owner_hash)
        self._signing_fingerprint = fingerprint(signing_material)
        self._db: Database | None = None
        self._limiter = FailedAttemptLimiter(
            max_failures=FAILED_ATTEMPT_LIMIT,
            window_seconds=FAILED_ATTEMPT_WINDOW_SECONDS,
        )
        self.access_token_ttl = settings.grok_mcp_oauth_access_token_ttl_seconds
        self.refresh_token_ttl_seconds = (
            settings.grok_mcp_oauth_refresh_token_ttl_days * 24 * 60 * 60
        )
        self.auth_code_ttl = settings.grok_mcp_oauth_auth_code_ttl_seconds

    def attach_database(self, db: Database) -> None:
        self._db = db

    def _store(self) -> Database:
        if self._db is None:
            raise RuntimeError("oauth store is not attached")
        return self._db

    def hash_secret_material(self, value: str) -> str:
        return keyed_hash(value, self._mac_key)

    def encrypt_payload(self, payload: dict[str, Any]) -> str:
        return encrypt_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True), self._fernet
        )

    def decrypt_payload(self, ciphertext: str) -> dict[str, Any]:
        decoded = json.loads(decrypt_text(ciphertext, self._fernet))
        if not isinstance(decoded, dict):
            raise RuntimeError("oauth storage decryption failed")
        return decoded

    async def prepare_storage(self) -> None:
        store = self._store()
        state = await store.oauth_load_runtime_state()
        if state is None:
            await store.oauth_initialize_runtime_state(
                storage_sentinel=encrypt_sentinel(self._fernet),
                owner_hash_fingerprint=self._owner_fingerprint,
                signing_key_fingerprint=self._signing_fingerprint,
            )
            await store.oauth_purge_expired(_iso_now())
            return
        verify_sentinel(str(state["storage_sentinel"]), self._fernet)
        if (
            str(state["owner_hash_fingerprint"]) != self._owner_fingerprint
            or str(state["signing_key_fingerprint"]) != self._signing_fingerprint
        ):
            revoked = await store.oauth_revoke_all_families()
            await store.oauth_update_runtime_fingerprints(
                owner_hash_fingerprint=self._owner_fingerprint,
                signing_key_fingerprint=self._signing_fingerprint,
            )
            logger.info("revoked grok oauth families after credential rotation count=%s", revoked)
        await store.oauth_purge_expired(_iso_now())

    def _canonical_resource(self, value: str | None) -> str:
        if value is None or not value.strip():
            raise AuthorizeError(
                error="invalid_request",
                error_description="resource is required",
            )
        if normalize_resource(value) != normalize_resource(self.resource):
            raise AuthorizeError(
                error="invalid_request",
                error_description="resource does not match this server",
            )
        return self.resource

    def _scopes(self, requested: list[str] | None) -> list[str]:
        scopes = list(requested or [GROK_OAUTH_SCOPE])
        if scopes != [GROK_OAUTH_SCOPE]:
            raise AuthorizeError(error="invalid_scope", error_description="invalid scope")
        return scopes

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = await self._store().oauth_get_client(client_id)
        if row is None:
            return None
        try:
            payload = self.decrypt_payload(str(row["ciphertext"]))
            return OAuthClientInformationFull.model_validate(payload)
        except RuntimeError:
            logger.info("oauth client record could not be decrypted client_id=%s", client_id)
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        normalized = normalize_registered_client(client_info)
        if normalized.client_id is None:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="client_id is required",
            )
        if (
            normalized.scope is not None
            and self.client_registration_options is not None
            and self.client_registration_options.valid_scopes is not None
        ):
            requested = set(normalized.scope.split())
            valid = set(self.client_registration_options.valid_scopes)
            if requested - valid:
                raise RegistrationError(
                    error="invalid_client_metadata",
                    error_description="requested scopes are not valid",
                )
        now = datetime.now(UTC)
        unused_before = now - timedelta(
            seconds=grok_oauth_constants.OAUTH_CLIENT_UNUSED_RETENTION_SECONDS
        )
        inserted = await self._store().oauth_insert_client_with_quota(
            client_id=normalized.client_id,
            ciphertext=self.encrypt_payload(normalized.model_dump(mode="json")),
            max_clients=grok_oauth_constants.OAUTH_CLIENT_MAX_COUNT,
            unused_before=unused_before.isoformat(),
            now=now.isoformat(),
        )
        if not inserted:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="client registration quota exceeded",
            )
        await self._store().oauth_record_audit(
            "client_registered", extra={"client_id": normalized.client_id}
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if client.client_id is None:
            raise AuthorizeError(
                error="unauthorized_client", error_description="Client ID is required"
            )
        if not is_valid_pkce_s256_challenge(params.code_challenge):
            raise AuthorizeError(
                error="invalid_request",
                error_description="PKCE S256 code_challenge is required",
            )
        resource = self._canonical_resource(params.resource)
        scopes = self._scopes(params.scopes)
        transaction_id = new_token()
        csrf_token = new_token()
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self.auth_code_ttl)
        payload = {
            "client_id": client.client_id,
            "client_name": client.client_name,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "state": params.state,
            "scopes": scopes,
            "resource": resource,
            "code_challenge": params.code_challenge,
            "csrf_token": csrf_token,
        }
        inserted = await self._store().oauth_create_transaction_with_quota(
            transaction_id=transaction_id,
            client_id=client.client_id,
            csrf_hash=self.hash_secret_material(csrf_token),
            ciphertext=self.encrypt_payload(payload),
            expires_at=expires_at.isoformat(),
            max_transactions=grok_oauth_constants.OAUTH_TRANSACTION_MAX_COUNT,
            max_per_client=grok_oauth_constants.OAUTH_TRANSACTION_MAX_PER_CLIENT,
            now=now.isoformat(),
        )
        if not inserted:
            raise AuthorizeError(
                error="invalid_request",
                error_description="authorization request quota exceeded",
            )
        logger.info(
            "oauth authorization started client_id=%s transaction_id=%s",
            client.client_id,
            transaction_id,
        )
        query = urlencode({"tx": transaction_id})
        return f"{GROK_OAUTH_CONSENT_PATH}?{query}"

    async def load_transaction(self, transaction_id: str) -> dict[str, Any] | None:
        row = await self._store().oauth_get_transaction(transaction_id)
        if row is None or int(row["consumed"]) != 0:
            return None
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        if expires_at <= datetime.now(UTC):
            return None
        payload = self.decrypt_payload(str(row["ciphertext"]))
        payload["transaction_id"] = transaction_id
        payload["csrf_hash"] = str(row["csrf_hash"])
        return payload

    def verify_csrf(self, transaction: dict[str, Any], csrf_token: str) -> bool:
        return self.hash_secret_material(csrf_token) == str(transaction.get("csrf_hash"))

    def is_rate_limited(self, key: str) -> bool:
        return self._limiter.is_blocked(key) or self._limiter.is_blocked(
            OWNER_SECRET_RATE_LIMIT_KEY
        )

    def record_failed_attempt(self, key: str) -> None:
        self._limiter.record_failure(key)
        self._limiter.record_failure(OWNER_SECRET_RATE_LIMIT_KEY)

    def clear_failed_attempts(self, key: str) -> None:
        self._limiter.clear(key)
        self._limiter.clear(OWNER_SECRET_RATE_LIMIT_KEY)

    def owner_secret_matches(self, secret: str) -> bool:
        return verify_owner_secret(secret=secret, secret_hash=self._owner_secret_hash)

    async def deny_transaction(self, transaction_id: str) -> dict[str, Any] | None:
        transaction = await self.load_transaction(transaction_id)
        if transaction is None:
            return None
        await self._store().oauth_consume_transaction(transaction_id)
        await self._store().oauth_record_audit(
            "authorization_denied", extra={"client_id": transaction.get("client_id")}
        )
        return transaction

    async def approve_transaction(self, transaction_id: str) -> str:
        store = self._store()
        transaction = await self.load_transaction(transaction_id)
        if transaction is None:
            raise AuthorizeError(error="access_denied", error_description=GENERIC_FAILURE)
        consumed = await store.oauth_consume_transaction(transaction_id)
        if not consumed:
            raise AuthorizeError(error="access_denied", error_description=GENERIC_FAILURE)
        code = new_token()
        expires_at = datetime.now(UTC) + timedelta(seconds=self.auth_code_ttl)
        await store.oauth_insert_authorization_code(
            code_hash=self.hash_secret_material(code),
            client_id=str(transaction["client_id"]),
            ciphertext=self.encrypt_payload(
                {
                    "redirect_uri": transaction["redirect_uri"],
                    "redirect_uri_provided_explicitly": transaction[
                        "redirect_uri_provided_explicitly"
                    ],
                    "scopes": transaction["scopes"],
                    "resource": transaction["resource"],
                    "code_challenge": transaction["code_challenge"],
                    "subject": GROK_OAUTH_SUBJECT,
                }
            ),
            expires_at=expires_at.isoformat(),
        )
        await store.oauth_record_audit(
            "authorization_approved", extra={"client_id": transaction["client_id"]}
        )
        logger.info(
            "oauth authorization approved client_id=%s transaction_id=%s",
            transaction["client_id"],
            transaction_id,
        )
        return construct_redirect_uri(
            str(transaction["redirect_uri"]),
            code=code,
            state=transaction.get("state"),
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = await self._store().oauth_get_authorization_code(
            self.hash_secret_material(authorization_code)
        )
        if row is None or str(row["client_id"]) != client.client_id:
            return None
        if int(row["consumed"]) != 0:
            return None
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        if expires_at <= datetime.now(UTC):
            return None
        payload = self.decrypt_payload(str(row["ciphertext"]))
        return AuthorizationCode(
            code=authorization_code,
            client_id=str(row["client_id"]),
            redirect_uri=AnyUrl(str(payload["redirect_uri"])),
            redirect_uri_provided_explicitly=bool(payload["redirect_uri_provided_explicitly"]),
            scopes=list(payload["scopes"]),
            expires_at=expires_at.timestamp(),
            code_challenge=str(payload["code_challenge"]),
            resource=str(payload["resource"]),
            subject=str(payload.get("subject") or GROK_OAUTH_SUBJECT),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")
        if normalize_resource(authorization_code.resource or "") != normalize_resource(
            self.resource
        ):
            raise TokenError("invalid_grant", "authorization code is bound to a different resource")
        consumed = await self._store().oauth_consume_authorization_code(
            self.hash_secret_material(authorization_code.code)
        )
        if not consumed:
            raise TokenError("invalid_grant", "Authorization code not found or already used.")
        return await self._issue_token_pair(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            family_id=None,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = await self._store().oauth_get_refresh_token(self.hash_secret_material(refresh_token))
        if row is None or str(row["client_id"]) != client.client_id:
            return None
        family = await self._store().oauth_get_family(str(row["family_id"]))
        if family is None:
            return None
        if int(row["consumed"]) != 0 or int(row["revoked"]) != 0 or int(family["revoked"]) != 0:
            await self._store().oauth_revoke_family(str(row["family_id"]))
            logger.info("oauth refresh reuse detected family_id=%s", row["family_id"])
            return None
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        if expires_at <= datetime.now(UTC):
            return None
        payload = self.decrypt_payload(str(row["ciphertext"]))
        return RefreshToken(
            token=refresh_token,
            client_id=str(row["client_id"]),
            scopes=list(payload["scopes"]),
            expires_at=int(expires_at.timestamp()),
            subject=str(payload.get("subject") or GROK_OAUTH_SUBJECT),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")
        original = set(refresh_token.scopes)
        requested = set(scopes)
        if not requested.issubset(original):
            raise TokenError("invalid_scope", "Requested scopes exceed those authorized.")
        token_hash = self.hash_secret_material(refresh_token.token)
        row = await self._store().oauth_get_refresh_token(token_hash)
        if row is None:
            raise TokenError("invalid_grant", "refresh token does not exist")
        consumed = await self._store().oauth_consume_refresh_token(token_hash)
        if not consumed:
            await self._store().oauth_revoke_family(str(row["family_id"]))
            raise TokenError("invalid_grant", "refresh token does not exist")
        return await self._issue_token_pair(
            client_id=client.client_id,
            scopes=list(requested) or refresh_token.scopes,
            family_id=str(row["family_id"]),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        claims = self._issuer.verify(token)
        if claims is None:
            return None
        family_id = str(claims.get("family_id") or "")
        jti = str(claims.get("jti") or "")
        if not family_id or not jti:
            return None
        family = await self._store().oauth_get_family(family_id)
        if family is None or int(family["revoked"]) != 0:
            return None
        jti_row = await self._store().oauth_get_access_jti(jti)
        if jti_row is None or int(jti_row["revoked"]) != 0:
            return None
        scope = str(claims.get("scope") or "")
        scopes = [part for part in scope.split() if part]
        if GROK_OAUTH_SCOPE not in scopes:
            return None
        expires_at = claims.get("exp")
        return AccessToken(
            token="[redacted]",
            client_id=str(claims.get("client_id") or ""),
            scopes=scopes,
            expires_at=int(expires_at) if isinstance(expires_at, int) else None,
            resource=self.resource,
            subject=GROK_OAUTH_SUBJECT,
            claims=claims,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.load_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            row = await self._store().oauth_get_refresh_token(
                self.hash_secret_material(token.token)
            )
            if row is not None:
                await self._store().oauth_revoke_family(str(row["family_id"]))
            return
        family_id = str((token.claims or {}).get("family_id") or "")
        if family_id:
            await self._store().oauth_revoke_family(family_id)

    async def revoke_all(self) -> int:
        count = await self._store().oauth_revoke_all_families()
        logger.info("revoked all grok oauth families count=%s", count)
        return count

    async def _issue_token_pair(
        self,
        *,
        client_id: str,
        scopes: list[str],
        family_id: str | None,
    ) -> OAuthToken:
        store = self._store()
        now = datetime.now(UTC)
        await store.oauth_purge_expired(now.isoformat())
        refresh_expires = now + timedelta(seconds=self.refresh_token_ttl_seconds)
        if family_id is None:
            family_id = uuid4().hex
            await store.oauth_insert_family(
                family_id=family_id,
                client_id=client_id,
                expires_at=refresh_expires.isoformat(),
            )
        else:
            family = await store.oauth_get_family(family_id)
            if family is None or int(family["revoked"]) != 0:
                raise TokenError("invalid_grant", "refresh token does not exist")
            family_expires = datetime.fromisoformat(str(family["expires_at"]))
            if family_expires <= now:
                await store.oauth_revoke_family(family_id)
                raise TokenError("invalid_grant", "refresh token has expired")
            refresh_expires = family_expires
        remaining = int((refresh_expires - now).total_seconds())
        if remaining <= 0:
            await store.oauth_revoke_family(family_id)
            raise TokenError("invalid_grant", "refresh token has expired")
        jti = uuid4().hex
        access = self._issuer.issue(
            client_id=client_id,
            scopes=scopes,
            jti=jti,
            family_id=family_id,
            expires_in=self.access_token_ttl,
        )
        refresh = new_token()
        await store.oauth_insert_access_jti(
            jti=jti,
            family_id=family_id,
            expires_at=(now + timedelta(seconds=self.access_token_ttl)).isoformat(),
        )
        await store.oauth_insert_refresh_token(
            token_hash=self.hash_secret_material(refresh),
            family_id=family_id,
            client_id=client_id,
            ciphertext=self.encrypt_payload(
                {
                    "scopes": scopes,
                    "resource": self.resource,
                    "subject": GROK_OAUTH_SUBJECT,
                }
            ),
            expires_at=refresh_expires.isoformat(),
        )
        await store.oauth_record_audit(
            "token_issued", extra={"client_id": client_id, "family_id": family_id}
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.access_token_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )


GENERIC_FAILURE = "Authorization failed."


def client_limiter_key(client_host: str | None, forwarded: str | None) -> str:
    if forwarded and forwarded.strip():
        return forwarded.strip()
    return (client_host or "unknown").strip() or "unknown"
