from __future__ import annotations

import re
from functools import cached_property
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.grok_oauth.constants import (
    ACCESS_TOKEN_TTL_MAX_SECONDS,
    ACCESS_TOKEN_TTL_MIN_SECONDS,
    AUTH_CODE_TTL_MAX_SECONDS,
    AUTH_CODE_TTL_MIN_SECONDS,
    DEPLOYMENT_SECRET_MIN_LENGTH,
    REFRESH_TOKEN_TTL_MAX_DAYS,
    REFRESH_TOKEN_TTL_MIN_DAYS,
)
from app.grok_oauth.crypto import is_argon2id_hash

SUPPORTED_TRANSCRIPTION_MODELS = frozenset(
    {
        "whisper-1",
        "gpt-4o-mini-transcribe",
        "gpt-4o-mini-transcribe-2025-12-15",
        "gpt-4o-transcribe",
        "gpt-4o-transcribe-diarize",
        "gpt-realtime-whisper",
    }
)
TRANSCRIPTION_DELAYS = frozenset({"minimal", "low", "medium", "high", "xhigh"})
_LOOPBACK_HOSTS = frozenset({"localhost", "localhost."})


def _is_loopback_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.casefold() in _LOOPBACK_HOSTS:
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    openai_api_key: SecretStr | None = None
    openai_webhook_secret: SecretStr | None = None
    openai_project_id: str | None = None
    exa_api_key: SecretStr | None = None
    exa_search_timeout_seconds: float = Field(default=3.0, gt=0, le=10)
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_caller_id: str | None = None
    twilio_http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    owner_phone_e164: str | None = None
    owner_display_name: str = "the owner"
    allowed_agent_user_id: str | None = None
    mcp_bearer_token: SecretStr | None = None
    debug_api_token: SecretStr | None = None
    deploy_guard_token: SecretStr | None = None
    agent_webhook_url: str | None = None
    agent_webhook_token: SecretStr | None = None
    agent_push_enabled: bool = False
    ask_agent_enabled: bool = False
    ask_agent_answer_timeout_seconds: float = Field(default=60.0, gt=0, le=120)
    ask_agent_max_questions_per_call: int = Field(default=5, ge=1, le=20)
    hold_detection_enabled: bool = False
    hold_max_seconds: float = Field(default=300.0, gt=0, le=600)
    wait_for_call_event_max_seconds: float = Field(default=20.0, gt=0, le=25)
    allowed_country_codes: list[str] = Field(default_factory=lambda: ["+1"])
    input_transcription_model: str = "gpt-realtime-whisper"
    input_transcription_delay: str | None = None
    semantic_vad_eagerness: Literal["low", "medium", "high", "auto"] = "auto"
    openai_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    openai_http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    openai_keepalive_expiry_seconds: float | None = Field(default=60.0, ge=5, le=300)
    openai_extraction_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    extractor_model: str = "gpt-5.4-nano-2026-03-17"
    database_url: str = "sqlite:///./agent_call.db"
    public_base_url: str | None = None
    realtime_model: Literal["gpt-realtime-2.1"] = "gpt-realtime-2.1"
    mini_models_enabled: bool = False
    setup_deadline_seconds: Literal[60] = 60
    watchdog_stale_seconds: Literal[15] = 15
    plan_ttl_seconds: Literal[600] = 600
    max_call_seconds: Literal[600] = 600
    owner_timezone: str = "America/Los_Angeles"

    grok_mcp_oauth_enabled: bool = False
    grok_mcp_oauth_owner_secret_hash: SecretStr | None = None
    grok_mcp_oauth_signing_key: SecretStr | None = None
    grok_mcp_oauth_storage_encryption_key: SecretStr | None = None
    grok_mcp_oauth_access_token_ttl_seconds: int = 3600
    grok_mcp_oauth_refresh_token_ttl_days: int = 90
    grok_mcp_oauth_auth_code_ttl_seconds: int = 300

    # Cost tracking (estimated pricing, USD per 1M tokens unless noted)
    realtime_text_input_price_per_1m: float = Field(default=4.00, ge=0)
    realtime_audio_input_price_per_1m: float = Field(default=32.00, ge=0)
    realtime_cached_text_input_price_per_1m: float = Field(default=0.40, ge=0)
    realtime_cached_audio_input_price_per_1m: float = Field(default=0.40, ge=0)
    realtime_text_output_price_per_1m: float = Field(default=16.00, ge=0)
    realtime_audio_output_price_per_1m: float = Field(default=64.00, ge=0)
    extractor_input_price_per_1m: float = Field(default=0.05, ge=0)
    extractor_output_price_per_1m: float = Field(default=0.40, ge=0)
    twilio_voice_price_per_minute: float = Field(default=0.014, ge=0)

    @field_validator("allowed_country_codes", mode="before")
    @classmethod
    def parse_country_codes(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("allowed_country_codes")
    @classmethod
    def validate_country_codes(cls, value: list[str]) -> list[str]:
        if not value or any(not re.fullmatch(r"\+[1-9]\d{0,2}", item) for item in value):
            raise ValueError("ALLOWED_COUNTRY_CODES must contain E.164 country prefixes")
        return value

    @field_validator("twilio_caller_id", "owner_phone_e164")
    @classmethod
    def validate_configured_phone(cls, value: str | None) -> str | None:
        if value and not re.fullmatch(r"\+[1-9]\d{1,14}", value):
            raise ValueError("configured phone numbers must use E.164")
        return value

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: str | None) -> str | None:
        if not value:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin without a path or query")
        return value.rstrip("/")

    @field_validator("agent_webhook_url")
    @classmethod
    def validate_agent_webhook_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        parsed = urlsplit(stripped)
        loopback = _is_loopback_host(parsed.hostname)
        https_ok = parsed.scheme == "https"
        http_loopback_ok = parsed.scheme == "http" and loopback
        if (
            not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not (https_ok or http_loopback_ok)
        ):
            raise ValueError(
                "AGENT_WEBHOOK_URL must be an HTTPS URL without credentials or a "
                "fragment; http:// is allowed only for localhost"
            )
        return stripped

    @field_validator("input_transcription_model")
    @classmethod
    def validate_transcription_model(cls, value: str) -> str:
        if value not in SUPPORTED_TRANSCRIPTION_MODELS:
            supported = ", ".join(sorted(SUPPORTED_TRANSCRIPTION_MODELS))
            raise ValueError(f"unsupported transcription model; expected one of: {supported}")
        return value

    @model_validator(mode="after")
    def validate_transcription_delay(self) -> Settings:
        if self.input_transcription_delay is not None:
            if self.input_transcription_model != "gpt-realtime-whisper":
                raise ValueError(
                    "INPUT_TRANSCRIPTION_DELAY is only valid with gpt-realtime-whisper"
                )
            if self.input_transcription_delay not in TRANSCRIPTION_DELAYS:
                raise ValueError("invalid INPUT_TRANSCRIPTION_DELAY")
        if self.mini_models_enabled:
            raise ValueError("mini realtime models are release-gated and disabled in v1")
        if self.openai_connect_timeout_seconds > self.openai_http_timeout_seconds:
            raise ValueError(
                "OPENAI_CONNECT_TIMEOUT_SECONDS cannot exceed OPENAI_HTTP_TIMEOUT_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def validate_agent_push_configuration(self) -> Settings:
        if not self.agent_push_enabled:
            return self
        token = self.agent_webhook_token
        token_missing = token is None or not token.get_secret_value().strip()
        if not self.agent_webhook_url or token_missing:
            raise ValueError(
                "AGENT_PUSH_ENABLED requires AGENT_WEBHOOK_URL and AGENT_WEBHOOK_TOKEN"
            )
        return self

    @model_validator(mode="after")
    def validate_grok_oauth_configuration(self) -> Settings:
        if not self.grok_mcp_oauth_enabled:
            return self
        missing: list[str] = []
        owner_hash = self.grok_mcp_oauth_owner_secret_hash
        signing_key = self.grok_mcp_oauth_signing_key
        storage_key = self.grok_mcp_oauth_storage_encryption_key
        if owner_hash is None or not owner_hash.get_secret_value().strip():
            missing.append("GROK_MCP_OAUTH_OWNER_SECRET_HASH")
        if signing_key is None or not signing_key.get_secret_value().strip():
            missing.append("GROK_MCP_OAUTH_SIGNING_KEY")
        if storage_key is None or not storage_key.get_secret_value().strip():
            missing.append("GROK_MCP_OAUTH_STORAGE_ENCRYPTION_KEY")
        if not self.public_base_url:
            missing.append("PUBLIC_BASE_URL")
        if missing:
            raise ValueError("GROK_MCP_OAUTH_ENABLED requires " + ", ".join(missing))
        assert owner_hash is not None
        assert signing_key is not None
        assert storage_key is not None
        if not is_argon2id_hash(owner_hash.get_secret_value()):
            raise ValueError("GROK_MCP_OAUTH_OWNER_SECRET_HASH must be an Argon2id hash")
        if len(signing_key.get_secret_value().strip()) < DEPLOYMENT_SECRET_MIN_LENGTH:
            raise ValueError("GROK_MCP_OAUTH_SIGNING_KEY is too short")
        if len(storage_key.get_secret_value().strip()) < DEPLOYMENT_SECRET_MIN_LENGTH:
            raise ValueError("GROK_MCP_OAUTH_STORAGE_ENCRYPTION_KEY is too short")
        if not (
            ACCESS_TOKEN_TTL_MIN_SECONDS
            <= self.grok_mcp_oauth_access_token_ttl_seconds
            <= ACCESS_TOKEN_TTL_MAX_SECONDS
        ):
            raise ValueError("GROK_MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS is outside the allowed range")
        if not (
            REFRESH_TOKEN_TTL_MIN_DAYS
            <= self.grok_mcp_oauth_refresh_token_ttl_days
            <= REFRESH_TOKEN_TTL_MAX_DAYS
        ):
            raise ValueError("GROK_MCP_OAUTH_REFRESH_TOKEN_TTL_DAYS is outside the allowed range")
        if not (
            AUTH_CODE_TTL_MIN_SECONDS
            <= self.grok_mcp_oauth_auth_code_ttl_seconds
            <= AUTH_CODE_TTL_MAX_SECONDS
        ):
            raise ValueError("GROK_MCP_OAUTH_AUTH_CODE_TTL_SECONDS is outside the allowed range")
        return self

    @cached_property
    def database_path(self) -> Path:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("v1 supports only sqlite:/// DATABASE_URL values")
        raw = self.database_url[len(prefix) :]
        path = Path(raw)
        return path if path.is_absolute() else Path.cwd() / path

    def require_runtime_configuration(self) -> None:
        required = {
            "OPENAI_API_KEY": self.openai_api_key,
            "OPENAI_WEBHOOK_SECRET": self.openai_webhook_secret,
            "OPENAI_PROJECT_ID": self.openai_project_id,
            "EXA_API_KEY": self.exa_api_key,
            "TWILIO_ACCOUNT_SID": self.twilio_account_sid,
            "TWILIO_AUTH_TOKEN": self.twilio_auth_token,
            "TWILIO_CALLER_ID": self.twilio_caller_id,
            "OWNER_PHONE_E164": self.owner_phone_e164,
            "ALLOWED_AGENT_USER_ID": self.allowed_agent_user_id,
            "MCP_BEARER_TOKEN": self.mcp_bearer_token,
            "DEBUG_API_TOKEN": self.debug_api_token,
            "DEPLOY_GUARD_TOKEN": self.deploy_guard_token,
            "PUBLIC_BASE_URL": self.public_base_url,
        }

        def is_missing(value: object) -> bool:
            if value is None:
                return True
            if isinstance(value, SecretStr):
                return not value.get_secret_value().strip()
            if isinstance(value, str):
                return not value.strip()
            return False

        if self.grok_mcp_oauth_enabled:
            required.update(
                {
                    "GROK_MCP_OAUTH_OWNER_SECRET_HASH": self.grok_mcp_oauth_owner_secret_hash,
                    "GROK_MCP_OAUTH_SIGNING_KEY": self.grok_mcp_oauth_signing_key,
                    "GROK_MCP_OAUTH_STORAGE_ENCRYPTION_KEY": (
                        self.grok_mcp_oauth_storage_encryption_key
                    ),
                }
            )

        missing = [name for name, value in required.items() if is_missing(value)]
        if missing:
            raise RuntimeError(f"missing required environment variables: {', '.join(missing)}")

    @staticmethod
    def reveal(value: SecretStr | None) -> str:
        if value is None:
            raise RuntimeError("required secret is not configured")
        return value.get_secret_value()
