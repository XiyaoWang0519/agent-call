from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.call_state import CallService
from app.db import Database
from app.models import (
    CallState,
    ContextPacket,
    EscalationContext,
    OwnerContext,
    StoredCallResult,
    TargetContext,
)
from app.settings import Settings
from app.twilio_bridge import ParticipantInfo


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        xai_api_key=SecretStr("xai-test"),
        xai_webhook_secret=SecretStr("whsec_" + base64.b64encode(b"test webhook secret").decode()),
        xai_sip_phone_number="+14155550199",
        xai_sip_auth_username="poke-call-test",
        xai_sip_auth_password=SecretStr("sip-test-password"),
        twilio_account_sid="AC" + "1" * 32,
        twilio_auth_token=SecretStr("twilio-test"),
        twilio_caller_id="+14155550199",
        owner_phone_e164="+14155550101",
        allowed_poke_user_id="poke-user-1",
        mcp_bearer_token=SecretStr("mcp-test"),
        debug_api_token=SecretStr("debug-test"),
        deploy_guard_token=SecretStr("deploy-guard-test"),
        public_base_url="https://example.test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        setup_deadline_seconds=60,
        watchdog_stale_seconds=15,
    )


@pytest.fixture
def packet() -> ContextPacket:
    return ContextPacket(
        owner=OwnerContext(
            display_name="Irvin",
            timezone="America/Los_Angeles",
            callback_number="+14155550101",
        ),
        target=TargetContext(
            name="Alex",
            organization="Example Clinic",
            phone="+14155550100",
        ),
        objective="Confirm the appointment time.",
        relevant_facts=["The requested date is 2026-07-20."],
        preferences=["Keep the call concise."],
        hard_constraints=["Do not change the appointment without confirmation."],
        allowed_commitments=["Confirm an existing appointment."],
        prohibited_actions=["Do not provide payment information."],
        escalation=EscalationContext(
            mode="transfer_to_owner",
            owner_phone="+14155550101",
        ),
    )


@pytest.fixture
async def database(settings: Settings):
    db = Database(settings.database_path)
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


class FakeTwilio:
    def __init__(self):
        self.completed: list[str | None] = []
        self.removed: list[tuple[str | None, str | None]] = []
        self.unmuted: list[tuple[str | None, str | None]] = []
        self.end_on_exit: list[tuple[str | None, str | None]] = []
        self.agent_creates = 0
        self.callee_creates = 0
        self.owner_creates = 0

    async def create_agent_participant(self, **kwargs) -> ParticipantInfo:
        self.agent_creates += 1
        return ParticipantInfo("CA" + "a" * 32, "CF" + "a" * 32)

    async def create_callee_participant(self, **kwargs) -> ParticipantInfo:
        self.callee_creates += 1
        return ParticipantInfo("CA" + "b" * 32, "CF" + "a" * 32)

    async def create_owner_participant(self, **kwargs) -> ParticipantInfo:
        self.owner_creates += 1
        return ParticipantInfo("CA" + "c" * 32, "CF" + "a" * 32)

    async def complete_conference(self, conference_sid_or_name) -> None:
        self.completed.append(conference_sid_or_name)

    async def remove_participant(self, conference_sid_or_name, participant_call_sid) -> None:
        self.removed.append((conference_sid_or_name, participant_call_sid))

    async def unmute_participant(self, conference_sid_or_name, participant_call_sid) -> None:
        self.unmuted.append((conference_sid_or_name, participant_call_sid))

    async def enable_end_conference_on_exit(
        self, conference_sid_or_name, participant_call_sid
    ) -> None:
        self.end_on_exit.append((conference_sid_or_name, participant_call_sid))


