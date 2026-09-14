from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from openai import AsyncOpenAI
from twilio.base.exceptions import TwilioRestException

from app.agent_push import push_message_to_agent
from app.call_activity import CallActivityTracker
from app.call_audio import CallAudio
from app.costs import compute_call_cost
from app.db import (
    ClaimOutcome,
    Database,
    DeploymentLockedError,
    LatencyMark,
    LatencyStage,
)
from app.evaluation import live_calls_disabled_error
from app.exa_search import ExaSearchClient, ExaSearchError
from app.finalizer import Finalizer
from app.logging_safety import provider_error_fields
from app.models import (
    TERMINAL_STATES,
    AdvisoryOutcome,
    AskAgentRequest,
    CallSnapshot,
    CallState,
    ContextPacket,
    PreparePhoneCallInput,
    PreparePhoneCallOutput,
    SendDtmfRequest,
    StartPhoneCallOutput,
    TranscriptTurn,
    VoiceEndCallRequest,
    WebSearchRequest,
)
from app.openai_client import create_openai_client
from app.openai_live import LIVE_SEND_TIMEOUT_SECONDS, LiveBridge
from app.owner_transfer import OwnerTransferCoordinator
from app.policy import validate_context
from app.settings import Settings
from app.twilio_bridge import ParticipantInfo, TwilioBridge

logger = logging.getLogger(__name__)

_CALL_ID_PATTERN = re.compile(r"^call_[A-Za-z0-9_-]{1,128}$")
_PLAN_ID_PATTERN = re.compile(r"^plan_[A-Za-z0-9_-]{1,128}$")

# Give the other person a turn after hearing our goodbye. Speech cancels this close.
VOICE_END_REPLY_GRACE_SECONDS = 3.0
TERMINATION_MEDIA_RETRY_DELAY_SECONDS = 0.1
TERMINATION_MEDIA_BACKGROUND_RETRY_BASE_SECONDS = 0.5
TERMINATION_MEDIA_BACKGROUND_RETRY_MAX_SECONDS = 15.0
# cancel_response + function_call_output each bound to LIVE_SEND_TIMEOUT_SECONDS; keep
# the stale-call carve-out long enough for both sends after the answer deadline fires.
WATCHDOG_QUESTION_GRACE_SECONDS = 2 * LIVE_SEND_TIMEOUT_SECONDS + 5.0
# A successful DTMF send is followed by deliberate silence while the IVR processes
# the tones. Bare beeps and silence produce no Realtime sideband frames, so give that
# wait its own bounded watchdog carve-out instead of treating it as a dead call.
POST_DTMF_LISTEN_GRACE_SECONDS = 30.0

# Watchdog cadence and the bounded backoff used after a failed supervisory pass.
# A failed pass marks the service not-ready for new calls but never aborts the
# loop or tears down an in-flight call.
WATCHDOG_INTERVAL_SECONDS = 5.0
WATCHDOG_MAX_BACKOFF_SECONDS = 30.0

# The MCP client is an LLM agent: it reliably follows explicit next_action directives
# embedded in tool RESULTS, but under-weights static tool descriptions. Optional agent
# push can wake OpenClaw, but polling via wait_for_call_event remains the canonical path
# (and the only path for clients without inbound webhooks), so every response for a
# non-terminal call must repeat this warning — otherwise a mid-call ask_agent question
# can arrive with nobody listening and time out.
ASK_AGENT_POLL_WARNING = (
    "Do not end your turn and do not stop polling until the call reaches a terminal "
    "state — if you stop, mid-call questions from the phone agent (ask_agent) will "
    "time out and the call may fail its objective. Prefer answering via "
    "answer_call_question; optional push is best-effort only."
)

# DetectMessageEnd can report machine_end_other for long IVR prompts as well as machines.
# A beep or greeting-ending silence is actionable voicemail evidence; "other" is not.
VOICEMAIL_AMD_RESULTS = frozenset({"machine_end_beep", "machine_end_silence"})


def _seconds_until(iso_timestamp: str | None) -> float | None:
    if not iso_timestamp:
        return None
    try:
        target = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return None
    return (target - datetime.now(UTC)).total_seconds()


@dataclass(slots=True)
class PendingQuestion:
    question_id: str
    tool_call_id: str
    deadline_monotonic: float
    delivering: bool = False


@dataclass(slots=True)
class LiveConversation:
    assistant_text: str = ""
    callee_text: str = ""
    output_revision: int = 0
    input_revision: int = 0
    closing_review_revision: int = -1
    closing_candidate_revision: int = 0
    assistant_revision_at_input: int = -1
    closing_review_running: bool = False
    first_audio_seen: bool = False
    # A backend response is task work, never evidence of a spoken reply.
    response_delegations: dict[str, str] = field(default_factory=dict)
    response_epochs: dict[str, int] = field(default_factory=dict)
    delegation_epochs: dict[str, int] = field(default_factory=dict)
    seen_tools: set[str] = field(default_factory=set)


@dataclass(slots=True)
class HoldState:
    started_monotonic: float


