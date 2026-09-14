from __future__ import annotations

import asyncio
import base64
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher
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


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_network: opt a test out of the external-network guard (R01). ",
    )


class ExternalNetworkBlocked(RuntimeError):
    """Raised when a test tries to open a non-loopback TCP connection."""


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", ""})


def _address_is_local(address: object) -> bool:
    # AF_UNIX paths arrive as str/bytes and are process-local.
    if isinstance(address, (str, bytes)):
        return True
    if isinstance(address, tuple) and address:
        return address[0] in _LOOPBACK_HOSTS
    return False


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    # The suite is meant to be fully offline: provider and Twilio SDK calls are
    # faked, and HTTP is mocked at the transport level. A real outbound socket in
    # a test is therefore a bug (or an unmocked code path), not a slow test.
    if request.node.get_closest_marker("allow_network") is not None:
        return
    real_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: object) -> object:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _address_is_local(address):
            raise ExternalNetworkBlocked(
                f"test attempted a non-loopback connection to {address!r}; "
                "mock the transport or mark the test with @pytest.mark.allow_network"
            )
        return real_connect(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        agent_call_profile="live",
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
        allowed_agent_user_id="agent-user-1",
        mcp_bearer_token=SecretStr("mcp-test"),
        debug_api_token=SecretStr("debug-test"),
        deploy_guard_token=SecretStr("deploy-guard-test"),
        agent_webhook_url="https://hooks.example.test/hooks/agent",
        agent_webhook_token=SecretStr("agent-webhook-test"),
        public_base_url="https://example.test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        setup_deadline_seconds=60,
        watchdog_stale_seconds=15,
    )


MCP_OAUTH_OWNER_SECRET = "owner-secret-for-tests"
_MCP_OAUTH_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
MCP_OAUTH_OWNER_SECRET_HASH = _MCP_OAUTH_HASHER.hash(MCP_OAUTH_OWNER_SECRET)


@pytest.fixture
def oauth_settings(settings: Settings) -> Settings:
    values = settings.model_dump()
    values.update(
        {
            "mcp_oauth_enabled": True,
            "mcp_oauth_owner_secret_hash": SecretStr(MCP_OAUTH_OWNER_SECRET_HASH),
            "mcp_oauth_signing_key": SecretStr("s" * 64),
            "mcp_oauth_storage_encryption_key": SecretStr("e" * 64),
        }
    )
    return Settings(**values)


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

    async def callee_status(self, call_sid):
        return {"CallSid": call_sid, "CallStatus": "in-progress", "CallDuration": "0"}

    async def stop_audio_monitor(self, callee_call_sid, stream_sid):
        return None

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


