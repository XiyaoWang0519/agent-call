from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from time import monotonic_ns
from typing import Any

from openai import AsyncOpenAI

from app.db import (
    TRANSFER_ELIGIBLE_STATES,
    Database,
    DeploymentLockedError,
    LatencyMark,
    LatencyStage,
)
from app.finalizer import Finalizer
from app.models import (
    TERMINAL_STATES,
    AdvisoryOutcome,
    CallSnapshot,
    CallState,
    ContextPacket,
    PreparePhoneCallInput,
    PreparePhoneCallOutput,
    StartPhoneCallOutput,
    VoiceEndCallRequest,
)
from app.policy import validate_context
from app.settings import Settings
from app.twilio_bridge import TwilioBridge
from app.xai_client import create_xai_client
from app.xai_realtime import RealtimeBridge

logger = logging.getLogger(__name__)

CALL_ACTIVITY_TOMBSTONE_TTL_SECONDS = 15 * 60
CALL_ACTIVITY_TOMBSTONE_MAX = 4096
OWNER_JOIN_TIMEOUT_SECONDS = 30
# Over SIP, response.done marks the end of audio *generation*; playback to the phone lags
# behind because xAI drains a server-side output buffer in real time. Termination after a
# final spoken turn (voice-initiated end_call goodbye, voicemail) must wait for
# output_audio_buffer.stopped or the callee hears the closing words cut off. The fallback
# below bounds that wait in case the event is never delivered; it stays under the 15s
# watchdog staleness window so a completed call is not misreported as timed out.
TERMINATION_AUDIO_DRAIN_TIMEOUT_SECONDS = 12.0
TERMINATION_MEDIA_RETRY_DELAY_SECONDS = 0.1
TERMINATION_MEDIA_BACKGROUND_RETRY_BASE_SECONDS = 0.5
TERMINATION_MEDIA_BACKGROUND_RETRY_MAX_SECONDS = 15.0


class OwnerTransferDeparted(RuntimeError):
    """The owner leg became terminal before the AI handoff finished."""


