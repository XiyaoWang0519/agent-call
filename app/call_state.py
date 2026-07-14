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

from app.db import Database, DeploymentLockedError, LatencyMark, LatencyStage
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
from app.openai_client import create_openai_client
from app.openai_realtime import RealtimeBridge
from app.policy import validate_context
from app.settings import Settings
from app.twilio_bridge import TwilioBridge

logger = logging.getLogger(__name__)

CALL_ACTIVITY_TOMBSTONE_TTL_SECONDS = 15 * 60
CALL_ACTIVITY_TOMBSTONE_MAX = 4096


class CallService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        twilio: TwilioBridge | None = None,
        openai: AsyncOpenAI | None = None,
    ):
        self.settings = settings
        self.db = db
        self._owns_openai_client = openai is None
        self.openai = openai if openai is not None else create_openai_client(settings)
        self.twilio = twilio or TwilioBridge(settings)
        self._latest_call_activity: dict[str, LatencyMark] = {}
        self._dirty_call_activity: dict[str, LatencyMark] = {}
        self._watchdog_claims: set[str] = set()
        self._activity_tombstones: OrderedDict[str, int] = OrderedDict()
        self.realtime = RealtimeBridge(
            settings,
            self.openai,
            on_event=self.handle_realtime_event,
            on_open=self.handle_sideband_open,
            on_fatal=self._handle_sideband_fatal,
            on_send=self.handle_realtime_send,
            on_activity=self._note_call_activity,
        )
        self.finalizer = Finalizer(settings, db, self.openai)
        self._background: set[asyncio.Task[Any]] = set()
        self._owner_join_events: dict[str, asyncio.Event] = {}
        self._opening_transition_locks: dict[str, asyncio.Lock] = {}
        self._voice_end_pending: dict[str, tuple[str, str | None]] = {}
        self._active_response_ids: dict[str, str | None] = {}
        self._queued_latency_events: dict[tuple[str, LatencyStage, str], LatencyMark] = {}
        self._watchdog_task: asyncio.Task[None] | None = None

    def _spawn(self, coroutine, *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _opening_transition_lock(self, call_id: str) -> asyncio.Lock:
        return self._opening_transition_locks.setdefault(call_id, asyncio.Lock())

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

    async def handle_openai_incoming(
        self, openai_call_id: str, sip_headers: list[dict[str, str]]
    ) -> str:
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
            await self.realtime.reject(openai_call_id)
            raise LookupError("incoming SIP call could not be mapped to an approved plan")
        if call.get("openai_call_id") and call["openai_call_id"] != openai_call_id:
            await self.realtime.reject(openai_call_id)
            raise RuntimeError("call already mapped to a different OpenAI call")
        await self.db.update_call(call_id, openai_call_id=openai_call_id)
        plan = await self.db.get_plan(call["plan_id"])
        packet = ContextPacket.model_validate(plan["context"])
        accept_request = LatencyMark.now()
        try:
            accept_status = await self.realtime.accept_and_connect(
                call_id=call_id,
                openai_call_id=openai_call_id,
                packet=packet,
            )
        except Exception:
            self._queue_latency_batch(
                call_id,
                (LatencyStage.OPENAI_ACCEPT_REQUEST, accept_request, ""),
            )
            logger.exception("failed to accept OpenAI call")
            await self.terminate_call(call_id, "openai_accept_failed")
            raise
        accept_completed = LatencyMark.now()
        self._queue_latency_batch(
            call_id,
            (LatencyStage.OPENAI_ACCEPT_REQUEST, accept_request, ""),
            (LatencyStage.OPENAI_ACCEPT_COMPLETED, accept_completed, ""),
        )
        try:
            await self.db.update_call(call_id, openai_accept_status=accept_status)
        except Exception:
            logger.exception("failed to persist OpenAI accept status")
            await self.terminate_call(call_id, "openai_accept_failed")
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
            semantic_vad_verified=int(vad_ok),
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
        if not self._note_call_activity(call_id, received):
            return
        event = (form.get("StatusCallbackEvent") or form.get("ConferenceStatus") or "").lower()
        label = (form.get("ParticipantLabel") or form.get("Label") or "").lower()
        call_sid = form.get("CallSid") or form.get("ParticipantCallSid")
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
            elif label == "owner":
                self._owner_join_events.setdefault(call_id, asyncio.Event()).set()
        elif event in {"participant-leave", "leave"}:
            if label == "callee" or (call_sid and call_sid == call.get("twilio_callee_call_sid")):
                await self.terminate_call(call_id, "callee_participant_leave")
        elif event in {"conference-end", "end"}:
            await self.terminate_call(call_id, "conference_end")
        else:
            await self.db.touch_call(call_id)

    async def handle_participant_status(self, call_id: str, leg: str, form: dict[str, str]) -> None:
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
        status = (form.get("CallStatus") or "").lower()
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
            and call["semantic_vad_verified"]
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
                and call["semantic_vad_verified"]
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
            self._queue_latency(call_id, LatencyStage.FIRST_OPENAI_AUDIO_DELTA, received)
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
        elif event_type == "response.output_audio_transcript.done":
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
            await self.db.increment_tool_calls(
                call_id,
                latency_mark=received,
                event_key=str(event.get("call_id") or event_id),
            )
            await self._handle_tool_call(call_id, event)
        elif event_type == "response.created":
            call = await self.db.get_call(call_id)
            if call and call.get("tool_call_count", 0) > 0:
                await self.db.update_call(call_id, tool_continuation_observed=1)
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
                self._spawn(
                    self.terminate_call(call_id, "voicemail_left"),
                    name=f"terminate:{call_id}:voicemail",
                )
        elif event_type in {"session.ended", "call.ended"}:
            self._spawn(
                self.terminate_call(call_id, "openai_terminal_event"),
                name=f"terminate:{call_id}:openai-terminal",
            )
        elif event_type == "error":
            error = event.get("error") or {}
            if error.get("code") == "response_cancel_not_active":
                # Benign race: the response we tried to cancel finished on its own.
                logger.info("stale response.cancel ignored call_id=%s event=%s", call_id, event)
                return
            logger.error("realtime error event call_id=%s event=%s", call_id, event)
            self._spawn(
                self.terminate_call(call_id, "openai_fatal_error"),
                name=f"terminate:{call_id}:openai-error",
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

    async def _handle_tool_call(self, call_id: str, event: dict[str, Any]) -> None:
        name = event.get("name")
        tool_call_id = event.get("call_id")
        try:
            arguments = json.loads(event.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if name == "record_call_outcome":
            try:
                advisory = AdvisoryOutcome.model_validate(arguments)
                await self.db.update_call(
                    call_id, advisory_outcome_json=advisory.model_dump(mode="json", by_alias=True)
                )
                output = {"accepted": True}
            except Exception as exc:
                output = {"accepted": False, "error": str(exc)}
            await self.realtime.send_tool_result(call_id, tool_call_id, output)
        elif name == "end_call":
            try:
                request = VoiceEndCallRequest.model_validate(arguments)
            except Exception as exc:
                await self.realtime.send_tool_result(
                    call_id,
                    tool_call_id,
                    {"accepted": False, "error": str(exc)},
                )
                return
            pending = (tool_call_id, None)
            self._voice_end_pending[call_id] = pending
            await self.realtime.send_tool_result(
                call_id,
                tool_call_id,
                {"accepted": True, "reason": request.reason},
                continuation_instructions=(
                    "The call is now ending. Briefly confirm the outcome or next step if useful, "
                    "then say one concise, natural goodbye. Do not call any function."
                ),
            )
            self._spawn(
                self._voice_end_fallback(call_id, tool_call_id),
                name=f"voice-end-fallback:{call_id}",
            )
        elif name == "transfer_to_owner":
            reason = arguments.get("reason", "requested")
            output, owner_call_sid = await self._join_owner(call_id)
            if not output.get("accepted"):
                await self.realtime.send_tool_result(call_id, tool_call_id, output)
                return
            try:
                # Return the function output while the AI sideband is still connected.
                await self.realtime.send_tool_result(call_id, tool_call_id, output)
            except Exception:
                logger.warning("transfer tool output could not be delivered", exc_info=True)
            if await self._finish_owner_transfer(call_id, reason, owner_call_sid):
                self._spawn(
                    self.terminate_call(call_id, "transfer_completed", preserve_conference=True),
                    name=f"terminate:{call_id}:transfer",
                )
        else:
            await self.realtime.send_tool_result(
                call_id, tool_call_id, {"accepted": False, "error": "unknown tool"}
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
        self._spawn(
            self.terminate_call(call_id, "voice_model_end_call"),
            name=f"terminate:{call_id}:voice-end",
        )

    async def _voice_end_fallback(self, call_id: str, tool_call_id: str) -> None:
        await asyncio.sleep(15)
        pending = self._voice_end_pending.get(call_id)
        if pending is None or pending[0] != tool_call_id:
            return
        self._voice_end_pending.pop(call_id, None)
        await self.terminate_call(call_id, "voice_model_end_call")

    async def transfer_to_owner(
        self, call_id: str, reason: str, *, terminate_after: bool = True
    ) -> dict[str, Any]:
        output, owner_call_sid = await self._join_owner(call_id)
        if not output.get("accepted"):
            return output
        if not await self._finish_owner_transfer(call_id, reason, owner_call_sid):
            return {"accepted": False, "error": "AI participant could not be removed"}
        if terminate_after:
            self._spawn(
                self.terminate_call(call_id, "transfer_completed", preserve_conference=True),
                name=f"terminate:{call_id}:transfer",
            )
        return {"accepted": True, "status": "transferred"}

    async def _join_owner(self, call_id: str) -> tuple[dict[str, Any], str | None]:
        call = await self.db.get_call(call_id)
        if call is None:
            return {"accepted": False, "error": "call not found"}, None
        plan = await self.db.get_plan(call["plan_id"])
        if plan is None:
            return {"accepted": False, "error": "call plan not found"}, None
        packet = ContextPacket.model_validate(plan["context"])
        if packet.escalation.mode != "transfer_to_owner":
            return {"accepted": False, "error": "owner transfer is not authorized"}, None
        event = self._owner_join_events.setdefault(call_id, asyncio.Event())
        owner_call_sid: str | None = None
        try:
            participant = await self.twilio.create_owner_participant(
                call_id=call_id,
                plan_id=call["plan_id"],
                conference_sid_or_name=call.get("conference_sid") or call["conference_name"],
                owner_phone=packet.escalation.owner_phone,
            )
            owner_call_sid = participant.call_sid
            await asyncio.wait_for(event.wait(), timeout=30)
            return {"accepted": True, "status": "owner_joined"}, owner_call_sid
        except Exception as exc:
            if owner_call_sid:
                try:
                    await self.twilio.remove_participant(
                        call.get("conference_sid") or call["conference_name"], owner_call_sid
                    )
                except Exception:
                    logger.warning("failed to clean up owner transfer leg", exc_info=True)
            await self.db.update_call(call_id, transfer_outcome=f"failed:{type(exc).__name__}")
            return {"accepted": False, "error": "owner did not join"}, None

    async def _finish_owner_transfer(
        self, call_id: str, reason: str, owner_call_sid: str | None
    ) -> bool:
        call = await self.db.get_call(call_id)
        if call is None:
            return False
        conference = call.get("conference_sid") or call["conference_name"]
        try:
            # Persist this before AI removal because Twilio/OpenAI terminal callbacks may race it.
            await self.db.update_call(call_id, transfer_outcome=f"in_progress:{reason}")
            await self.twilio.remove_participant(conference, call.get("twilio_ai_call_sid"))
            await self.db.update_call(call_id, transfer_outcome=f"completed:{reason}")
            return True
        except Exception as exc:
            if owner_call_sid:
                try:
                    await self.twilio.remove_participant(conference, owner_call_sid)
                except Exception:
                    logger.warning("failed to roll back owner transfer leg", exc_info=True)
            await self.db.update_call(call_id, transfer_outcome=f"failed:{type(exc).__name__}")
            return False

    async def terminate_call(
        self,
        call_id: str,
        reason: str,
        *,
        preserve_conference: bool = False,
        await_finalizer: bool = False,
    ) -> bool:
        call = await self.db.get_call(call_id)
        if call is None:
            self._clear_call_activity(call_id)
            return False
        state = CallState(call["state"])
        if state in TERMINAL_STATES:
            self._tombstone_call_activity(call_id)
            return False
        if state == CallState.TERMINATING:
            self._tombstone_call_activity(call_id)
        if (call.get("transfer_outcome") or "").startswith(("in_progress:", "completed:")):
            preserve_conference = True
            reason = "transfer_completed"
        if not await self.db.set_flag_once(call_id, "termination_claimed"):
            return False
        self._tombstone_call_activity(call_id)
        self._voice_end_pending.pop(call_id, None)
        self._active_response_ids.pop(call_id, None)
        await self.db.update_call(
            call_id, state=CallState.TERMINATING.value, termination_reason=reason
        )
        media_tasks = [self.realtime.hangup(call.get("openai_call_id"))]
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
        await self.realtime.drain_and_close(call_id)
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
            "openai_terminal_event",
        }:
            terminal = CallState.COMPLETED
        else:
            terminal = CallState.FAILED
        await self.db.update_call(
            call_id,
            state=terminal.value,
            ended_at=ended_at.isoformat(),
            duration_seconds=duration,
        )
        self._tombstone_call_activity(call_id)
        await self.db.add_transcript_turn(
            call_id=call_id,
            turn_id=f"telephony_{secrets.token_urlsafe(10)}",
            speaker="system",
            text=f"Call ended with telephony reason: {reason}.",
            source_event_type="telephony.terminal",
            source_event_id=f"terminal:{call_id}:{reason}",
        )
        if await_finalizer:
            await self.finalizer.finalize(call_id)
        else:
            self._spawn(self.finalizer.finalize(call_id), name=f"finalize:{call_id}")
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
            await self.db.reset_termination_claim(call["call_id"])
            await self.terminate_call(call["call_id"], "startup_recovery", await_finalizer=True)
            recovered.add(call["call_id"])
        # A crash can occur after telephony is terminal but before finalization committed.
        for call in await self.db.list_terminal_calls_needing_finalization():
            if call["call_id"] not in recovered:
                await self.finalizer.finalize(call["call_id"])

    async def start_watchdog(self) -> None:
        if self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._watchdog(), name="call-watchdog")

    async def stop(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
            await asyncio.gather(self._watchdog_task, return_exceptions=True)
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self._flush_call_activity()
        if self._owns_openai_client:
            await self.openai.close()

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
