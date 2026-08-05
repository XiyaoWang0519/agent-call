from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import respx
from openai import APIStatusError, APITimeoutError
from pydantic import SecretStr

from app.finalizer import Finalizer, format_owner_summary
from app.models import CallState, ExtractedCallResult, StoredCallResult
from tests.conftest import seed_call


class FakeResponses:
    def __init__(self, parsed=None, error=None, usage=None):
        self.parsed = parsed
        self.error = error
        self.usage = usage
        self.calls = 0
        self.timeouts: list[float] = []

    async def parse(self, **kwargs):
        self.calls += 1
        self.timeouts.append(kwargs["timeout"])
        if self.error:
            raise self.error
        return SimpleNamespace(output_parsed=self.parsed, usage=self.usage)


def _stored_result(**overrides) -> StoredCallResult:
    fields = dict(
        call_id="call_test",
        call_status="completed",
        finalization_status="succeeded",
        outcome="completed",
        result_source="post_call_extractor",
        summary="Booked table for 2 at 7pm.",
        transcript_complete=True,
        raw_transcript_available=True,
    )
    fields.update(overrides)
    return StoredCallResult(**fields)


def test_format_owner_summary_completed_with_confirmation_and_action_follow_up():
    result = _stored_result(
        confirmation_numbers=[{"value": "48", "evidence_turn_ids": ["turn_1"]}],
        follow_ups=[
            {
                "value": "Call back to confirm allergy info.",
                "evidence_turn_ids": ["turn_1"],
                "owner_action_required": True,
            },
            {
                "value": "No action needed here.",
                "evidence_turn_ids": ["turn_1"],
                "owner_action_required": False,
            },
        ],
    )

    text = format_owner_summary(result)

    assert text.startswith("📞 Booked table for 2 at 7pm.")
    assert "Confirmation #48" in text
    assert "Action needed: Call back to confirm allergy info." in text
    assert "No action needed here." not in text
    assert "call call_test — details via get_call_result" in text


def test_format_owner_summary_non_completed_outcome_shows_label():
    result = _stored_result(outcome="declined", summary="They said no thanks.")

    text = format_owner_summary(result)

    assert text.startswith("📞 Declined: They said no thanks.")


def test_format_owner_summary_multiple_confirmation_numbers():
    result = _stored_result(
        confirmation_numbers=[
            {"value": "48", "evidence_turn_ids": ["turn_1"]},
            {"value": "49", "evidence_turn_ids": ["turn_2"]},
        ]
    )

    text = format_owner_summary(result)

    assert "Confirmations: #48, #49" in text


def test_format_owner_summary_failed_finalization_notes_extraction_problem():
    result = _stored_result(
        finalization_status="failed",
        outcome="unknown",
        result_source="extraction_failed",
        summary="The call ended, but structured extraction failed.",
    )

    text = format_owner_summary(result)

    assert "Automatic extraction had problems; the full transcript is saved." in text


def status_error(status_code: int) -> APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(status_code, request=request)
    return APIStatusError(
        f"OpenAI returned {status_code}",
        response=response,
        body=None,
    )


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
    assert responses.timeouts == [30.0]


