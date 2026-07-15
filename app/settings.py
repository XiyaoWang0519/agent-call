from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    xai_api_key: SecretStr | None = None
    xai_webhook_secret: SecretStr | None = None
    xai_sip_phone_number: str | None = None
    xai_sip_auth_username: str | None = None
    xai_sip_auth_password: SecretStr | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_caller_id: str | None = None
    twilio_http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    owner_phone_e164: str | None = None
    allowed_poke_user_id: str | None = None
    mcp_bearer_token: SecretStr | None = None
    debug_api_token: SecretStr | None = None
    deploy_guard_token: SecretStr | None = None
    poke_api_key: SecretStr | None = None
    poke_push_enabled: bool = False
    allowed_country_codes: list[str] = Field(default_factory=lambda: ["+1"])
    input_transcription_model: Literal["grok-transcribe"] = "grok-transcribe"
    server_vad_silence_duration_ms: int = Field(default=700, ge=0, le=10000)
    server_vad_prefix_padding_ms: int = Field(default=333, ge=0, le=10000)
    xai_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    xai_http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    xai_keepalive_expiry_seconds: float | None = Field(default=60.0, ge=5, le=300)
    xai_extraction_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    extractor_model: Literal["grok-4.3"] = "grok-4.3"
    database_url: str = "sqlite:///./poke_call.db"
    public_base_url: str | None = None
    realtime_model: Literal["grok-voice-think-fast-1.0"] = "grok-voice-think-fast-1.0"
    setup_deadline_seconds: Literal[60] = 60
    watchdog_stale_seconds: Literal[15] = 15
    plan_ttl_seconds: Literal[600] = 600
    max_call_seconds: Literal[600] = 600
    owner_timezone: str = "America/Los_Angeles"

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

    @field_validator("twilio_caller_id", "owner_phone_e164", "xai_sip_phone_number")
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

    @model_validator(mode="after")
    def validate_provider_configuration(self) -> Settings:
        if self.xai_connect_timeout_seconds > self.xai_http_timeout_seconds:
            raise ValueError("XAI_CONNECT_TIMEOUT_SECONDS cannot exceed XAI_HTTP_TIMEOUT_SECONDS")
        if (
            self.twilio_caller_id
            and self.xai_sip_phone_number
            and self.twilio_caller_id != self.xai_sip_phone_number
        ):
            raise ValueError("XAI_SIP_PHONE_NUMBER must match TWILIO_CALLER_ID")
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
            "XAI_API_KEY": self.xai_api_key,
            "XAI_WEBHOOK_SECRET": self.xai_webhook_secret,
            "XAI_SIP_PHONE_NUMBER": self.xai_sip_phone_number,
            "XAI_SIP_AUTH_USERNAME": self.xai_sip_auth_username,
            "XAI_SIP_AUTH_PASSWORD": self.xai_sip_auth_password,
            "TWILIO_ACCOUNT_SID": self.twilio_account_sid,
            "TWILIO_AUTH_TOKEN": self.twilio_auth_token,
            "TWILIO_CALLER_ID": self.twilio_caller_id,
            "OWNER_PHONE_E164": self.owner_phone_e164,
            "ALLOWED_POKE_USER_ID": self.allowed_poke_user_id,
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

        missing = [name for name, value in required.items() if is_missing(value)]
        if missing:
            raise RuntimeError(f"missing required environment variables: {', '.join(missing)}")

    @staticmethod
    def reveal(value: SecretStr | None) -> str:
        if value is None:
            raise RuntimeError("required secret is not configured")
        return value.get_secret_value()