class FakeRealtime:
    def __init__(self):
        self.events: list[tuple[str, str]] = []
        self.initial_updates: list[str] = []
        self.hangups: list[str | None] = []
        self.rejects: list[str] = []
        self.closed: list[str] = []
        self.close_all_calls = 0
        self.tool_results: list[tuple[str, str, dict]] = []
        self.tool_result_continuations: list[bool] = []
        self.accepts: list[tuple[str, str]] = []
        self.update_event = {
            "type": "session.updated",
            "session": {
                "turn_detection": {
                    "type": "server_vad",
                    "silence_duration_ms": 700,
                    "prefix_padding_ms": 333,
                }
            },
        }
        self.initial_update_event = {
            "type": "session.updated",
            "session": {
                "audio": {
                    "input": {
                        "transcription": {"model": "grok-transcribe"},
                    }
                },
                "turn_detection": None,
            },
        }

    async def verify_initial_session(self, call_id: str):
        self.initial_updates.append(call_id)
        return self.initial_update_event

    async def enable_automatic_responses(self, call_id: str):
        self.events.append(("session.update", call_id))
        return self.update_event

    async def connect(self, *, call_id: str, xai_call_id: str, packet: ContextPacket) -> int:
        self.accepts.append((call_id, xai_call_id))
        return 101

    def activation_update_confirmed(self, event):
        turn = event["session"]["turn_detection"]
        return (
            turn["type"] == "server_vad"
            and turn["silence_duration_ms"] == 700
            and turn["prefix_padding_ms"] == 333
        )

    async def create_opening(self, call_id: str) -> None:
        self.events.append(("opening", call_id))

    async def cancel_response(self, call_id: str, response_id: str | None = None) -> None:
        self.events.append(("cancel_response", call_id))

    async def create_voicemail(self, call_id: str) -> None:
        self.events.append(("voicemail", call_id))

    async def hangup(self, xai_call_id: str | None) -> None:
        self.hangups.append(xai_call_id)

    async def reject(self, xai_call_id: str) -> None:
        self.rejects.append(xai_call_id)

    async def drain_and_close(self, call_id: str) -> None:
        self.closed.append(call_id)

    async def close_all(self) -> None:
        self.close_all_calls += 1

    async def close(self) -> None:
        self.close_all_calls += 1

    async def send_tool_result(
        self,
        call_id: str,
        tool_call_id: str,
        output: dict,
        *,
        continue_response: bool = True,
        continuation_instructions: str | None = None,
    ) -> None:
        self.tool_results.append((call_id, tool_call_id, output))
        self.tool_result_continuations.append(continue_response)
        if continuation_instructions:
            self.events.append(("tool_continuation", call_id))

    def expected_transcription_echoed(self, event) -> bool:
        transcription = (
            event.get("session", {}).get("audio", {}).get("input", {}).get("transcription", {})
        )
        return transcription.get("model") == "grok-transcribe"

    @staticmethod
    def expected_initial_vad_echoed(event) -> bool:
        return event.get("session", {}).get("turn_detection") is None


class FakeFinalizer:
    def __init__(self, db: Database):
        self.db = db
        self.states_seen: list[str] = []

    async def finalize(self, call_id: str) -> StoredCallResult:
        call = await self.db.get_call(call_id)
        self.states_seen.append(call["state"])
        transcript = await self.db.get_transcript(call_id)
        status = (
            call["state"]
            if call["state"] in {"completed", "transferred", "timed_out"}
            else "failed"
        )
        result = StoredCallResult(
            call_id=call_id,
            call_status=status,
            finalization_status="telephony_only",
            outcome="failed" if status == "failed" else "unknown",
            result_source="telephony_only",
            summary="fake result",
            answered_by=call.get("answered_by"),
            answer_handling=call.get("answer_handling"),
            transcript_complete=True,
            raw_transcript_available=True,
        )
        await self.db.save_result_with_transcript(call_id, result, transcript)
        return result


async def seed_call(
    db: Database,
    packet: ContextPacket,
    *,
    call_id: str = "call_test",
    state: CallState = CallState.PREWARMING,
    xai_call_id: str = "rtc_test",
) -> str:
    plan_id = f"plan_{call_id}"
    await db.create_plan(
        plan_id,
        packet.model_dump(mode="json"),
        "Owner explicitly requested the call",
        datetime.now(UTC) + timedelta(minutes=10),
    )
    claimed = await db.claim_plan_and_create_call(
        plan_id=plan_id,
        call_id=call_id,
        conference_name=f"conference_{call_id}",
        confirmation_text="Confirmed",
    )
    assert claimed
    # Every state past PREWARMING is only reachable in production after the callee has
    # answered, so default callee_joined accordingly. Callers testing the pre-answer
    # window keep the default PREWARMING state and set callee_joined explicitly.
    await db.update_call(
        call_id,
        state=state.value,
        conference_sid="CF" + "a" * 32,
        twilio_ai_call_sid="CA" + "a" * 32,
        twilio_callee_call_sid="CA" + "b" * 32,
        xai_call_id=xai_call_id,
        transcription_verified=1,
        vad_verified=1,
        callee_joined=int(state != CallState.PREWARMING),
    )
    return call_id


@pytest.fixture
async def service(settings: Settings):
    db = Database(settings.database_path)
    await db.initialize()
    twilio = FakeTwilio()
    placeholder_openai = SimpleNamespace()
    svc = CallService(settings, db, twilio=twilio, xai=placeholder_openai)
    realtime = FakeRealtime()
    finalizer = FakeFinalizer(db)
    svc.realtime = realtime
    svc.finalizer = finalizer
    svc._test_twilio = twilio
    svc._test_realtime = realtime
    svc._test_finalizer = finalizer
    yield svc
    try:
        await svc.stop()
    finally:
        await db.close()


async def wait_background() -> None:
    for _ in range(50):
        await asyncio.sleep(0.01)