@pytest.mark.asyncio
async def test_extractor_timeout_can_exceed_live_control_timeout(settings, service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    parsed = ExtractedCallResult(
        outcome="completed",
        summary="Appointment confirmed.",
        commitments=[],
        confirmation_numbers=[],
        follow_ups=[],
        confidence=0.95,
    )
    settings.openai_extraction_timeout_seconds = 45
    responses = FakeResponses(parsed=parsed)

    result = await Finalizer(settings, service.db, SimpleNamespace(responses=responses)).finalize(
        call_id
    )

    assert result.finalization_status == "succeeded"
    assert responses.timeouts == [45]


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
    responses = FakeResponses(parsed=parsed)
    finalizer = Finalizer(settings, service.db, SimpleNamespace(responses=responses))
    result = await finalizer.finalize(call_id)
    assert responses.calls == 2
    assert result.call_status == "completed"
    assert result.finalization_status == "failed"
    assert result.outcome == "unknown"
    assert result.result_source == "extraction_failed"
    assert result.raw_transcript_available is True
    assert (await service.db.get_transcript(call_id))[0].text == "Reference ABC-123."


@pytest.mark.asyncio
async def test_extractor_unknown_evidence_retry_carries_feedback_and_can_recover(
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
    bad = ExtractedCallResult(
        outcome="completed",
        summary="Completed.",
        commitments=[],
        confirmation_numbers=[{"value": "ABC-123", "evidence_turn_ids": ["evt_real"]}],
        follow_ups=[],
        confidence=0.9,
    )
    good = ExtractedCallResult(
        outcome="completed",
        summary="Completed.",
        commitments=[],
        confirmation_numbers=[{"value": "ABC-123", "evidence_turn_ids": ["turn_real"]}],
        follow_ups=[],
        confidence=0.9,
    )

    class BadThenGood(FakeResponses):
        def __init__(self):
            super().__init__()
            self.instructions_seen: list[str] = []

        async def parse(inner_self, **kwargs):
            inner_self.calls += 1
            inner_self.instructions_seen.append(kwargs["instructions"])
            return SimpleNamespace(output_parsed=bad if inner_self.calls == 1 else good)

    responses = BadThenGood()
    result = await Finalizer(settings, service.db, SimpleNamespace(responses=responses)).finalize(
        call_id
    )

    assert responses.calls == 2
    assert "evt_real" in responses.instructions_seen[1]
    assert result.finalization_status == "succeeded"
    assert result.result_source == "post_call_extractor"


@pytest.mark.asyncio
async def test_extractor_citation_with_surrounding_whitespace_is_canonicalized(
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
    padded = ExtractedCallResult(
        outcome="completed",
        summary="Completed.",
        commitments=[],
        confirmation_numbers=[{"value": "ABC-123", "evidence_turn_ids": [" turn_real \n"]}],
        follow_ups=[],
        confidence=0.9,
    )
    responses = FakeResponses(parsed=padded)
    finalizer = Finalizer(settings, service.db, SimpleNamespace(responses=responses))
    result = await finalizer.finalize(call_id)

    assert responses.calls == 1
    assert result.finalization_status == "succeeded"
    assert result.confirmation_numbers[0].evidence_turn_ids == ["turn_real"]


@pytest.mark.asyncio
async def test_extractor_payload_exposes_only_citable_turn_fields(settings, service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    await service.db.add_transcript_turn(
        call_id=call_id,
        turn_id="item_abc",
        speaker="callee",
        text="Confirmed for 7 PM.",
        source_event_type="transcription.completed",
        source_event_id="event_xyz",
    )
    parsed = ExtractedCallResult(
        outcome="completed",
        summary="Completed.",
        commitments=[],
        confirmation_numbers=[],
        follow_ups=[],
        confidence=0.9,
    )

    class CapturingResponses(FakeResponses):
        def __init__(self, parsed):
            super().__init__(parsed=parsed)
            self.inputs: list[str] = []

        async def parse(inner_self, **kwargs):
            inner_self.inputs.append(kwargs["input"])
            return await super().parse(**kwargs)

    responses = CapturingResponses(parsed)
    await Finalizer(settings, service.db, SimpleNamespace(responses=responses)).finalize(call_id)

    payload = json.loads(responses.inputs[0])
    assert payload["transcript"] == [
        {"turn_id": "item_abc", "speaker": "callee", "text": "Confirmed for 7 PM."}
    ]


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
    settings.agent_push_enabled = True
    settings.agent_webhook_token = SecretStr("agent-webhook-test")
    route = respx.post("https://hooks.example.test/hooks/agent").mock(
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
@respx.mock
async def test_optional_push_sends_owner_summary_text(settings, service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    settings.agent_push_enabled = True
    settings.agent_webhook_token = SecretStr("agent-webhook-test")
    route = respx.post("https://hooks.example.test/hooks/agent").mock(
        return_value=httpx.Response(200)
    )
    parsed = ExtractedCallResult(
        outcome="completed",
        summary="Booked table for 2 at 7pm.",
        commitments=[],
        confirmation_numbers=[{"value": "48", "evidence_turn_ids": ["turn_1"]}],
        follow_ups=[],
        confidence=0.95,
    )
    await service.db.add_transcript_turn(
        call_id=call_id,
        turn_id="turn_1",
        speaker="callee",
        text="Your table is confirmed, reference 48.",
        source_event_type="transcription.completed",
        source_event_id="evt_1",
    )
    finalizer = Finalizer(
        settings, service.db, SimpleNamespace(responses=FakeResponses(parsed=parsed))
    )

    result = await finalizer.finalize(call_id)

    assert route.called
    body = json.loads(route.calls[-1].request.content)
    assert body["name"] == "Agent Call"
    assert body["wakeMode"] == "now"
    assert isinstance(body["message"], str)
    assert "Booked table for 2 at 7pm." in body["message"]
    assert "Confirmation #48" in body["message"]
    assert body["message"] == format_owner_summary(result)


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


@pytest.mark.asyncio
async def test_extractor_retries_one_server_error(settings, service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    parsed = ExtractedCallResult(
        outcome="completed",
        summary="Completed after a server retry.",
        commitments=[],
        confirmation_numbers=[],
        follow_ups=[],
        confidence=0.8,
    )

    class ServerErrorThenSuccess(FakeResponses):
        async def parse(inner_self, **kwargs):
            inner_self.calls += 1
            if inner_self.calls == 1:
                raise status_error(500)
            return SimpleNamespace(output_parsed=parsed)

    responses = ServerErrorThenSuccess()
    result = await Finalizer(settings, service.db, SimpleNamespace(responses=responses)).finalize(
        call_id
    )

    assert responses.calls == 2
    assert result.finalization_status == "succeeded"


@pytest.mark.asyncio
async def test_extractor_usage_is_persisted_to_calls_row(settings, service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    parsed = ExtractedCallResult(
        outcome="completed",
        summary="Completed.",
        commitments=[],
        confirmation_numbers=[],
        follow_ups=[],
        confidence=0.9,
    )
    responses = FakeResponses(
        parsed=parsed, usage=SimpleNamespace(input_tokens=123, output_tokens=45)
    )
    finalizer = Finalizer(settings, service.db, SimpleNamespace(responses=responses))

    result = await finalizer.finalize(call_id)

    assert result.finalization_status == "succeeded"
    call = await service.db.get_call(call_id)
    assert call["extractor_input_tokens"] == 123
    assert call["extractor_output_tokens"] == 45


@pytest.mark.asyncio
async def test_extractor_does_not_retry_nontransient_client_error(settings, service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    responses = FakeResponses(error=status_error(400))

    result = await Finalizer(settings, service.db, SimpleNamespace(responses=responses)).finalize(
        call_id
    )

    assert responses.calls == 1
    assert result.finalization_status == "failed"