class CallService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        twilio: TwilioBridge | None = None,
        openai: AsyncOpenAI | None = None,
        exa: ExaSearchClient | None = None,
        live: LiveBridge | None = None,
        finalizer: Finalizer | None = None,
    ):
        self.settings = settings
        self.db = db
        self._owns_openai_client = openai is None
        self.openai = openai if openai is not None else create_openai_client(settings)
        self._owns_exa_client = exa is None
        self.exa = exa if exa is not None else ExaSearchClient(settings)
        self.twilio = twilio or TwilioBridge(settings)
        self._activity = CallActivityTracker(
            self.db,
            is_closing=lambda call_id: call_id in self._voice_end_reply_waits,
            clear_closing=self._cancel_voice_end,
        )
        # Live and finalization collaborators are constructor-injected so tests and
        # future composition roots can supply narrow fakes without monkeypatching
        # attributes on a fully built service. They stay plain attributes because
        # a few late-bound callbacks (owner transfer) read them on the current
        # service instance.
        self.live = (
            live
            if live is not None
            else LiveBridge(
                settings,
                self.openai,
                on_event=lambda call_id, event: self.handle_live_event(
                    call_id, event, _observed=True
                ),
                on_finalized=self._record_final_live_usage,
                on_open=self.handle_sideband_open,
                on_fatal=self._handle_sideband_fatal,
                on_send=self.handle_live_send,
                on_activity=self._note_call_activity,
                on_observe=self._observe_live_event,
            )
        )
        self.finalizer = (
            finalizer if finalizer is not None else Finalizer(settings, db, self.openai)
        )
        self._background: set[asyncio.Task[Any]] = set()
        self._must_finish_background: set[asyncio.Task[Any]] = set()
        self._conference_retry_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._stopping = False
        self._owner_transfer = OwnerTransferCoordinator(
            self.db,
            self.twilio,
            live=lambda: self.live,
            is_stopping=lambda: self._stopping,
            spawn=lambda coro, **kwargs: self._spawn(coro, **kwargs),
            await_network_task=lambda task, **kwargs: self._await_network_task(task, **kwargs),
            finish_claimed_termination=(
                lambda call, reason, **kwargs: self._finish_claimed_termination(
                    call, reason, **kwargs
                )
            ),
            complete_conference_or_schedule=(
                lambda call: self._complete_conference_or_schedule(call)
            ),
            terminate_call=lambda call_id, reason, **kwargs: self.terminate_call(
                call_id, reason, **kwargs
            ),
        )
        self._activation_locks: dict[str, asyncio.Lock] = {}
        self._voice_end_pending: dict[str, tuple[str, str | None]] = {}
        self._voice_end_reply_waits: dict[str, object] = {}
        self._callee_speech_epochs: dict[str, int] = {}
        self._callee_speaking: set[str] = set()
        self._live_conversations: dict[str, LiveConversation] = {}
        self._call_audio: dict[str, CallAudio] = {}
        self._media_tokens: dict[str, str] = {}
        self._media_ready: dict[str, asyncio.Event] = {}
        self._tool_seen_calls: set[str] = set()
        self._queued_latency_events: dict[tuple[str, LatencyStage, str], LatencyMark] = {}
        self._watchdog_task: asyncio.Task[None] | None = None
        # Supervision is healthy until a watchdog pass fails. The flag gates new
        # call admission and /readyz; it never aborts an in-flight call.
        self._supervision_healthy = True
        self._watchdog_failures = 0
        self._watchdog_last_success_monotonic: float | None = None
        self._pending_questions: dict[str, PendingQuestion] = {}
        self._hold_state: dict[str, HoldState] = {}
        # Answered questions whose tool result never reached the sideband; an agent retry
        # of answer_call_question re-attempts delivery instead of reporting already_answered.
        self._undelivered_answers: dict[str, set[str]] = {}
        self._event_notifiers: dict[str, asyncio.Event] = {}

    def _spawn(
        self,
        coroutine: Any,
        *,
        name: str,
        must_finish: bool = False,
    ) -> asyncio.Task[Any]:
        if self._stopping and not must_finish:
            # Reject new ordinary work once shutdown starts, but still register a
            # no-op task so callers holding the returned Task cannot outlive the
            # stable background drain.
            coroutine.close()

            async def skipped_during_shutdown() -> None:
                return None

            coroutine = skipped_during_shutdown()
        task = asyncio.create_task(coroutine, name=name)
        self._background.add(task)
        if must_finish:
            self._must_finish_background.add(task)

        def finished(completed: asyncio.Task[Any]) -> None:
            self._background.discard(completed)
            self._must_finish_background.discard(completed)
            if completed.cancelled():
                return
            # Some callers (e.g. owner transfer) attach their own logging callback too;
            # a duplicate log line there is acceptable so every fire-and-forget failure
            # is guaranteed to surface instead of only appearing as a GC-time warning.
            error = completed.exception()
            if error is not None:
                logger.error(
                    "background task failed name=%s",
                    completed.get_name(),
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(finished)
        return task

    def _activation_lock(self, call_id: str) -> asyncio.Lock:
        return self._activation_locks.setdefault(call_id, asyncio.Lock())

    # -- Owner-transfer delegators -------------------------------------------
    # Kept under their original names because internal call sites elsewhere in
    # this class (webhook handling, transfer locking) still call them under
    # these names; they just forward to the coordinator that owns the actual
    # state. Not called or patched directly by tests.

    def _owner_transfer_lock(self, call_id: str) -> asyncio.Lock:
        return self._owner_transfer.lock(call_id)

    def _record_owner_join(self, call_id: str, call_sid: str | None) -> None:
        self._owner_transfer.record_owner_join(call_id, call_sid)

    def _record_owner_failure(self, call_id: str, call_sid: str | None, reason: str) -> None:
        self._owner_transfer.record_owner_failure(call_id, call_sid, reason)

    # -- Legacy owner-transfer attribute properties --------------------------
    # Forward to the coordinator so external code (mainly tests) that reads or
    # mutates-in-place these dict attributes on the service keeps working.

    @property
    def _owner_join_events(self) -> dict[str, asyncio.Event]:
        return self._owner_transfer.join_events

    @property
    def _owner_departure_events(self) -> dict[str, asyncio.Event]:
        return self._owner_transfer.departure_events

    @property
    def _owner_expected_sids(self) -> dict[str, str]:
        return self._owner_transfer.expected_sids

    @property
    def _owner_joined_sids(self) -> dict[str, str | None]:
        return self._owner_transfer.joined_sids

    @property
    def _owner_failures(self) -> dict[str, tuple[str | None, str]]:
        return self._owner_transfer.failures

    @property
    def _owner_transfer_tasks(self) -> dict[str, asyncio.Task[dict[str, Any]]]:
        return self._owner_transfer.tasks

    @property
    def _owner_transfer_locks(self) -> dict[str, asyncio.Lock]:
        return self._owner_transfer.locks

    # -- Legacy call-activity delegators -----------------------------------
    # Kept under their original names because LiveBridge wiring
    # (on_activity=self._note_call_activity) and tests bind/patch these
    # directly. Internal call sites in this class use self._activity.<method>
    # instead of routing back through these.

    def _note_call_activity(self, call_id: str, mark: LatencyMark | None = None) -> bool:
        return self._activity.note(call_id, mark)

    def _assistant_work_is_live(self, call_id: str) -> bool:
        return self._activity.assistant_work_is_live(call_id)

    def _clear_assistant_work(self, call_id: str) -> None:
        self._activity.clear_assistant_work(call_id)

    def _clear_call_activity(self, call_id: str) -> None:
        self._activity.clear(call_id)

    def _tombstone_call_activity(self, call_id: str) -> None:
        self._activity.tombstone(call_id)
        self._undelivered_answers.pop(call_id, None)
        self._hold_state.pop(call_id, None)

    def _prune_activity_tombstones(self, now_ns: int | None = None) -> None:
        self._activity.prune_tombstones(now_ns)

    @staticmethod
    def _call_activity_is_closed(call: dict[str, Any]) -> bool:
        return CallActivityTracker.is_closed(call)

    async def _flush_call_activity(self) -> None:
        await self._activity.flush()

    # -- Legacy call-activity attribute properties ---------------------------
    # Forward to the tracker so external code (mainly tests) that reads or
    # mutates-in-place these dict/set attributes on the service keeps working.

    @property
    def _latest_call_activity(self) -> dict[str, LatencyMark]:
        return self._activity.latest

    @property
    def _dirty_call_activity(self) -> dict[str, LatencyMark]:
        return self._activity.dirty

    @property
    def _watchdog_claims(self) -> set[str]:
        return self._activity.watchdog_claims

    @property
    def _activity_tombstones(self) -> OrderedDict[str, int]:
        return self._activity.tombstones

    @property
    def _active_response_ids(self) -> dict[str, str | None]:
        return self._activity.active_response_ids

    @property
    def _sip_output_playing(self) -> set[str]:
        return self._activity.sip_output_playing

    @property
    def _inflight_tools(self) -> set[str]:
        return self._activity.inflight_tools

    @property
    def _dtmf_listen_deadlines_ns(self) -> dict[str, int]:
        return self._activity.dtmf_listen_deadlines_ns

    async def _record_latency(
        self,
        call_id: str,
        *events: tuple[LatencyStage, LatencyMark, str],
    ) -> None:
        try:
            await self.db.record_latency_events(call_id, events)
        except Exception:
            # Losing telemetry is preferable to changing the outcome of a live call.
            logger.warning(
                "failed to persist call latency event call_id=%s", call_id, exc_info=True
            )

    def _queue_latency_batch(
        self,
        call_id: str,
        *events: tuple[LatencyStage, LatencyMark, str],
    ) -> None:
        pending: list[tuple[LatencyStage, LatencyMark, str]] = []
        for stage, mark, event_key in events:
            key = (call_id, stage, event_key)
            previous = self._queued_latency_events.get(key)
            if previous is not None and previous.monotonic_ns <= mark.monotonic_ns:
                continue
            self._queued_latency_events[key] = mark
            pending.append((stage, mark, event_key))
        if not pending:
            return
        self._spawn(
            self._record_latency(call_id, *pending),
            name=f"latency:{call_id}:{pending[0][0].value}",
        )

    def _queue_latency(
        self,
        call_id: str,
        stage: LatencyStage,
        mark: LatencyMark,
        *,
        event_key: str = "",
    ) -> None:
        self._queue_latency_batch(call_id, (stage, mark, event_key))

    @staticmethod
    def _confirmation_summary(packet: ContextPacket) -> str:
        target = packet.target
        return (
            f"Call {target.name}"
            f"{f' at {target.organization}' if target.organization else ''} "
            f"on {target.phone} to: {packet.objective}. "
            "The assistant may only make the listed allowed commitments and will follow all "
            "hard constraints."
        )

    async def prepare(self, request: PreparePhoneCallInput) -> PreparePhoneCallOutput:
        missing: list[str] = []
        if not request.requested_by_owner:
            missing.append("requested_by_owner")
        if not request.authority_basis:
            missing.append("authority_basis")
        errors = validate_context(request.context, self.settings)
        if errors:
            first = errors[0]
            raise ValueError(
                json.dumps(
                    {
                        "code": first.code,
                        "message": first.message,
                        "details": first.details,
                    }
                )
            )
        summary = self._confirmation_summary(request.context)
        if missing:
            return PreparePhoneCallOutput(
                confirmation_summary=summary,
                missing_fields=missing,
            )
        plan_id = f"plan_{secrets.token_urlsafe(18)}"
        expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.plan_ttl_seconds)
        await self.db.create_plan(
            plan_id,
            request.context.model_dump(mode="json"),
            request.authority_basis,
            expires_at,
        )
        return PreparePhoneCallOutput(
            plan_id=plan_id,
            confirmation_summary=summary,
            missing_fields=[],
            expires_at=expires_at,
        )

    async def _run_latency_marked_step(
        self,
        call_id: str,
        step: Callable[[], Awaitable[Any]],
        *,
        request_stage: LatencyStage,
        completed_stage: LatencyStage,
        failure_log_message: str,
        failure_reason: str,
        swallow_failure: bool = False,
    ) -> Any:
        """Bound an external call with a REQUEST/CREATED latency mark pair.

        On failure, queue only the REQUEST mark, log, and terminate the call.
        By default the failure re-raises so the caller's own error handling
        (e.g. surfacing a 5xx) still applies unchanged; with ``swallow_failure``
        the step returns None instead and the caller is expected to bail out.
        """
        request_mark = LatencyMark.now()
        try:
            result = await step()
        except Exception:
            self._queue_latency_batch(call_id, (request_stage, request_mark, ""))
            logger.exception(failure_log_message)
            await self.terminate_call(call_id, failure_reason)
            if swallow_failure:
                return None
            raise
        completed_mark = LatencyMark.now()
        self._queue_latency_batch(
            call_id,
            (request_stage, request_mark, ""),
            (completed_stage, completed_mark, ""),
        )
        return result

    async def _run_cancellation_safe_create(
        self,
        call_id: str,
        step: Callable[[], Coroutine[Any, Any, Any]],
        *,
        operation: str,
        request_stage: LatencyStage | None,
        completed_stage: LatencyStage | None,
        failure_log_message: str,
        failure_reason: str,
        on_late_success: Callable[[Any], Awaitable[None]],
        swallow_failure: bool = False,
        terminate_on_failure: bool = True,
    ) -> Any | None:
        """Run an external create that may outlive a caller cancellation.

        Twilio's SDK call executes in a worker thread, so cancelling the awaiting
        coroutine does not cancel the remote create. This wraps the create in its
        own task and shields it: on cancellation we wait for the task to settle,
        recover a late SID, and hand it to a must-finish compensation callback
        before re-raising, instead of reporting an unknown result as a failure and
        retrying (which would create a second billable leg). A definite create
        error is still treated as a failure and terminates the call.
        """
        request_mark = LatencyMark.now()
        task: asyncio.Task[Any] = asyncio.create_task(step(), name=f"{operation}:{call_id}")
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if request_stage is not None:
                self._queue_latency_batch(call_id, (request_stage, request_mark, ""))
            late = await self._settle_late_create(task)
            if late is not None:
                compensation = self._spawn(
                    on_late_success(late),
                    name=f"compensate-{operation}:{call_id}",
                    must_finish=True,
                )
                await self._await_network_task(compensation)
            raise
        except Exception:
            if request_stage is not None:
                self._queue_latency_batch(call_id, (request_stage, request_mark, ""))
            logger.exception(failure_log_message)
            if terminate_on_failure:
                await self.terminate_call(call_id, failure_reason)
            if swallow_failure:
                return None
            raise
        if request_stage is not None and completed_stage is not None:
            completed_mark = LatencyMark.now()
            self._queue_latency_batch(
                call_id,
                (request_stage, request_mark, ""),
                (completed_stage, completed_mark, ""),
            )
        return result

    @staticmethod
    async def _settle_late_create(task: asyncio.Task[Any]) -> Any | None:
        """Resolve a shielded external create after the caller was cancelled.

        Returns the created value when the remote call succeeded, or None when it
        failed definitively (nothing to compensate).
        """
        try:
            return await CallService._await_network_task(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("late remote create failed after caller cancellation", exc_info=True)
            return None

    @staticmethod
    def _participant_updates(kind: str, participant: ParticipantInfo) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if kind == "agent":
            updates["twilio_ai_call_sid"] = participant.call_sid
        else:
            updates["twilio_callee_call_sid"] = participant.call_sid
        if participant.conference_sid:
            updates["conference_sid"] = participant.conference_sid
        return updates

    async def _compensate_late_participant(
        self,
        call_id: str,
        reason: str,
        updates: dict[str, Any],
    ) -> None:
        """Persist a late participant SID and terminate so it cannot keep billing."""
        try:
            await self.db.update_call(call_id, **updates)
        except Exception:
            logger.exception("failed to persist late participant call_id=%s", call_id)
        # Pass the compensation task explicitly so teardown still runs during
        # shutdown (terminate_call otherwise refuses work once _stopping is set).
        terminated = await self.terminate_call(
            call_id, reason, _initiating_task=asyncio.current_task()
        )
        if terminated:
            return
        # Termination lost the claim (already terminal or another teardown owns it).
        # Completing the conference is the DB-independent guarantee that the late
        # leg cannot keep billing; a successful transfer is the one case that must
        # keep its owner-callee conference.
        current = await self.db.get_call(call_id)
        if current is None or CallState(current["state"]) is CallState.TRANSFERRED:
            return
        await self._complete_conference_or_schedule(current)

    async def _persist_late_media_stream(self, call_id: str, stream_sid: Any) -> None:
        if not isinstance(stream_sid, str):
            return
        try:
            await self.db.update_call(call_id, media_stream_sid=stream_sid)
        except Exception:
            logger.exception("failed to persist late media stream call_id=%s", call_id)

    async def start(
        self, plan_id: str, *, explicit_confirmation: bool, confirmation_text: str
    ) -> StartPhoneCallOutput:
        if not self.settings.live_calls_enabled:
            raise live_calls_disabled_error()
        if not self.supervision_ready():
            raise ValueError(
                json.dumps(
                    {
                        "code": "supervision_unavailable",
                        "message": (
                            "Call supervision is not healthy; retry after readiness recovers"
                        ),
                    }
                )
            )
        if not explicit_confirmation or not confirmation_text.strip():
            raise ValueError(
                json.dumps(
                    {
                        "code": "confirmation_required",
                        "message": "Explicit confirmation and the read-back confirmation text are required",
                    }
                )
            )
        plan = await self.db.get_plan(plan_id)
        if plan is None:
            raise ValueError(json.dumps({"code": "plan_not_found", "message": "Unknown plan_id"}))
        packet = ContextPacket.model_validate(plan["context"])
        if confirmation_text.strip() != self._confirmation_summary(packet):
            raise ValueError(
                json.dumps(
                    {
                        "code": "confirmation_mismatch",
                        "message": "Confirmation text must exactly match the prepared read-back summary",
                    }
                )
            )
        call_id = f"call_{secrets.token_urlsafe(18)}"
        conference_name = f"agent-{secrets.token_hex(16)}"
        try:
            claimed = await self.db.claim_plan_and_create_call(
                plan_id=plan_id,
                call_id=call_id,
                conference_name=conference_name,
                confirmation_text=confirmation_text,
            )
        except DeploymentLockedError as exc:
            raise ValueError(
                json.dumps(
                    {
                        "code": "deployment_in_progress",
                        "message": "A deployment is starting; retry the confirmed call shortly",
                    }
                )
            ) from exc
        if not claimed:
            if claimed.outcome is ClaimOutcome.BUSY:
                raise ValueError(
                    json.dumps(
                        {
                            "code": "call_busy",
                            "message": (
                                "Another call is already in progress; wait for it to finish "
                                "before starting a new call"
                            ),
                        }
                    )
                )
            if claimed.outcome is ClaimOutcome.CALL_ENDING:
                raise ValueError(
                    json.dumps(
                        {
                            "code": "call_ending",
                            "message": (
                                "The current call is still shutting down; retry the confirmed "
                                "call shortly"
                            ),
                        }
                    )
                )
            raise ValueError(
                json.dumps(
                    {"code": "plan_unavailable", "message": "Plan is expired or already started"}
                )
            )
        participant = await self._run_cancellation_safe_create(
            call_id,
            lambda: self.twilio.create_agent_participant(
                call_id=call_id,
                plan_id=plan_id,
                conference_name=conference_name,
            ),
            operation="create-agent-participant",
            request_stage=LatencyStage.TWILIO_AGENT_REQUEST,
            completed_stage=LatencyStage.TWILIO_AGENT_CREATED,
            failure_log_message="failed to originate agent leg",
            failure_reason="agent_leg_setup_failed",
            on_late_success=lambda late: self._compensate_late_participant(
                call_id,
                "agent_leg_setup_failed",
                self._participant_updates("agent", late),
            ),
        )
        if participant is None:  # pragma: no cover - swallow_failure is False here
            raise RuntimeError("agent participant creation returned no participant")
        try:
            await self.db.update_call(
                call_id,
                twilio_ai_call_sid=participant.call_sid,
                conference_sid=participant.conference_sid,
            )
        except Exception:
            logger.exception("failed to persist originated agent leg")
            await self.terminate_call(call_id, "agent_leg_setup_failed")
            raise
        self._spawn(self._setup_deadline(call_id), name=f"setup-deadline:{call_id}")
        return StartPhoneCallOutput(call_id=call_id, state=CallState.PREWARMING)

    async def handle_openai_incoming(
        self, openai_call_id: str, sip_headers: list[dict[str, str]]
    ) -> str:
        def exact_header(name: str) -> str | None:
            values = [
                item.get("value", "").strip()
                for item in sip_headers
                if item.get("name", "").strip().lower() == name
            ]
            return values[0] if len(values) == 1 else None

        call_id = exact_header("x-bridge-call-id")
        plan_id = exact_header("x-plan-id")
        if (
            call_id is None
            or plan_id is None
            or not _CALL_ID_PATTERN.fullmatch(call_id)
            or not _PLAN_ID_PATTERN.fullmatch(plan_id)
        ):
            await self.live.reject(openai_call_id)
            raise LookupError("incoming SIP call could not be mapped to an approved plan")

        if not await self.db.bind_openai_call(
            call_id=call_id,
            plan_id=plan_id,
            openai_call_id=openai_call_id,
        ):
            call = await self.db.get_call(call_id)
            await self.live.reject(openai_call_id)
            if call is not None and call.get("openai_call_id"):
                raise RuntimeError("call already mapped to an OpenAI call")
            raise LookupError("incoming SIP call could not be mapped to an approved plan")

        call = await self.db.get_call(call_id)
        if call is None:
            await self.live.reject(openai_call_id)
            raise LookupError("incoming SIP call could not be mapped to an approved plan")
        mapped_call_id = call_id
        plan = await self.db.get_plan(call["plan_id"])
        if plan is None:
            await self.live.reject(openai_call_id)
            raise LookupError("incoming SIP call plan is missing")
        packet = ContextPacket.model_validate(plan["context"])
        accept_status = await self._run_latency_marked_step(
            mapped_call_id,
            lambda: self.live.accept_and_connect(
                call_id=mapped_call_id,
                openai_call_id=openai_call_id,
                packet=packet,
            ),
            request_stage=LatencyStage.OPENAI_ACCEPT_REQUEST,
            completed_stage=LatencyStage.OPENAI_ACCEPT_COMPLETED,
            failure_log_message="failed to accept OpenAI call",
            failure_reason="openai_accept_failed",
        )
        try:
            await self.db.update_call(mapped_call_id, openai_accept_status=accept_status)
        except Exception:
            logger.exception("failed to persist OpenAI accept status")
            await self.terminate_call(mapped_call_id, "openai_accept_failed")
            raise
        return mapped_call_id

    async def handle_sideband_open(self, call_id: str) -> None:
        sideband_open = LatencyMark.now()
        await self.db.set_flag_once(call_id, "sideband_open")
        try:
            updated = await self.live.verify_initial_session(call_id)
        except Exception:
            self._queue_latency_batch(
                call_id,
                (LatencyStage.SIDEBAND_OPEN, sideband_open, ""),
            )
            await self.terminate_call(call_id, "initial_session_update_timeout")
            return
        initial_session_ack = LatencyMark.now()
        self._queue_latency_batch(
            call_id,
            (LatencyStage.SIDEBAND_OPEN, sideband_open, ""),
            (LatencyStage.INITIAL_SESSION_ACK, initial_session_ack, ""),
        )
        configuration_ok = self.live.session_configuration_confirmed(updated)
        await self.db.update_call(
            call_id,
            live_session_verified=int(configuration_ok),
        )
        if not configuration_ok:
            await self.terminate_call(call_id, "live_session_config_mismatch")
            return
        call = await self.db.get_call(call_id)
        if call is not None and await self.db.claim_callee_dial(call_id):
            plan = await self.db.get_plan(call["plan_id"])
            if plan is None:
                await self.terminate_call(call_id, "plan_missing_after_sideband_open")
                return
            packet = ContextPacket.model_validate(plan["context"])
            participant = await self._run_cancellation_safe_create(
                call_id,
                lambda: self.twilio.create_callee_participant(
                    call_id=call_id,
                    plan_id=call["plan_id"],
                    conference_sid_or_name=call.get("conference_sid") or call["conference_name"],
                    packet=packet,
                ),
                operation="create-callee-participant",
                request_stage=LatencyStage.TWILIO_CALLEE_REQUEST,
                completed_stage=LatencyStage.TWILIO_CALLEE_CREATED,
                failure_log_message="failed to originate callee leg",
                failure_reason="callee_leg_setup_failed",
                on_late_success=lambda late: self._compensate_late_participant(
                    call_id,
                    "callee_leg_setup_failed",
                    self._participant_updates("callee", late),
                ),
                swallow_failure=True,
            )
            if participant is None:
                return
            try:
                await self.db.update_call(
                    call_id,
                    twilio_callee_call_sid=participant.call_sid,
                    conference_sid=participant.conference_sid or call.get("conference_sid"),
                )
            except Exception:
                logger.exception("failed to persist originated callee leg")
                await self.terminate_call(call_id, "callee_leg_setup_failed")
                return
        await self._check_activation_gate(call_id)

    async def _admit_webhook_call(
        self, call_id: str, received: LatencyMark
    ) -> dict[str, Any] | None:
        """Shared guard for Twilio webhook handlers: note activity, load the call, and
        bail out (clearing or tombstoning activity as appropriate) if the call is gone,
        already closed, or activity lost the liveness race. Returns the call row to
        keep processing, or None when the caller should return immediately."""
        if not self._activity.note(call_id, received):
            return None
        call = await self.db.get_call(call_id)
        if call is None:
            self._activity.clear(call_id)
            return None
        if self._activity.is_closed(call):
            self._activity.tombstone(call_id)
            return None
        if not self._activity.note(call_id, received):
            return None
        return call

    async def handle_amd(self, call_id: str, answered_by: str) -> None:
        received = LatencyMark.now()
        if await self._admit_webhook_call(call_id, received) is None:
            return
        normalized = (answered_by or "unknown").lower()
        if normalized == "fax":
            handling = "fax"
        elif normalized in VOICEMAIL_AMD_RESULTS:
            handling = "voicemail"
        elif normalized == "human":
            handling = "human"
        else:
            handling = "assumed_human"
            normalized = answered_by or "unknown"
        if not await self.db.set_amd_once(call_id, normalized, handling):
            return
        if handling == "fax":
            await self.terminate_call(call_id, "fax_detected")
            return
        await self._check_activation_gate(call_id)
        if handling == "voicemail":
            await self._send_voicemail(call_id)

    async def handle_conference_event(self, call_id: str, form: dict[str, str]) -> None:
        received = LatencyMark.now()
        event = (form.get("StatusCallbackEvent") or form.get("ConferenceStatus") or "").lower()
        label = (form.get("ParticipantLabel") or form.get("Label") or "").lower()
        call_sid = form.get("CallSid") or form.get("ParticipantCallSid")
        if label == "owner":
            if event in {"participant-join", "join"}:
                self._record_owner_join(call_id, call_sid)
            elif event in {"participant-leave", "leave"}:
                self._record_owner_failure(call_id, call_sid, "owner_left")
        call = await self._admit_webhook_call(call_id, received)
        if call is None:
            return
        if event in {"conference-start", "start"}:
            # Twilio fires conference-start as soon as the agent SIP leg enters the
            # REST-created conference, while the callee is still ringing. It is not
            # evidence the callee answered, so it must not mark the callee joined or
            # trigger the opening turn; the callee's own participant-join event and
            # answered status callback do that instead.
            await self.db.touch_call(call_id)
        elif event in {"participant-join", "join"}:
            if label == "callee" or (call_sid and call_sid == call.get("twilio_callee_call_sid")):
                answered = received
                try:
                    await self.db.set_flag_once(call_id, "callee_joined")
                    if not call.get("answered_at"):
                        await self.db.update_call(call_id, answered_at=answered.occurred_at)
                    await self._check_activation_gate(call_id)
                finally:
                    self._queue_latency_batch(
                        call_id,
                        (LatencyStage.CALLEE_ANSWERED, answered, ""),
                    )
        elif event in {"participant-leave", "leave"}:
            if label == "callee" or (call_sid and call_sid == call.get("twilio_callee_call_sid")):
                await self.terminate_call(call_id, "callee_participant_leave")
        elif event in {"conference-end", "end"}:
            await self.terminate_call(call_id, "conference_end")
        else:
            await self.db.touch_call(call_id)

    async def handle_participant_status(self, call_id: str, leg: str, form: dict[str, str]) -> None:
        received = LatencyMark.now()
        status = (form.get("CallStatus") or "").lower()
        participant_sid = form.get("CallSid") or form.get("ParticipantCallSid")
        if leg == "owner" and status in {
            "completed",
            "failed",
            "busy",
            "no-answer",
            "canceled",
        }:
            self._record_owner_failure(call_id, participant_sid, f"owner_{status}")
        if await self._admit_webhook_call(call_id, received) is None:
            return
        await self.db.touch_call(call_id)
        if leg == "callee" and status in {"in-progress", "answered"}:
            # The callee's answered status callback usually reaches us before the
            # conference participant-join event; whichever arrives first enables
            # natural turn-taking and starts the brief silence fallback timer.
            answered = received
            try:
                if await self.db.set_flag_once(call_id, "callee_joined"):
                    await self.db.update_call(call_id, answered_at=answered.occurred_at)
                await self._check_activation_gate(call_id)
            finally:
                self._queue_latency_batch(
                    call_id,
                    (LatencyStage.CALLEE_ANSWERED, answered, ""),
                )
        elif leg == "callee" and status in {"completed", "failed", "busy", "no-answer", "canceled"}:
            duration = int(form.get("CallDuration") or 0)
            reason = "callee_call_completed" if status == "completed" else f"callee_{status}"
            if status == "completed":
                if duration >= self.settings.max_call_seconds:
                    reason = "time_limit"
                await self.db.update_call(call_id, twilio_reported_duration_seconds=duration)
            await self.terminate_call(call_id, reason)
        elif leg == "agent" and status in {"completed", "failed"}:
            await self.terminate_call(call_id, "agent_call_completed")

    async def _check_activation_gate(self, call_id: str) -> None:
        call = await self.db.get_call(call_id)
        if call is None or CallState(call["state"]) in TERMINAL_STATES:
            return
        if not (
            call["sideband_open"]
            and call["callee_joined"]
            and call["live_session_verified"]
            and call.get("twilio_callee_call_sid")
        ):
            return
        if not await self.db.cas_state(call_id, CallState.PREWARMING, CallState.READY_TO_ACTIVATE):
            return
        await self._activate(call_id)

    def _observe_live_event(self, call_id: str, event: dict[str, Any]) -> None:
        if call_id in self._activity.tombstones or call_id in self._activity.watchdog_claims:
            return
        state = self._live_conversations.setdefault(call_id, LiveConversation())
        event_type = event.get("type")
        if event_type == "session.output_audio.delta" and not state.first_audio_seen:
            state.first_audio_seen = True
            self._queue_latency(call_id, LatencyStage.FIRST_OPENAI_AUDIO_DELTA, LatencyMark.now())
        if event_type == "session.input_transcript.delta":
            delta = event.get("delta")
            if not isinstance(delta, str) or not delta:
                return
            state.input_revision += 1
            state.callee_text = (state.callee_text + delta)[-4000:]
            # Transcript arrival is a second, conservative closing cancellation signal.
            # Acoustic detection normally reaches this path earlier.
            if self._cancel_voice_end(call_id):
                self._spawn(self._notify_call_resumed(call_id), name=f"voice-end-resumed:{call_id}")
        elif event_type == "session.output_transcript.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                previous_length = len(state.assistant_text)
                combined = state.assistant_text + delta
                state.assistant_text = combined[-8000:]
                state.output_revision += 1
                if any(
                    match.end() > previous_length
                    for match in re.finditer(
                        r"\b(?:goodbye|bye|take care|good night|have a (?:good|great|nice) (?:day|night|evening))\b",
                        combined,
                        re.IGNORECASE,
                    )
                ):
                    state.closing_candidate_revision = state.output_revision
        elif event_type == "session.delegation.created":
            delegation = event.get("delegation") or {}
            delegation_id = str(delegation.get("id") or event.get("delegation_id") or "")
            if delegation_id:
                state.delegation_epochs[delegation_id] = self._callee_speech_epochs.get(call_id, 0)
        elif event_type == "response.event":
            inner = event.get("event") or {}
            delegation_id = str(event.get("delegation_id") or "")
            state.delegation_epochs.setdefault(
                delegation_id, self._callee_speech_epochs.get(call_id, 0)
            )
            response_id = str(
                (inner.get("response") or {}).get("id")
                or inner.get("response_id")
                or event.get("_backend_response_id")
                or ""
            )
            if response_id:
                state.response_delegations[response_id] = delegation_id
                if inner.get("type") == "response.created":
                    state.response_epochs[response_id] = self._callee_speech_epochs.get(call_id, 0)

    def observe_media_audio(
        self, call_id: str, track: str, timestamp_ms: int, payload: str
    ) -> None:
        if call_id in self._activity.tombstones or call_id in self._activity.watchdog_claims:
            return
        audio = self._call_audio.get(call_id)
        if audio is None or not audio.connected:
            return
        started, stopped = audio.observe(track, timestamp_ms, payload)
        self._note_call_activity(call_id)
        if track == "inbound":
            if started:
                self._callee_speech_epochs[call_id] = self._callee_speech_epochs.get(call_id, 0) + 1
                self._callee_speaking.add(call_id)
                if audio.outbound.speaking:
                    self._spawn(
                        self.db.set_flag_once(call_id, "interruption_observed"),
                        name=f"carrier-interruption:{call_id}",
                    )
                conversation = self._live_conversations.get(call_id)
                if conversation:
                    conversation.assistant_revision_at_input = conversation.output_revision
                    conversation.callee_text = ""
                if self._cancel_voice_end(call_id):
                    self._spawn(
                        self._notify_call_resumed(call_id), name=f"voice-end-resumed:{call_id}"
                    )
            if stopped:
                received = LatencyMark.now()
                silence_ms = audio.inbound.end_ms - (
                    audio.inbound.last_voice_ms or audio.inbound.end_ms
                )
                speech_end = LatencyMark(
                    (
                        datetime.fromisoformat(received.occurred_at)
                        - timedelta(milliseconds=silence_ms)
                    ).isoformat(),
                    received.monotonic_ns - int(silence_ms * 1_000_000),
                )
                self._queue_latency(
                    call_id,
                    LatencyStage.CALLEE_SPEECH_ENDED,
                    speech_end,
                    event_key=str(self._callee_speech_epochs.get(call_id, 0)),
                )
                self._callee_speaking.discard(call_id)
        else:
            if started:
                received = LatencyMark.now()
                start = LatencyMark(
                    (
                        datetime.fromisoformat(received.occurred_at)
                        - timedelta(milliseconds=audio.outbound.voice_samples / 8)
                    ).isoformat(),
                    received.monotonic_ns - int(audio.outbound.voice_samples / 8 * 1_000_000),
                )
                self._queue_latency(
                    call_id, LatencyStage.CARRIER_OUTPUT_STARTED, start, event_key=str(timestamp_ms)
                )
                self._activity.sip_output_playing.add(call_id)
            if stopped:
                self._queue_latency(
                    call_id,
                    LatencyStage.CARRIER_OUTPUT_ENDED,
                    LatencyMark.now(),
                    event_key=str(timestamp_ms),
                )
                self._activity.sip_output_playing.discard(call_id)
                self._spawn(self._review_spoken_closing(call_id), name=f"closing-review:{call_id}")

    async def _review_spoken_closing(self, call_id: str) -> None:
        state = self._live_conversations.get(call_id)
        audio = self._call_audio.get(call_id)
        if (
            state is None
            or state.output_revision <= state.assistant_revision_at_input
            or not state.closing_candidate_revision
            or state.closing_review_running
            or state.closing_review_revision >= state.closing_candidate_revision
            or audio is None
            or not audio.connected
            or audio.outbound.speaking
            or call_id in self._callee_speaking
            or call_id in self._voice_end_pending
            or call_id in self._pending_questions
            or call_id in self._inflight_tools
            or call_id in self._activity.tombstones
        ):
            return
        # This only asks the configured backend to assess the actual conversation.
        # A farewell-shaped phrase (including a quote) never authorizes hangup itself.
        revision = state.closing_candidate_revision
        state.closing_review_running = True
        try:
            if await self.live.review_closing(call_id, assistant_text=state.assistant_text[-2000:]):
                state.closing_review_revision = revision
        finally:
            state.closing_review_running = False

    async def _start_media_monitor(self, call: dict[str, Any]) -> None:
        call_id = call["call_id"]
        self._call_audio[call_id] = CallAudio()
        self._media_tokens[call_id] = secrets.token_urlsafe(32)
        ready = self._media_ready.setdefault(call_id, asyncio.Event())
        stream_sid = await self._run_cancellation_safe_create(
            call_id,
            lambda: self.twilio.start_audio_monitor(
                call_id=call_id,
                plan_id=call["plan_id"],
                callee_call_sid=call["twilio_callee_call_sid"],
                token=self._media_tokens[call_id],
            ),
            operation="create-media-stream",
            request_stage=None,
            completed_stage=None,
            failure_log_message="failed to start carrier audio monitor",
            failure_reason="media_monitor_setup_failed",
            on_late_success=lambda late: self._persist_late_media_stream(call_id, late),
            terminate_on_failure=False,
        )
        await self.db.update_call(call_id, media_stream_sid=stream_sid)
        await asyncio.wait_for(ready.wait(), timeout=5)
        if self._call_audio[call_id].stream_sid != stream_sid:
            raise RuntimeError("carrier stream identity mismatch")

    async def accept_media_monitor(self, call_id: str, plan_id: str, start: dict[str, Any]) -> bool:
        call = await self.resolve_webhook_call(call_id, plan_id)
        audio = self._call_audio.get(call_id)
        parameters = start.get("customParameters") or {}
        token = parameters.get("token")
        expected = self._media_tokens.get(call_id)
        if (
            call is None
            or self._activity.is_closed(call)
            or audio is None
            or audio.connected
            or not isinstance(token, str)
            or expected is None
            or not secrets.compare_digest(token, expected)
            or start.get("accountSid") != self.settings.twilio_account_sid
            or start.get("callSid") != call.get("twilio_callee_call_sid")
            or start.get("mediaFormat")
            != {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
            or set(start.get("tracks") or []) != {"inbound", "outbound"}
            or not isinstance(start.get("streamSid"), str)
        ):
            return False
        audio.connected = True
        audio.stream_sid = start["streamSid"]
        self._media_tokens.pop(call_id, None)
        self._media_ready[call_id].set()
        return True

    async def media_monitor_closed(self, call_id: str) -> None:
        audio = self._call_audio.get(call_id)
        if audio:
            audio.connected = False
        self._cancel_voice_end(call_id)
        call = await self.db.get_call(call_id)
        if call is None or self._activity.is_closed(call):
            return
        # A normal callee hangup can close the stream before its signed status callback.
        # Reconcile the carrier state so that ordering does not turn a normal end into failure.
        try:
            status = await self.twilio.callee_status(call["twilio_callee_call_sid"])
        except Exception:
            logger.warning("could not reconcile closed carrier stream call_id=%s", call_id)
        else:
            if status.get("CallStatus") in {"completed", "failed", "busy", "no-answer", "canceled"}:
                await self.handle_participant_status(call_id, "callee", status)
                return
        await self.terminate_call(call_id, "carrier_monitor_lost")

    async def _unmute_agent(
        self, call_id: str, conference: str | None, agent_call_sid: str | None
    ) -> None:
        try:
            await self.twilio.unmute_participant(conference, agent_call_sid)
        except Exception:
            logger.warning(
                "failed to unmute agent participant call_id=%s",
                call_id,
                exc_info=True,
            )

    async def _activate(self, call_id: str) -> None:
        async with self._activation_lock(call_id):
            if not await self.db.cas_state(
                call_id, CallState.READY_TO_ACTIVATE, CallState.ACTIVATING
            ):
                return
            call = await self.db.get_call(call_id)
            if call is None:
                return
            try:
                await self._start_media_monitor(call)
                if not await self.db.cas_state(call_id, CallState.ACTIVATING, CallState.ACTIVE):
                    return
                await self._unmute_agent(
                    call_id,
                    call.get("conference_sid") or call.get("conference_name"),
                    call.get("twilio_ai_call_sid"),
                )
                await self.live.enable_conversation(call_id)
            except Exception:
                logger.warning("Live activation failed call_id=%s", call_id, exc_info=True)
                await self.terminate_call(call_id, "live_activation_failed")
                return
        await self._send_voicemail(call_id)

    async def _send_voicemail(self, call_id: str) -> None:
        async with self._activation_lock(call_id):
            call = await self.db.get_call(call_id)
            if (
                call is None
                or call["state"] != CallState.ACTIVE.value
                or call.get("answer_handling") != "voicemail"
                or call.get("voicemail_sent")
            ):
                return
            if await self.db.set_flag_once(call_id, "voicemail_sent"):
                await self.live.create_voicemail(call_id)

    async def handle_live_event(
        self, call_id: str, event: dict[str, Any], *, _observed: bool = False
    ) -> None:
        received = LatencyMark.now()
        if not _observed:
            self._observe_live_event(call_id, event)
        event_type = event.get("type", "")
        event_id = str(event.get("event_id") or f"evt_{secrets.token_urlsafe(12)}")
        if event_type in {"session.input_transcript.delta", "session.output_transcript.delta"}:
            delta = event.get("delta")
            if not isinstance(delta, str) or not delta:
                return
            speaker: Literal["callee", "assistant"] = (
                "callee" if event_type == "session.input_transcript.delta" else "assistant"
            )
            await self.db.add_transcript_turn(
                call_id=call_id,
                turn_id=event_id,
                speaker=speaker,
                text=delta,
                source_event_type=event_type,
                source_event_id=event_id,
                start_ms=event.get("start_ms"),
                end_ms=event.get("end_ms"),
            )
            if speaker == "assistant":
                self._queue_latency(call_id, LatencyStage.FIRST_ASSISTANT_TRANSCRIPT, received)
            # Transcript fragments are not complete turns. The backend owns hold detection;
            # audio and frontend continue independently while it records or exits hold.
        elif event_type == "response.event":
            inner = event.get("event") or {}
            kind = inner.get("type")
            response = inner.get("response") or {}
            response_id = str(
                response.get("id")
                or inner.get("response_id")
                or event.get("_backend_response_id")
                or ""
            )
            if kind == "response.created":
                self._queue_latency(
                    call_id, LatencyStage.BACKEND_RESPONSE_CREATED, received, event_key=response_id
                )
                self._activity.active_response_ids[call_id] = response_id
                if call_id in self._tool_seen_calls:
                    self._tool_seen_calls.discard(call_id)
                    await self.db.mark_tool_continuation_observed(call_id)
            elif kind == "response.output_item.done":
                item = inner.get("item") or {}
                if item.get("type") != "function_call":
                    return
                tool_id = item.get("call_id")
                if not isinstance(tool_id, str) or not item.get("name"):
                    raise ValueError("incomplete Live backend function call")
                state = self._live_conversations.setdefault(call_id, LiveConversation())
                if tool_id in state.seen_tools:
                    return
                state.seen_tools.add(tool_id)
                self._tool_seen_calls.add(call_id)
                delegation_id = str(event.get("delegation_id") or "")
                epoch = state.response_epochs.get(
                    response_id,
                    state.delegation_epochs.get(
                        delegation_id, self._callee_speech_epochs.get(call_id, 0)
                    ),
                )
                # A changed request invalidates queued consequential actions. Read-only lookups
                # may complete; their result is not proof a stale action should be executed.
                if item.get("name") in {
                    "send_dtmf",
                    "transfer_to_owner",
                    "finish_call_after_goodbye",
                } and epoch != self._callee_speech_epochs.get(call_id, 0):
                    logger.info(
                        "rejected superseded Live action call_id=%s response_id=%s tool=%s",
                        call_id,
                        response_id,
                        item["name"],
                    )
                    await self._guarded_send_tool_result(
                        call_id, tool_id, {"accepted": False, "error": "request_superseded"}
                    )
                    return
                await self._handle_tool_call(
                    call_id,
                    {**item, "response_id": response_id, "delegation_id": delegation_id},
                    received=received,
                    event_key=tool_id,
                )
            elif kind in {
                "response.completed",
                "response.failed",
                "response.incomplete",
                "response.cancelled",
            }:
                if self._activity.active_response_ids.get(call_id) == response_id:
                    self._activity.active_response_ids.pop(call_id, None)
                self._queue_latency(
                    call_id,
                    LatencyStage.BACKEND_RESPONSE_COMPLETED,
                    received,
                    event_key=response_id,
                )
                await self._record_backend_usage(call_id, response)
                self._spawn(self._review_spoken_closing(call_id), name=f"closing-review:{call_id}")
                if kind != "response.completed":
                    logger.warning(
                        "Live delegated response ended unsuccessfully call_id=%s type=%s",
                        call_id,
                        kind,
                    )
            # Other Responses events are deliberately not voice lifecycle events.
        elif event_type in {"session.usage.updated", "session.closed"}:
            usage = event.get("usage") or {}
            if "seconds" in usage:
                await self.db.record_live_usage(
                    call_id, seconds=usage["seconds"], finalized=event_type == "session.closed"
                )
            if event_type == "session.closed":
                self._spawn(
                    self.terminate_call(call_id, "openai_terminal_event"),
                    name=f"terminate:{call_id}:live-terminal",
                )
        elif event_type == "error":
            error = event.get("error") or {}
            code, error_type, message = provider_error_fields(error)
            logger.error(
                "Live error call_id=%s code=%s type=%s message=%s",
                call_id,
                code,
                error_type,
                message,
            )
            self._spawn(
                self.terminate_call(call_id, "openai_fatal_error"),
                name=f"terminate:{call_id}:live-error",
            )

    async def _record_final_live_usage(self, call_id: str, event: dict[str, Any]) -> None:
        usage = event.get("usage") or {}
        if "seconds" in usage:
            await self.db.record_live_usage(call_id, seconds=usage["seconds"], finalized=True)

    async def _record_backend_usage(self, call_id: str, response: dict[str, Any]) -> None:
        usage = response.get("usage") or {}
        if not usage or not response.get("id"):
            return
        await self.db.record_backend_usage(
            call_id,
            response_id=response["id"],
            input_tokens=int(usage.get("input_tokens") or 0),
            cached_input_tokens=int(
                (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
            ),
            output_tokens=int(usage.get("output_tokens") or 0),
        )

    async def handle_live_send(self, call_id: str, event: dict[str, Any]) -> None:
        sent = LatencyMark.now()
        # Outbound control (response.create, tool results) is proof the call is live even
        # when the SIP sideband is quiet between assistant audio frames.
        self._activity.note(call_id, sent)
        event_type = event.get("type")
        if event_type == "session.instructions.append":
            self._queue_latency(call_id, LatencyStage.FIRST_RESPONSE_CREATE, sent)
            return
        item = event.get("item") or {}
        if event_type == "response.item.create" and item.get("type") == "function_call_output":
            try:
                output = json.loads(item.get("output") or "{}")
            except (TypeError, ValueError):
                output = {}
            if isinstance(output, dict) and (output.get("ok") is False or output.get("error")):
                self._queue_latency(
                    call_id,
                    LatencyStage.TOOL_RESULT_FAILED,
                    sent,
                    event_key=str(item.get("call_id") or "unknown"),
                )
            self._queue_latency(
                call_id,
                LatencyStage.TOOL_RESULT_SENT,
                sent,
                event_key=str(item.get("call_id") or "unknown"),
            )

    async def _guarded_send_tool_result(
        self,
        call_id: str,
        tool_call_id: str,
        output: dict[str, Any],
        *,
        continue_response: bool = True,
        continuation_instructions: str | None = None,
    ) -> bool:
        """Swallow benign teardown races (e.g. "sideband is not open") so they cannot
        escalate into a fatal error and redundant termination. CancelledError must still
        propagate so the dispatcher stays cancellable. Returns False when delivery
        failed, so callers that must retry (question answers) can record that."""

        try:
            await self.live.send_tool_result(
                call_id,
                tool_call_id,
                output,
                continue_response=continue_response,
                continuation_instructions=continuation_instructions,
            )
        except asyncio.CancelledError:
            logger.warning("nontransfer tool output was cancelled call_id=%s", call_id)
            raise
        except Exception:
            logger.warning(
                "nontransfer tool output could not be delivered call_id=%s",
                call_id,
                exc_info=True,
            )
            return False
        return True

    async def _send_nontransfer_tool_result(
        self,
        call_id: str,
        tool_call_id: str,
        output: dict[str, Any],
        *,
        received: LatencyMark,
        event_key: str,
        advisory_outcome: dict[str, Any] | None = None,
        continue_response: bool = True,
        continuation_instructions: str | None = None,
    ) -> None:
        # Begin the one durable tool write before yielding to the WebSocket, but do not
        # put SQLite on the response path. Await it before returning so the FIFO dispatcher
        # cannot process response.created before tool_call_count is committed.
        persistence = asyncio.create_task(
            self.db.record_tool_call(
                call_id,
                latency_mark=received,
                event_key=event_key,
                advisory_outcome=advisory_outcome,
            ),
            name=f"persist-tool:{call_id}:{event_key}",
        )
        if advisory_outcome is not None:
            # accepted=true represents business data that must survive a crash. Do not
            # acknowledge a valid advisory until the fused transaction is durable.
            try:
                await persistence
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("advisory outcome persistence failed call_id=%s", call_id)
                # The model must not be left hanging with no tool result at all; best-effort
                # tell it the outcome was not recorded, then surface the durability failure.
                await self._guarded_send_tool_result(
                    call_id,
                    tool_call_id,
                    {"accepted": False, "error": "outcome could not be persisted"},
                )
                raise
            await self._guarded_send_tool_result(
                call_id,
                tool_call_id,
                output,
                continue_response=continue_response,
                continuation_instructions=continuation_instructions,
            )
            return
        try:
            await self._guarded_send_tool_result(
                call_id,
                tool_call_id,
                output,
                continue_response=continue_response,
                continuation_instructions=continuation_instructions,
            )
        finally:
            await persistence

    async def _handle_tool_call(
        self,
        call_id: str,
        event: dict[str, Any],
        *,
        received: LatencyMark | None = None,
        event_key: str | None = None,
    ) -> None:
        name = event.get("name")
        tool_call_id = str(event.get("call_id") or "unknown")
        received = received or LatencyMark.now()
        event_key = event_key or tool_call_id
        if isinstance(name, str) and name in {
            "search_web",
            "send_dtmf",
            "record_call_outcome",
            "finish_call_after_goodbye",
            "transfer_to_owner",
            "ask_agent",
            "report_hold",
        }:
            self._queue_latency(
                call_id,
                LatencyStage.TOOL_DISPATCHED,
                received,
                event_key=f"{name}:{tool_call_id}",
            )
        try:
            arguments = json.loads(event.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if name == "search_web":
            await self._tool_search_web(
                call_id, tool_call_id, arguments, received=received, event_key=event_key
            )
        elif name == "send_dtmf":
            try:
                request = SendDtmfRequest.model_validate(arguments)
            except Exception:
                logger.info("invalid send_dtmf tool arguments call_id=%s", call_id)
                await self._send_nontransfer_tool_result(
                    call_id,
                    tool_call_id,
                    {"ok": False, "error": "invalid_dtmf_request"},
                    received=received,
                    event_key=event_key,
                )
                return

            call = await self.db.get_call(call_id)
            if call is None or call["state"] != CallState.ACTIVE.value:
                await self._send_nontransfer_tool_result(
                    call_id,
                    tool_call_id,
                    {"ok": False, "error": "call_not_ready"},
                    received=received,
                    event_key=event_key,
                )
                return
            conference = call.get("conference_sid") or call.get("conference_name")
            callee_sid = call.get("twilio_callee_call_sid")
            if not conference or not callee_sid:
                await self._send_nontransfer_tool_result(
                    call_id,
                    tool_call_id,
                    {"ok": False, "error": "call_not_ready"},
                    received=received,
                    event_key=event_key,
                )
                return

            self._note_call_activity(call_id)
            self._inflight_tools.add(call_id)
            try:
                await self.twilio.send_dtmf(
                    conference,
                    callee_sid,
                    call_id=call_id,
                    plan_id=call["plan_id"],
                    digits=request.digits,
                )
                output = {"ok": True, "digits": request.digits}
            except TwilioRestException:
                logger.warning(
                    "send_dtmf Twilio call failed call_id=%s digits=%s", call_id, request.digits
                )
                output = {"ok": False, "error": "dtmf_failed"}
            except Exception:
                logger.exception("unexpected send_dtmf failure call_id=%s", call_id)
                output = {"ok": False, "error": "dtmf_failed"}
            finally:
                self._inflight_tools.discard(call_id)
                self._note_call_activity(call_id)
            if output.get("ok"):
                self._activity.begin_dtmf_listen_grace(
                    call_id,
                    seconds=POST_DTMF_LISTEN_GRACE_SECONDS,
                )
            await self._send_nontransfer_tool_result(
                call_id,
                tool_call_id,
                output,
                received=received,
                event_key=event_key,
                # A response.create after successful DTMF invites a spoken acknowledgement.
                # Let the next IVR audio turn trigger the model instead.
                continue_response=not output.get("ok", False),
            )
        elif name == "record_call_outcome":
            await self._tool_record_call_outcome(
                call_id, tool_call_id, arguments, received=received, event_key=event_key
            )
        elif name == "finish_call_after_goodbye":
            await self._tool_end_call(
                call_id,
                tool_call_id,
                arguments,
                received=received,
                event_key=event_key,
                response_id=event.get("response_id"),
            )
        elif name == "transfer_to_owner":
            await self._tool_transfer_to_owner_branch(
                call_id, tool_call_id, arguments, received=received, event_key=event_key
            )
        elif name == "ask_agent":
            await self._handle_ask_agent(
                call_id,
                tool_call_id,
                arguments,
                received=received,
                event_key=event_key,
            )
        elif name == "report_hold":
            await self._handle_report_hold(
                call_id,
                tool_call_id,
                arguments,
                received=received,
                event_key=event_key,
            )
        else:
            await self._tool_unknown(call_id, tool_call_id, received=received, event_key=event_key)

    async def _tool_search_web(
        self,
        call_id: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        *,
        received: LatencyMark,
        event_key: str,
    ) -> None:
        try:
            request = WebSearchRequest.model_validate(arguments)
        except Exception:
            logger.info("invalid web search tool arguments call_id=%s", call_id)
            await self._send_nontransfer_tool_result(
                call_id,
                tool_call_id,
                {"ok": False, "error": "invalid_search_request"},
                received=received,
                event_key=event_key,
            )
            return

        self._queue_latency(
            call_id,
            LatencyStage.EXA_SEARCH_STARTED,
            LatencyMark.now(),
            event_key=event_key,
        )
        self._activity.inflight_tools.add(call_id)
        self._activity.note(call_id)
        try:
            result = await self.exa.search(request.query)
            output = result.output
            logger.info(
                "Exa search completed call_id=%s request_id=%s search_type=%s "
                "results=%s output_bytes=%s cost_dollars=%s",
                call_id,
                result.request_id,
                result.search_type,
                result.result_count,
                result.output_bytes,
                result.cost_dollars,
            )
            await self.db.record_exa_search(call_id, cost_dollars=result.cost_dollars or 0.0)
        except ExaSearchError as exc:
            logger.warning("Exa search failed call_id=%s code=%s", call_id, exc.code)
            output = {"ok": False, "error": exc.code}
        except Exception:
            logger.exception("unexpected Exa search failure call_id=%s", call_id)
            output = {"ok": False, "error": "search_unavailable"}
        finally:
            self._activity.inflight_tools.discard(call_id)
            self._activity.note(call_id)
            self._queue_latency(
                call_id,
                LatencyStage.EXA_SEARCH_COMPLETED,
                LatencyMark.now(),
                event_key=event_key,
            )
        await self._send_nontransfer_tool_result(
            call_id,
            tool_call_id,
            output,
            received=received,
            event_key=event_key,
        )

    async def _tool_record_call_outcome(
        self,
        call_id: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        *,
        received: LatencyMark,
        event_key: str,
    ) -> None:
        advisory_outcome: dict[str, Any] | None = None
        try:
            advisory = AdvisoryOutcome.model_validate(arguments)
            advisory_outcome = advisory.model_dump(mode="json", by_alias=True)
            output: dict[str, Any] = {"accepted": True}
        except Exception as exc:
            output = {"accepted": False, "error": str(exc)}
        await self._send_nontransfer_tool_result(
            call_id,
            tool_call_id,
            output,
            received=received,
            event_key=event_key,
            advisory_outcome=advisory_outcome,
        )

    async def _tool_end_call(
        self,
        call_id: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        *,
        received: LatencyMark,
        event_key: str,
        response_id: str | None = None,
    ) -> None:
        try:
            request = VoiceEndCallRequest.model_validate(arguments)
        except Exception:
            await self._send_nontransfer_tool_result(
                call_id,
                tool_call_id,
                {"accepted": False, "error": "invalid_closing_request"},
                received=received,
                event_key=event_key,
            )
            return
        state = self._live_conversations.get(call_id)
        audio = self._call_audio.get(call_id)

        def normalize(text: str) -> str:
            return " ".join(re.findall(r"\w+", text.casefold()))

        farewell = normalize(request.farewell)
        if (
            state is None
            or state.output_revision <= state.assistant_revision_at_input
            or not farewell
            or f" {farewell} " not in f" {normalize(state.assistant_text)} "
            or audio is None
            or not audio.connected
            or audio.outbound.last_voice_ms is None
            or call_id in self._callee_speaking
            or call_id in self._hold_state
            or call_id in self._pending_questions
            or self._undelivered_answers.get(call_id)
            or call_id in self._inflight_tools
        ):
            await self._send_nontransfer_tool_result(
                call_id,
                tool_call_id,
                {
                    "accepted": False,
                    "error": "farewell_or_pending_work_not_resolved",
                    "next_action": "Address any pending request. After an actual spoken farewell, retry with its exact words.",
                },
                received=received,
                event_key=event_key,
            )
            return
        self._cancel_voice_end(call_id)
        token = object()
        self._voice_end_pending[call_id] = (tool_call_id, None)
        self._voice_end_reply_waits[call_id] = token
        self._spawn(
            self._end_after_goodbye_reply_window(call_id, token), name=f"voice-end-reply:{call_id}"
        )
        await self._send_nontransfer_tool_result(
            call_id,
            tool_call_id,
            {
                "accepted": True,
                "status": "closing_pending",
                "call_connected": True,
                "next_action": "The farewell has been observed. Stay silent for the reply window. A callee reply cancels closing.",
            },
            received=received,
            event_key=event_key,
            continue_response=False,
        )

    async def _tool_transfer_to_owner_branch(
        self,
        call_id: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        *,
        received: LatencyMark,
        event_key: str,
    ) -> None:
        reason = str(arguments.get("reason") or "requested")
        persistence = asyncio.create_task(
            self.db.record_tool_call(
                call_id,
                latency_mark=received,
                event_key=event_key,
            ),
            name=f"persist-tool:{call_id}:{event_key}",
        )
        try:
            transfer_task, error = await self._start_owner_transfer(
                call_id, reason, tool_call_id=tool_call_id
            )
            if transfer_task is None:
                await self.live.send_tool_result(
                    call_id,
                    tool_call_id,
                    {"accepted": False, "error": error},
                )
        finally:
            await persistence

    async def _tool_unknown(
        self,
        call_id: str,
        tool_call_id: str,
        *,
        received: LatencyMark,
        event_key: str,
    ) -> None:
        await self._send_nontransfer_tool_result(
            call_id,
            tool_call_id,
            {"accepted": False, "error": "unknown tool"},
            received=received,
            event_key=event_key,
        )

    async def _reject_ask_agent(
        self,
        call_id: str,
        tool_call_id: str,
        error: str,
        *,
        received: LatencyMark,
        event_key: str,
    ) -> None:
        logger.info(
            "ask_agent rejected call_id=%s tool_call_id=%s error=%s",
            call_id,
            tool_call_id,
            error,
        )
        await self._send_nontransfer_tool_result(
            call_id,
            tool_call_id,
            {"status": "error", "error": error},
            received=received,
            event_key=event_key,
        )

    async def _register_ask_agent_question(
        self,
        call_id: str,
        tool_call_id: str,
        request: AskAgentRequest,
        row: dict[str, Any],
        *,
        received: LatencyMark,
        event_key: str,
    ) -> None:
        """Register the accepted question in memory, persist the tool receipt, and
        kick off its notifications (latency mark, agent push, deadline watcher)."""
        # Register the pending question and wake parked long-polls before any further
        # awaits, so a concurrent answer or termination sees (and can clear) the entry
        # instead of racing a registration that has not happened yet.
        self._pending_questions[call_id] = PendingQuestion(
            question_id=row["question_id"],
            tool_call_id=tool_call_id,
            deadline_monotonic=time.monotonic() + self.settings.ask_agent_answer_timeout_seconds,
        )
        self._notify_call_event(call_id)
        try:
            await self.db.record_tool_call(
                call_id,
                latency_mark=received,
                event_key=event_key,
            )
        except Exception:
            logger.exception("ask_agent tool receipt persistence failed call_id=%s", call_id)
        self._queue_latency(
            call_id,
            LatencyStage.ASK_AGENT_ASKED,
            LatencyMark.now(),
            event_key=str(row["question_id"]),
        )
        logger.info(
            "ask_agent asked call_id=%s question_id=%s tool_call_id=%s sequence=%s "
            "question_chars=%s reason_present=%s",
            call_id,
            row["question_id"],
            tool_call_id,
            row["sequence_number"],
            len(request.question),
            request.reason is not None,
        )
        if self.settings.agent_push_enabled:
            reason_line = f"\nReason: {request.reason}" if request.reason else ""
            push_text = (
                f"Mid-call question on call {call_id} (question_id={row['question_id']}, "
                f"sequence={row['sequence_number']}).\n"
                f"Question: {request.question}{reason_line}\n"
                "Answer via the answer_call_question MCP tool with this call_id and "
                "question_id, then resume wait_for_call_event."
            )
            self._spawn(
                push_message_to_agent(self.settings, push_text),
                name=f"agent-push-question:{call_id}:{row['question_id']}",
            )
        self._spawn(
            self._question_deadline(call_id, row["question_id"]),
            name=f"question-deadline:{call_id}:{row['question_id']}",
            must_finish=False,
        )

    async def _handle_ask_agent(
        self,
        call_id: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        *,
        received: LatencyMark,
        event_key: str,
    ) -> None:
        try:
            request = AskAgentRequest.model_validate(arguments)
        except Exception:
            logger.info("invalid ask_agent tool arguments call_id=%s", call_id)
            await self._reject_ask_agent(
                call_id, tool_call_id, "invalid_question", received=received, event_key=event_key
            )
            return

        if not self.settings.ask_agent_enabled:
            await self._reject_ask_agent(
                call_id,
                tool_call_id,
                "ask_agent_disabled",
                received=received,
                event_key=event_key,
            )
            return
        if call_id in self._voice_end_pending:
            await self._reject_ask_agent(
                call_id, tool_call_id, "call_ending", received=received, event_key=event_key
            )
            return

        deadline_at = (
            datetime.now(UTC) + timedelta(seconds=self.settings.ask_agent_answer_timeout_seconds)
        ).isoformat()
        # The question quota is enforced inside create_question, after its duplicate
        # tool_call_id check, so a redelivered ask_agent at the limit reuses the pending
        # row instead of closing the still-open function call with question_limit_reached.
        row, error = await self.db.create_question(
            call_id,
            tool_call_id=tool_call_id,
            question=request.question,
            reason=request.reason,
            deadline_at=deadline_at,
            max_questions=self.settings.ask_agent_max_questions_per_call,
        )
        if error is not None or row is None:
            await self._reject_ask_agent(
                call_id,
                tool_call_id,
                error or "call_not_active",
                received=received,
                event_key=event_key,
            )
            return

        # Redelivered ask_agent for an already-tracked pending question: leave the open
        # function call alone so the eventual answer/timeout can deliver exactly once.
        existing_pending = self._pending_questions.get(call_id)
        if (
            existing_pending is not None
            and existing_pending.question_id == row["question_id"]
            and existing_pending.tool_call_id == tool_call_id
        ):
            logger.info(
                "ask_agent redelivered call_id=%s question_id=%s tool_call_id=%s",
                call_id,
                row["question_id"],
                tool_call_id,
            )
            return

        await self._register_ask_agent_question(
            call_id, tool_call_id, request, row, received=received, event_key=event_key
        )
        # Leave the OpenAI function call open — answer/timeout deliver out-of-band.

    async def _handle_report_hold(
        self,
        call_id: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        *,
        received: LatencyMark,
        event_key: str,
    ) -> None:
        holding = arguments.get("holding", True)
        if not isinstance(holding, bool):
            output = {"status": "error", "error": "invalid_hold_request"}
        elif not holding:
            if call_id in self._hold_state:
                await self._exit_hold(call_id, heard_text="backend confirmed return")
            output = {"status": "resumed"}
        else:
            entered = call_id in self._hold_state or await self._enter_hold(
                call_id, trigger="model_tool"
            )
            output = {"status": "holding" if entered else "not_on_hold"}
        await self._send_nontransfer_tool_result(
            call_id,
            tool_call_id,
            output,
            received=received,
            event_key=event_key,
            continue_response=False,
        )

    def _notify_call_event(self, call_id: str) -> None:
        ev = self._event_notifiers.pop(call_id, None)
        if ev is not None:
            ev.set()

    async def _enter_hold(self, call_id: str, *, trigger: str) -> bool:
        if not self.settings.hold_detection_enabled or call_id in self._hold_state:
            return False
        call = await self.db.get_call(call_id)
        if call is None or CallState(call["state"]) != CallState.ACTIVE:
            return False
        # Record hold state before the suspend-responses round trip so a concurrent
        # detection (transcript race with the model tool) cannot double-enter.
        self._hold_state[call_id] = HoldState(started_monotonic=time.monotonic())
        try:
            await self.live.suspend_conversation(call_id)
        except Exception:
            logger.warning(
                "failed to suspend automatic responses for hold call_id=%s",
                call_id,
                exc_info=True,
            )
            # The call stays live rather than half-held: no suppressed responses without
            # a confirmed session update.
            self._hold_state.pop(call_id, None)
            return False
        self._note_call_activity(call_id, LatencyMark.now())
        logger.info("call entered hold call_id=%s trigger=%s", call_id, trigger)
        self._queue_latency(call_id, LatencyStage.HOLD_ENTERED, LatencyMark.now())
        return True

    async def _exit_hold(self, call_id: str, *, heard_text: str) -> None:
        # Pop first, deterministically: if re-enabling automatic responses fails below,
        # the stale watchdog reaps the call instead of leaving it muted forever.
        self._hold_state.pop(call_id, None)
        try:
            await self.live.enable_conversation(call_id)
        except Exception:
            logger.warning(
                "failed to resume automatic responses after hold call_id=%s",
                call_id,
                exc_info=True,
            )
            return
        self._note_call_activity(call_id, LatencyMark.now())
        logger.info("call exited hold call_id=%s", call_id)
        self._queue_latency(call_id, LatencyStage.HOLD_EXITED, LatencyMark.now())

    def _clear_pending_question(self, call_id: str, question_id: str | None = None) -> None:
        pending = self._pending_questions.get(call_id)
        if pending is None:
            return
        if question_id is not None and pending.question_id != question_id:
            return
        self._pending_questions.pop(call_id, None)

    def _mark_question_delivering(self, call_id: str, question_id: str) -> None:
        pending = self._pending_questions.get(call_id)
        if pending is not None and pending.question_id == question_id:
            pending.delivering = True

    async def _deliver_question_answer(self, call_id: str, question_row: dict[str, Any]) -> None:
        question_id = question_row["question_id"]
        if call_id in self._voice_end_pending:
            # The goodbye turn owns the sideband now; do not inject an answer relay.
            logger.info(
                "ask_agent answer delivery suppressed during voice end call_id=%s question_id=%s",
                call_id,
                question_id,
            )
            self._clear_pending_question(call_id, question_id)
            self._notify_call_event(call_id)
            return
        self._mark_question_delivering(call_id, question_id)
        answer = question_row["answer"] or ""
        output = {"status": "answered", "answer": answer}
        delivered = await self._guarded_send_tool_result(
            call_id,
            question_row["tool_call_id"],
            output,
            continuation_instructions=(
                "The owner's assistant answered your question. Relay the relevant part to "
                "the callee naturally, in one or two sentences. Do not read metadata or "
                "mention the assistant by name."
            ),
        )
        if delivered:
            undelivered = self._undelivered_answers.get(call_id)
            if undelivered is not None:
                undelivered.discard(question_id)
                if not undelivered:
                    self._undelivered_answers.pop(call_id, None)
            self._queue_latency(
                call_id,
                LatencyStage.ASK_AGENT_RESOLVED,
                LatencyMark.now(),
                event_key=str(question_id),
            )
            self._activity.note(call_id)
            logger.info(
                "ask_agent answer delivered call_id=%s question_id=%s tool_call_id=%s "
                "answer_chars=%s",
                call_id,
                question_id,
                question_row["tool_call_id"],
                len(answer),
            )
        else:
            # The question is durably 'answered' but the model never saw the output.
            # Remember it so an agent retry re-attempts delivery instead of stopping at
            # already_answered with the function call still open.
            self._undelivered_answers.setdefault(call_id, set()).add(question_id)
            logger.warning(
                "ask_agent answer delivery failed call_id=%s question_id=%s tool_call_id=%s",
                call_id,
                question_id,
                question_row["tool_call_id"],
            )
        self._clear_pending_question(call_id, question_id)
        self._notify_call_event(call_id)

    async def _question_deadline(self, call_id: str, question_id: str) -> None:
        await asyncio.sleep(self.settings.ask_agent_answer_timeout_seconds)
        if call_id in self._voice_end_pending:
            # end_call already cancels pending questions; the goodbye owns the sideband.
            logger.info(
                "ask_agent question cancelled call_id=%s question_id=%s "
                "reason=voice_end_pending_owns_cancellation",
                call_id,
                question_id,
            )
            return
        self._mark_question_delivering(call_id, question_id)
        try:
            row = await self.db.claim_question_expiry(question_id)
        except Exception:
            logger.exception(
                "question expiry claim failed call_id=%s question_id=%s", call_id, question_id
            )
            self._clear_pending_question(call_id, question_id)
            self._notify_call_event(call_id)
            return
        if row is None:
            # Lost the claim (answered, cancelled, or termination owns the call and
            # will cancel it); drop a stale watchdog carve-out entry left behind if
            # resolution raced our registration. Not a cancellation — the winner
            # already owns the final status.
            logger.info(
                "ask_agent question expiry claim lost call_id=%s question_id=%s "
                "reason=expiry_claim_lost_race",
                call_id,
                question_id,
            )
            self._clear_pending_question(call_id, question_id)
            return
        await self._guarded_send_tool_result(
            call_id,
            row["tool_call_id"],
            {
                "status": "timeout",
                "error": "no_answer_from_agent",
                "guidance": "Owner's assistant did not respond in time.",
            },
            continuation_instructions=(
                "You could not confirm this information. Tell the callee you cannot confirm it "
                "right now. Do NOT guess or invent an answer. Offer to take a message or proceed "
                "without it. Only offer transfer_to_owner if it is already authorized for this call."
            ),
        )
        self._queue_latency(
            call_id,
            LatencyStage.ASK_AGENT_RESOLVED,
            LatencyMark.now(),
            event_key=question_id,
        )
        self._activity.note(call_id)
        logger.info(
            "ask_agent timed out call_id=%s question_id=%s tool_call_id=%s",
            call_id,
            question_id,
            row["tool_call_id"],
        )
        self._clear_pending_question(call_id, question_id)
        self._notify_call_event(call_id)

    async def wait_for_call_event(
        self,
        call_id: str,
        after_sequence: int = 0,
        timeout_seconds: float = 20.0,
    ) -> dict[str, Any]:
        timeout = max(
            0.0, min(float(timeout_seconds), self.settings.wait_for_call_event_max_seconds)
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        # Clamp to SQLite's INTEGER range; an absurd cursor is a caller bug, not a 500.
        after = min(max(0, int(after_sequence)), 2**63 - 1)
        logger.info(
            "wait_for_call_event started call_id=%s after_sequence=%s timeout_seconds=%.2f",
            call_id,
            after,
            timeout,
        )

        while True:
            # Subscribe before reading so a notify that fires between the reads below
            # and the wait cannot be lost; a set event only costs one extra re-read.
            notifier = self._event_notifiers.setdefault(call_id, asyncio.Event())
            call = await self.db.get_call(call_id)
            if call is None:
                if self._event_notifiers.get(call_id) is notifier:
                    self._event_notifiers.pop(call_id, None)
                raise LookupError(call_id)
            state = CallState(call["state"])
            # TERMINATING is not terminal: get_call_result has no final row yet, so clients
            # must keep long-polling until a durable TERMINAL_STATES transition.
            terminal = state in TERMINAL_STATES
            questions = await self.db.get_questions_after(call_id, after)
            remaining = deadline - loop.time()
            if questions or terminal or remaining <= 0:
                if terminal and self._event_notifiers.get(call_id) is notifier:
                    self._event_notifiers.pop(call_id, None)
                events = [
                    {
                        "sequence": row["sequence_number"],
                        "type": "question",
                        "question_id": row["question_id"],
                        "question": row["question"],
                        "reason": row.get("reason"),
                        "status": row["status"],
                        "asked_at": row["asked_at"],
                        "deadline_at": row["deadline_at"],
                    }
                    for row in questions
                ]
                next_after = max(
                    (event["sequence"] for event in events),
                    default=after,
                )
                pending_events = [event for event in events if event["status"] == "pending"]
                if terminal:
                    next_action = (
                        f"Call is terminal (state={state.value}). The call is over; stop "
                        "polling and call get_call_result to retrieve the result."
                    )
                elif pending_events:
                    question_event = pending_events[-1]
                    answer_remaining = _seconds_until(question_event["deadline_at"])
                    if answer_remaining is None:
                        answer_remaining = self.settings.ask_agent_answer_timeout_seconds
                    next_action = (
                        "Begin the required retrieval now for "
                        f"question_id={question_event['question_id']}; you have "
                        f"~{max(0, round(answer_remaining))}s. Accuracy is more important than "
                        "speed within that window. For owner-specific facts, search agent_memory "
                        "and conversation_history first, then relevant integrations. A miss in one "
                        "source is not a not_found result, and call context such as 'this is a test' "
                        "does not make the question optional. Call answer_call_question once with "
                        "only the final, ready-to-relay result, its resolution, and the sources "
                        "actually checked. resolution=not_found requires both agent_memory and "
                        "conversation_history in sources_checked. Do not send a progress update. "
                        + ASK_AGENT_POLL_WARNING
                    )
                elif events:
                    next_action = (
                        "New call events are available but none are pending questions. Call "
                        f"wait_for_call_event again NOW with after_sequence={next_after}. "
                        + ASK_AGENT_POLL_WARNING
                    )
                else:
                    next_action = (
                        "No new events. Call wait_for_call_event again NOW with "
                        f"after_sequence={next_after}. The call is still in progress. "
                        + ASK_AGENT_POLL_WARNING
                    )
                if events or terminal:
                    logger.info(
                        "wait_for_call_event returned call_id=%s after_sequence=%s "
                        "next_after_sequence=%s state=%s terminal=%s event_count=%s "
                        "event_statuses=%s",
                        call_id,
                        after,
                        next_after,
                        state.value,
                        terminal,
                        len(events),
                        [event["status"] for event in events],
                    )
                else:
                    logger.debug(
                        "wait_for_call_event idle timeout call_id=%s after_sequence=%s "
                        "state=%s waited_seconds=%.2f",
                        call_id,
                        after,
                        state.value,
                        timeout,
                    )
                return {
                    "call_id": call_id,
                    "state": state.value,
                    "terminal": terminal,
                    "events": events,
                    "next_after_sequence": next_after,
                    "next_action": next_action,
                }
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(notifier.wait(), remaining)

    async def answer_call_question(
        self,
        call_id: str,
        question_id: str,
        answer: str,
    ) -> dict[str, Any]:
        logger.info(
            "answer_call_question received call_id=%s question_id=%s answer_chars=%s",
            call_id,
            question_id,
            len(answer),
        )
        row = await self.db.claim_question_answer(call_id, question_id, answer)
        if row is not None:
            self._spawn(
                self._deliver_question_answer(call_id, row),
                name=f"deliver-question:{call_id}:{question_id}",
                must_finish=True,
            )
            logger.info(
                "answer_call_question accepted call_id=%s question_id=%s",
                call_id,
                question_id,
            )
            return {
                "status": "accepted",
                "question_id": question_id,
                "next_action": (
                    "Answer accepted. Resume polling: call wait_for_call_event with "
                    f"after_sequence={row['sequence_number']}. " + ASK_AGENT_POLL_WARNING
                ),
            }

        existing = await self.db.get_question(question_id)
        if existing is None or existing.get("call_id") != call_id:
            logger.info(
                "answer_call_question unknown question call_id=%s question_id=%s",
                call_id,
                question_id,
            )
            raise LookupError("unknown question")
        status = existing.get("status")
        if status == "answered":
            undelivered = self._undelivered_answers.get(call_id)
            if undelivered is not None and question_id in undelivered:
                # Claim the retry before awaiting so a concurrent retry cannot also
                # spawn delivery; the first answer text (already stored) wins.
                undelivered.discard(question_id)
                if not undelivered:
                    self._undelivered_answers.pop(call_id, None)
                call = await self.db.get_call(call_id)
                if call is None or self._call_activity_is_closed(call):
                    logger.info(
                        "answer_call_question call_ended on retry call_id=%s question_id=%s",
                        call_id,
                        question_id,
                    )
                    return {"status": "call_ended"}
                if call_id not in self._pending_questions:
                    # Restore the watchdog carve-out for the re-delivery window.
                    self._pending_questions[call_id] = PendingQuestion(
                        question_id=question_id,
                        tool_call_id=existing["tool_call_id"],
                        deadline_monotonic=time.monotonic(),
                        delivering=True,
                    )
                self._spawn(
                    self._deliver_question_answer(call_id, existing),
                    name=f"deliver-question:{call_id}:{question_id}",
                    must_finish=True,
                )
                logger.info(
                    "answer_call_question retrying undelivered answer call_id=%s question_id=%s",
                    call_id,
                    question_id,
                )
                return {
                    "status": "accepted",
                    "question_id": question_id,
                    "next_action": (
                        "Answer accepted. Resume polling: call wait_for_call_event with "
                        f"after_sequence={existing['sequence_number']}. " + ASK_AGENT_POLL_WARNING
                    ),
                }
            logger.info(
                "answer_call_question already_answered call_id=%s question_id=%s",
                call_id,
                question_id,
            )
            return {
                "status": "already_answered",
                "next_action": (
                    "Question already answered. Resume polling: call wait_for_call_event "
                    f"with after_sequence={existing['sequence_number']}. " + ASK_AGENT_POLL_WARNING
                ),
            }
        if status == "expired":
            logger.info(
                "answer_call_question expired call_id=%s question_id=%s",
                call_id,
                question_id,
            )
            return {
                "status": "expired",
                "detail": "timeout already sent to the agent",
                "next_action": (
                    "Answer window already expired. Resume polling: call wait_for_call_event "
                    f"with after_sequence={existing['sequence_number']}. " + ASK_AGENT_POLL_WARNING
                ),
            }
        if status == "cancelled":
            logger.info(
                "answer_call_question call_ended call_id=%s question_id=%s status=%s",
                call_id,
                question_id,
                status,
            )
            return {"status": "call_ended"}
        call = await self.db.get_call(call_id)
        if call is None or self._activity.is_closed(call):
            logger.info(
                "answer_call_question call_ended call_id=%s question_id=%s status=%s",
                call_id,
                question_id,
                status,
            )
            return {"status": "call_ended"}
        # Pending claim lost to a concurrent race without a readable winner — treat as
        # already handled so the agent can advance rather than retry forever.
        logger.info(
            "answer_call_question already_answered after race call_id=%s question_id=%s",
            call_id,
            question_id,
        )
        return {
            "status": "already_answered",
            "next_action": (
                "Question already handled. Resume polling: call wait_for_call_event "
                f"with after_sequence={existing['sequence_number']}. " + ASK_AGENT_POLL_WARNING
            ),
        }

    def _cancel_voice_end(self, call_id: str) -> bool:
        closing = call_id in self._voice_end_pending or call_id in self._voice_end_reply_waits
        self._voice_end_pending.pop(call_id, None)
        self._voice_end_reply_waits.pop(call_id, None)
        return closing

    async def _notify_call_resumed(self, call_id: str) -> None:
        if (
            call_id in self._activity.tombstones
            or call_id in self._activity.watchdog_claims
            or call_id in self._voice_end_pending
        ):
            return
        await self.live.notify_call_resumed(call_id)

    async def _end_after_goodbye_reply_window(self, call_id: str, token: object) -> None:
        # Use continuous outbound carrier media, not wall-clock silence after a transcript
        # or a backend completion. Gaps, stale media, speech, and disconnects never count.
        while self._voice_end_reply_waits.get(call_id) is token:
            audio = self._call_audio.get(call_id)
            if audio is None or not audio.connected:
                return
            silence = audio.outbound.silence_seconds(now=time.monotonic())
            if (
                silence is not None
                and silence >= VOICE_END_REPLY_GRACE_SECONDS
                and call_id not in self._callee_speaking
                and call_id not in self._pending_questions
                and not self._undelivered_answers.get(call_id)
                and call_id not in self._inflight_tools
            ):
                call = await self.db.get_call(call_id)
                reason = (
                    "voicemail_left"
                    if call and call.get("voicemail_sent")
                    else "voice_model_end_call"
                )

                def claim_if_still_quiet() -> bool:
                    if self._voice_end_reply_waits.get(call_id) is not token:
                        return False
                    current_audio = self._call_audio.get(call_id)
                    current_silence = (
                        current_audio.outbound.silence_seconds(now=time.monotonic())
                        if current_audio and current_audio.connected
                        else None
                    )
                    if (
                        current_silence is None
                        or current_silence < VOICE_END_REPLY_GRACE_SECONDS
                        or call_id in self._callee_speaking
                    ):
                        return False
                    self._voice_end_reply_waits.pop(call_id, None)
                    self._voice_end_pending.pop(call_id, None)
                    return True

                await self.terminate_call(call_id, reason, _claim_guard=claim_if_still_quiet)
                return
            await asyncio.sleep(0.05)

    async def transfer_to_owner(
        self,
        call_id: str,
        reason: str,
        *,
        terminate_after: bool = True,  # accepted for backward compatibility, unused
    ) -> dict[str, Any]:
        # A successful handoff is always terminal. Keep the legacy keyword for callers
        # but do not allow a claimed transfer to strand the call in TERMINATING.
        task, error = await self._start_owner_transfer(call_id, reason, tool_call_id=None)
        if task is None:
            return {"accepted": False, "error": error}
        return await asyncio.shield(task)

    async def _start_owner_transfer(
        self,
        call_id: str,
        reason: str,
        *,
        tool_call_id: str | None,
    ) -> tuple[asyncio.Task[dict[str, Any]] | None, str | None]:
        return await self._owner_transfer.start(call_id, reason, tool_call_id=tool_call_id)

    @staticmethod
    async def _await_network_task(
        task: asyncio.Task[Any], *, propagate_cancellation: bool = False
    ) -> Any:
        """Let a to_thread-backed operation settle despite caller cancellation."""

        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Twilio's synchronous request is still running in its worker thread.
                # Its configured HTTP timeout bounds this wait; abandoning the result
                # could lose the participant SID needed for cleanup.
                interrupted = True
                continue
        try:
            result = task.result()
        except BaseException:
            if interrupted and propagate_cancellation:
                raise asyncio.CancelledError from None
            raise
        if interrupted and propagate_cancellation:
            raise asyncio.CancelledError
        return result

    def _schedule_conference_completion_retry(
        self,
        call: dict[str, Any],
        *,
        reason: str | None,
        expected_transfer_outcome: str | None = None,
        await_finalizer: bool = False,
    ) -> None:
        """Retry required conference teardown without blocking callers or startup."""

        if self._stopping:
            return
        call_id = call["call_id"]
        mode = f"termination:{reason}" if reason is not None else "compensation"
        key = (call_id, mode)
        existing = self._conference_retry_tasks.get(key)
        if existing is not None and not existing.done():
            return

        async def retry() -> None:
            attempt = 0
            while True:
                delay = min(
                    TERMINATION_MEDIA_BACKGROUND_RETRY_BASE_SECONDS * (2 ** min(attempt, 10)),
                    TERMINATION_MEDIA_BACKGROUND_RETRY_MAX_SECONDS,
                )
                await asyncio.sleep(delay)
                attempt += 1
                try:
                    if reason is not None:
                        current = await self.db.get_call(call_id)
                        if current is None or CallState(current["state"]) in TERMINAL_STATES:
                            return
                    if reason is None:
                        completion = asyncio.create_task(
                            self.twilio.complete_conference(
                                call.get("conference_sid") or call.get("conference_name")
                            ),
                            name=f"retry-compensation-conference:{call_id}:{attempt}",
                        )
                        await self._await_network_task(completion, propagate_cancellation=True)
                        try:
                            await self.db.set_conference_cleanup_pending(call_id, False)
                        except Exception:
                            logger.warning(
                                "failed to clear conference cleanup pending flag call_id=%s",
                                call_id,
                                exc_info=True,
                            )
                        return
                    terminalization = asyncio.create_task(
                        self._finish_claimed_termination(
                            call,
                            reason,
                            preserve_conference=False,
                            await_finalizer=await_finalizer,
                            expected_transfer_outcome=expected_transfer_outcome,
                            schedule_conference_retry=False,
                        ),
                        name=f"retry-terminalize:{call_id}:{attempt}",
                    )
                    if await self._await_network_task(terminalization, propagate_cancellation=True):
                        return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "conference completion background retry failed call_id=%s attempt=%s",
                        call_id,
                        attempt,
                        exc_info=True,
                    )

        task = self._spawn(retry(), name=f"conference-retry:{call_id}:{mode}")
        self._conference_retry_tasks[key] = task

        def clear(completed: asyncio.Task[Any]) -> None:
            if self._conference_retry_tasks.get(key) is completed:
                self._conference_retry_tasks.pop(key, None)

        task.add_done_callback(clear)

    async def _complete_conference_or_schedule(self, call: dict[str, Any]) -> bool:
        """Fail closed now, retaining bounded in-process ownership on failure."""

        call_id = call["call_id"]
        completion = asyncio.create_task(
            self.twilio.complete_conference(
                call.get("conference_sid") or call.get("conference_name")
            ),
            name=f"compensate-conference:{call_id}",
        )
        try:
            await self._await_network_task(completion)
        except Exception:
            logger.warning("failed to compensate conference call_id=%s", call_id, exc_info=True)
            try:
                # The in-process retry must still be scheduled even if this durable marker
                # write fails; the marker only aids crash recovery, it does not gate the retry.
                await self.db.set_conference_cleanup_pending(call_id, True)
            except Exception:
                logger.warning(
                    "failed to persist conference cleanup pending flag call_id=%s",
                    call_id,
                    exc_info=True,
                )
            self._schedule_conference_completion_retry(call, reason=None)
            return False
        try:
            await self.db.set_conference_cleanup_pending(call_id, False)
        except Exception:
            logger.warning(
                "failed to clear conference cleanup pending flag call_id=%s",
                call_id,
                exc_info=True,
            )
        return True

    async def terminate_call(
        self,
        call_id: str,
        reason: str,
        *,
        preserve_conference: bool = False,
        await_finalizer: bool = False,
        _initiating_task: asyncio.Task[Any] | None = None,
        _claim_guard: Callable[[], bool] | None = None,
    ) -> bool:
        if self._stopping and _initiating_task is None:
            return False
        initiating_task = _initiating_task or asyncio.current_task()
        continuation = self._spawn(
            self._claim_and_finish_termination(
                call_id,
                reason,
                preserve_conference=preserve_conference,
                await_finalizer=await_finalizer,
                initiating_task=initiating_task,
                claim_guard=_claim_guard,
            ),
            name=f"claimed-termination:{call_id}",
            must_finish=True,
        )
        # Once a termination claim may commit, caller cancellation must not strand
        # billing/media teardown behind an unreclaimable termination_claimed flag.
        result = await self._await_network_task(continuation)
        return bool(result)

    async def _claim_and_finish_termination(
        self,
        call_id: str,
        reason: str,
        *,
        preserve_conference: bool,
        await_finalizer: bool,
        initiating_task: asyncio.Task[Any] | None,
        claim_guard: Callable[[], bool] | None = None,
    ) -> bool:
        transfer_task: asyncio.Task[dict[str, Any]] | None = None
        async with self._owner_transfer_lock(call_id):
            claim_error: Exception | None = None
            try:
                if claim_guard is None:
                    call = await self.db.claim_termination(call_id, reason)
                else:
                    call = await self.db.claim_termination(call_id, reason, claim_guard=claim_guard)
            except Exception as exc:
                call = None
                claim_error = exc
            if call is None and claim_error is not None:
                current = await self.db.get_call(call_id)
                exact_claim = bool(
                    current
                    and current.get("state") == CallState.TERMINATING.value
                    and current.get("termination_claimed")
                    and current.get("termination_reason") == reason
                )
                if not exact_claim:
                    raise claim_error
                call = current
                logger.warning("reconciled ambiguous termination claim call_id=%s", call_id)
            if call is not None:
                transfer_task = self._owner_transfer_tasks.get(call_id)
                # Cancel pending questions at claim time so answers cannot race media
                # teardown and mark a question answered after the call left ACTIVE.
                self._pending_questions.pop(call_id, None)
                try:
                    cancelled = await self.db.cancel_pending_questions(call_id)
                    for cancelled_row in cancelled:
                        logger.info(
                            "ask_agent question cancelled call_id=%s question_id=%s "
                            "reason=termination_claim reason_code=%s",
                            call_id,
                            cancelled_row["question_id"],
                            reason,
                        )
                except Exception:
                    logger.warning(
                        "pending question cancellation failed at termination claim call_id=%s",
                        call_id,
                        exc_info=True,
                    )
                self._notify_call_event(call_id)
        if call is None:
            current = await self.db.get_call(call_id)
            if current is None:
                self._activity.clear(call_id)
            elif CallState(current["state"]) in TERMINAL_STATES or current.get(
                "termination_claimed"
            ):
                self._activity.tombstone(call_id)
            return False

        # An ordinary termination can only claim a pre-promotion transfer. Stop its
        # worker and wait for late Twilio creation/removal cleanup before completing
        # the conference. A promoted transfer already owns termination and cannot
        # reach this branch.
        if transfer_task is not None and transfer_task is not initiating_task:
            transfer_task.cancel()
            try:
                await self._await_network_task(transfer_task)
            except asyncio.CancelledError:
                logger.warning(
                    "owner transfer task cancelled before cleanup started call_id=%s", call_id
                )
            except Exception:
                logger.warning(
                    "owner transfer cleanup failed during termination call_id=%s",
                    call_id,
                    exc_info=True,
                )

        return await self._finish_claimed_termination(
            call,
            reason,
            preserve_conference=preserve_conference,
            await_finalizer=await_finalizer,
            expected_transfer_outcome=call.get("transfer_outcome"),
            initiating_task=initiating_task,
        )

    async def _teardown_call_media(
        self,
        call: dict[str, Any],
        *,
        preserve_conference: bool,
        initiating_task: asyncio.Task[Any] | None = None,
    ) -> bool:
        """Hang up the OpenAI leg and, unless preserved, complete the Twilio conference.

        Retries the conference completion once in-process on failure. Returns True if
        the conference completion is still unresolved afterwards, meaning the caller
        must keep the durable claim nonterminal for startup recovery to retry.
        """
        call_id = call["call_id"]
        # Keep the sideband receiving final usage while carrier legs disconnect.
        # Waiting for session.closed before hanging up strands the callee in silence
        # and adds billable time after the already-completed reply window.
        media_tasks = [self.live.hangup(call.get("openai_call_id"))]
        if not preserve_conference:
            media_tasks.append(
                self.twilio.complete_conference(
                    call.get("conference_sid") or call.get("conference_name")
                )
            )
        if (
            preserve_conference
            and call.get("media_stream_sid")
            and call.get("twilio_callee_call_sid")
        ):
            media_tasks.append(
                self.twilio.stop_audio_monitor(
                    call["twilio_callee_call_sid"], call["media_stream_sid"]
                )
            )
        media_tasks.append(self.live.drain_and_close(call_id, dependent_task=initiating_task))
        results = await asyncio.gather(*media_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.warning("media teardown operation failed", exc_info=result)
        conference_failed = (
            not preserve_conference and len(results) > 1 and isinstance(results[1], Exception)
        )
        if conference_failed:
            # Retry once in the live process. If Twilio still cannot confirm conference
            # completion, retain TERMINATING so startup recovery can adopt it again.
            await asyncio.sleep(TERMINATION_MEDIA_RETRY_DELAY_SECONDS)
            retry = asyncio.create_task(
                self.twilio.complete_conference(
                    call.get("conference_sid") or call.get("conference_name")
                ),
                name=f"retry-complete-conference:{call_id}",
            )
            try:
                await self._await_network_task(retry)
                conference_failed = False
            except Exception:
                logger.warning(
                    "conference completion retry failed call_id=%s", call_id, exc_info=True
                )
        return conference_failed

    @staticmethod
    def _classify_terminal_state(reason: str) -> CallState:
        """Map a termination reason to the durable terminal CallState it produces."""
        if reason == "transfer_completed":
            return CallState.TRANSFERRED
        if reason in {"time_limit", "setup_deadline", "watchdog_stale", "hold_timeout"}:
            return CallState.TIMED_OUT
        if reason in {
            "callee_participant_leave",
            "callee_call_completed",
            "conference_end",
            "voicemail_left",
            "owner_request",
            "voice_model_end_call",
            "openai_terminal_event",
        }:
            return CallState.COMPLETED
        return CallState.FAILED

    def _clear_call_runtime_state(self, call_id: str) -> None:
        """Drop the per-call in-memory tracking entries once termination is durable.

        Must run with no intervening await so nothing can observe a call as both
        terminal in the database and still tracked as in-flight here.
        """
        self._tombstone_call_activity(call_id)
        self._owner_transfer.clear_call(call_id)
        self._activation_locks.pop(call_id, None)
        self._tool_seen_calls.discard(call_id)
        self._pending_questions.pop(call_id, None)
        self._voice_end_pending.pop(call_id, None)
        self._voice_end_reply_waits.pop(call_id, None)
        self._callee_speech_epochs.pop(call_id, None)
        self._callee_speaking.discard(call_id)
        self._live_conversations.pop(call_id, None)
        self._call_audio.pop(call_id, None)
        self._media_tokens.pop(call_id, None)
        self._media_ready.pop(call_id, None)

    async def _finalize_call_best_effort(self, call_id: str) -> None:
        try:
            await self.finalizer.finalize(call_id)
        except Exception:
            logger.warning("terminal finalization failed call_id=%s", call_id, exc_info=True)

    async def _finish_claimed_termination(
        self,
        call: dict[str, Any],
        reason: str,
        *,
        preserve_conference: bool,
        await_finalizer: bool,
        expected_transfer_outcome: str | None = None,
        schedule_conference_retry: bool = True,
        initiating_task: asyncio.Task[Any] | None = None,
    ) -> bool:
        call_id = call["call_id"]
        # These two clears run before any media teardown I/O so the call stops looking
        # live to the watchdog/activity trackers immediately, not only once the (slow)
        # teardown below finishes.
        self._tombstone_call_activity(call_id)
        self._voice_end_pending.pop(call_id, None)
        self._voice_end_reply_waits.pop(call_id, None)
        conference_failed = await self._teardown_call_media(
            call, preserve_conference=preserve_conference, initiating_task=initiating_task
        )
        if conference_failed:
            # Keep the durable claim nonterminal. Startup recovery will retry the
            # required media teardown instead of publishing a false terminal result.
            if schedule_conference_retry:
                self._schedule_conference_completion_retry(
                    call,
                    reason=reason,
                    expected_transfer_outcome=expected_transfer_outcome,
                    await_finalizer=await_finalizer,
                )
            return False
        ended_at = datetime.now(UTC)
        started_at = (
            datetime.fromisoformat(call["started_at"]) if call.get("started_at") else ended_at
        )
        duration = max(0, int((ended_at - started_at).total_seconds()))
        terminal = self._classify_terminal_state(reason)
        terminal_error: BaseException | None = None
        try:
            terminalized = await self.db.finish_claimed_termination(
                call_id,
                expected_reason=reason,
                terminal_state=terminal,
                ended_at=ended_at.isoformat(),
                duration_seconds=duration,
                expected_transfer_outcome=expected_transfer_outcome,
            )
        except asyncio.CancelledError as exc:
            terminalized = False
            terminal_error = exc
        except Exception as exc:
            terminalized = False
            terminal_error = exc
        if not terminalized:
            current = await self.db.get_call(call_id)
            exact_terminal = bool(
                current
                and current.get("state") == terminal.value
                and current.get("termination_claimed")
                and current.get("termination_reason") == reason
                and current.get("transfer_outcome") == expected_transfer_outcome
            )
            if not exact_terminal:
                if schedule_conference_retry and not preserve_conference:
                    self._schedule_conference_completion_retry(
                        call,
                        reason=reason,
                        expected_transfer_outcome=expected_transfer_outcome,
                        await_finalizer=await_finalizer,
                    )
                if terminal_error is not None:
                    raise terminal_error
                logger.error(
                    "claimed termination lost its durable state call_id=%s reason=%s",
                    call_id,
                    reason,
                )
                return False
            if current is None:
                return False
            call = current
            logger.warning(
                "reconciled ambiguous terminal transition call_id=%s state=%s",
                call_id,
                terminal.value,
            )
        self._clear_call_runtime_state(call_id)
        try:
            cancelled = await self.db.cancel_pending_questions(call_id)
            for cancelled_row in cancelled:
                logger.info(
                    "ask_agent question cancelled call_id=%s question_id=%s "
                    "reason=termination_finished reason_code=%s",
                    call_id,
                    cancelled_row["question_id"],
                    reason,
                )
        except Exception:
            logger.warning(
                "pending question cancellation failed call_id=%s", call_id, exc_info=True
            )
        self._notify_call_event(call_id)
        try:
            await self.db.add_transcript_turn(
                call_id=call_id,
                turn_id=f"telephony_{secrets.token_urlsafe(10)}",
                speaker="system",
                text=f"Call ended with telephony reason: {reason}.",
                source_event_type="telephony.terminal",
                source_event_id=f"terminal:{call_id}:{reason}",
            )
        except asyncio.CancelledError:
            # The terminal row is the linearization point. Cancellation after that
            # commit may skip optional bookkeeping, but it must never unwind into
            # transfer compensation and tear down a successful handoff.
            logger.warning("terminal transcript bookkeeping cancelled call_id=%s", call_id)
        except Exception:
            logger.warning(
                "terminal transcript bookkeeping failed call_id=%s", call_id, exc_info=True
            )

        if await_finalizer:
            await self._finalize_call_best_effort(call_id)
        else:
            self._spawn(self._finalize_call_best_effort(call_id), name=f"finalize:{call_id}")
        self._queued_latency_events = {
            key: mark for key, mark in self._queued_latency_events.items() if key[0] != call_id
        }
        return True

    async def get_snapshot(self, call_id: str) -> CallSnapshot:
        call = await self.db.get_call(call_id)
        if call is None:
            raise LookupError(call_id)
        result = await self.db.get_result(call_id)
        return CallSnapshot(
            call_id=call_id,
            state=CallState(call["state"]),
            created_at=datetime.fromisoformat(call["created_at"]),
            started_at=datetime.fromisoformat(call["started_at"])
            if call.get("started_at")
            else None,
            answered_at=datetime.fromisoformat(call["answered_at"])
            if call.get("answered_at")
            else None,
            ended_at=datetime.fromisoformat(call["ended_at"]) if call.get("ended_at") else None,
            answered_by=call.get("answered_by"),
            answer_handling=call.get("answer_handling"),
            duration_seconds=call.get("duration_seconds"),
            result=result,
            cost=compute_call_cost(call, self.settings),
        )

    async def get_result(self, call_id: str) -> dict[str, Any]:
        snapshot = await self.get_snapshot(call_id)
        cost = snapshot.cost
        if snapshot.state in TERMINAL_STATES:
            result = snapshot.result
            if result is None or result.finalization_status == "telephony_only":
                # Telephony is terminal; never report in_progress while finalization catches up.
                result = await self.finalizer.finalize(call_id)
                # Finalization may have just persisted extractor token usage; refresh cost.
                call = await self.db.get_call(call_id)
                if call is not None:
                    cost = compute_call_cost(call, self.settings)
            return {
                "call_id": call_id,
                "state": snapshot.state,
                "result": result.model_dump(mode="json"),
                "cost": cost.model_dump(mode="json") if cost else None,
            }
        return {
            "call_id": call_id,
            "state": snapshot.state,
            "result": None,
            "cost": cost.model_dump(mode="json") if cost else None,
        }

    # -- Transport-facing delegators -----------------------------------------
    # Thin wrappers so routes never reach through the service into self.db /
    # self.openai directly; each preserves the exact args/return/exceptions of
    # the underlying call.

    def unwrap_openai_webhook(self, payload: bytes, headers: Any) -> dict[str, Any]:
        self.openai.webhooks.verify_signature(
            payload, headers, secret=Settings.reveal(self.settings.openai_webhook_secret)
        )
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise ValueError("invalid OpenAI event")
        return event

    async def record_webhook_once(self, webhook_id: str) -> bool:
        return await self.db.record_webhook_once(webhook_id)

    async def resolve_webhook_call(self, call_id: str, plan_id: str) -> dict[str, Any] | None:
        """Look up a Twilio-webhook call and confirm it maps to the given plan.

        Returns the call row when it exists and matches ``plan_id``, otherwise None.
        """
        call = await self.db.get_call(call_id)
        if call is None or call["plan_id"] != plan_id:
            return None
        return call

    async def get_call_record(self, call_id: str) -> dict[str, Any] | None:
        return await self.db.get_call(call_id)

    async def get_transcript_records(self, call_id: str) -> list[TranscriptTurn]:
        return await self.db.get_transcript(call_id)

    async def get_latency_event_records(self, call_id: str) -> list[dict[str, Any]]:
        return await self.db.get_latency_events(call_id)

    async def list_call_records(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self.db.list_calls(limit)

    async def acquire_deployment_lock(self) -> int:
        return await self.db.acquire_deployment_lock()

    async def release_deployment_lock(self) -> None:
        await self.db.release_deployment_lock()

    async def _setup_deadline(self, call_id: str) -> None:
        await asyncio.sleep(self.settings.setup_deadline_seconds)
        call = await self.db.get_call(call_id)
        setup_states = {
            CallState.PREWARMING,
            CallState.READY_TO_ACTIVATE,
            CallState.ACTIVATING,
        }
        if call and CallState(call["state"]) in setup_states:
            await self.terminate_call(call_id, "setup_deadline")

    async def _handle_sideband_fatal(self, call_id: str, reason: str) -> None:
        self._spawn(self.terminate_call(call_id, reason), name=f"terminate:{call_id}:sideband")

    async def _recover_nonterminal_call(self, call: dict[str, Any]) -> bool:
        """Adopt one still-open call left behind by a crash or restart.

        Returns True if this pass drove the call to a durable terminal state (so the
        caller should treat it as already recovered for the later cleanup passes).
        """
        call_id = call["call_id"]
        transfer_outcome = call.get("transfer_outcome")
        completed_transfer = bool(transfer_outcome and transfer_outcome.startswith("completed:"))
        claimed = await self.db.claim_startup_recovery(
            call_id,
            expected_transfer_outcome=transfer_outcome,
            completed_transfer=completed_transfer,
        )
        if claimed is None:
            return False
        expected_transfer_outcome = (
            transfer_outcome
            if completed_transfer
            else ("failed:startup_recovery" if transfer_outcome is not None else None)
        )
        if completed_transfer:
            owner_call_sid = claimed.get("twilio_owner_call_sid")
            owner_exit_armed = False
            if owner_call_sid:
                arm_owner_exit = asyncio.create_task(
                    self.twilio.enable_end_conference_on_exit(
                        claimed.get("conference_sid") or claimed.get("conference_name"),
                        owner_call_sid,
                    ),
                    name=f"recover-owner-conference-exit:{call_id}",
                )
                try:
                    await self._await_network_task(arm_owner_exit)
                    owner_exit_armed = True
                except Exception:
                    logger.warning(
                        "startup could not arm transferred owner exit call_id=%s",
                        call_id,
                        exc_info=True,
                    )
            if not owner_exit_armed:
                failure = "failed:owner_exit_unarmed"
                failure_reason = "transfer_failed:owner_exit_unarmed"
                try:
                    if not isinstance(transfer_outcome, str):
                        failed_closed = False
                    else:
                        failed_closed = await self.db.fail_promoted_transfer(
                            call_id,
                            transfer_outcome,
                            failure,
                            failure_reason,
                        )
                except Exception:
                    failed_closed = False
                    logger.warning(
                        "startup could not persist owner-exit failure call_id=%s",
                        call_id,
                        exc_info=True,
                    )
                if failed_closed:
                    await self._finish_claimed_termination(
                        claimed,
                        failure_reason,
                        preserve_conference=False,
                        await_finalizer=True,
                        expected_transfer_outcome=failure,
                    )
                else:
                    await self._complete_conference_or_schedule(claimed)
                return False
        try:
            terminalized = await self._finish_claimed_termination(
                claimed,
                "transfer_completed" if completed_transfer else "startup_recovery",
                preserve_conference=completed_transfer,
                await_finalizer=True,
                expected_transfer_outcome=expected_transfer_outcome,
            )
        except Exception:
            logger.exception("startup recovery failed call_id=%s", call_id)
            return False
        if not terminalized:
            logger.error("startup recovery scheduled teardown retry call_id=%s", call_id)
            return False
        return True

    async def _recover_pending_conference_cleanup(self, recovered: set[str]) -> None:
        # A crash between a failed compensation attempt and the in-process retry taking
        # over would otherwise permanently orphan the Twilio conference: the call row is
        # typically already terminal, so it is invisible to list_nonterminal_calls above.
        for call in await self.db.list_conference_cleanup_pending():
            call_id = call["call_id"]
            if call_id in recovered:
                continue
            try:
                # Twilio conference completion is idempotent (a 404 is treated as success
                # in twilio_bridge), so a duplicate completion here is harmless.
                await self._complete_conference_or_schedule(call)
            except Exception:
                logger.warning(
                    "startup conference cleanup recovery failed call_id=%s",
                    call_id,
                    exc_info=True,
                )

    async def _recover_pending_finalization(self, recovered: set[str]) -> None:
        # A crash can occur after telephony is terminal but before finalization committed.
        for call in await self.db.list_terminal_calls_needing_finalization():
            if call["call_id"] not in recovered:
                try:
                    await self.finalizer.finalize(call["call_id"])
                except Exception:
                    logger.warning(
                        "startup finalization failed call_id=%s",
                        call["call_id"],
                        exc_info=True,
                    )

    async def recover_startup(self) -> None:
        try:
            cancelled = await self.db.cancel_all_pending_questions()
            for cancelled_row in cancelled:
                logger.info(
                    "ask_agent question cancelled call_id=%s question_id=%s reason=startup_recovery",
                    cancelled_row["call_id"],
                    cancelled_row["question_id"],
                )
        except Exception:
            logger.warning("startup could not cancel pending questions", exc_info=True)
        recovered: set[str] = set()
        for call in await self.db.list_nonterminal_calls():
            if await self._recover_nonterminal_call(call):
                recovered.add(call["call_id"])
        await self._recover_pending_conference_cleanup(recovered)
        await self._recover_pending_finalization(recovered)

    def supervision_ready(self) -> bool:
        """Whether safety-relevant supervision is healthy enough for new calls.

        Distinct from process liveness: a live process can have a dead or failing
        watchdog, in which case it must stop accepting new billable calls while
        still servicing existing ones.
        """
        return self._supervision_healthy and not self._stopping

    async def start_watchdog(self) -> None:
        if not self._stopping and self._watchdog_task is None:
            task = asyncio.create_task(self._watchdog(), name="call-watchdog")
            task.add_done_callback(self._watchdog_finished)
            self._watchdog_task = task

    def _watchdog_finished(self, task: asyncio.Task[None]) -> None:
        if self._watchdog_task is task:
            self._watchdog_task = None
        if self._stopping:
            return
        # Reaching here means the supervisor loop died without an expected stop:
        # surface it as not-ready rather than silently losing supervision.
        self._supervision_healthy = False
        if task.cancelled():
            logger.error("call watchdog was cancelled outside of shutdown")
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "call watchdog exited unexpectedly",
                exc_info=(type(error), error, error.__traceback__),
            )
        else:  # pragma: no cover - defensive: the loop only returns via cancellation
            logger.error("call watchdog exited unexpectedly without an exception")

    async def stop(self) -> None:
        self._stopping = True
        if self._watchdog_task:
            self._watchdog_task.cancel()
            await asyncio.gather(self._watchdog_task, return_exceptions=True)
            self._watchdog_task = None
        # Shutdown is fail-closed even if a deployment guard or operator invariant
        # is violated. Claim ordinary calls before canceling transfer workers so the
        # existing claim protocol owns late owner-leg cleanup and conference teardown.
        try:
            nonterminal = await self.db.list_nonterminal_calls()
        except Exception:
            nonterminal = []
            logger.exception("failed to enumerate calls during shutdown")
        current = asyncio.current_task()
        shutdown_results = await asyncio.gather(
            *(
                self.terminate_call(
                    call["call_id"],
                    "service_shutdown",
                    _initiating_task=current,
                )
                for call in nonterminal
            ),
            return_exceptions=True,
        )
        for call, result in zip(nonterminal, shutdown_results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "shutdown call teardown failed call_id=%s",
                    call["call_id"],
                    exc_info=(type(result), result, result.__traceback__),
                )
        await self.live.close_all()
        # Drain to a stable empty set. Cancellation handlers may register shielded
        # compensation after the first snapshot; must-finish tasks are awaited rather
        # than cancelled, while the stopping gate turns all new ordinary work into no-ops.
        while True:
            pending = [task for task in self._background if task is not current]
            if not pending:
                break
            for task in pending:
                if task not in self._must_finish_background:
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(0)
        await self._activity.flush()
        try:
            if self._owns_exa_client:
                await self.exa.close()
        finally:
            if self._owns_openai_client:
                await self.openai.close()

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            await self._watchdog_iteration()
            if self._watchdog_failures:
                # Bounded exponential backoff so a persistent database failure does
                # not hot-loop, while still recovering as soon as a pass succeeds.
                delay = min(
                    WATCHDOG_INTERVAL_SECONDS * (2 ** min(self._watchdog_failures, 4)),
                    WATCHDOG_MAX_BACKOFF_SECONDS,
                )
                await asyncio.sleep(delay)

    async def _watchdog_iteration(self) -> None:
        """One supervised watchdog pass; never lets a transient failure kill the loop."""
        try:
            await self._watchdog_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._watchdog_failures += 1
            self._supervision_healthy = False
            logger.exception(
                "call watchdog iteration failed consecutive_failures=%s",
                self._watchdog_failures,
            )
        else:
            if not self._supervision_healthy:
                logger.warning(
                    "call watchdog supervision recovered after %s failure(s)",
                    self._watchdog_failures,
                )
            self._watchdog_failures = 0
            self._supervision_healthy = True
            self._watchdog_last_success_monotonic = time.monotonic()

    async def _watchdog_once(self) -> None:
        await self._activity.flush()
        now = LatencyMark.now()
        cutoff = datetime.fromisoformat(now.occurred_at) - timedelta(
            seconds=self.settings.watchdog_stale_seconds
        )
        stale_ns = self.settings.watchdog_stale_seconds * 1_000_000_000
        calls = await self.db.list_nonterminal_calls()
        for call in calls:
            call_id = call["call_id"]
            if self._activity.is_closed(call):
                self._activity.tombstone(call_id)
                continue
            pending = self._pending_questions.get(call_id)
            if pending is not None and (
                pending.delivering
                or time.monotonic() < pending.deadline_monotonic + WATCHDOG_QUESTION_GRACE_SECONDS
            ):
                # Outstanding ask_agent: silence is expected until answer/deadline delivery
                # finishes (delivering) or deadline+grace (covers cancel+send bounds).
                continue
            hold = self._hold_state.get(call_id)
            if hold is not None:
                if time.monotonic() - hold.started_monotonic >= self.settings.hold_max_seconds:
                    # Hold elapsed is monotonic and one-way, so no re-check dance is needed
                    # between this decision and the claim; reuse the stale-path protocol.
                    self._watchdog_claims.add(call_id)
                    terminated = False
                    try:
                        terminated = await self.terminate_call(call_id, "hold_timeout")
                    finally:
                        if not terminated:
                            self._watchdog_claims.discard(call_id)
                # else: silence is expected while on hold, within budget.
                continue
            if self._activity.dtmf_listen_grace_is_live(
                call_id,
                now_ns=now.monotonic_ns,
            ):
                # Do not refresh last_event_at here. When the bounded grace expires,
                # a still-silent call should become stale immediately rather than
                # receiving an additional full watchdog window.
                continue
            if datetime.fromisoformat(call["last_event_at"]) >= cutoff:
                continue
            if self._activity.assistant_work_is_live(call_id):
                # SIP playback / tool waits can exceed the heartbeat gap without sideband
                # frames. Refresh liveness so durable last_event_at stays aligned.
                self._activity.note(call_id, now)
                continue
            activity_before = self._activity.latest.get(call_id)
            if (
                activity_before is not None
                and now.monotonic_ns - activity_before.monotonic_ns <= stale_ns
            ):
                continue

            # The initial list can go stale while another callback updates the call.
            # Re-read both durable and in-memory liveness before claiming timeout.
            current = await self.db.get_call(call_id)
            if current is None:
                self._activity.clear(call_id)
                continue
            if self._activity.is_closed(current):
                self._activity.tombstone(call_id)
                continue
            if datetime.fromisoformat(current["last_event_at"]) >= cutoff:
                continue
            if self._activity.assistant_work_is_live(call_id):
                self._activity.note(call_id, now)
                continue
            activity_after = self._activity.latest.get(call_id)
            if activity_after is not None:
                changed = (
                    activity_before is None
                    or activity_after.monotonic_ns > activity_before.monotonic_ns
                )
                fresh = now.monotonic_ns - activity_after.monotonic_ns <= stale_ns
                if changed or fresh:
                    continue

            # No await is allowed between the final monotonic check and this claim.
            # That makes all pre-claim activity win and all post-claim activity lose.
            self._activity.watchdog_claims.add(call_id)
            terminated = False
            try:
                terminated = await self.terminate_call(call_id, "watchdog_stale")
            finally:
                if not terminated:
                    self._activity.watchdog_claims.discard(call_id)
