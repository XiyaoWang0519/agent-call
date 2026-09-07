from __future__ import annotations

from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import E164


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LIVE_TEST_", extra="ignore")

    public_url: str
    app_url: str
    instance_id: str = Field(min_length=16)
    token: SecretStr = Field(min_length=32)
    mcp_token: SecretStr
    agent_user_id: str
    debug_token: SecretStr
    twilio_account_sid: str = Field(pattern=r"^AC[0-9a-fA-F]{32}$")
    twilio_auth_token: SecretStr
    openai_api_key: SecretStr
    caller_number: E164
    callee_number: E164
    owner_number: E164
    artifacts: Path = Path(".live-phone")
    max_suite_seconds: int = Field(default=5400, ge=60, le=7200)
    tts_model: str = "gpt-4o-mini-tts"
    asr_model: str = "gpt-4o-mini-transcribe"
    voice: str = "alloy"

    @model_validator(mode="after")
    def validate_isolation(self) -> Self:
        for value in (self.public_url, self.app_url):
            url = urlsplit(value)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in ("", "/")
            ):
                raise ValueError("test URLs must be HTTPS origins without credentials")
        if self.public_url.rstrip("/") == self.app_url.rstrip("/"):
            raise ValueError("harness and application must have separate origins")
        if len({self.caller_number, self.callee_number, self.owner_number}) != 3:
            raise ValueError("caller, automated callee and automated owner must be distinct")
        return self
