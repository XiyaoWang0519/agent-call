from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from time import monotonic_ns
from typing import Any

from app.db import Database, LatencyMark
from app.models import TERMINAL_STATES, CallState

logger = logging.getLogger(__name__)

CALL_ACTIVITY_TOMBSTONE_TTL_SECONDS = 15 * 60
CALL_ACTIVITY_TOMBSTONE_MAX = 4096


class CallActivityTracker:
    """Owns call-liveness/activity bookkeeping for CallService.

    A composed collaborator (not a mixin): CallService holds one instance and
    delegates activity/liveness tracking to it. ``_audio_drain_terminations``
    stays on CallService (it is termination-flow state, not activity state);
    this tracker consults/clears it through the callables supplied at
    construction time instead of owning it directly.
    """

    def __init__(
        self,
        db: Database,
        *,
        is_audio_drain_active: Callable[[str], bool],
        clear_audio_drain: Callable[[str], object],
    ) -> None:
        self._db = db
        self._is_audio_drain_active = is_audio_drain_active
        self._clear_audio_drain = clear_audio_drain
        self.latest: dict[str, LatencyMark] = {}
        self.dirty: dict[str, LatencyMark] = {}
        self.watchdog_claims: set[str] = set()
        self.tombstones: OrderedDict[str, int] = OrderedDict()
        self.active_response_ids: dict[str, str | None] = {}
        self.sip_output_playing: set[str] = set()
        self.inflight_tools: set[str] = set()
        self.dtmf_listen_deadlines_ns: dict[str, int] = {}

    def note(self, call_id: str, mark: LatencyMark | None = None) -> bool:
        # A watchdog claim is the liveness linearization point. Activity observed
        # before the claim updates these maps; activity after it cannot resurrect a
        # call whose timeout teardown has already won.
        self.prune_tombstones()
        if call_id in self.watchdog_claims or call_id in self.tombstones:
            return False
        observed = mark or LatencyMark.now()
        latest = self.latest.get(call_id)
        if latest is not None and latest.monotonic_ns >= observed.monotonic_ns:
            return True
        self.latest[call_id] = observed
        dirty = self.dirty.get(call_id)
        if dirty is None or dirty.monotonic_ns < observed.monotonic_ns:
            self.dirty[call_id] = observed
        return True

    def assistant_work_is_live(self, call_id: str) -> bool:
        """True when the assistant is generating, playing SIP audio, or awaiting a tool.

        The 15s stale watchdog only sees sideband/Twilio heartbeats. SIP media and
        in-flight tool waits can leave that clock idle while the callee still hears
        (or is about to hear) the assistant, so those states must count as live.
        """
        return (
            call_id in self.active_response_ids
            or call_id in self.sip_output_playing
            or self._is_audio_drain_active(call_id)
            or call_id in self.inflight_tools
        )

    def begin_dtmf_listen_grace(self, call_id: str, *, seconds: float) -> bool:
        """Keep an intentional post-DTMF silence out of the stale-call path.

        Realtime emits no sideband frames for silence or a bare IVR beep. After a
        successful tone send, that quiet period is expected while the remote system
        processes the input. The deadline is monotonic, process-local, and reset by a
        later DTMF send.
        """

        self.prune_tombstones()
        if call_id in self.watchdog_claims or call_id in self.tombstones:
            return False
        self.dtmf_listen_deadlines_ns[call_id] = monotonic_ns() + int(seconds * 1_000_000_000)
        return True

    def dtmf_listen_grace_is_live(self, call_id: str, *, now_ns: int | None = None) -> bool:
        deadline_ns = self.dtmf_listen_deadlines_ns.get(call_id)
        if deadline_ns is None:
            return False
        observed_ns = monotonic_ns() if now_ns is None else now_ns
        if observed_ns < deadline_ns:
            return True
        self.dtmf_listen_deadlines_ns.pop(call_id, None)
        return False

    def clear_assistant_work(self, call_id: str) -> None:
        self.active_response_ids.pop(call_id, None)
        self.sip_output_playing.discard(call_id)
        self._clear_audio_drain(call_id)
        self.inflight_tools.discard(call_id)
        self.dtmf_listen_deadlines_ns.pop(call_id, None)

    def clear(self, call_id: str) -> None:
        self.latest.pop(call_id, None)
        self.dirty.pop(call_id, None)
        self.watchdog_claims.discard(call_id)

    def tombstone(self, call_id: str) -> None:
        now_ns = monotonic_ns()
        self.prune_tombstones(now_ns)
        self.tombstones.pop(call_id, None)
        self.tombstones[call_id] = now_ns + (CALL_ACTIVITY_TOMBSTONE_TTL_SECONDS * 1_000_000_000)
        while len(self.tombstones) > CALL_ACTIVITY_TOMBSTONE_MAX:
            self.tombstones.popitem(last=False)
        self.clear_assistant_work(call_id)
        self.clear(call_id)

    def prune_tombstones(self, now_ns: int | None = None) -> None:
        cutoff = monotonic_ns() if now_ns is None else now_ns
        while self.tombstones:
            _, expires_at = next(iter(self.tombstones.items()))
            if expires_at > cutoff:
                break
            self.tombstones.popitem(last=False)

    @staticmethod
    def is_closed(call: dict[str, Any]) -> bool:
        state = CallState(call["state"])
        return state == CallState.TERMINATING or state in TERMINAL_STATES

    async def flush(self) -> None:
        if not self.dirty:
            return
        pending = self.dirty
        self.dirty = {}
        try:
            await self._db.touch_calls(
                (call_id, mark.occurred_at) for call_id, mark in pending.items()
            )
        except BaseException as exc:
            # Keep the newest observation if more activity arrived while the write
            # was in flight. A terminal cleanup removes the latest map entry and
            # therefore prevents a failed flush from re-adding a dead call.
            for call_id, failed_mark in pending.items():
                latest = self.latest.get(call_id)
                if latest is None:
                    continue
                candidate = (
                    latest if latest.monotonic_ns >= failed_mark.monotonic_ns else failed_mark
                )
                current = self.dirty.get(call_id)
                if current is None or current.monotonic_ns < candidate.monotonic_ns:
                    self.dirty[call_id] = candidate
            if not isinstance(exc, Exception):
                raise
            logger.warning("failed to persist batched call activity", exc_info=True)
