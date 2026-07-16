"""Owner-transfer saga: dial the owner into the conference and hand off from the AI leg.

This is race-sensitive asyncio code. Comments about ordering/atomicity are load-bearing
and every await sequence here must be preserved exactly as written.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.db import TRANSFER_ELIGIBLE_STATES, Database
from app.models import ContextPacket
from app.openai_realtime import RealtimeBridge
from app.twilio_bridge import TwilioBridge

logger = logging.getLogger(__name__)

OWNER_JOIN_TIMEOUT_SECONDS = 30


class OwnerTransferDeparted(RuntimeError):
    """The owner leg became terminal before the AI handoff finished."""


class OwnerTransferCoordinator:
    """Owns the per-call owner-transfer state and starts transfer attempts.

    A composed collaborator (not a mixin): CallService holds one instance and
    delegates owner-transfer bookkeeping to it, mirroring CallActivityTracker's
    relationship to CallService. Each transfer attempt is driven by its own
    OwnerTransferRun instance; the coordinator only owns the state shared across
    attempts (join/departure signaling, expected/joined SIDs, failures, tasks,
    per-call locks) and the entry points that reach into it from webhooks,
    termination, and the public transfer_to_owner API.

    ``db`` and ``twilio`` are stable for the coordinator's lifetime and are held as
    typed objects. ``realtime`` can be replaced on CallService after construction
    (tests do this to install a fake), so it is supplied as a callable returning the
    current value rather than captured once. The remaining CallService behaviors this
    coordinator doesn't own (spawning tasks, awaiting network tasks, checking/using
    shutdown state, finishing a claimed termination, completing/scheduling the
    conference, terminating a call) are supplied as named callables so CallService can
    wire them as late-binding lambdas that keep working when tests monkeypatch the
    underlying CallService attributes.
    """

    def __init__(
        self,
        db: Database,
        twilio: TwilioBridge,
        *,
        realtime: Callable[[], RealtimeBridge],
        is_stopping: Callable[[], bool],
        spawn: Callable[..., asyncio.Task[Any]],
        await_network_task: Callable[..., Awaitable[Any]],
        finish_claimed_termination: Callable[..., Awaitable[bool]],
        complete_conference_or_schedule: Callable[[dict[str, Any]], Awaitable[bool]],
        terminate_call: Callable[..., Awaitable[bool]],
    ) -> None:
        self._db = db
        self._twilio = twilio
        self._realtime = realtime
        self._is_stopping = is_stopping
        self._spawn = spawn
        self._await_network_task = await_network_task
        self._finish_claimed_termination = finish_claimed_termination
        self._complete_conference_or_schedule = complete_conference_or_schedule
        self._terminate_call = terminate_call
        self.join_events: dict[str, asyncio.Event] = {}
        self.departure_events: dict[str, asyncio.Event] = {}
        self.expected_sids: dict[str, str] = {}
        self.joined_sids: dict[str, str | None] = {}
        self.failures: dict[str, tuple[str | None, str]] = {}
        self.tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, call_id: str) -> asyncio.Lock:
        return self.locks.setdefault(call_id, asyncio.Lock())

    def clear_call(self, call_id: str) -> None:
        """Drop every per-call entry this coordinator tracks. Synchronous on purpose:
        callers rely on no intervening await while runtime state is being cleared."""
        self.join_events.pop(call_id, None)
        self.departure_events.pop(call_id, None)
        self.expected_sids.pop(call_id, None)
        self.joined_sids.pop(call_id, None)
        self.failures.pop(call_id, None)
        self.tasks.pop(call_id, None)
        self.locks.pop(call_id, None)

    def owner_sid_matches(self, call_id: str, call_sid: str | None) -> bool:
        expected = self.expected_sids.get(call_id)
        return expected is None or call_sid is None or call_sid == expected

    def record_owner_join(self, call_id: str, call_sid: str | None) -> None:
        event = self.join_events.get(call_id)
        if event is None or not self.owner_sid_matches(call_id, call_sid):
            return
        self.joined_sids[call_id] = call_sid
        event.set()

    def record_owner_failure(self, call_id: str, call_sid: str | None, reason: str) -> None:
        event = self.join_events.get(call_id)
        if event is None or not self.owner_sid_matches(call_id, call_sid):
            return
        self.failures[call_id] = (call_sid, reason)
        self.departure_events.setdefault(call_id, asyncio.Event()).set()
        event.set()

    def track(self, call_id: str, task: asyncio.Task[dict[str, Any]]) -> None:
        self.tasks[call_id] = task

        def finished(completed: asyncio.Task[dict[str, Any]]) -> None:
            if self.tasks.get(call_id) is completed:
                self.tasks.pop(call_id, None)
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

    def _failure_reason(self, call_id: str, owner_call_sid: str | None) -> str | None:
        failure = self.failures.get(call_id)
        if failure is None:
            return None
        failure_sid, failure_reason = failure
        if owner_call_sid and failure_sid and failure_sid != owner_call_sid:
            return None
        return failure_reason

    async def start(
        self,
        call_id: str,
        reason: str,
        *,
        tool_call_id: str | None,
    ) -> tuple[asyncio.Task[dict[str, Any]] | None, str | None]:
        if self._is_stopping():
            return None, "service is stopping"
        call = await self._db.get_call(call_id)
        if call is None:
            return None, "call not found"
        plan = await self._db.get_plan(call["plan_id"])
        if plan is None:
            return None, "call plan not found"
        packet = ContextPacket.model_validate(plan["context"])
        if packet.escalation.mode != "transfer_to_owner":
            return None, "owner transfer is not authorized"

        async def claim_and_spawn() -> tuple[asyncio.Task[dict[str, Any]] | None, str | None]:
            async with self.lock(call_id):
                if call_id in self.tasks:
                    return None, "owner transfer already in progress"
                if self._is_stopping():
                    return None, "service is stopping"
                joining = f"joining:{reason}"
                claim_error: Exception | None = None
                try:
                    claimed = await self._db.claim_transfer_joining(call_id, reason)
                except Exception as exc:
                    claimed = False
                    claim_error = exc
                claimed_call = call
                if not claimed:
                    current = await self._db.get_call(call_id)
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
                if self._is_stopping():
                    await self._db.fail_joining_transfer(
                        call_id, joining, "failed:service_stopping"
                    )
                    return None, "service is stopping"
                # Install callback-visible state before dialing can emit owner status.
                self.join_events.setdefault(call_id, asyncio.Event())
                self.departure_events.setdefault(call_id, asyncio.Event())
                run = OwnerTransferRun(
                    self,
                    claimed_call,
                    packet,
                    reason,
                    tool_call_id=tool_call_id,
                )
                task = self._spawn(
                    run.run(),
                    name=f"owner-transfer:{call_id}",
                )
                self.track(call_id, task)
                return task, None

        # Cancellation after SQLite commits but before this caller observes the result
        # must not leave durable joining without a registered cleanup workflow.
        claim = self._spawn(
            claim_and_spawn(),
            name=f"claim-owner-transfer:{call_id}",
            must_finish=True,
        )
        return await self._await_network_task(claim)


class OwnerTransferRun:
    """Drives a single owner-transfer attempt.

    Holds the per-attempt mutable state that would otherwise have to be threaded as
    arguments through every helper (call, owner_call_sid, phase, promoted,
    completed_outcome, owner_cleanup_started, the join/departure events, ...). Helpers
    read/write that state via ``self`` instead of receiving it as parameters.
    """

    def __init__(
        self,
        coordinator: OwnerTransferCoordinator,
        call: dict[str, Any],
        packet: ContextPacket,
        reason: str,
        *,
        tool_call_id: str | None,
    ) -> None:
        self._coordinator = coordinator
        self.call = call
        self.packet = packet
        self.reason = reason
        self.tool_call_id = tool_call_id
        self.call_id = call["call_id"]
        self.joining = f"joining:{reason}"
        self.in_progress = f"in_progress:{reason}"
        self.conference = call.get("conference_sid") or call["conference_name"]
        self.event = coordinator.join_events.setdefault(self.call_id, asyncio.Event())
        self.departure_event = coordinator.departure_events.setdefault(
            self.call_id, asyncio.Event()
        )
        self.owner_call_sid: str | None = None
        self.owner_cleanup_started = False
        self.promoted: dict[str, Any] | None = None
        self.phase = "joining"
        self.completed_outcome: str | None = None
        self.workflow_task: asyncio.Task[Any] | None = None

    async def _spawn_and_await_compensation(self, coro: Any, *, name: str) -> None:
        """Run a must-finish owner-transfer compensation coroutine via _spawn so caller
        cancellation cannot abandon it mid-flight, then wait for it to settle."""
        compensation = self._coordinator._spawn(coro, name=name, must_finish=True)
        await self._coordinator._await_network_task(compensation)

    async def _remove_owner_transfer_leg(self) -> bool:
        owner_call_sid = self.owner_call_sid
        if owner_call_sid is None:
            raise RuntimeError("owner transfer leg removal requires a created owner SID")
        cleanup = asyncio.create_task(
            self._coordinator._twilio.remove_participant(self.conference, owner_call_sid),
            name=f"remove-owner:{self.call_id}",
        )
        try:
            await self._coordinator._await_network_task(cleanup)
            return True
        except Exception:
            # The primary transfer/termination outcome must survive cleanup failure.
            # Full conference teardown is the remaining compensation path.
            logger.warning(
                "failed to clean up owner transfer leg call_id=%s", self.call_id, exc_info=True
            )
            return False

    def _raise_if_owner_departed(self) -> None:
        failure = self._coordinator._failure_reason(self.call_id, self.owner_call_sid)
        if failure is not None or self.departure_event.is_set():
            raise OwnerTransferDeparted(failure or "owner_departed")

    def _reconcile_owner_transfer_callbacks(self) -> None:
        """Drop pre-create callbacks that belong to a different owner leg."""

        coordinator = self._coordinator
        call_id = self.call_id
        owner_call_sid = self.owner_call_sid
        event = self.event
        departure_event = self.departure_event

        event_was_set = event.is_set()
        had_join = call_id in coordinator.joined_sids
        had_failure = call_id in coordinator.failures
        joined_sid = coordinator.joined_sids.get(call_id)
        failure = coordinator.failures.get(call_id)
        if had_join and joined_sid is not None and joined_sid != owner_call_sid:
            coordinator.joined_sids.pop(call_id, None)
            had_join = False
        if (
            had_failure
            and failure is not None
            and failure[0] is not None
            and failure[0] != owner_call_sid
        ):
            coordinator.failures.pop(call_id, None)
            had_failure = False

        event.clear()
        departure_event.clear()
        # Tests and legacy callback senders may signal a join without a SID.
        if had_join or had_failure or (event_was_set and not (had_join or had_failure)):
            event.set()
        if had_failure:
            departure_event.set()

    async def _send_owner_transfer_failure(self, error: str) -> None:
        if self.tool_call_id is None:
            return
        try:
            await self._coordinator._realtime().send_tool_result(
                self.call_id,
                self.tool_call_id,
                {"accepted": False, "error": error},
            )
        except asyncio.CancelledError:
            logger.warning("transfer failure output was cancelled call_id=%s", self.call_id)
        except Exception:
            logger.warning(
                "transfer failure output could not be delivered call_id=%s",
                self.call_id,
                exc_info=True,
            )

    async def _persist_owner_transfer_sid(self) -> None:
        """Persist the freshly created owner leg's SID onto the joining transfer row.

        Raises if persistence cannot be confirmed even after reconciling an ambiguous
        write against the current row.
        """
        call_id = self.call_id
        owner_call_sid = self.owner_call_sid
        if owner_call_sid is None:
            raise RuntimeError("owner transfer SID persistence requires a created owner SID")
        db = self._coordinator._db
        owner_sid_error: Exception | None = None
        try:
            owner_sid_persisted = await db.record_transfer_owner_sid(
                call_id, self.joining, owner_call_sid
            )
        except Exception as exc:
            owner_sid_persisted = False
            owner_sid_error = exc
        if not owner_sid_persisted:
            current = await db.get_call(call_id)
            exact_owner_sid = bool(
                current
                and current.get("state") in TRANSFER_ELIGIBLE_STATES
                and not current.get("termination_claimed")
                and current.get("transfer_outcome") == self.joining
                and current.get("twilio_owner_call_sid") == owner_call_sid
            )
            if not exact_owner_sid:
                if owner_sid_error is not None:
                    raise owner_sid_error
                raise RuntimeError("owner transfer SID could not be persisted")
            logger.warning("reconciled ambiguous owner SID persistence call_id=%s", call_id)
        self.call["twilio_owner_call_sid"] = owner_call_sid

    async def _remove_ai_leg_racing_departure(self) -> None:
        """Remove the AI leg once the owner has joined, racing the owner's departure.

        A departure observed first still waits for removal to finish (or fail) before
        raising OwnerTransferDeparted, so cleanup never races the outcome it reports.
        """
        call_id = self.call_id
        ai_call_sid = self.call.get("twilio_ai_call_sid")
        remove_ai = asyncio.create_task(
            self._coordinator._twilio.remove_participant(self.conference, ai_call_sid),
            name=f"remove-ai:{call_id}",
        )
        departure_wait = asyncio.create_task(
            self.departure_event.wait(), name=f"wait-owner-departure:{call_id}"
        )
        try:
            done, _ = await asyncio.wait(
                {remove_ai, departure_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if departure_wait in done:
                try:
                    await self._coordinator._await_network_task(remove_ai)
                except Exception:
                    logger.warning(
                        "AI removal also failed after owner departure call_id=%s",
                        call_id,
                        exc_info=True,
                    )
                self._raise_if_owner_departed()
            remove_ai.result()
        except asyncio.CancelledError:
            try:
                await self._coordinator._await_network_task(remove_ai)
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
        self._raise_if_owner_departed()

    async def _fail_joining_owner_transfer(self, *, failure: str, cleaned: bool) -> None:
        coordinator = self._coordinator
        call = self.call
        call_id = self.call_id
        transitioned: bool | None
        try:
            transitioned = await coordinator._db.fail_joining_transfer(
                call_id, self.joining, failure
            )
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
                promoted_transition = await coordinator._db.fail_promoted_transfer(
                    call_id,
                    self.in_progress,
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
                    finished = await coordinator._finish_claimed_termination(
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
                await coordinator._complete_conference_or_schedule(call)
                return
            if promoted_transition is None:
                await coordinator._complete_conference_or_schedule(call)
                return
        if not cleaned and transitioned is True:
            try:
                terminated = await coordinator._terminate_call(
                    call_id,
                    "transfer_cleanup_failed",
                    _initiating_task=self.workflow_task,
                )
            except Exception:
                terminated = False
                logger.warning(
                    "owner cleanup termination failed call_id=%s",
                    call_id,
                    exc_info=True,
                )
            if not terminated:
                await coordinator._complete_conference_or_schedule(call)
        elif self.owner_call_sid is not None and (transitioned is None or not cleaned):
            # SQLite state is ambiguous. Conference completion is the only
            # DB-independent guarantee that the owner leg cannot keep billing.
            await coordinator._complete_conference_or_schedule(call)

    async def _fail_promoted_owner_transfer(
        self,
        *,
        expected: str,
        failure: str,
        failure_reason: str,
    ) -> None:
        coordinator = self._coordinator
        call = self.call
        call_id = self.call_id
        transitioned: bool | None
        try:
            transitioned = await coordinator._db.fail_promoted_transfer(
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
            and expected == self.in_progress
            and self.completed_outcome is not None
        ):
            try:
                transitioned = await coordinator._db.fail_promoted_transfer(
                    call_id,
                    self.completed_outcome,
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
                finished = await coordinator._finish_claimed_termination(
                    self.promoted or call,
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
        await coordinator._complete_conference_or_schedule(call)

    async def _cleanup_owner(self) -> bool:
        if self.owner_call_sid is None:
            return True
        if self.owner_cleanup_started:
            return False
        self.owner_cleanup_started = True
        return await self._remove_owner_transfer_leg()

    async def _fail_joining_and_compensate(self, failure: str, cleaned: bool) -> None:
        await self._spawn_and_await_compensation(
            self._fail_joining_owner_transfer(failure=failure, cleaned=cleaned),
            name=f"compensate-joining-transfer:{self.call_id}",
        )

    async def _fail_promoted_and_compensate(
        self, expected: str, failure: str, failure_reason: str
    ) -> None:
        await self._spawn_and_await_compensation(
            self._fail_promoted_owner_transfer(
                expected=expected,
                failure=failure,
                failure_reason=failure_reason,
            ),
            name=f"compensate-promoted-transfer:{self.call_id}",
        )

    async def run(self) -> dict[str, Any]:
        coordinator = self._coordinator
        call = self.call
        call_id = self.call_id
        reason = self.reason
        tool_call_id = self.tool_call_id
        self.workflow_task = asyncio.current_task()
        create_task: asyncio.Task[Any] | None = None

        try:
            create_task = asyncio.create_task(
                coordinator._twilio.create_owner_participant(
                    call_id=call_id,
                    plan_id=call["plan_id"],
                    conference_sid_or_name=self.conference,
                    owner_phone=self.packet.escalation.owner_phone,
                ),
                name=f"create-owner:{call_id}",
            )
            participant = await asyncio.shield(create_task)
            self.owner_call_sid = participant.call_sid
            self._coordinator.expected_sids[call_id] = self.owner_call_sid
            self._reconcile_owner_transfer_callbacks()
            await self._persist_owner_transfer_sid()
            await asyncio.wait_for(self.event.wait(), timeout=OWNER_JOIN_TIMEOUT_SECONDS)
            self._raise_if_owner_departed()
            self.promoted = await coordinator._db.promote_transfer(call_id, reason)
            if self.promoted is None:
                cleaned = await self._cleanup_owner()
                await self._fail_joining_and_compensate("failed:termination_won", cleaned)
                await self._send_owner_transfer_failure(
                    "call ended before owner transfer completed"
                )
                return {"accepted": False, "error": "call ended during owner transfer"}
            self.phase = "promoted"
            self._raise_if_owner_departed()

            if tool_call_id is not None:
                try:
                    # A successful tool output must be observable before the AI leg disappears.
                    await coordinator._realtime().send_tool_result(
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
            self._raise_if_owner_departed()

            await self._remove_ai_leg_racing_departure()

            self.completed_outcome = f"completed:{reason}"
            if not await coordinator._db.complete_promoted_transfer(
                call_id, self.in_progress, self.completed_outcome
            ):
                raise RuntimeError("owner transfer completion state was lost")
            self.phase = "completed"
            self._raise_if_owner_departed()
            # The owner becomes responsible for conference lifetime only after the
            # handoff outcome is durably completed. If arming this fails, compensate
            # the transfer rather than leave the callee alone and billing later.
            arm_owner_exit = asyncio.create_task(
                coordinator._twilio.enable_end_conference_on_exit(
                    self.conference, self.owner_call_sid
                ),
                name=f"arm-owner-conference-exit:{call_id}",
            )
            await coordinator._await_network_task(arm_owner_exit)
            self._raise_if_owner_departed()
            terminalization = coordinator._spawn(
                coordinator._finish_claimed_termination(
                    self.promoted,
                    "transfer_completed",
                    preserve_conference=True,
                    await_finalizer=False,
                    expected_transfer_outcome=self.completed_outcome,
                ),
                name=f"terminalize-owner-transfer:{call_id}",
                must_finish=True,
            )
            terminalized = await coordinator._await_network_task(terminalization)
            if not terminalized:
                cleaned = await self._cleanup_owner()
                await self._fail_promoted_and_compensate(
                    self.completed_outcome,
                    "failed:terminal_cas",
                    "transfer_failed:terminal_cas",
                )
                if not cleaned:
                    await coordinator._complete_conference_or_schedule(call)
                return {"accepted": False, "error": "transfer teardown requires retry"}
            self.phase = "terminal"
            return {"accepted": True, "status": "transferred"}
        except TimeoutError:
            cleaned = await self._cleanup_owner()
            await self._fail_joining_and_compensate("failed:owner_join_timeout", cleaned)
            await self._send_owner_transfer_failure("owner did not join")
            return {"accepted": False, "error": "owner did not join"}
        except asyncio.CancelledError:
            # If creation completed in its worker thread after cancellation, recover
            # its SID before returning so the remote owner leg cannot leak.
            if self.owner_call_sid is None and create_task is not None:
                try:
                    participant = await coordinator._await_network_task(create_task)
                    self.owner_call_sid = participant.call_sid
                except Exception:
                    pass
            cleaned = await self._cleanup_owner()
            if self.phase == "promoted":
                failure_reason = "transfer_failed:CancelledError"
                failure = "failed:CancelledError"
                await self._fail_promoted_and_compensate(
                    self.in_progress,
                    failure,
                    failure_reason,
                )
            elif self.phase == "completed" and self.completed_outcome is not None:
                await self._fail_promoted_and_compensate(
                    self.completed_outcome,
                    "failed:CancelledError",
                    "transfer_failed:CancelledError",
                )
            elif self.phase != "terminal":
                await self._fail_joining_and_compensate("failed:CancelledError", cleaned)
            return {"accepted": False, "error": "owner transfer cancelled"}
        except Exception as exc:
            logger.exception("owner transfer failed call_id=%s", call_id)
            cleaned = await self._cleanup_owner()
            error_type = type(exc).__name__
            failure_reason = f"transfer_failed:{error_type}"
            failure = f"failed:{error_type}"
            if self.phase == "promoted":
                await self._fail_promoted_and_compensate(
                    self.in_progress,
                    failure,
                    failure_reason,
                )
            elif self.phase == "completed" and self.completed_outcome is not None:
                await self._fail_promoted_and_compensate(
                    self.completed_outcome,
                    failure,
                    failure_reason,
                )
            elif self.phase != "terminal":
                await self._fail_joining_and_compensate(failure, cleaned)
                await self._send_owner_transfer_failure("owner transfer failed")
            return {"accepted": False, "error": "owner transfer failed"}