class FakeLive:
    def __init__(self):
        self.events: list[tuple[str, str]] = []
        self.initial_updates: list[str] = []
        self.hangups: list[str | None] = []
        self.rejects: list[str] = []
        self.closed: list[str] = []
        self.close_all_calls = 0
        self.tool_results: list[tuple[str, str, dict]] = []
        self.resumed_calls: list[str] = []
        self.closing_checks: list[tuple[str, str]] = []
        self.tool_result_continuations: list[bool] = []
        self.tool_result_continuation_texts: list[str | None] = []
        self.tool_result_failures_remaining = 0
        self.accepts: list[tuple[str, str]] = []
        self.suspend_calls: list[str] = []
        self.suspend_failures_remaining = 0
        self.request_response_calls: list[tuple[str, str | None]] = []
        self.initial_update_event = {
            "type": "session.updated",
            "session": {
                "model": "gpt-live-1",
                "delegation": {
                    "type": "responses",
                    "responses": {"model": "gpt-5.6-terra", "parallel_tool_calls": False},
                },
            },
        }

    def session_configuration_confirmed(self, event):
        from app.openai_live import LiveBridge

        return LiveBridge.session_configuration_confirmed(
            SimpleNamespace(
                settings=SimpleNamespace(
                    live_model="gpt-live-1", live_backend_model="gpt-5.6-terra"
                )
            ),
            event,
        )

    async def enable_conversation(self, call_id):
        self.events.append(("session.instructions.append", call_id))

    async def suspend_conversation(self, call_id):
        if self.suspend_failures_remaining:
            self.suspend_failures_remaining -= 1
            raise RuntimeError("injected suspend failure")
        self.suspend_calls.append(call_id)

    async def verify_initial_session(self, call_id: str):
        self.initial_updates.append(call_id)
        return self.initial_update_event

    async def request_response(self, call_id: str, *, instructions: str | None = None) -> None:
        self.request_response_calls.append((call_id, instructions))
        self.events.append(("opening", call_id))

    async def review_closing(self, call_id: str, *, assistant_text: str = "") -> bool:
        self.events.append(("closing_review", call_id))
        return True

    async def accept_and_connect(
        self, *, call_id: str, openai_call_id: str, packet: ContextPacket
    ) -> int:
        self.accepts.append((call_id, openai_call_id))
        return 200

    async def create_voicemail(self, call_id: str) -> None:
        self.events.append(("voicemail", call_id))

    async def hangup(self, openai_call_id: str | None) -> None:
        self.hangups.append(openai_call_id)

    async def reject(self, openai_call_id: str) -> None:
        self.rejects.append(openai_call_id)

    async def drain_and_close(self, call_id: str, *, dependent_task=None) -> None:
        self.closed.append(call_id)

    async def close_all(self) -> None:
        self.close_all_calls += 1

    async def check_spoken_closing(self, call_id: str, response_id: str) -> None:
        self.closing_checks.append((call_id, response_id))

    async def notify_call_resumed(self, call_id: str) -> None:
        self.resumed_calls.append(call_id)

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
        # Fixtures seed historical/stranded rows directly and some tests need more
        # than one live row in one database; production admission goes through
        # CallService.start, which enforces the capacity policy.
        enforce_single_call_capacity=False,
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
        live_session_verified=1,
        callee_joined=int(state != CallState.PREWARMING),
    )
    return call_id


class TestHarness:
    """Explicit holder for the injected doubles and the service under test (R02).

    Tests build the service once through :meth:`build`; the real ``CallService``
    receives its Live/Finalizer/Telephony/Exa collaborators by constructor instead
    of having attributes swapped in after construction. Tests that need to see
    what a double recorded read it off ``harness.live`` (or the equivalent public
    attribute on the service).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        twilio: FakeTwilio,
        live: FakeLive,
        finalizer: FakeFinalizer,
        exa: FakeExa,
        service: CallService,
    ) -> None:
        self.settings = settings
        self.db = db
        self.twilio = twilio
        self.live = live
        self.finalizer = finalizer
        self.exa = exa
        self.service = service

    @classmethod
    async def build(cls, settings: Settings) -> TestHarness:
        db = Database(settings.database_path)
        await db.initialize()
        twilio = FakeTwilio()
        live = FakeLive()
        finalizer = FakeFinalizer(db)
        exa = FakeExa()
        service = CallService(
            settings,
            db,
            twilio=twilio,
            openai=SimpleNamespace(),
            exa=exa,
            live=live,
            finalizer=finalizer,
        )
        harness = cls(
            settings=settings,
            db=db,
            twilio=twilio,
            live=live,
            finalizer=finalizer,
            exa=exa,
            service=service,
        )

        async def start_audio_monitor(**kwargs):
            call_id = kwargs["call_id"]
            accepted = await service.accept_media_monitor(
                call_id,
                kwargs["plan_id"],
                {
                    "customParameters": {"token": kwargs["token"]},
                    "accountSid": settings.twilio_account_sid,
                    "callSid": kwargs["callee_call_sid"],
                    "mediaFormat": {
                        "encoding": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "channels": 1,
                    },
                    "tracks": ["inbound", "outbound"],
                    "streamSid": "MZ" + "d" * 32,
                },
            )
            assert accepted
            return "MZ" + "d" * 32

        twilio.start_audio_monitor = start_audio_monitor
        return harness

    async def aclose(self) -> None:
        try:
            await self.service.stop()
        finally:
            await self.db.close()


@pytest.fixture
async def harness(settings: Settings):
    built = await TestHarness.build(settings)
    try:
        yield built
    finally:
        await built.aclose()


@pytest.fixture
async def service(harness: TestHarness) -> CallService:
    return harness.service


async def wait_background() -> None:
    for _ in range(50):
        await asyncio.sleep(0.01)
