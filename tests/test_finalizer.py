from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
import respx
from openai import APITimeoutError
from pydantic import SecretStr

from app.finalizer import Finalizer
from app.models import CallState, ExtractedCallResult
from tests.conftest import seed_call


class FakeResponses:
    def __init__(self, parsed=None, error=None):
        self.parsed = parsed
        self.error = error
        self.calls = 0

    async def parse(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(output_parsed=self.parsed)


@pytest.mark.asyncio
async def test_terminal_state_and_raw_transcript_saved_before_extraction(settings, service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    await service.db.add_transcript_turn(
        call_id=call_id,
        turn_id="turn_1",
        speaker="callee",
        text="Your appointment is confirmed for July 20 at 2 PM.",
        source_event_type="transcription.completed",
        source_event_id="evt_1",
    )

    class InspectingResponses(FakeResponses):
        async def parse(inner_self, **kwargs):
            stored = await service.db.get_result(call_id)
            assert stored.finalization_status == "telephony_only"
            assert stored.raw_transcript_available is True
            return await super().parse(**kwargs)

    parsed = ExtractedCallResult(
        outcome="completed",
        summary="Appointment confirmed.",
        commitments=[],
        confirmation_numbers=[],
        follow_ups=[],
        confidence=0.95,
    )
    responses = InspectingResponses(parsed=parsed)
    finalizer = Finalizer(settings, service.db, SimpleNamespace(responses=responses))
    result = await finalizer.finalize(call_id)
    assert result.finalization_status == "succeeded"
    assert result.result_source == "post_call_extractor"


@pytest.mark.asyncio
async def test_extractor_rejects_unknown_evidence_and_preserves_raw_transcript(
    settings, service, packet
):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    await service.db.add_transcript_turn(
        call_id=call_id,
        turn_id="turn_real",
        speaker="callee",
        text="Reference ABC-123.",
        source_event_type="transcription.completed",
        source_event_id="evt_real",
    )
    parsed = ExtractedCallResult(
        outcome="completed",
        summary="Completed.",
        commitments=[],
        confirmation_numbers=[{"value": "ABC-123", "evidence_turn_ids": ["turn_invented"]}],
        follow_ups=[],
        confidence=0.9,
    )
    finalizer = Finalizer(
        settings,
        service.db,
        SimpleNamespace(responses=FakeResponses(parsed=parsed)),
    )
    result = await finalizer.finalize(call_id)
    assert result.call_status == "completed"
    assert result.finalization_status == "failed"
    assert result.outcome == "unknown"
    assert result.result_source == "extraction_failed"
    assert result.raw_transcript_available is True
    assert (await service.db.get_transcript(call_id))[0].text == "Reference ABC-123."


@pytest.mark.asyncio
async def test_concurrent_finalization_runs_extractor_once(settings, service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    parsed = ExtractedCallResult(
        outcome="completed",
        summary="Completed.",
        commitments=[],
        confirmation_numbers=[],
        follow_ups=[],
        confidence=0.9,
    )

    class SlowResponses(FakeResponses):
        async def parse(inner_self, **kwargs):
            await asyncio.sleep(0.02)
            return await super().parse(**kwargs)

    responses = SlowResponses(parsed=parsed)
    finalizer = Finalizer(settings, service.db, SimpleNamespace(responses=responses))
    first, second = await asyncio.gather(finalizer.finalize(call_id), finalizer.finalize(call_id))
    assert first == second
    assert responses.calls == 1


@pytest.mark.asyncio
@respx.mock
async def test_optional_push_http_failure_never_changes_stored_result(settings, service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    settings.poke_push_enabled = True
    settings.poke_api_key = SecretStr("poke-test")
    route = respx.post("https://poke.com/api/v1/inbound/api-message").mock(
        return_value=httpx.Response(503)
    )
    finalizer = Finalizer(
        settings,
        service.db,
        SimpleNamespace(responses=FakeResponses(error=ValueError("invalid extraction"))),
    )

    result = await finalizer.finalize(call_id)

    assert route.called
    assert result.finalization_status == "failed"
    assert await service.db.get_result(call_id) == result


@pytest.mark.asyncio
async def test_extractor_retries_one_transient_failure_only(settings, service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    parsed = ExtractedCallResult(
        outcome="completed",
        summary="Completed after one retry.",
        commitments=[],
        confirmation_numbers=[],
        follow_ups=[],
        confidence=0.8,
    )

    class TransientThenSuccess(FakeResponses):
        async def parse(inner_self, **kwargs):
            inner_self.calls += 1
            if inner_self.calls == 1:
                raise APITimeoutError(request=httpx.Request("POST", "https://api.openai.com"))
            return SimpleNamespace(output_parsed=parsed)

    responses = TransientThenSuccess()
    finalizer = Finalizer(settings, service.db, SimpleNamespace(responses=responses))
    result = await finalizer.finalize(call_id)

    assert responses.calls == 2
    assert result.finalization_status == "succeeded"
