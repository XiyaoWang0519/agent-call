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
from app.exa_search import ExaSearchResult
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
        openai_api_key=SecretStr("sk-test"),
        openai_webhook_secret=SecretStr(
            "whsec_" + base64.b64encode(b"test webhook secret").decode()
        ),
        openai_project_id="proj_test",
        exa_api_key=SecretStr("exa-test"),
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
            display_name="the owner",
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
        self.dtmf: list[tuple[str, str, str]] = []
        self.dtmf_exc: Exception | None = None

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

    async def send_dtmf(
        self, conference_sid_or_name, participant_call_sid, *, call_id, plan_id, digits
    ) -> None:
        if self.dtmf_exc is not None:
            raise self.dtmf_exc
        self.dtmf.append((conference_sid_or_name, participant_call_sid, digits))


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
        self.tool_result_continuation_texts: list[str | None] = []
        self.tool_result_failures_remaining = 0
        self.accepts: list[tuple[str, str]] = []
        self.suspend_calls: list[str] = []
        self.suspend_failures_remaining = 0
        self.request_response_calls: list[tuple[str, str | None]] = []
        self.update_event = {
            "type": "session.updated",
            "session": {
                "audio": {
                    "input": {
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": "auto",
                            "create_response": True,
                            "interrupt_response": True,
                        }
                    }
                }
            },
        }
        self.initial_update_event = {
            "type": "session.updated",
            "session": {
                "audio": {
                    "input": {
                        "transcription": {"model": "gpt-realtime-whisper"},
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": "auto",
                            "create_response": False,
                            "interrupt_response": False,
                        },
                    }
                }
            },
        }

    async def verify_initial_session(self, call_id: str):
        self.initial_updates.append(call_id)
        return self.initial_update_event

    async def enable_automatic_responses(self, call_id: str):
        self.events.append(("session.update", call_id))
        return self.update_event

    async def suspend_automatic_responses(self, call_id: str):
        if self.suspend_failures_remaining > 0:
            self.suspend_failures_remaining -= 1
            raise RuntimeError("injected suspend failure")
        self.suspend_calls.append(call_id)
        self.events.append(("session.update", call_id))
        return self.initial_update_event

    async def request_response(self, call_id: str, *, instructions: str | None = None) -> None:
        self.request_response_calls.append((call_id, instructions))
        self.events.append(("opening", call_id))

    async def accept_and_connect(
        self, *, call_id: str, openai_call_id: str, packet: ContextPacket
    ) -> int:
        self.accepts.append((call_id, openai_call_id))
        return 200

    def activation_update_confirmed(self, event):
        turn = event["session"]["audio"]["input"]["turn_detection"]
        return (
            turn["type"] == "semantic_vad"
            and turn["eagerness"] == "auto"
            and turn["create_response"] is True
            and turn["interrupt_response"] is True
        )

    async def create_opening(self, call_id: str) -> None:
        self.events.append(("opening", call_id))

    async def cancel_response(self, call_id: str, response_id: str | None = None) -> None:
        self.events.append(("cancel_response", call_id))

    async def create_voicemail(self, call_id: str) -> None:
        self.events.append(("voicemail", call_id))

    async def hangup(self, openai_call_id: str | None) -> None:
        self.hangups.append(openai_call_id)

    async def reject(self, openai_call_id: str) -> None:
        self.rejects.append(openai_call_id)

    async def drain_and_close(self, call_id: str) -> None:
        self.closed.append(call_id)

    async def close_all(self) -> None:
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
        if self.tool_result_failures_remaining > 0:
            self.tool_result_failures_remaining -= 1
            raise RuntimeError("injected sideband send failure")
        self.tool_results.append((call_id, tool_call_id, output))
        self.tool_result_continuations.append(continue_response)
        self.tool_result_continuation_texts.append(continuation_instructions)
        if continuation_instructions:
            self.events.append(("tool_continuation", call_id))

    def expected_transcription_echoed(self, event) -> bool:
        transcription = (
            event.get("session", {}).get("audio", {}).get("input", {}).get("transcription", {})
        )
        return transcription.get("model") == "gpt-realtime-whisper"

    @staticmethod
    def expected_initial_vad_echoed(event) -> bool:
        turn = event.get("session", {}).get("audio", {}).get("input", {}).get("turn_detection", {})
        return (
            turn.get("type") == "semantic_vad"
            and turn.get("eagerness") == "auto"
            and turn.get("create_response") is False
            and turn.get("interrupt_response") is False
        )


class FakeExa:
    def __init__(self):
        self.queries: list[str] = []
        self.error: Exception | None = None
        self.result = ExaSearchResult(
            output={
                "ok": True,
                "results": [
                    {
                        "title": "Example result",
                        "url": "https://example.test/result",
                        "highlights": ["The requested fact is supported."],
                    }
                ],
            },
            request_id="exa_request_test",
            search_type="auto",
            result_count=1,
            output_bytes=180,
            cost_dollars=0.007,
        )

    async def search(self, query: str) -> ExaSearchResult:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.result


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
    openai_call_id: str = "rtc_test",
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
        openai_call_id=openai_call_id,
        transcription_verified=1,
        semantic_vad_verified=1,
        callee_joined=int(state != CallState.PREWARMING),
    )
    return call_id


@pytest.fixture
async def service(settings: Settings):
    db = Database(settings.database_path)
    await db.initialize()
    twilio = FakeTwilio()
    placeholder_openai = SimpleNamespace()
    exa = FakeExa()
    svc = CallService(settings, db, twilio=twilio, openai=placeholder_openai, exa=exa)
    realtime = FakeRealtime()
    finalizer = FakeFinalizer(db)
    svc.realtime = realtime
    svc.finalizer = finalizer
    svc._test_twilio = twilio
    svc._test_realtime = realtime
    svc._test_finalizer = finalizer
    svc._test_exa = exa
    yield svc
    try:
        await svc.stop()
    finally:
        await db.close()


async def wait_background() -> None:
    for _ in range(50):
        await asyncio.sleep(0.01)
