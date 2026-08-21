from __future__ import annotations

import json
import re
from typing import Any

from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from app.grok_oauth.constants import (
    OAUTH_CLIENT_CONTACT_MAX_COUNT,
    OAUTH_CLIENT_CONTACT_MAX_LENGTH,
    OAUTH_CLIENT_GRANT_TYPE_MAX_COUNT,
    OAUTH_CLIENT_GRANT_TYPE_MAX_LENGTH,
    OAUTH_CLIENT_ID_MAX_LENGTH,
    OAUTH_CLIENT_JWKS_MAX_JSON_BYTES,
    OAUTH_CLIENT_METADATA_MAX_JSON_BYTES,
    OAUTH_CLIENT_NAME_MAX_LENGTH,
    OAUTH_CLIENT_REDIRECT_URI_MAX_COUNT,
    OAUTH_CLIENT_SECRET_MAX_LENGTH,
    OAUTH_CLIENT_SOFTWARE_MAX_LENGTH,
    OAUTH_CLIENT_URI_MAX_LENGTH,
    PKCE_S256_CHALLENGE_LENGTH,
)

_PKCE_S256_RE = re.compile(rf"^[A-Za-z0-9_-]{{{PKCE_S256_CHALLENGE_LENGTH}}}$")
_URI_FIELDS = ("client_uri", "logo_uri", "tos_uri", "policy_uri", "jwks_uri")


def is_valid_pkce_s256_challenge(value: str | None) -> bool:
    if value is None:
        return False
    return _PKCE_S256_RE.fullmatch(value) is not None


def _reject(description: str) -> None:
    raise RegistrationError(
        error="invalid_client_metadata",
        error_description=description,
    )


def _bounded_optional_text(value: str | None, *, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > max_length:
        _reject(f"{field} exceeds the maximum length of {max_length}")
    return stripped


def _bounded_uri(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > OAUTH_CLIENT_URI_MAX_LENGTH:
        _reject(f"{field} exceeds the maximum length of {OAUTH_CLIENT_URI_MAX_LENGTH}")
    return text


def normalize_registered_client(
    client_info: OAuthClientInformationFull,
) -> OAuthClientInformationFull:
    """Bound and normalize DCR metadata before it is encrypted and stored."""
    client_id = client_info.client_id
    if not client_id or not str(client_id).strip():
        _reject("client_id is required")
    client_id = str(client_id).strip()
    if len(client_id) > OAUTH_CLIENT_ID_MAX_LENGTH:
        _reject("client_id exceeds the maximum length")

    client_secret = client_info.client_secret
    if client_secret is not None:
        if not client_secret or len(client_secret) > OAUTH_CLIENT_SECRET_MAX_LENGTH:
            _reject("client_secret is invalid")

    redirect_uris = list(client_info.redirect_uris or [])
    if not redirect_uris:
        raise RegistrationError(
            error="invalid_redirect_uri",
            error_description="at least one redirect_uri is required",
        )
    if len(redirect_uris) > OAUTH_CLIENT_REDIRECT_URI_MAX_COUNT:
        raise RegistrationError(
            error="invalid_redirect_uri",
            error_description=(
                f"at most {OAUTH_CLIENT_REDIRECT_URI_MAX_COUNT} redirect_uris are allowed"
            ),
        )
    normalized_redirects: list[AnyUrl] = []
    for uri in redirect_uris:
        text = str(uri).strip()
        if not text or len(text) > OAUTH_CLIENT_URI_MAX_LENGTH:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="redirect_uri is missing or too long",
            )
        normalized_redirects.append(AnyUrl(text))

    grant_types = list(client_info.grant_types or [])
    if len(grant_types) > OAUTH_CLIENT_GRANT_TYPE_MAX_COUNT:
        _reject("too many grant_types")
    for grant in grant_types:
        if not grant or len(str(grant)) > OAUTH_CLIENT_GRANT_TYPE_MAX_LENGTH:
            _reject("grant_type is invalid")

    response_types = list(client_info.response_types or [])
    if len(response_types) > OAUTH_CLIENT_GRANT_TYPE_MAX_COUNT:
        _reject("too many response_types")
    for response_type in response_types:
        if not response_type or len(str(response_type)) > OAUTH_CLIENT_GRANT_TYPE_MAX_LENGTH:
            _reject("response_type is invalid")

    contacts: list[str] | None = None
    if client_info.contacts is not None:
        if len(client_info.contacts) > OAUTH_CLIENT_CONTACT_MAX_COUNT:
            _reject(f"at most {OAUTH_CLIENT_CONTACT_MAX_COUNT} contacts are allowed")
        contacts = []
        for contact in client_info.contacts:
            item = _bounded_optional_text(
                str(contact),
                field="contacts",
                max_length=OAUTH_CLIENT_CONTACT_MAX_LENGTH,
            )
            if item is not None:
                contacts.append(item)

    uri_updates = {
        field: _bounded_uri(getattr(client_info, field), field=field) for field in _URI_FIELDS
    }
    if client_info.jwks is not None:
        try:
            encoded_jwks = json.dumps(
                client_info.jwks, separators=(",", ":"), sort_keys=True, default=str
            )
        except TypeError:
            _reject("jwks is invalid")
        if len(encoded_jwks.encode("utf-8")) > OAUTH_CLIENT_JWKS_MAX_JSON_BYTES:
            _reject("jwks exceeds the maximum size")

    normalized = client_info.model_copy(
        update={
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": normalized_redirects,
            "grant_types": grant_types,
            "response_types": response_types,
            "client_name": _bounded_optional_text(
                client_info.client_name,
                field="client_name",
                max_length=OAUTH_CLIENT_NAME_MAX_LENGTH,
            ),
            "scope": _bounded_optional_text(
                client_info.scope,
                field="scope",
                max_length=OAUTH_CLIENT_NAME_MAX_LENGTH,
            ),
            "software_id": _bounded_optional_text(
                client_info.software_id,
                field="software_id",
                max_length=OAUTH_CLIENT_SOFTWARE_MAX_LENGTH,
            ),
            "software_version": _bounded_optional_text(
                client_info.software_version,
                field="software_version",
                max_length=OAUTH_CLIENT_SOFTWARE_MAX_LENGTH,
            ),
            "contacts": contacts,
            **uri_updates,
        }
    )
    payload = normalized.model_dump(mode="json")
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > OAUTH_CLIENT_METADATA_MAX_JSON_BYTES:
        _reject("client metadata exceeds the maximum size")
    return normalized
