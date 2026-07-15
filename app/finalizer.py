from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

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


class Finalizer:
    def __init__(self, settings: Settings, db: Database, openai: AsyncOpenAI):
        self.settings = settings
        self.db = db
        self.openai = openai
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _call_status(state: str) -> str:
        if state in {CallState.COMPLETED.value, CallState.TRANSFERRED.value}:
            return state
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
        fallback_outcome = "failed" if call_status == "failed" else "unknown"
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
            extracted = await self._extract(call, plan, transcript)
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
        await self.db.save_result_with_transcript(call_id, result, transcript)
        await self._maybe_push(result)
        return result

    async def _extract(
        self, call: dict[str, Any], plan: dict[str, Any], transcript: list[Any]
    ) -> ExtractedCallResult:
        evidence_ids = {turn.turn_id for turn in transcript}
        payload = {
            "approved_plan": ContextPacket.model_validate(plan["context"]).model_dump(mode="json"),
            "termination_reason": call.get("termination_reason"),
            "answered_by": call.get("answered_by"),
            "duration_seconds": call.get("duration_seconds"),
            "transfer_outcome": call.get("transfer_outcome"),
            "realtime_advisory_outcome": call.get("advisory_outcome"),
            "transcript": [turn.model_dump(mode="json") for turn in transcript],
        }
        for attempt in range(2):
            try:
                response = await self.openai.responses.parse(
                    model=self.settings.extractor_model,
                    instructions=EXTRACTOR_INSTRUCTIONS,
                    input=json.dumps(payload, ensure_ascii=False),
                    text_format=ExtractedCallResult,
                    store=False,
                    timeout=self.settings.openai_extraction_timeout_seconds,
                )
                parsed = response.output_parsed
                if parsed is None:
                    raise ValueError("extractor returned no parsed output")
                for group in (
                    parsed.commitments,
                    parsed.confirmation_numbers,
                    parsed.follow_ups,
                ):
                    for item in group:
                        if not set(item.evidence_turn_ids).issubset(evidence_ids):
                            raise ValueError("extractor cited an unknown transcript turn_id")
                return parsed
            except Exception as exc:
                if attempt == 0 and self._retryable_extraction_error(exc):
                    await asyncio.sleep(0.25)
                    continue
                raise
        raise RuntimeError("unreachable")

    async def _maybe_push(self, result: StoredCallResult) -> None:
        from app.poke_push import push_message_to_poke

        await push_message_to_poke(self.settings, result.model_dump(mode="json"))
