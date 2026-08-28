"""Fail-closed evaluation (dummy) profile constants and the live-call gate."""

from __future__ import annotations

import json

from app.models import ContextPacket, EscalationContext, OwnerContext, TargetContext

LIVE_CALLS_DISABLED_CODE = "live_calls_disabled"
LIVE_CALLS_DISABLED_MESSAGE = (
    "Live calls are disabled in evaluation mode. prepare_phone_call is available; "
    "start_phone_call cannot originate provider legs. Set AGENT_CALL_PROFILE=live "
    "with real credentials to place a call."
)

EVALUATION_PUBLIC_BASE_URL = "https://127.0.0.1"
EVALUATION_TWILIO_ACCOUNT_SID = "ACeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
EVALUATION_TWILIO_CALLER_ID = "+15550000000"
EVALUATION_OWNER_PHONE = "+15550000001"
EVALUATION_OPENAI_PROJECT_ID = "proj_evaluation"
EVALUATION_ALLOWED_AGENT_USER_ID = "evaluation-agent"
EVALUATION_MCP_BEARER = "evaluation-mcp-bearer"
EVALUATION_DEBUG_TOKEN = "evaluation-debug-token"
EVALUATION_DEPLOY_TOKEN = "evaluation-deploy-token"
EVALUATION_OPENAI_KEY = "sk-evaluation-not-a-real-key"
EVALUATION_OPENAI_WEBHOOK = "whsec_evaluation-not-a-real-secret"
EVALUATION_EXA_KEY = "exa-evaluation-not-a-real-key"
EVALUATION_TWILIO_AUTH = "evaluation-twilio-auth-token"

# Sample destination used only by doctor --prepare-only policy checks and smoke.
EVALUATION_TARGET_PHONE = "+15550000002"

EVALUATION_SECRET_FIELDS: tuple[tuple[str, str], ...] = (
    ("openai_api_key", EVALUATION_OPENAI_KEY),
    ("openai_webhook_secret", EVALUATION_OPENAI_WEBHOOK),
    ("exa_api_key", EVALUATION_EXA_KEY),
    ("twilio_auth_token", EVALUATION_TWILIO_AUTH),
    ("mcp_bearer_token", EVALUATION_MCP_BEARER),
    ("debug_api_token", EVALUATION_DEBUG_TOKEN),
    ("deploy_guard_token", EVALUATION_DEPLOY_TOKEN),
)
EVALUATION_STRING_FIELDS: tuple[tuple[str, str], ...] = (
    ("openai_project_id", EVALUATION_OPENAI_PROJECT_ID),
    ("twilio_account_sid", EVALUATION_TWILIO_ACCOUNT_SID),
    ("twilio_caller_id", EVALUATION_TWILIO_CALLER_ID),
    ("owner_phone_e164", EVALUATION_OWNER_PHONE),
    ("allowed_agent_user_id", EVALUATION_ALLOWED_AGENT_USER_ID),
    ("public_base_url", EVALUATION_PUBLIC_BASE_URL),
)

EVALUATION_PREPARE_OBJECTIVE = "Prepare-only evaluation; do not place a call"
EVALUATION_PREPARE_AUTHORITY = "Owner requested this prepare-only evaluation"


def live_calls_disabled_payload() -> dict[str, str]:
    return {"code": LIVE_CALLS_DISABLED_CODE, "message": LIVE_CALLS_DISABLED_MESSAGE}


def live_calls_disabled_error() -> ValueError:
    return ValueError(json.dumps(live_calls_disabled_payload()))


def evaluation_prepare_packet(
    *,
    owner_phone: str = EVALUATION_OWNER_PHONE,
    owner_display_name: str = "the owner",
    owner_timezone: str = "America/Los_Angeles",
    target_phone: str = EVALUATION_TARGET_PHONE,
) -> ContextPacket:
    return ContextPacket(
        owner=OwnerContext(
            display_name=owner_display_name,
            timezone=owner_timezone,
            callback_number=owner_phone,
        ),
        target=TargetContext(name="Evaluation target", phone=target_phone),
        objective=EVALUATION_PREPARE_OBJECTIVE,
        escalation=EscalationContext(mode="end_call", owner_phone=owner_phone),
    )


def evaluation_prepare_arguments(
    *,
    owner_phone: str = EVALUATION_OWNER_PHONE,
    owner_display_name: str = "the owner",
    owner_timezone: str = "America/Los_Angeles",
    target_phone: str = EVALUATION_TARGET_PHONE,
) -> dict[str, object]:
    packet = evaluation_prepare_packet(
        owner_phone=owner_phone,
        owner_display_name=owner_display_name,
        owner_timezone=owner_timezone,
        target_phone=target_phone,
    )
    return {
        "context": packet.model_dump(mode="json"),
        "authority_basis": EVALUATION_PREPARE_AUTHORITY,
        "requested_by_owner": True,
    }
