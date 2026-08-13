from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

from app.agent_push import push_message_to_agent
from app.db import Database
from app.models import (
    CallState,
    ContextPacket,
    ExtractedCallResult,
    FollowUp,
    StoredCallResult,
)
from app.prompts import EXTRACTOR_INSTRUCTIONS
from app.settings import Settings

logger = logging.getLogger(__name__)

_OUTCOME_LABELS: dict[str, str] = {
    "completed": "Done",
    "partially_completed": "Partially done",
    "needs_follow_up": "Needs follow-up",
    "declined": "Declined",
    "voicemail_left": "Voicemail left",
    "wrong_number": "Wrong number",
    "transferred": "Transferred to a human",
    "failed": "Call failed",
    "unknown": "Outcome unknown",
}


def format_owner_summary(result: StoredCallResult) -> str:
    """Short owner-facing text for the post-call agent push."""
    lines: list[str] = []
    summary = result.summary.strip()
    label = _OUTCOME_LABELS.get(result.outcome, "Outcome unknown")
    if result.outcome == "completed" and summary:
        lines.append(f"📞 {summary}")
    elif summary:
        lines.append(f"📞 {label}: {summary}")
    else:
        lines.append(f"📞 {label}.")
    if result.confirmation_numbers:
        values = [item.value for item in result.confirmation_numbers]
        if len(values) == 1:
            lines.append(f"Confirmation #{values[0]}")
        else:
            lines.append("Confirmations: " + ", ".join(f"#{v}" for v in values))
    confirmed = [c.value for c in result.commitments if c.status == "confirmed"]
    if confirmed:
        lines.append("Confirmed: " + "; ".join(confirmed))
    actions = [f.value for f in result.follow_ups if f.owner_action_required]
    if actions:
        lines.append("Action needed: " + "; ".join(actions))
    if result.finalization_status != "succeeded":
        lines.append("Automatic extraction had problems; the full transcript is saved.")
    lines.append(f"(call {result.call_id} — details via get_call_result)")
    return "\n".join(lines)


class UnknownEvidenceError(ValueError):
    def __init__(self, unknown_ids: set[str]):
        self.unknown_ids = unknown_ids
        super().__init__(f"extractor cited unknown transcript turn_ids: {sorted(unknown_ids)}")