class CallService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        twilio: TwilioBridge | None = None,
        xai: AsyncOpenAI | None = None,
    ):
        self.settings = settings
        self.db = db
        self._owns_xai_client = xai is None
        self.xai = xai if xai is not None else create_xai_client(settings)
        self.twilio = twilio or TwilioBridge(settings)
        self._latest_call_activity: dict[str, LatencyMark] = {}
        self._dirty_call_activity: dict[str, LatencyMark] = {}
        self._watchdog_claims: set[str] = set()
        self._activity_tombstones: OrderedDict[str, int] = OrderedDict()
        self.realtime = RealtimeBridge(
            settings,
            on_event=self.handle_realtime_event,
            on_open=self.handle_sideband_open,
            on_fatal=self._handle_sideband_fatal,
            on_send=self.handle_realtime_send,
            on_activity=self._note_call_activity,
        )
        self.finalizer = Finalizer(settings, db, self.xai)
        self._background: set[asyncio.Task[Any]] = set()
        self._must_finish_background: set[asyncio.Task[Any]] = set()
        self._conference_retry_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._stopping = False
        self._owner_join_events: dict[str, asyncio.Event] = {}
        self._owner_departure_events: dict[str, asyncio.Event] = {}
        self._owner_expected_sids: dict[str, str] = {}
        self._owner_joined_sids: dict[str, str | None] = {}
        self._owner_failures: dict[str, tuple[str | None, str]] = {}
        self._owner_transfer_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._owner_transfer_locks: dict[str, asyncio.Lock] = {}
        self._opening_transition_locks: dict[str, asyncio.Lock] = {}
        self._voice_end_pending: dict[str, tuple[str, str | None]] = {}
        self._audio_drain_terminations: dict[str, tuple[str | None, str]] = {}
        self._active_response_ids: dict[str, str | None] = {}
        self._tool_seen_calls: set[str] = set()
        self._queued_latency_events: dict[tuple[str, LatencyStage, str], LatencyMark] = {}
        self._watchdog_task: asyncio.Task[None] | None = None

    def _spawn(self, coroutine, *, name: str, must_finish: bool = False) -> asyncio.Task[Any]:
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

    def _opening_transition_lock(self, call_id: str) -> asyncio.Lock:
        return self._opening_transition_locks.setdefault(call_id, asyncio.Lock())

    def _owner_transfer_lock(self, call_id: str) -> asyncio.Lock:
        return self._owner_transfer_locks.setdefault(call_id, asyncio.Lock())

    def _owner_sid_matches(self, call_id: str, call_sid: str | None) -> bool:
        expected = self._owner_expected_sids.get(call_id)
        return expected is None or call_sid is None or call_sid == expected

    def _record_owner_join(self, call_id: str, call_sid: str | None) -> None:
        event = self._owner_join_events.get(call_id)
        if event is None or not self._owner_sid_matches(call_id, call_sid):
            return
        self._owner_joined_sids[call_id] = call_sid
        event.set()

    def _record_owner_failure(self, call_id: str, call_sid: str | None, reason: str) -> None:
        event = self._owner_join_events.get(call_id)
        if event is None or not self._owner_sid_matches(call_id, call_sid):
            return
        self._owner_failures[call_id] = (call_sid, reason)
        self._owner_departure_events.setdefault(call_id, asyncio.Event()).set()
        event.set()

    def _track_owner_transfer(self, call_id: str, task: asyncio.Task[dict[str, Any]]) -> None:
        self._owner_transfer_tasks[call_id] = task

        def finished(completed: asyncio.Task[dict[str, Any]]) -> None:
            if self._owner_transfer_tasks.get(call_id) is completed:
                self._owner_transfer_tasks.pop(call_id, None)
            if completed.cancelled():
                return
            try:
                error = completed.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                logger.error(
                    "owner transfer task failed call_id=%s",
                    call_id,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(finished)

    def _note_call_activity(self, call_id: str, mark: LatencyMark | None = None) -> bool:
        # A watchdog claim is the liveness linearization point. Activity observed
        # before the claim updates these maps; activity after it cannot resurrect a
        # call whose timeout teardown has already won.
        self._prune_activity_tombstones()
        if call_id in self._watchdog_claims or call_id in self._activity_tombstones:
            return False
        observed = mark or LatencyMark.now()
        latest = self._latest_call_activity.get(call_id)
        if latest is not None and latest.monotonic_ns >= observed.monotonic_ns:
            return True
        self._latest_call_activity[call_id] = observed
        dirty = self._dirty_call_activity.get(call_id)
        if dirty is None or dirty.monotonic_ns < observed.monotonic_ns:
            self._dirty_call_activity[call_id] = observed
        return True

    def _clear_call_activity(self, call_id: str) -> None:
        self._latest_call_activity.pop(call_id, None)
        self._dirty_call_activity.pop(call_id, None)
        self._watchdog_claims.discard(call_id)

    def _tombstone_call_activity(self, call_id: str) -> None:
        now_ns = monotonic_ns()
        self._prune_activity_tombstones(now_ns)
        self._activity_tombstones.pop(call_id, None)
        self._activity_tombstones[call_id] = now_ns + (
            CALL_ACTIVITY_TOMBSTONE_TTL_SECONDS * 1_000_000_000
        )
        while len(self._activity_tombstones) > CALL_ACTIVITY_TOMBSTONE_MAX:
            self._activity_tombstones.popitem(last=False)
        self._clear_call_activity(call_id)

    def _prune_activity_tombstones(self, now_ns: int | None = None) -> None:
        cutoff = monotonic_ns() if now_ns is None else now_ns
        while self._activity_tombstones:
            _, expires_at = next(iter(self._activity_tombstones.items()))
            if expires_at > cutoff:
                break
            self._activity_tombstones.popitem(last=False)

    @staticmethod
    def _call_activity_is_closed(call: dict[str, Any]) -> bool:
        state = CallState(call["state"])
        return state == CallState.TERMINATING or state in TERMINAL_STATES

    async def _flush_call_activity(self) -> None:
        if not self._dirty_call_activity:
            return
        pending = self._dirty_call_activity
        self._dirty_call_activity = {}
        try:
            await self.db.touch_calls(
                (call_id, mark.occurred_at) for call_id, mark in pending.items()
            )
        except BaseException as exc:
            # Keep the newest observation if more activity arrived while the write
            # was in flight. A terminal cleanup removes the latest map entry and
            # therefore prevents a failed flush from re-adding a dead call.
            for call_id, failed_mark in pending.items():
                latest = self._latest_call_activity.get(call_id)
                if latest is None:
                    continue
                candidate = (
                    latest if latest.monotonic_ns >= failed_mark.monotonic_ns else failed_mark
                )
                current = self._dirty_call_activity.get(call_id)
                if current is None or current.monotonic_ns < candidate.monotonic_ns:
                    self._dirty_call_activity[call_id] = candidate
            if not isinstance(exc, Exception):
                raise
            logger.warning("failed to persist batched call activity", exc_info=True)

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

    async def start(
        self, plan_id: str, *, explicit_confirmation: bool, confirmation_text: str
    ) -> StartPhoneCallOutput:
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
        conference_name = f"poke-{secrets.token_hex(16)}"
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
            raise ValueError(
                json.dumps(
                    {"code": "plan_unavailable", "message": "Plan is expired or already started"}
                )
            )
        agent_request = LatencyMark.now()
        try:
            participant = await self.twilio.create_agent_participant(
                call_id=call_id,
                plan_id=plan_id,
                conference_name=conference_name,
            )
        except Exception:
            self._queue_latency_batch(
                call_id,
                (LatencyStage.TWILIO_AGENT_REQUEST, agent_request, ""),
            )
            logger.exception("failed to originate agent leg")
            await self.terminate_call(call_id, "agent_leg_setup_failed")
            raise
        agent_created = LatencyMark.now()
        self._queue_latency_batch(
            call_id,
            (LatencyStage.TWILIO_AGENT_REQUEST, agent_request, ""),
            (LatencyStage.TWILIO_AGENT_CREATED, agent_created, ""),
        )
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

    async def handle_xai_incoming(self, xai_call_id: str, sip_headers: list[dict[str, str]]) -> str:
        headers = {item.get("name", "").lower(): item.get("value", "") for item in sip_headers}
        call_id = headers.get("x-bridge-call-id") or headers.get("x_bridge_call_id")
        plan_id = headers.get("x-plan-id") or headers.get("x_plan_id")
        if call_id is None:
            joined = " ".join(headers.values())
            for candidate in await self.db.list_nonterminal_calls():
                if candidate["call_id"] in joined or candidate["plan_id"] in joined:
                    call_id = candidate["call_id"]
                    break
        call = await self.db.get_call(call_id) if call_id else None
        if call is None or (plan_id and call["plan_id"] != plan_id):
            await self.realtime.reject(xai_call_id)
            raise LookupError("incoming SIP call could not be mapped to an approved plan")
        if call.get("xai_call_id") and call["xai_call_id"] != xai_call_id:
            await self.realtime.reject(xai_call_id)
            raise RuntimeError("call already mapped to a different XAI call")
        await self.db.update_call(call_id, xai_call_id=xai_call_id)
        plan = await self.db.get_plan(call["plan_id"])
        packet = ContextPacket.model_validate(plan["context"])
        accept_request = LatencyMark.now()
        try:
            connect_status = await self.realtime.connect(
                call_id=call_id,
                xai_call_id=xai_call_id,
                packet=packet,
            )
        except Exception:
            self._queue_latency_batch(
                call_id,
                (LatencyStage.XAI_CONNECT_REQUEST, accept_request, ""),
            )
            logger.exception("failed to connect xAI call")
            await self.terminate_call(call_id, "xai_connect_failed")
            raise
        accept_completed = LatencyMark.now()
        self._queue_latency_batch(
            call_id,
            (LatencyStage.XAI_CONNECT_REQUEST, accept_request, ""),
            (LatencyStage.XAI_CONNECT_COMPLETED, accept_completed, ""),
        )
        try:
            await self.db.update_call(call_id, xai_connect_status=connect_status)
        except Exception:
            logger.exception("failed to persist XAI connect status")
            await self.terminate_call(call_id, "xai_connect_failed")
            raise
        return call_id

    async def handle_sideband_open(self, call_id: str) -> None:
        sideband_open = LatencyMark.now()
        await self.db.set_flag_once(call_id, "sideband_open")
        try:
            updated = await self.realtime.verify_initial_session(call_id)
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
        transcription_ok = self.realtime.expected_transcription_echoed(updated)
        vad_ok = self.realtime.expected_initial_vad_echoed(updated)
        await self.db.update_call(
            call_id,
            transcription_verified=int(transcription_ok),
            vad_verified=int(vad_ok),
        )
        if not transcription_ok or not vad_ok:
            await self.terminate_call(call_id, "transcription_config_mismatch")
            return
        call = await self.db.get_call(call_id)
        if (
            call
            and CallState(call["state"]) not in TERMINAL_STATES
            and await self.db.set_flag_once(call_id, "callee_dialed")
        ):
            plan = await self.db.get_plan(call["plan_id"])
            packet = ContextPacket.model_validate(plan["context"])
            callee_request = LatencyMark.now()
            try:
                participant = await self.twilio.create_callee_participant(
                    call_id=call_id,
                    plan_id=call["plan_id"],
                    conference_sid_or_name=call.get("conference_sid") or call["conference_name"],
                    packet=packet,
                )
            except Exception:
                self._queue_latency_batch(
                    call_id,
                    (LatencyStage.TWILIO_CALLEE_REQUEST, callee_request, ""),
                )
                logger.exception("failed to originate callee leg")
                await self.terminate_call(call_id, "callee_leg_setup_failed")
                return
            callee_created = LatencyMark.now()
            self._queue_latency_batch(
                call_id,
                (LatencyStage.TWILIO_CALLEE_REQUEST, callee_request, ""),
                (LatencyStage.TWILIO_CALLEE_CREATED, callee_created, ""),
            )
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

    async def handle_amd(self, call_id: str, answered_by: str) -> None:
        received = LatencyMark.now()
        if not self._note_call_activity(call_id, received):
            return
        call = await self.db.get_call(call_id)
        if call is None:
            self._clear_call_activity(call_id)
            return
        if self._call_activity_is_closed(call):
            self._tombstone_call_activity(call_id)
            return
        if not self._note_call_activity(call_id, received):
            return
        normalized = (answered_by or "unknown").lower()
        if normalized == "fax":
            handling = "fax"
        elif normalized.startswith("machine_end_"):
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
        if not self._note_call_activity(call_id, received):
            return
        call = await self.db.get_call(call_id)
        if call is None:
            self._clear_call_activity(call_id)
            return
        if self._call_activity_is_closed(call):
            self._tombstone_call_activity(call_id)
            return
        if not self._note_call_activity(call_id, received):
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
                    await self._start_opening_on_answer(call_id)
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
        if not self._note_call_activity(call_id, received):
            return
        call = await self.db.get_call(call_id)
        if call is None:
            self._clear_call_activity(call_id)
            return
        if self._call_activity_is_closed(call):
            self._tombstone_call_activity(call_id)
            return
        if not self._note_call_activity(call_id, received):
            return
        await self.db.touch_call(call_id)
        if leg == "callee" and status in {"in-progress", "answered"}:
            # The callee's answered status callback usually reaches us before the
            # conference participant-join event; whichever arrives first starts the
            # opening turn so the callee hears the model sooner.
            answered = received
            try:
                if await self.db.set_flag_once(call_id, "callee_joined"):
                    await self.db.update_call(call_id, answered_at=answered.occurred_at)
                await self._start_opening_on_answer(call_id)
                await self._check_activation_gate(call_id)
            finally:
                self._queue_latency_batch(
                    call_id,
                    (LatencyStage.CALLEE_ANSWERED, answered, ""),
                )
        elif leg == "callee" and status in {"completed", "failed", "busy", "no-answer", "canceled"}:
            duration = int(form.get("CallDuration") or 0)
            reason = "callee_call_completed" if status == "completed" else f"callee_{status}"
            if status == "completed" and duration >= self.settings.max_call_seconds:
                reason = "time_limit"
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
            and call["amd_result"]
            and call["transcription_verified"]
            and call["vad_verified"]
        ):
            return
        if not await self.db.cas_state(call_id, CallState.PREWARMING, CallState.READY_TO_ACTIVATE):
            return
        await self._activate(call_id)

    async def _start_opening_on_answer(self, call_id: str) -> None:
        """Let the model open the call as soon as the callee joins.

        Conference Participant AMD runs asynchronously, so waiting for its callback leaves a
        human callee in several seconds of silence. Keep automatic responses disabled until AMD
        completes, but ask the model for an opening turn immediately. The application supplies no
        response-specific script; the model chooses the opening from Poke's approved context.
        """
        async with self._opening_transition_lock(call_id):
            call = await self.db.get_call(call_id)
            if call is None or CallState(call["state"]) in TERMINAL_STATES:
                return
            if not (
                call["sideband_open"]
                and call["callee_joined"]
                and call["transcription_verified"]
                and call["vad_verified"]
            ):
                return
            if call.get("answer_handling") == "voicemail":
                return
            if not await self.db.claim_opening_if_not_voicemail(call_id):
                return

            # Keep the atomic claim and its WebSocket write ordered against the voicemail
            # cancel/write pair. The lock is per call and does not cover Twilio network I/O.
            await self.realtime.create_opening(call_id)

        conference = call.get("conference_sid") or call.get("conference_name")
        # The explicit unmute is a race-safety net. Sending the opening first ensures its
        # Twilio round-trip can never delay the model's response.create.
        await self._unmute_agent(call_id, conference, call.get("twilio_ai_call_sid"))

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
        if not await self.db.cas_state(call_id, CallState.READY_TO_ACTIVATE, CallState.ACTIVATING):
            return
        call = await self.db.get_call(call_id)
        if call is None:
            return
        is_voicemail = call["answer_handling"] == "voicemail"
        if not is_voicemail:
            try:
                updated = await self.realtime.enable_automatic_responses(call_id)
            except Exception:
                await self.terminate_call(call_id, "session_update_timeout")
                return
            if not self.realtime.activation_update_confirmed(updated):
                await self.terminate_call(call_id, "session_update_mismatch")
                return
        if not await self.db.cas_state(call_id, CallState.ACTIVATING, CallState.ACTIVE):
            return
        if is_voicemail:
            conference = call.get("conference_sid") or call.get("conference_name")
            unmute = asyncio.create_task(
                self._unmute_agent(call_id, conference, call.get("twilio_ai_call_sid")),
                name=f"unmute:{call_id}:voicemail",
            )
            try:
                async with self._opening_transition_lock(call_id):
                    current = await self.db.get_call(call_id)
                    if current is None:
                        return
                    if await self.db.set_flag_once(call_id, "voicemail_sent"):
                        # If the opening claim won, its response.create completed under this
                        # same lock. Otherwise AMD was persisted first and the atomic opening
                        # claim cannot succeed after this voicemail write.
                        if current.get("opening_sent"):
                            response_id = self._active_response_ids.pop(call_id, None)
                            await self.realtime.cancel_response(call_id, response_id)
                        await self.realtime.create_voicemail(call_id)
            finally:
                await unmute
        else:
            await self._start_opening_on_answer(call_id)

    async def handle_realtime_event(self, call_id: str, event: dict[str, Any]) -> None:
        received = LatencyMark.now()
        event_type = event.get("type", "")
        event_id = event.get("event_id") or f"evt_{secrets.token_urlsafe(12)}"
        if event_type in {
            "response.output_audio_transcript.delta",
            "response.output_audio_transcript.done",
            "response.audio_transcript.delta",
            "response.audio_transcript.done",
        }:
            self._queue_latency(call_id, LatencyStage.FIRST_ASSISTANT_TRANSCRIPT, received)
        elif event_type in {"response.output_audio.delta", "response.audio.delta"}:
            self._queue_latency(call_id, LatencyStage.FIRST_XAI_AUDIO_DELTA, received)
        if event_type == "session.created":
            # Observational only. A SIP sideband attaches to an existing session and may
            # miss this startup event, so readiness is established by the explicit
            # session.update/session.updated handshake in handle_sideband_open.
            logger.debug("observed session.created call_id=%s", call_id)
        elif event_type == "conversation.item.input_audio_transcription.completed":
            text = event.get("transcript", "").strip()
            if text:
                await self.db.add_transcript_turn(
                    call_id=call_id,
                    turn_id=event.get("item_id") or event_id,
                    speaker="callee",
                    text=text,
                    source_event_type=event_type,
                    source_event_id=event_id,
                )
        elif event_type in {
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        }:
            text = event.get("transcript", "").strip()
            if text:
                await self.db.add_transcript_turn(
                    call_id=call_id,
                    turn_id=event.get("item_id") or event_id,
                    speaker="assistant",
                    text=text,
                    source_event_type=event_type,
                    source_event_id=event_id,
                )
        elif event_type == "response.function_call_arguments.done":
            self._tool_seen_calls.add(call_id)
            await self._handle_tool_call(
                call_id,
                event,
                received=received,
                event_key=str(event.get("call_id") or event_id),
            )
        elif event_type == "response.created":
            if call_id in self._tool_seen_calls:
                self._tool_seen_calls.discard(call_id)
                await self.db.mark_tool_continuation_observed(call_id)
            response = event.get("response") or {}
            response_id = response.get("id") or event.get("response_id")
            self._active_response_ids[call_id] = response_id
            pending = self._voice_end_pending.get(call_id)
            if pending and pending[1] is None and response_id:
                self._voice_end_pending[call_id] = (pending[0], response_id)
        elif event_type in {"response.done", "response.audio.done"}:
            call = await self.db.get_call(call_id)
            response = event.get("response") or {}
            status = response.get("status")
            if event_type == "response.done":
                self._active_response_ids.pop(call_id, None)
            if status in {"cancelled", "canceled"}:
                await self.db.update_call(call_id, interruption_observed=1)
            elif status == "failed":
                logger.error(
                    "realtime response failed call_id=%s status_details=%s event=%s",
                    call_id,
                    response.get("status_details"),
                    event,
                )
            if event_type == "response.done":
                await self._handle_voice_end_response_done(call_id, event)
            # A cancelled response.done here is the opening turn we cancelled when AMD
            # reported voicemail; the voicemail response itself is still in flight.
            if call and call.get("voicemail_sent") and status not in {"cancelled", "canceled"}:
                self._terminate_after_audio_drain(
                    call_id,
                    response.get("id") or event.get("response_id"),
                    "voicemail_left",
                )
        elif event_type in {"output_audio_buffer.stopped", "output_audio_buffer.cleared"}:
            self._handle_output_audio_drained(call_id, event)
        elif event_type in {"session.ended", "call.ended"}:
            self._spawn(
                self.terminate_call(call_id, "xai_terminal_event"),
                name=f"terminate:{call_id}:xai-terminal",
            )
        elif event_type == "error":
            error = event.get("error") or {}
            if error.get("code") == "response_cancel_not_active":
                # Benign race: the response we tried to cancel finished on its own.
                logger.info("stale response.cancel ignored call_id=%s event=%s", call_id, event)
                return
            logger.error("realtime error event call_id=%s event=%s", call_id, event)
            self._spawn(
                self.terminate_call(call_id, "xai_fatal_error"),
                name=f"terminate:{call_id}:xai-error",
            )

    async def handle_realtime_send(self, call_id: str, event: dict[str, Any]) -> None:
        sent = LatencyMark.now()
        event_type = event.get("type")
        if event_type == "response.create":
            self._queue_latency(call_id, LatencyStage.FIRST_RESPONSE_CREATE, sent)
            return
        item = event.get("item") or {}
        if event_type == "conversation.item.create" and item.get("type") == "function_call_output":
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
        continuation_instructions: str | None = None,
    ) -> None:
        """Swallow benign teardown races (e.g. "sideband is not open") so they cannot
        escalate into a fatal error and redundant termination. CancelledError must still
        propagate so the dispatcher stays cancellable."""

        try:
            await self.realtime.send_tool_result(
                call_id,
                tool_call_id,
                output,
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

    async def _send_nontransfer_tool_result(
        self,
        call_id: str,
        tool_call_id: str,
        output: dict[str, Any],
        *,
        received: LatencyMark,
        event_key: str,
        advisory_outcome: dict[str, Any] | None = None,
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
                continuation_instructions=continuation_instructions,
            )
            return
        try:
            await self._guarded_send_tool_result(
                call_id,
                tool_call_id,
                output,
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
        try:
            arguments = json.loads(event.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if name == "record_call_outcome":
            advisory_outcome: dict[str, Any] | None = None
            try:
                advisory = AdvisoryOutcome.model_validate(arguments)
                advisory_outcome = advisory.model_dump(mode="json", by_alias=True)
                output = {"accepted": True}
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
        elif name == "end_call":
            try:
                request = VoiceEndCallRequest.model_validate(arguments)
            except Exception as exc:
                await self._send_nontransfer_tool_result(
                    call_id,
                    tool_call_id,
                    {"accepted": False, "error": str(exc)},
                    received=received,
                    event_key=event_key,
                )
                return
            pending = (tool_call_id, None)
            self._voice_end_pending[call_id] = pending
            # Arm teardown before either the WebSocket send or post-send bookkeeping
            # can fail. Otherwise a durable-write failure could skip the only fallback.
            self._spawn(
                self._voice_end_fallback(call_id, tool_call_id),
                name=f"voice-end-fallback:{call_id}",
            )
            await self._send_nontransfer_tool_result(
                call_id,
                tool_call_id,
                {"accepted": True, "reason": request.reason},
                received=received,
                event_key=event_key,
                continuation_instructions=(
                    "The call is now ending. Briefly confirm the outcome or next step if useful, "
                    "then say one concise, natural goodbye. Do not call any function."
                ),
            )
        elif name == "transfer_to_owner":
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
                    await self.realtime.send_tool_result(
                        call_id,
                        tool_call_id,
                        {"accepted": False, "error": error},
                    )
            finally:
                await persistence
        else:
            await self._send_nontransfer_tool_result(
                call_id,
                tool_call_id,
                {"accepted": False, "error": "unknown tool"},
                received=received,
                event_key=event_key,
            )

    async def _handle_voice_end_response_done(self, call_id: str, event: dict[str, Any]) -> None:
        pending = self._voice_end_pending.get(call_id)
        if pending is None:
            return
        _, expected_response_id = pending
        if expected_response_id is None:
            return
        response = event.get("response") or {}
        response_id = response.get("id") or event.get("response_id")
        if expected_response_id and response_id and expected_response_id != response_id:
            return
        status = response.get("status")
        if status in {"cancelled", "canceled", "failed", "incomplete"}:
            self._voice_end_pending.pop(call_id, None)
            return
        if status != "completed":
            return
        self._voice_end_pending.pop(call_id, None)
        self._terminate_after_audio_drain(call_id, response_id, "voice_model_end_call")

    async def _voice_end_fallback(self, call_id: str, tool_call_id: str) -> None:
        await asyncio.sleep(15)
        pending = self._voice_end_pending.get(call_id)
        if pending is None or pending[0] != tool_call_id:
            return
        self._voice_end_pending.pop(call_id, None)
        await self.terminate_call(call_id, "voice_model_end_call")

    def _terminate_after_audio_drain(
        self, call_id: str, response_id: str | None, reason: str
    ) -> None:
        """Delay a post-final-response termination until SIP playback finishes.

        response.done only means generation finished; hanging up right away truncates the
        closing words still buffered on xAI's side. output_audio_buffer.stopped (or
        .cleared, when the callee interrupts) marks actual end of playback. A bounded
        fallback still terminates if neither event is ever delivered.
        """

        self._audio_drain_terminations[call_id] = (response_id, reason)
        self._spawn(
            self._audio_drain_fallback(call_id, response_id, reason),
            name=f"audio-drain-fallback:{call_id}",
        )

    def _handle_output_audio_drained(self, call_id: str, event: dict[str, Any]) -> None:
        pending = self._audio_drain_terminations.get(call_id)
        if pending is None:
            return
        expected_response_id, reason = pending
        response_id = event.get("response_id")
        if expected_response_id and response_id and expected_response_id != response_id:
            return
        self._audio_drain_terminations.pop(call_id, None)
        self._spawn(
            self.terminate_call(call_id, reason),
            name=f"terminate:{call_id}:audio-drained",
        )

    async def _audio_drain_fallback(
        self, call_id: str, response_id: str | None, reason: str
    ) -> None:
        await asyncio.sleep(TERMINATION_AUDIO_DRAIN_TIMEOUT_SECONDS)
        pending = self._audio_drain_terminations.get(call_id)
        if pending is None or pending != (response_id, reason):
            return
        self._audio_drain_terminations.pop(call_id, None)
        await self.terminate_call(call_id, reason)

    async def transfer_to_owner(
        self, call_id: str, reason: str, *, terminate_after: bool = True
    ) -> dict[str, Any]:
        # A successful handoff is always terminal. Keep the legacy keyword for callers
        # but do not allow a claimed transfer to strand the call in TERMINATING.
        del terminate_after
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
        if self._stopping:
            return None, "service is stopping"
        call = await self.db.get_call(call_id)
        if call is None:
            return None, "call not found"
        plan = await self.db.get_plan(call["plan_id"])
        if plan is None:
            return None, "call plan not found"
        packet = ContextPacket.model_validate(plan["context"])
        if packet.escalation.mode != "transfer_to_owner":
            return None, "owner transfer is not authorized"

        async def claim_and_spawn() -> tuple[asyncio.Task[dict[str, Any]] | None, str | None]:
            async with self._owner_transfer_lock(call_id):
                if call_id in self._owner_transfer_tasks:
                    return None, "owner transfer already in progress"
                if self._stopping:
                    return None, "service is stopping"
                joining = f"joining:{reason}"
                claim_error: Exception | None = None
                try:
                    claimed = await self.db.claim_transfer_joining(call_id, reason)
                except Exception as exc:
                    claimed = False
                    claim_error = exc
                claimed_call = call
                if not claimed:
                    current = await self.db.get_call(call_id)
                    exact_joining = bool(
                        current
                        and current.get("state") in TRANSFER_ELIGIBLE_STATES
                        and not current.get("termination_claimed")
                        and current.get("transfer_outcome") == joining
                    )
                    if not exact_joining:
                        if claim_error is not None:
                            raise claim_error
                        return None, "owner transfer already attempted or call is ending"
                    claimed_call = current
                    logger.warning("reconciled ambiguous owner transfer claim call_id=%s", call_id)
                if self._stopping:
                    await self.db.fail_joining_transfer(call_id, joining, "failed:service_stopping")
                    return None, "service is stopping"
                # Install callback-visible state before dialing can emit owner status.
                self._owner_join_events.setdefault(call_id, asyncio.Event())
                self._owner_departure_events.setdefault(call_id, asyncio.Event())
                task = self._spawn(
                    self._owner_transfer_workflow(
                        claimed_call,
                        packet,
                        reason,
                        tool_call_id=tool_call_id,
                    ),
                    name=f"owner-transfer:{call_id}",
                )
                self._track_owner_transfer(call_id, task)
                return task, None

        # Cancellation after SQLite commits but before this caller observes the result
        # must not leave durable joining without a registered cleanup workflow.
        claim = self._spawn(
            claim_and_spawn(),
            name=f"claim-owner-transfer:{call_id}",
            must_finish=True,
        )
        return await self._await_network_task(claim)

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

    async def _owner_transfer_workflow(
        self,
        call: dict[str, Any],
        packet: ContextPacket,
        reason: str,
        *,
        tool_call_id: str | None,
    ) -> dict[str, Any]:
        call_id = call["call_id"]
        joining = f"joining:{reason}"
        in_progress = f"in_progress:{reason}"
        conference = call.get("conference_sid") or call["conference_name"]
        event = self._owner_join_events.setdefault(call_id, asyncio.Event())
        departure_event = self._owner_departure_events.setdefault(call_id, asyncio.Event())
        owner_call_sid: str | None = None
        owner_cleanup_started = False
        create_task: asyncio.Task[Any] | None = None
        promoted: dict[str, Any] | None = None
        phase = "joining"
        completed_outcome: str | None = None
        workflow_task = asyncio.current_task()

        async def cleanup_owner() -> bool:
            nonlocal owner_cleanup_started
            if owner_call_sid is None:
                return True
            if owner_cleanup_started:
                return False
            owner_cleanup_started = True
            cleanup = asyncio.create_task(
                self.twilio.remove_participant(conference, owner_call_sid),
                name=f"remove-owner:{call_id}",
            )
            try:
                await self._await_network_task(cleanup)
                return True
            except Exception:
                # The primary transfer/termination outcome must survive cleanup failure.
                # Full conference teardown is the remaining compensation path.
                logger.warning(
                    "failed to clean up owner transfer leg call_id=%s", call_id, exc_info=True
                )
                return False

        async def complete_conference_compensation() -> bool:
            return await self._complete_conference_or_schedule(call)

        def owner_failure_reason() -> str | None:
            failure = self._owner_failures.get(call_id)
            if failure is None:
                return None
            failure_sid, failure_reason = failure
            if owner_call_sid and failure_sid and failure_sid != owner_call_sid:
                return None
            return failure_reason

        async def _fail_joining_and_compensate(failure: str, cleaned: bool) -> None:
            transitioned: bool | None
            promoted_transition: bool | None = False
            try:
                transitioned = await self.db.fail_joining_transfer(call_id, joining, failure)
            except Exception:
                transitioned = None
                logger.warning(
                    "joining transfer failure could not be persisted call_id=%s",
                    call_id,
                    exc_info=True,
                )
            if transitioned is not True:
                # The promotion UPDATE may have committed even if awaiting it raised.
                # Probe the exact in-progress value with a guarded CAS; this adopts
                # teardown ownership without a fresh read or a false transferred result.
                suffix = failure.removeprefix("failed:") or "owner_transfer"
                failure_reason = f"transfer_failed:{suffix}"
                try:
                    promoted_transition = await self.db.fail_promoted_transfer(
                        call_id,
                        in_progress,
                        failure,
                        failure_reason,
                    )
                except Exception:
                    promoted_transition = None
                    logger.warning(
                        "ambiguous transfer promotion could not be adopted call_id=%s",
                        call_id,
                        exc_info=True,
                    )
                if promoted_transition is True:
                    try:
                        finished = await self._finish_claimed_termination(
                            call,
                            failure_reason,
                            preserve_conference=False,
                            await_finalizer=False,
                            expected_transfer_outcome=failure,
                        )
                    except Exception:
                        finished = False
                        logger.warning(
                            "adopted transfer termination failed call_id=%s",
                            call_id,
                            exc_info=True,
                        )
                    if finished:
                        return
                    await complete_conference_compensation()
                    return
                if promoted_transition is None:
                    await complete_conference_compensation()
                    return
            if not cleaned and transitioned is True:
                try:
                    terminated = await self.terminate_call(
                        call_id,
                        "transfer_cleanup_failed",
                        _initiating_task=workflow_task,
                    )
                except Exception:
                    terminated = False
                    logger.warning(
                        "owner cleanup termination failed call_id=%s",
                        call_id,
                        exc_info=True,
                    )
                if not terminated:
                    await complete_conference_compensation()
            elif owner_call_sid is not None and (transitioned is None or not cleaned):
                # SQLite state is ambiguous. Conference completion is the only
                # DB-independent guarantee that the owner leg cannot keep billing.
                await complete_conference_compensation()

        async def fail_joining_and_compensate(failure: str, cleaned: bool) -> None:
            compensation = self._spawn(
                _fail_joining_and_compensate(failure, cleaned),
                name=f"compensate-joining-transfer:{call_id}",
                must_finish=True,
            )
            await self._await_network_task(compensation)

        async def _fail_promoted_and_compensate(
            expected: str, failure: str, failure_reason: str
        ) -> None:
            transitioned: bool | None
            try:
                transitioned = await self.db.fail_promoted_transfer(
                    call_id,
                    expected,
                    failure,
                    failure_reason,
                )
            except Exception:
                transitioned = None
                logger.warning(
                    "promoted transfer failure could not be persisted call_id=%s",
                    call_id,
                    exc_info=True,
                )
            # Completing the promoted transfer can also commit before its await raises.
            # If in-progress no longer matches, adopt the exact completed value before
            # resorting to DB-independent conference compensation.
            if (
                transitioned is not True
                and expected == in_progress
                and completed_outcome is not None
            ):
                try:
                    transitioned = await self.db.fail_promoted_transfer(
                        call_id,
                        completed_outcome,
                        failure,
                        failure_reason,
                    )
                except Exception:
                    transitioned = None
                    logger.warning(
                        "ambiguous transfer completion could not be adopted call_id=%s",
                        call_id,
                        exc_info=True,
                    )
            if transitioned is True:
                try:
                    finished = await self._finish_claimed_termination(
                        promoted or call,
                        failure_reason,
                        preserve_conference=False,
                        await_finalizer=False,
                        expected_transfer_outcome=failure,
                    )
                except Exception:
                    finished = False
                    logger.warning(
                        "promoted transfer termination failed call_id=%s",
                        call_id,
                        exc_info=True,
                    )
                if finished:
                    return
            # A lost/ambiguous CAS must never be interpreted as a successful handoff.
            # Complete the conference without relying on another database read.
            await complete_conference_compensation()

        async def fail_promoted_and_compensate(
            expected: str, failure: str, failure_reason: str
        ) -> None:
            compensation = self._spawn(
                _fail_promoted_and_compensate(expected, failure, failure_reason),
                name=f"compensate-promoted-transfer:{call_id}",
                must_finish=True,
            )
            await self._await_network_task(compensation)

        async def send_failure(error: str) -> None:
            if tool_call_id is None:
                return
            try:
                await self.realtime.send_tool_result(
                    call_id,
                    tool_call_id,
                    {"accepted": False, "error": error},
                )
            except asyncio.CancelledError:
                logger.warning("transfer failure output was cancelled call_id=%s", call_id)
            except Exception:
                logger.warning(
                    "transfer failure output could not be delivered call_id=%s",
                    call_id,
                    exc_info=True,
                )

        def reconcile_owner_callbacks() -> None:
            """Drop pre-create callbacks that belong to a different owner leg."""

            event_was_set = event.is_set()
            had_join = call_id in self._owner_joined_sids
            had_failure = call_id in self._owner_failures
            joined_sid = self._owner_joined_sids.get(call_id)
            failure = self._owner_failures.get(call_id)
            if had_join and joined_sid is not None and joined_sid != owner_call_sid:
                self._owner_joined_sids.pop(call_id, None)
                had_join = False
            if (
                had_failure
                and failure is not None
                and failure[0] is not None
                and failure[0] != owner_call_sid
            ):
                self._owner_failures.pop(call_id, None)
                had_failure = False

            event.clear()
            departure_event.clear()
            # Tests and legacy callback senders may signal a join without a SID.
            if had_join or had_failure or (event_was_set and not (had_join or had_failure)):
                event.set()
            if had_failure:
                departure_event.set()

        def raise_if_owner_departed() -> None:
            failure = owner_failure_reason()
            if failure is not None or departure_event.is_set():
                raise OwnerTransferDeparted(failure or "owner_departed")

        try:
            create_task = asyncio.create_task(
                self.twilio.create_owner_participant(
                    call_id=call_id,
                    plan_id=call["plan_id"],
                    conference_sid_or_name=conference,
                    owner_phone=packet.escalation.owner_phone,
                ),
                name=f"create-owner:{call_id}",
            )
            participant = await asyncio.shield(create_task)
            owner_call_sid = participant.call_sid
            self._owner_expected_sids[call_id] = owner_call_sid
            reconcile_owner_callbacks()
            owner_sid_error: Exception | None = None
            try:
                owner_sid_persisted = await self.db.record_transfer_owner_sid(
                    call_id, joining, owner_call_sid
                )
            except Exception as exc:
                owner_sid_persisted = False
                owner_sid_error = exc
            if not owner_sid_persisted:
                current = await self.db.get_call(call_id)
                exact_owner_sid = bool(
                    current
                    and current.get("state") in TRANSFER_ELIGIBLE_STATES
                    and not current.get("termination_claimed")
                    and current.get("transfer_outcome") == joining
                    and current.get("twilio_owner_call_sid") == owner_call_sid
                )
                if not exact_owner_sid:
                    if owner_sid_error is not None:
                        raise owner_sid_error
                    raise RuntimeError("owner transfer SID could not be persisted")
                logger.warning("reconciled ambiguous owner SID persistence call_id=%s", call_id)
            call["twilio_owner_call_sid"] = owner_call_sid
            await asyncio.wait_for(event.wait(), timeout=OWNER_JOIN_TIMEOUT_SECONDS)
            raise_if_owner_departed()
            promoted = await self.db.promote_transfer(call_id, reason)
            if promoted is None:
                cleaned = await cleanup_owner()
                await fail_joining_and_compensate("failed:termination_won", cleaned)
                await send_failure("call ended before owner transfer completed")
                return {"accepted": False, "error": "call ended during owner transfer"}
            phase = "promoted"
            raise_if_owner_departed()

            if tool_call_id is not None:
                try:
                    # A successful tool output must be observable before the AI leg disappears.
                    await self.realtime.send_tool_result(
                        call_id,
                        tool_call_id,
                        {"accepted": True, "status": "owner_joined"},
                    )
                except asyncio.CancelledError:
                    logger.warning("transfer tool output was cancelled call_id=%s", call_id)
                except Exception:
                    logger.warning(
                        "transfer tool output could not be delivered call_id=%s",
                        call_id,
                        exc_info=True,
                    )
            raise_if_owner_departed()

            remove_ai = asyncio.create_task(
                self.twilio.remove_participant(conference, call.get("twilio_ai_call_sid")),
                name=f"remove-ai:{call_id}",
            )
            departure_wait = asyncio.create_task(
                departure_event.wait(), name=f"wait-owner-departure:{call_id}"
            )
            try:
                done, _ = await asyncio.wait(
                    {remove_ai, departure_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                if departure_wait in done:
                    try:
                        await self._await_network_task(remove_ai)
                    except Exception:
                        logger.warning(
                            "AI removal also failed after owner departure call_id=%s",
                            call_id,
                            exc_info=True,
                        )
                    raise_if_owner_departed()
                remove_ai.result()
            except asyncio.CancelledError:
                try:
                    await self._await_network_task(remove_ai)
                except Exception:
                    logger.warning(
                        "AI removal failed while transfer was cancelled call_id=%s",
                        call_id,
                        exc_info=True,
                    )
                raise
            finally:
                departure_wait.cancel()
                await asyncio.gather(departure_wait, return_exceptions=True)
            raise_if_owner_departed()

            completed_outcome = f"completed:{reason}"
            if not await self.db.complete_promoted_transfer(
                call_id, in_progress, completed_outcome
            ):
                raise RuntimeError("owner transfer completion state was lost")
            phase = "completed"
            raise_if_owner_departed()
            # The owner becomes responsible for conference lifetime only after the
            # handoff outcome is durably completed. If arming this fails, compensate
            # the transfer rather than leave the callee alone and billing later.
            arm_owner_exit = asyncio.create_task(
                self.twilio.enable_end_conference_on_exit(conference, owner_call_sid),
                name=f"arm-owner-conference-exit:{call_id}",
            )
            await self._await_network_task(arm_owner_exit)
            raise_if_owner_departed()
            terminalization = self._spawn(
                self._finish_claimed_termination(
                    promoted,
                    "transfer_completed",
                    preserve_conference=True,
                    await_finalizer=False,
                    expected_transfer_outcome=completed_outcome,
                ),
                name=f"terminalize-owner-transfer:{call_id}",
                must_finish=True,
            )
            terminalized = await self._await_network_task(terminalization)
            if not terminalized:
                cleaned = await cleanup_owner()
                await fail_promoted_and_compensate(
                    completed_outcome,
                    "failed:terminal_cas",
                    "transfer_failed:terminal_cas",
                )
                if not cleaned:
                    await complete_conference_compensation()
                return {"accepted": False, "error": "transfer teardown requires retry"}
            phase = "terminal"
            return {"accepted": True, "status": "transferred"}
        except TimeoutError:
            cleaned = await cleanup_owner()
            await fail_joining_and_compensate("failed:owner_join_timeout", cleaned)
            await send_failure("owner did not join")
            return {"accepted": False, "error": "owner did not join"}
        except asyncio.CancelledError:
            # If creation completed in its worker thread after cancellation, recover
            # its SID before returning so the remote owner leg cannot leak.
            if owner_call_sid is None and create_task is not None:
                try:
                    participant = await self._await_network_task(create_task)
                    owner_call_sid = participant.call_sid
                except Exception:
                    pass
            cleaned = await cleanup_owner()
            if phase == "promoted":
                failure_reason = "transfer_failed:CancelledError"
                failure = "failed:CancelledError"
                await fail_promoted_and_compensate(
                    in_progress,
                    failure,
                    failure_reason,
                )
            elif phase == "completed" and completed_outcome is not None:
                await fail_promoted_and_compensate(
                    completed_outcome,
                    "failed:CancelledError",
                    "transfer_failed:CancelledError",
                )
            elif phase != "terminal":
                await fail_joining_and_compensate("failed:CancelledError", cleaned)
            return {"accepted": False, "error": "owner transfer cancelled"}
        except Exception as exc:
            logger.exception("owner transfer failed call_id=%s", call_id)
            cleaned = await cleanup_owner()
            error_type = type(exc).__name__
            failure_reason = f"transfer_failed:{error_type}"
            failure = f"failed:{error_type}"
            if phase == "promoted":
                await fail_promoted_and_compensate(
                    in_progress,
                    failure,
                    failure_reason,
                )
            elif phase == "completed" and completed_outcome is not None:
                await fail_promoted_and_compensate(
                    completed_outcome,
                    failure,
                    failure_reason,
                )
            elif phase != "terminal":
                await fail_joining_and_compensate(failure, cleaned)
                await send_failure("owner transfer failed")
            return {"accepted": False, "error": "owner transfer failed"}

    async def terminate_call(
        self,
        call_id: str,
        reason: str,
        *,
        preserve_conference: bool = False,
        await_finalizer: bool = False,
        _initiating_task: asyncio.Task[Any] | None = None,
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
            ),
            name=f"claimed-termination:{call_id}",
            must_finish=True,
        )
        # Once a termination claim may commit, caller cancellation must not strand
        # billing/media teardown behind an unreclaimable termination_claimed flag.
        return await self._await_network_task(continuation)

    async def _claim_and_finish_termination(
        self,
        call_id: str,
        reason: str,
        *,
        preserve_conference: bool,
        await_finalizer: bool,
        initiating_task: asyncio.Task[Any] | None,
    ) -> bool:
        transfer_task: asyncio.Task[dict[str, Any]] | None = None
        async with self._owner_transfer_lock(call_id):
            claim_error: Exception | None = None
            try:
                call = await self.db.claim_termination(call_id, reason)
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
        if call is None:
            current = await self.db.get_call(call_id)
            if current is None:
                self._clear_call_activity(call_id)
            elif CallState(current["state"]) in TERMINAL_STATES or current.get(
                "termination_claimed"
            ):
                self._tombstone_call_activity(call_id)
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
        )

    async def _finish_claimed_termination(
        self,
        call: dict[str, Any],
        reason: str,
        *,
        preserve_conference: bool,
        await_finalizer: bool,
        expected_transfer_outcome: str | None = None,
        schedule_conference_retry: bool = True,
    ) -> bool:
        call_id = call["call_id"]
        self._tombstone_call_activity(call_id)
        self._voice_end_pending.pop(call_id, None)
        self._audio_drain_terminations.pop(call_id, None)
        self._active_response_ids.pop(call_id, None)
        media_tasks = [self.realtime.hangup(call.get("xai_call_id"))]
        if not preserve_conference:
            media_tasks.append(
                self.twilio.complete_conference(
                    call.get("conference_sid") or call.get("conference_name")
                )
            )
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
        try:
            await self.realtime.drain_and_close(call_id)
        except Exception:
            logger.warning("Realtime drain/close failed call_id=%s", call_id, exc_info=True)
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
        if reason == "transfer_completed":
            terminal = CallState.TRANSFERRED
        elif reason in {"time_limit", "setup_deadline", "watchdog_stale"}:
            terminal = CallState.TIMED_OUT
        elif reason in {
            "callee_participant_leave",
            "callee_call_completed",
            "conference_end",
            "voicemail_left",
            "owner_request",
            "voice_model_end_call",
            "xai_terminal_event",
        }:
            terminal = CallState.COMPLETED
        else:
            terminal = CallState.FAILED
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
            call = current
            logger.warning(
                "reconciled ambiguous terminal transition call_id=%s state=%s",
                call_id,
                terminal.value,
            )
        self._tombstone_call_activity(call_id)
        self._owner_join_events.pop(call_id, None)
        self._owner_departure_events.pop(call_id, None)
        self._owner_expected_sids.pop(call_id, None)
        self._owner_joined_sids.pop(call_id, None)
        self._owner_failures.pop(call_id, None)
        self._owner_transfer_tasks.pop(call_id, None)
        self._owner_transfer_locks.pop(call_id, None)
        self._opening_transition_locks.pop(call_id, None)
        self._tool_seen_calls.discard(call_id)
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

        async def finalize_best_effort() -> None:
            try:
                await self.finalizer.finalize(call_id)
            except Exception:
                logger.warning("terminal finalization failed call_id=%s", call_id, exc_info=True)

        if await_finalizer:
            await finalize_best_effort()
        else:
            self._spawn(finalize_best_effort(), name=f"finalize:{call_id}")
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
        )

    async def get_result(self, call_id: str) -> dict[str, Any]:
        snapshot = await self.get_snapshot(call_id)
        if snapshot.state in TERMINAL_STATES:
            result = snapshot.result
            if result is None or result.finalization_status == "telephony_only":
                # Telephony is terminal; never report in_progress while finalization catches up.
                result = await self.finalizer.finalize(call_id)
            return {
                "call_id": call_id,
                "state": snapshot.state,
                "result": result.model_dump(mode="json"),
            }
        return {"call_id": call_id, "state": snapshot.state, "result": None}

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

    async def recover_startup(self) -> None:
        recovered: set[str] = set()
        for call in await self.db.list_nonterminal_calls():
            call_id = call["call_id"]
            transfer_outcome = call.get("transfer_outcome")
            completed_transfer = bool(
                transfer_outcome and transfer_outcome.startswith("completed:")
            )
            claimed = await self.db.claim_startup_recovery(
                call_id,
                expected_transfer_outcome=transfer_outcome,
                completed_transfer=completed_transfer,
            )
            if claimed is None:
                continue
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
                    continue
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
                continue
            if not terminalized:
                logger.error("startup recovery scheduled teardown retry call_id=%s", call_id)
                continue
            recovered.add(call_id)
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

    async def start_watchdog(self) -> None:
        if not self._stopping and self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._watchdog(), name="call-watchdog")

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
        await self.realtime.close()
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
        await self._flush_call_activity()
        if self._owns_xai_client:
            await self.xai.close()

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self._watchdog_once()

    async def _watchdog_once(self) -> None:
        await self._flush_call_activity()
        now = LatencyMark.now()
        cutoff = datetime.fromisoformat(now.occurred_at) - timedelta(
            seconds=self.settings.watchdog_stale_seconds
        )
        stale_ns = self.settings.watchdog_stale_seconds * 1_000_000_000
        calls = await self.db.list_nonterminal_calls()
        for call in calls:
            call_id = call["call_id"]
            if self._call_activity_is_closed(call):
                self._tombstone_call_activity(call_id)
                continue
            if datetime.fromisoformat(call["last_event_at"]) >= cutoff:
                continue
            activity_before = self._latest_call_activity.get(call_id)
            if (
                activity_before is not None
                and now.monotonic_ns - activity_before.monotonic_ns <= stale_ns
            ):
                continue

            # The initial list can go stale while another callback updates the call.
            # Re-read both durable and in-memory liveness before claiming timeout.
            current = await self.db.get_call(call_id)
            if current is None:
                self._clear_call_activity(call_id)
                continue
            if self._call_activity_is_closed(current):
                self._tombstone_call_activity(call_id)
                continue
            if datetime.fromisoformat(current["last_event_at"]) >= cutoff:
                continue
            activity_after = self._latest_call_activity.get(call_id)
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
            self._watchdog_claims.add(call_id)
            terminated = False
            try:
                terminated = await self.terminate_call(call_id, "watchdog_stale")
            finally:
                if not terminated:
                    self._watchdog_claims.discard(call_id)