class Finalizer:
    def __init__(self, settings: Settings, db: Database, openai: AsyncOpenAI):
        self.settings = settings
        self.db = db
        self.openai = openai
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _call_status(state: str) -> Literal["completed", "transferred", "failed", "timed_out"]:
        if state == CallState.COMPLETED.value:
            return "completed"
        if state == CallState.TRANSFERRED.value:
            return "transferred"
        if state == CallState.TIMED_OUT.value:
            return "timed_out"
        return "failed"

    @staticmethod
    def _retryable_extraction_error(exc: Exception) -> bool:
        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return True
        if isinstance(exc, APIStatusError):
            return exc.status_code in {408, 409, 429} or exc.status_code >= 500
        return False

    async def finalize(self, call_id: str) -> StoredCallResult:
        lock = self._locks.setdefault(call_id, asyncio.Lock())
        async with lock:
            stored = await self.db.get_result(call_id)
            if stored is not None and stored.finalization_status != "telephony_only":
                return stored
            return await self._finalize_once(call_id)

    async def _finalize_once(self, call_id: str) -> StoredCallResult:
        call = await self.db.get_call(call_id)
        if call is None:
            raise LookupError(call_id)
        plan = await self.db.get_plan(call["plan_id"])
        if plan is None:
            raise LookupError(call["plan_id"])
        transcript = await self.db.get_transcript(call_id)
        if not transcript:
            await self.db.add_transcript_turn(
                call_id=call_id,
                turn_id=f"telephony_empty_{call_id}",
                speaker="system",
                text="No speech transcript turns were available when the call was finalized.",
                source_event_type="finalizer.transcript_empty",
                source_event_id=f"finalizer:transcript-empty:{call_id}",
            )
            transcript = await self.db.get_transcript(call_id)
        call_status = self._call_status(call["state"])
        fatal_reasons = {
            "sideband_error",
            "openai_fatal_error",
            "transcription_config_mismatch",
            "session_update_timeout",
            "session_update_mismatch",
        }
        reason = call.get("termination_reason") or ""
        transcript_complete = any(turn.speaker != "system" for turn in transcript) and not any(
            reason.startswith(prefix) for prefix in fatal_reasons
        )
        fallback_outcome: Literal[
            "completed",
            "partially_completed",
            "needs_follow_up",
            "declined",
            "voicemail_left",
            "wrong_number",
            "transferred",
            "failed",
            "unknown",
        ] = "failed" if call_status == "failed" else "unknown"
        if call.get("answer_handling") == "voicemail":
            fallback_outcome = "voicemail_left"
        fallback = StoredCallResult(
            call_id=call_id,
            call_status=call_status,
            finalization_status="telephony_only",
            outcome=fallback_outcome,
            result_source="telephony_only",
            summary=f"Call ended: {call.get('termination_reason') or 'telephony terminal'}.",
            answered_by=call.get("answered_by"),
            answer_handling=call.get("answer_handling"),
            transcript_complete=transcript_complete,
            raw_transcript_available=True,
        )
        # This transaction occurs before the external extraction request.
        await self.db.save_result_with_transcript(call_id, fallback, transcript)

        try:
            extracted, (extractor_input_tokens, extractor_output_tokens) = await self._extract(
                call, plan, transcript
            )
        except Exception:
            logger.exception("post-call extraction failed for %s", call_id)
            turn_ids = [turn.turn_id for turn in transcript]
            failure = fallback.model_copy(
                update={
                    "finalization_status": "failed",
                    "outcome": "unknown" if call_status != "failed" else "failed",
                    "result_source": "extraction_failed",
                    "summary": "The call ended, but structured extraction failed.",
                    "follow_ups": [
                        FollowUp(
                            value="Review the raw transcript.",
                            evidence_turn_ids=turn_ids[:1],
                            owner_action_required=True,
                        )
                    ],
                }
            )
            await self.db.save_result_with_transcript(call_id, failure, transcript)
            await self._maybe_push(failure)
            return failure

        result = StoredCallResult(
            call_id=call_id,
            call_status=call_status,
            finalization_status="succeeded",
            outcome=extracted.outcome,
            result_source="post_call_extractor",
            summary=extracted.summary,
            commitments=extracted.commitments,
            confirmation_numbers=extracted.confirmation_numbers,
            follow_ups=extracted.follow_ups,
            answered_by=call.get("answered_by"),
            answer_handling=call.get("answer_handling"),
            transcript_complete=transcript_complete,
            raw_transcript_available=True,
        )
        if extractor_input_tokens or extractor_output_tokens:
            await self.db.add_extractor_usage(
                call_id,
                input_tokens=extractor_input_tokens,
                output_tokens=extractor_output_tokens,
            )
        await self.db.save_result_with_transcript(call_id, result, transcript)
        await self._maybe_push(result)
        return result

    async def _extract(
        self, call: dict[str, Any], plan: dict[str, Any], transcript: list[Any]
    ) -> tuple[ExtractedCallResult, tuple[int, int]]:
        # Normalize (but do not case-fold) turn_ids so an extractor citation padded with
        # incidental whitespace still validates; a matching stripped id is canonicalized
        # back to the exact transcript turn_id before being persisted.
        evidence_ids_by_stripped = {turn.turn_id.strip(): turn.turn_id for turn in transcript}
        payload = {
            "approved_plan": ContextPacket.model_validate(plan["context"]).model_dump(mode="json"),
            "termination_reason": call.get("termination_reason"),
            "answered_by": call.get("answered_by"),
            "duration_seconds": call.get("duration_seconds"),
            "transfer_outcome": call.get("transfer_outcome"),
            "realtime_advisory_outcome": call.get("advisory_outcome"),
            # Only the citable id is exposed; source_event_id and friends are omitted so
            # the extractor cannot confuse a similar-looking id namespace with turn_id.
            "transcript": [
                {"turn_id": turn.turn_id, "speaker": turn.speaker, "text": turn.text}
                for turn in transcript
            ],
        }
        last_unknown: list[str] = []
        total_input_tokens = 0
        total_output_tokens = 0
        for attempt in range(2):
            instructions = EXTRACTOR_INSTRUCTIONS
            if last_unknown:
                instructions += (
                    "\nYour previous attempt cited turn_ids that do not exist in the "
                    f"transcript: {json.dumps(last_unknown)}. Cite only exact turn_id "
                    "values that appear in the provided transcript entries."
                )
            try:
                response = await self.openai.responses.parse(
                    model=self.settings.extractor_model,
                    instructions=instructions,
                    input=json.dumps(payload, ensure_ascii=False),
                    text_format=ExtractedCallResult,
                    store=False,
                    timeout=self.settings.openai_extraction_timeout_seconds,
                )
                usage = getattr(response, "usage", None)
                total_input_tokens += getattr(usage, "input_tokens", 0) or 0
                total_output_tokens += getattr(usage, "output_tokens", 0) or 0
                parsed = response.output_parsed
                if parsed is None:
                    raise ValueError("extractor returned no parsed output")
                unknown: set[str] = set()
                for group in (
                    parsed.commitments,
                    parsed.confirmation_numbers,
                    parsed.follow_ups,
                ):
                    for item in group:
                        canonical_ids = []
                        for turn_id in item.evidence_turn_ids:
                            canonical = evidence_ids_by_stripped.get(turn_id.strip())
                            if canonical is None:
                                unknown.add(turn_id)
                                canonical_ids.append(turn_id)
                            else:
                                canonical_ids.append(canonical)
                        item.evidence_turn_ids = canonical_ids
                if unknown:
                    raise UnknownEvidenceError(unknown)
                return parsed, (total_input_tokens, total_output_tokens)
            except UnknownEvidenceError as exc:
                if attempt == 0:
                    last_unknown = sorted(exc.unknown_ids)
                    continue
                raise
            except Exception as exc:
                if attempt == 0 and self._retryable_extraction_error(exc):
                    await asyncio.sleep(0.25)
                    continue
                raise
        raise RuntimeError("unreachable")

    async def _maybe_push(self, result: StoredCallResult) -> None:
        await push_message_to_agent(self.settings, format_owner_summary(result))
