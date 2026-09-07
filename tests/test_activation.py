from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.db import LatencyMark
from app.models import CallState, PreparePhoneCallInput
from app.openai_realtime import RESPONSE_PURPOSE_METADATA_KEY, VOICEMAIL_RESPONSE_PURPOSE
from tests.conftest import seed_call, wait_background


@pytest.fixture(autouse=True)
def short_opening_delay(monkeypatch):
    monkeypatch.setattr("app.call_state.OPENING_LISTEN_SECONDS", 0.02)
    monkeypatch.setattr("app.call_state.VOICE_END_REPLY_GRACE_SECONDS", 0.02)


async def wait_opening(service, call_id):
    task = service._opening_tasks.get(call_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=1)


@pytest.mark.asyncio
async def test_callee_is_not_dialed_until_accept_and_sideband_open(service, packet):
    prepared = await service.prepare(
        PreparePhoneCallInput(
            context=packet,
            authority_basis="Owner explicitly requested this call",
            requested_by_owner=True,
        )
    )
    started = await service.start(
        prepared.plan_id,
        explicit_confirmation=True,
        confirmation_text=prepared.confirmation_summary,
    )
    assert service._test_twilio.agent_creates == 1
    assert service._test_twilio.callee_creates == 0

    mapped = await service.handle_openai_incoming(
        "rtc_incoming",
        [
            {"name": "X-Plan-Id", "value": prepared.plan_id},
            {"name": "X-Bridge-Call-Id", "value": started.call_id},
        ],
    )
    assert mapped == started.call_id
    assert service._test_realtime.accepts == [(started.call_id, "rtc_incoming")]
    assert service._test_twilio.callee_creates == 0

    await service.handle_sideband_open(started.call_id)
    assert service._test_twilio.callee_creates == 1
    assert service._test_realtime.initial_updates == [started.call_id]
    await wait_background()
    latency = await service.db.get_latency_events(started.call_id)
    assert [event["stage"] for event in latency] == [
        "twilio_agent_request",
        "twilio_agent_created",
        "openai_accept_request",
        "openai_accept_completed",
        "sideband_open",
        "initial_session_ack",
        "twilio_callee_request",
        "twilio_callee_created",
    ]
    assert all(datetime.fromisoformat(event["occurred_at"]).tzinfo is not None for event in latency)
    assert len({event["clock_id"] for event in latency}) == 1
    assert [event["monotonic_ns"] for event in latency] == sorted(
        event["monotonic_ns"] for event in latency
    )


@pytest.mark.asyncio
async def test_unmapped_incoming_sip_call_is_explicitly_rejected(service):
    with pytest.raises(LookupError):
        await service.handle_openai_incoming(
            "rtc_unknown",
            [
                {"name": "X-Plan-Id", "value": "plan_unknown"},
                {"name": "X-Bridge-Call-Id", "value": "call_unknown"},
            ],
        )
    assert service._test_realtime.rejects == ["rtc_unknown"]


@pytest.mark.asyncio
async def test_incoming_sip_requires_exact_call_and_plan_headers(service, packet):
    call_id = await seed_call(service.db, packet, openai_call_id=None)
    plan_id = f"plan_{call_id}"

    with pytest.raises(LookupError):
        await service.handle_openai_incoming(
            "rtc_missing_call_header",
            [
                {"name": "X-Plan-Id", "value": plan_id},
                {"name": "X-Unrelated", "value": f"prefix {call_id} suffix"},
            ],
        )
    with pytest.raises(LookupError):
        await service.handle_openai_incoming(
            "rtc_missing_plan_header",
            [{"name": "X-Bridge-Call-Id", "value": call_id}],
        )
    with pytest.raises(LookupError):
        await service.handle_openai_incoming(
            "rtc_duplicate_header",
            [
                {"name": "X-Plan-Id", "value": plan_id},
                {"name": "X-Bridge-Call-Id", "value": call_id},
                {"name": "X-Bridge-Call-Id", "value": "call_other"},
            ],
        )
    with pytest.raises(LookupError):
        await service.handle_openai_incoming(
            "rtc_malformed_header",
            [
                {"name": "X-Plan-Id", "value": plan_id},
                {"name": "X-Bridge-Call-Id", "value": f"{call_id} extra"},
            ],
        )

    assert service._test_realtime.rejects == [
        "rtc_missing_call_header",
        "rtc_missing_plan_header",
        "rtc_duplicate_header",
        "rtc_malformed_header",
    ]


@pytest.mark.asyncio
async def test_incoming_sip_binding_is_atomic(service, packet):
    call_id = await seed_call(service.db, packet, openai_call_id=None)
    plan_id = f"plan_{call_id}"
    headers = [
        {"name": "X-Plan-Id", "value": plan_id},
        {"name": "X-Bridge-Call-Id", "value": call_id},
    ]

    results = await asyncio.gather(
        service.handle_openai_incoming("rtc_first", headers),
        service.handle_openai_incoming("rtc_second", headers),
        return_exceptions=True,
    )

    assert sum(result == call_id for result in results) == 1
    assert sum(isinstance(result, RuntimeError) for result in results) == 1
    call = await service.db.get_call(call_id)
    assert call["openai_call_id"] in {"rtc_first", "rtc_second"}
    assert len(service._test_realtime.accepts) == 1
    assert len(service._test_realtime.rejects) == 1


@pytest.mark.asyncio
async def test_realtime_error_logs_exclude_raw_provider_events(service, packet, caplog):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    provider_secret = "sk-provider-secret-123456789"

    with caplog.at_level("INFO", logger="app.call_state"):
        await service.handle_realtime_event(
            call_id,
            {
                "type": "error",
                "event_id": "evt_secret",
                "error": {
                    "code": "provider_error",
                    "type": "invalid_request_error",
                    "message": f"api_key={provider_secret}\ninvalid request",
                },
            },
        )
    await wait_background()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "provider_error" in messages
    assert "invalid_request_error" in messages
    assert provider_secret not in messages
    assert "evt_secret" not in messages


@pytest.mark.asyncio
async def test_failed_response_logs_exclude_raw_status_details(service, packet, caplog):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    provider_secret = "whsec_provider-secret-123456789"

    with caplog.at_level("ERROR", logger="app.call_state"):
        await service.handle_realtime_event(
            call_id,
            {
                "type": "response.done",
                "event_id": "evt_secret",
                "response": {
                    "id": "resp_failed",
                    "status": "failed",
                    "status_details": {
                        "error": {
                            "code": "response_failed",
                            "type": "server_error",
                            "message": f"secret={provider_secret}",
                        }
                    },
                },
            },
        )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "response_failed" in messages
    assert "server_error" in messages
    assert provider_secret not in messages
    assert "evt_secret" not in messages


@pytest.mark.asyncio
async def test_initial_session_update_mismatch_terminates_before_dialing(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(
        call_id, transcription_verified=0, semantic_vad_verified=0, callee_dialed=0
    )
    service._test_realtime.initial_update_event["session"]["audio"]["input"]["transcription"][
        "model"
    ] = "unexpected-model"
    await service.handle_sideband_open(call_id)
    call = await service.db.get_call(call_id)
    assert call["transcription_verified"] == 0
    assert call["semantic_vad_verified"] == 1
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "transcription_config_mismatch"
    assert service._test_twilio.callee_creates == 0


@pytest.mark.asyncio
async def test_missing_session_created_activates_from_explicit_session_update(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(
        call_id,
        sideband_open=1,
        callee_joined=1,
        transcription_verified=0,
        semantic_vad_verified=0,
    )
    await service.handle_amd(call_id, "human")
    assert (await service.db.get_call(call_id))["state"] == CallState.PREWARMING.value
    await wait_opening(service, call_id)
    assert service._test_realtime.events == []

    await service.handle_sideband_open(call_id)
    assert (await service.db.get_call(call_id))["state"] == CallState.ACTIVE.value
    assert service._test_realtime.initial_updates == [call_id]
    await wait_opening(service, call_id)
    assert service._test_realtime.events == [
        ("session.update", call_id),
        ("opening", call_id),
    ]


@pytest.mark.asyncio
async def test_opening_unmutes_agent_before_speech(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)
    await service.handle_amd(call_id, "human")
    assert service._test_twilio.unmuted == [("CF" + "a" * 32, "CA" + "a" * 32)]
    await wait_opening(service, call_id)
    assert service._test_realtime.events == [
        ("session.update", call_id),
        ("opening", call_id),
    ]


@pytest.mark.asyncio
async def test_callee_join_stays_muted_until_amd_and_listens_before_opening(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_conference_event(
        call_id,
        {
            "StatusCallbackEvent": "participant-join",
            "ParticipantLabel": "callee",
            "CallSid": "CA" + "b" * 32,
        },
    )
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.PREWARMING.value
    assert call["amd_result"] is None
    assert call["opening_sent"] == 0
    assert service._test_realtime.events == []
    assert service._test_twilio.unmuted == []
    await wait_opening(service, call_id)
    await service.handle_amd(call_id, "human")
    await wait_opening(service, call_id)
    assert (await service.db.get_call(call_id))["opening_sent"] == 1
    assert service._test_realtime.events == [("session.update", call_id), ("opening", call_id)]


@pytest.mark.asyncio
async def test_callee_answered_status_activates_before_conference_join(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    call = await service.db.get_call(call_id)
    assert call["callee_joined"] == 1
    assert call["answered_at"] is not None
    assert call["opening_sent"] == 0
    assert call["state"] == CallState.PREWARMING.value
    assert service._test_realtime.events == []
    await service.handle_conference_event(
        call_id,
        {
            "StatusCallbackEvent": "participant-join",
            "ParticipantLabel": "callee",
            "CallSid": "CA" + "b" * 32,
        },
    )
    await service.handle_amd(call_id, "human")
    await wait_opening(service, call_id)
    assert (await service.db.get_call(call_id))["opening_sent"] == 1
    assert service._test_realtime.events == [("session.update", call_id), ("opening", call_id)]


@pytest.mark.asyncio
async def test_opening_exactly_once_after_confirmed_session_update(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_conference_event(
        call_id,
        {
            "StatusCallbackEvent": "participant-join",
            "ParticipantLabel": "callee",
            "CallSid": "CA" + "b" * 32,
        },
    )
    await service.handle_amd(call_id, "human")
    await service.handle_amd(call_id, "human")
    await service.handle_conference_event(
        call_id,
        {
            "StatusCallbackEvent": "participant-join",
            "ParticipantLabel": "callee",
            "CallSid": "CA" + "b" * 32,
        },
    )

    await wait_opening(service, call_id)
    assert service._test_realtime.events == [
        ("session.update", call_id),
        ("opening", call_id),
    ]
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.ACTIVE.value
    assert call["opening_sent"] == 1


@pytest.mark.asyncio
async def test_conference_start_alone_does_not_mark_callee_joined(service, packet):
    """conference-start fires when the agent SIP leg connects, before the callee answers.

    Regression: treating it as callee join used to send the opening turn into an empty
    conference while the callee's phone was still ringing, so the callee heard only the
    tail of the opening followed by silence until the watchdog hung up.
    """
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)

    await service.handle_conference_event(call_id, {"StatusCallbackEvent": "conference-start"})

    call = await service.db.get_call(call_id)
    assert call["callee_joined"] == 0
    assert call["answered_at"] is None
    assert call["opening_sent"] == 0
    assert call["state"] == CallState.PREWARMING.value
    assert service._test_realtime.events == []

    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    call = await service.db.get_call(call_id)
    assert call["callee_joined"] == 1
    assert call["answered_at"] is not None
    assert call["opening_sent"] == 0
    assert call["state"] == CallState.PREWARMING.value
    assert service._test_realtime.events == []
    await service.handle_amd(call_id, "human")
    await wait_opening(service, call_id)
    assert service._test_realtime.events == [("session.update", call_id), ("opening", call_id)]


@pytest.mark.asyncio
async def test_session_update_must_echo_both_flags(service, packet):
    call_id = await seed_call(service.db, packet)
    service._test_realtime.update_event["session"]["audio"]["input"]["turn_detection"][
        "interrupt_response"
    ] = False
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)
    await service.handle_amd(call_id, "human")
    assert (await service.db.get_call(call_id))["state"] == CallState.FAILED.value
    assert not [event for event in service._test_realtime.events if event[0] == "opening"]


@pytest.mark.asyncio
async def test_caller_speech_uses_auto_response_without_manual_create(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)
    await service.handle_amd(call_id, "human")
    before = list(service._test_realtime.events)
    await service.handle_realtime_event(
        call_id,
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "event_id": "evt_speech",
            "item_id": "turn_user_1",
            "transcript": "Yes, this is Alex.",
        },
    )
    await service.handle_realtime_event(
        call_id,
        {"type": "response.created", "event_id": "evt_auto_response"},
    )
    assert service._test_realtime.events == before
    transcript = await service.db.get_transcript(call_id)
    assert transcript[0].turn_id == "turn_user_1"


@pytest.mark.asyncio
async def test_assistant_transcript_persists_for_aliased_done_event(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)
    await service.handle_amd(call_id, "human")
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.audio_transcript.done",
            "event_id": "evt_alias_done",
            "item_id": "turn_assistant_alias",
            "transcript": "Hello from the alias event.",
        },
    )
    transcript = await service.db.get_transcript(call_id)
    assert [(turn.speaker, turn.text) for turn in transcript] == [
        ("assistant", "Hello from the alias event.")
    ]
    assert transcript[0].turn_id == "turn_assistant_alias"


@pytest.mark.asyncio
async def test_tool_output_is_followed_by_observed_continuation(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.function_call_arguments.done",
            "event_id": "evt_tool",
            "call_id": "tool_1",
            "name": "record_call_outcome",
            "arguments": (
                '{"status":"completed","summary":"Done","commitments":[],"followUps":[]}'
            ),
        },
    )
    await service.handle_realtime_event(
        call_id, {"type": "response.created", "event_id": "evt_tool_continue"}
    )
    call = await service.db.get_call(call_id)
    assert call["tool_call_count"] == 1
    assert call["tool_continuation_observed"] == 1
    assert call["advisory_outcome"]["summary"] == "Done"


@pytest.mark.asyncio
async def test_latency_stages_capture_answer_first_output_and_tool_turnaround(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "answered"})
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.output_audio.delta",
            "event_id": "evt_audio",
            "delta": "not-persisted-audio",
        },
    )
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.output_audio_transcript.delta",
            "event_id": "evt_transcript",
            "delta": "Hello",
        },
    )
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.function_call_arguments.done",
            "event_id": "evt_tool",
            "call_id": "tool_latency",
            "name": "record_call_outcome",
            "arguments": (
                '{"status":"completed","summary":"Done","commitments":[],"followUps":[]}'
            ),
        },
    )
    await service.handle_realtime_send(
        call_id,
        {
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": "tool_latency"},
        },
    )
    await service.handle_realtime_send(call_id, {"type": "response.create"})
    await wait_background()

    events = await service.db.get_latency_events(call_id)
    by_stage = {event["stage"]: event for event in events}
    assert {
        "callee_answered",
        "first_openai_audio_delta",
        "first_assistant_transcript",
        "tool_call_received",
        "tool_result_sent",
        "first_response_create",
    } <= by_stage.keys()
    assert by_stage["tool_call_received"]["event_key"] == "tool_latency"
    assert by_stage["tool_result_sent"]["event_key"] == "tool_latency"
    assert (
        by_stage["tool_call_received"]["monotonic_ns"]
        <= by_stage["tool_result_sent"]["monotonic_ns"]
    )


@pytest.mark.asyncio
async def test_earlier_answer_callback_mark_wins_when_it_finishes_later(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    marks = iter(
        [
            LatencyMark("2026-07-14T12:00:00+00:00", 100),
            LatencyMark("2026-07-14T12:00:01+00:00", 200),
        ]
    )
    monkeypatch.setattr(LatencyMark, "now", classmethod(lambda cls: next(marks)))

    original_get_call = service.db.get_call
    first_get_started = asyncio.Event()
    release_first_get = asyncio.Event()
    get_count = 0

    async def delay_first_get(call_id: str):
        nonlocal get_count
        get_count += 1
        if get_count == 1:
            first_get_started.set()
            await release_first_get.wait()
        return await original_get_call(call_id)

    monkeypatch.setattr(service.db, "get_call", delay_first_get)
    earlier_callback = asyncio.create_task(
        service.handle_conference_event(
            call_id,
            {
                "StatusCallbackEvent": "participant-join",
                "ParticipantLabel": "callee",
                "CallSid": "CA" + "b" * 32,
            },
        )
    )
    await first_get_started.wait()

    # This callback has a later receipt mark but reaches persistence first.
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "answered"})
    release_first_get.set()
    await earlier_callback
    await wait_background()

    answer_event = next(
        event
        for event in await service.db.get_latency_events(call_id)
        if event["stage"] == "callee_answered"
    )
    assert answer_event["monotonic_ns"] == 100
    assert answer_event["occurred_at"] == "2026-07-14T12:00:00+00:00"


@pytest.mark.asyncio
async def test_voice_model_ends_call_only_after_its_completed_response(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service._handle_tool_call(
        call_id,
        {
            "call_id": "tool_end",
            "response_id": "resp_function",
            "name": "end_call",
            "arguments": '{"reason":"objective_completed"}',
        },
    )
    assert (await service.db.get_call(call_id))["state"] == CallState.ACTIVE.value
    assert service._test_realtime.tool_result_continuations[-1] is True

    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.done",
            "response": {"id": "resp_function", "status": "completed"},
        },
    )
    assert (await service.db.get_call(call_id))["state"] == CallState.ACTIVE.value
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.created",
            "response": {"id": "resp_closing"},
        },
    )

    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.done",
            "response": {"id": "resp_closing", "status": "completed"},
        },
    )
    await wait_background()

    # Generation finishing must not hang up while the goodbye is still playing over SIP.
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.ACTIVE.value
    assert service._test_realtime.hangups == []

    await service.handle_realtime_event(
        call_id,
        {"type": "output_audio_buffer.stopped", "response_id": "resp_closing"},
    )
    await wait_background()

    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.COMPLETED.value
    assert call["termination_reason"] == "voice_model_end_call"
    assert service._test_realtime.hangups == ["rtc_test"]
    assert service._test_twilio.completed == ["CF" + "a" * 32]


@pytest.mark.asyncio
async def test_voice_end_ignores_stale_audio_buffer_stopped_events(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service._handle_tool_call(
        call_id,
        {
            "call_id": "tool_end",
            "name": "end_call",
            "arguments": '{"reason":"objective_completed"}',
        },
    )
    await service.handle_realtime_event(
        call_id,
        {"type": "response.created", "response": {"id": "resp_closing"}},
    )
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.done",
            "response": {"id": "resp_closing", "status": "completed"},
        },
    )

    # A drain event for an earlier response must not end the call early.
    await service.handle_realtime_event(
        call_id,
        {"type": "output_audio_buffer.stopped", "response_id": "resp_earlier"},
    )
    await wait_background()
    assert (await service.db.get_call(call_id))["state"] == CallState.ACTIVE.value
    assert service._test_realtime.hangups == []

    await service.handle_realtime_event(
        call_id,
        {"type": "output_audio_buffer.stopped", "response_id": "resp_closing"},
    )
    await wait_background()
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.COMPLETED.value
    assert call["termination_reason"] == "voice_model_end_call"


@pytest.mark.asyncio
async def test_voice_end_interrupted_playback_returns_to_conversation(service, packet):
    """Cleared playback is an interruption, not a completed farewell exchange."""

    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service._handle_tool_call(
        call_id,
        {
            "call_id": "tool_end",
            "name": "end_call",
            "arguments": '{"reason":"objective_completed"}',
        },
    )
    await service.handle_realtime_event(
        call_id,
        {"type": "response.created", "response": {"id": "resp_closing"}},
    )
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.done",
            "response": {"id": "resp_closing", "status": "completed"},
        },
    )
    await service.handle_realtime_event(
        call_id,
        {"type": "output_audio_buffer.cleared", "response_id": "resp_closing"},
    )
    await wait_background()
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.ACTIVE.value
    assert service._test_realtime.hangups == []
    assert call_id not in service._audio_drain_terminations


@pytest.mark.asyncio
async def test_voice_end_terminates_after_drain_timeout_without_buffer_event(
    service, packet, monkeypatch
):
    monkeypatch.setattr("app.call_state.TERMINATION_AUDIO_DRAIN_TIMEOUT_SECONDS", 0.05)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service._handle_tool_call(
        call_id,
        {
            "call_id": "tool_end",
            "name": "end_call",
            "arguments": '{"reason":"objective_completed"}',
        },
    )
    await service.handle_realtime_event(
        call_id,
        {"type": "response.created", "response": {"id": "resp_closing"}},
    )
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.done",
            "response": {"id": "resp_closing", "status": "completed"},
        },
    )
    await wait_background()
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.COMPLETED.value
    assert call["termination_reason"] == "voice_model_end_call"


@pytest.mark.asyncio
async def test_interrupted_voice_closing_does_not_end_call(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service._handle_tool_call(
        call_id,
        {
            "call_id": "tool_end",
            "response_id": "resp_function",
            "name": "end_call",
            "arguments": '{"reason":"objective_completed"}',
        },
    )
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.created",
            "response": {"id": "resp_closing"},
        },
    )
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.done",
            "response": {"id": "resp_closing", "status": "cancelled"},
        },
    )
    await wait_background()

    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.ACTIVE.value
    assert call["interruption_observed"] == 1
    assert service._test_realtime.hangups == []


@pytest.mark.asyncio
async def test_transfer_tool_returns_output_before_removing_ai(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._owner_join_events.setdefault(call_id, __import__("asyncio").Event()).set()
    order: list[str] = []
    original_send = service._test_realtime.send_tool_result
    original_remove = service._test_twilio.remove_participant

    async def ordered_send(*args, **kwargs):
        order.append("tool_output")
        await original_send(*args, **kwargs)

    async def ordered_remove(*args, **kwargs):
        order.append("remove_participant")
        await original_remove(*args, **kwargs)

    service._test_realtime.send_tool_result = ordered_send
    service._test_twilio.remove_participant = ordered_remove
    await service._handle_tool_call(
        call_id,
        {
            "call_id": "tool_transfer",
            "name": "transfer_to_owner",
            "arguments": '{"reason":"owner needed"}',
        },
    )
    await wait_background()
    assert order[:2] == ["tool_output", "remove_participant"]


@pytest.mark.asyncio
@pytest.mark.parametrize("answered_by", ["unknown", "future_new_value"])
async def test_unknown_amd_assumes_human_and_never_hangs_up(service, packet, answered_by):
    call_id = await seed_call(service.db, packet, call_id=f"call_{answered_by}")
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)
    await service.handle_amd(call_id, answered_by)
    call = await service.db.get_call(call_id)
    assert call["answer_handling"] == "assumed_human"
    assert call["state"] == CallState.ACTIVE.value
    assert service._test_realtime.hangups == []


@pytest.mark.asyncio
async def test_machine_waits_for_message_end_and_all_gates(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_amd(call_id, "machine_end_beep")
    assert service._test_realtime.events == []
    await service.db.update_call(call_id, sideband_open=1)
    await service._check_activation_gate(call_id)
    assert service._test_realtime.events == []
    await service.db.update_call(call_id, callee_joined=1)
    await service._check_activation_gate(call_id)
    assert service._test_realtime.events == [
        ("voicemail", call_id),
    ]


@pytest.mark.asyncio
async def test_machine_end_other_does_not_create_an_extra_response(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    assert service._test_realtime.events == []
    await service.handle_amd(call_id, "machine_end_other")
    call = await service.db.get_call(call_id)
    assert call["answered_by"] == "machine_end_other"
    assert call["answer_handling"] == "assumed_human"
    assert call["state"] == CallState.ACTIVE.value
    await wait_opening(service, call_id)
    assert service._test_realtime.events == [("session.update", call_id), ("opening", call_id)]
    before = list(service._test_realtime.events)
    await service.handle_amd(call_id, "machine_end_other")
    assert service._test_realtime.events == before
    assert service._test_realtime.request_response_calls == []


@pytest.mark.asyncio
async def test_voicemail_activation_never_enables_automatic_responses(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)

    async def forbidden_update(_call_id: str):
        raise AssertionError("voicemail must not enable automatic responses")

    service._test_realtime.enable_automatic_responses = forbidden_update

    await service.handle_amd(call_id, "machine_end_beep")

    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.ACTIVE.value
    assert service._test_realtime.events == [("voicemail", call_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("classification", ["machine_end_beep", "machine_end_silence"])
async def test_late_amd_never_exposes_ordinary_audio(service, packet, classification):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    for kind in ["speech_started", "speech_stopped", "committed"]:
        service._observe_opening_event(call_id, {"type": f"input_audio_buffer.{kind}"})
    await asyncio.sleep(0.04)
    assert service._test_realtime.events == []
    assert service._test_twilio.unmuted == []
    await service.handle_amd(call_id, classification)
    await wait_opening(service, call_id)
    assert service._test_realtime.events == [("voicemail", call_id)]
    assert service._test_realtime.suspend_calls == []


@pytest.mark.asyncio
async def test_classification_and_unmute_finish_before_enabling_responses(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    reached = asyncio.Event()
    release = asyncio.Event()
    original = service._test_twilio.unmute_participant

    async def slow_unmute(*args):
        reached.set()
        await release.wait()
        await original(*args)

    service._test_twilio.unmute_participant = slow_unmute
    task = asyncio.create_task(service.handle_amd(call_id, "human"))
    await asyncio.wait_for(reached.wait(), 1)
    assert service._test_realtime.events == []
    # Duplicated/concurrent callbacks cannot change accepted classification.
    await service.handle_amd(call_id, "machine_end_beep")
    release.set()
    await task
    await wait_opening(service, call_id)
    assert service._test_realtime.events == [("session.update", call_id), ("opening", call_id)]
    assert (await service.db.get_call(call_id))["answer_handling"] == "human"


@pytest.mark.asyncio
async def test_termination_during_unmute_prevents_response_enable(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})

    async def terminate_during_unmute(*args):
        await service.terminate_call(call_id, "callee_participant_leave")

    service._test_twilio.unmute_participant = terminate_during_unmute
    await service.handle_amd(call_id, "human")
    assert service._test_realtime.events == []
    assert (await service.db.get_call(call_id))["state"] == CallState.COMPLETED.value


@pytest.mark.asyncio
async def test_late_voicemail_amd_cancels_in_flight_opening(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    # Defensive legacy-state test; new calls remain muted before AMD.
    await service.db.update_call(call_id, state=CallState.ACTIVE.value)
    service._opening_listen[call_id].enabled = True
    await service._test_realtime.enable_automatic_responses(call_id)
    await service._test_realtime.create_opening(call_id)
    await wait_opening(service, call_id)
    assert service._test_realtime.events == [("session.update", call_id), ("opening", call_id)]

    await service.handle_realtime_event(
        call_id, {"type": "response.created", "response": {"id": "resp_opening"}}
    )
    await service.handle_amd(call_id, "machine_end_beep")

    assert service._test_realtime.events == [
        ("session.update", call_id),
        ("opening", call_id),
        ("session.update", call_id),
        ("cancel_response", call_id),
        ("voicemail", call_id),
    ]

    # The cancelled opening's response.done must not count as the voicemail finishing.
    await service.handle_realtime_event(
        call_id,
        {"type": "response.done", "response": {"id": "resp_opening", "status": "cancelled"}},
    )
    await wait_background()
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.ACTIVE.value

    # An unrelated completed response must not be mistaken for the voicemail response.
    await service.handle_realtime_event(
        call_id,
        {"type": "response.done", "response": {"id": "resp_other", "status": "completed"}},
    )
    await wait_background()
    assert call_id not in service._audio_drain_terminations

    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.done",
            "response": {
                "id": "resp_vm",
                "status": "completed",
                "metadata": {
                    RESPONSE_PURPOSE_METADATA_KEY: VOICEMAIL_RESPONSE_PURPOSE,
                },
            },
        },
    )
    await wait_background()
    # The voicemail hangup waits for playback to drain so the message is not cut off.
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.ACTIVE.value

    await service.handle_realtime_event(
        call_id,
        {"type": "output_audio_buffer.stopped", "response_id": "resp_vm"},
    )
    await wait_background()
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.COMPLETED.value
    assert call["termination_reason"] == "voicemail_left"


@pytest.mark.asyncio
async def test_stale_response_cancel_error_is_not_fatal(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)
    await service.handle_amd(call_id, "human")

    await service.handle_realtime_event(
        call_id,
        {"type": "error", "error": {"code": "response_cancel_not_active"}},
    )
    await wait_background()
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.ACTIVE.value


@pytest.mark.asyncio
async def test_fax_terminates(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_amd(call_id, "fax")
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "fax_detected"


@pytest.mark.asyncio
async def test_conflicting_duplicate_amd_cannot_override_first_result(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)
    await service.handle_amd(call_id, "human")
    await service.handle_amd(call_id, "fax")
    call = await service.db.get_call(call_id)
    assert call["answered_by"] == "human"
    assert call["answer_handling"] == "human"
    assert call["state"] == CallState.ACTIVE.value
    assert service._test_realtime.hangups == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_only", [False, True])
@pytest.mark.parametrize(
    "event",
    [
        {"type": "input_audio_buffer.speech_started", "item_id": "greeting"},
        {"type": "response.created", "response": {"id": "native_reply"}},
    ],
)
async def test_first_activity_suppresses_silent_opening(service, packet, reader_only, event):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    await service.handle_amd(call_id, "human")
    if reader_only:
        # Reader observation must protect the timer even if the dispatcher is busy.
        service._observe_opening_event(call_id, event)
    else:
        await service.handle_realtime_event(call_id, event)
    await wait_opening(service, call_id)
    assert (await service.db.get_call(call_id))["opening_sent"] == 0
    assert service._test_realtime.events == [("session.update", call_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intervening_event",
    [
        None,
        {"type": "input_audio_buffer.speech_started", "item_id": "second_turn"},
        {"type": "response.created", "response": {"id": "native_reply"}},
    ],
)
async def test_completed_pre_activation_turn_resumes_only_when_unanswered(
    service, packet, intervening_event
):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    service._observe_opening_event(
        call_id, {"type": "input_audio_buffer.speech_started", "item_id": "early_greeting"}
    )
    service._observe_opening_event(
        call_id, {"type": "input_audio_buffer.speech_stopped", "item_id": "early_greeting"}
    )
    service._observe_opening_event(
        call_id, {"type": "input_audio_buffer.committed", "item_id": "early_greeting"}
    )
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    await service.handle_amd(call_id, "human")
    if intervening_event:
        service._observe_opening_event(call_id, intervening_event)
    await wait_opening(service, call_id)
    expected = [("session.update", call_id)]
    if intervening_event is None:
        expected.append(("opening", call_id))
    assert service._test_realtime.events == expected


@pytest.mark.asyncio
async def test_late_voicemail_suspends_native_reply_without_opening_claim(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    # Defensive legacy-state test; new calls remain muted before AMD.
    await service.db.update_call(call_id, state=CallState.ACTIVE.value)
    service._opening_listen[call_id].enabled = True
    await service._test_realtime.enable_automatic_responses(call_id)
    await service.handle_realtime_event(
        call_id, {"type": "response.created", "response": {"id": "native_reply"}}
    )
    assert (await service.db.get_call(call_id))["opening_sent"] == 0
    await service.handle_amd(call_id, "machine_end_beep")
    await service.handle_amd(call_id, "machine_end_beep")
    await wait_opening(service, call_id)
    assert service._test_realtime.events == [
        ("session.update", call_id),
        ("session.update", call_id),
        ("cancel_response", call_id),
        ("voicemail", call_id),
    ]
    assert service._test_realtime.suspend_calls == [call_id]


@pytest.mark.asyncio
async def test_termination_before_listen_delay_prevents_opening(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    await service.handle_amd(call_id, "fax")
    await wait_opening(service, call_id)
    assert ("opening", call_id) not in service._test_realtime.events
    assert (await service.db.get_call(call_id))["termination_reason"] == "fax_detected"


@pytest.mark.asyncio
@pytest.mark.parametrize("attributed", [True, False])
async def test_only_opening_race_response_error_is_benign(service, packet, attributed):
    call_id = await seed_call(service.db, packet)
    await service.handle_realtime_event(
        call_id,
        {
            "type": "error",
            "error": {
                "code": "conversation_already_has_active_response",
                "event_id": f"opening_{call_id}" if attributed else "other_request",
            },
        },
    )
    await wait_background()
    call = await service.db.get_call(call_id)
    assert (call["termination_reason"] == "openai_fatal_error") is not attributed


@pytest.mark.asyncio
async def test_dispatcher_does_not_replay_stale_speech_over_reader_state(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    started = {"type": "input_audio_buffer.speech_started"}
    stopped = {"type": "input_audio_buffer.speech_stopped"}
    service._observe_opening_event(call_id, started)
    service._observe_opening_event(call_id, stopped)
    service._observe_opening_event(call_id, started)
    await service.handle_realtime_event(call_id, stopped, _observed=True)
    assert service._opening_listen[call_id].speech_active


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "mismatch"])
async def test_late_voicemail_fails_closed_if_responses_cannot_be_suspended(
    service, packet, failure
):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "answered"})
    # Defensive legacy-state test; new calls remain muted before AMD.
    await service.db.update_call(call_id, state=CallState.ACTIVE.value)
    service._opening_listen[call_id].enabled = True
    await service._test_realtime.enable_automatic_responses(call_id)
    if failure == "timeout":
        service._test_realtime.suspend_failures_remaining = 1
    else:
        service._test_realtime.initial_update_event = service._test_realtime.update_event
    await service.handle_amd(call_id, "machine_end_beep")
    call = await service.db.get_call(call_id)
    assert call["termination_reason"] == f"session_update_{failure}"
    assert ("voicemail", call_id) not in service._test_realtime.events


@pytest.mark.asyncio
async def test_old_cancelled_response_cannot_clear_new_active_response(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_realtime_event(
        call_id, {"type": "response.created", "response": {"id": "new_response"}}
    )
    await service.handle_realtime_event(
        call_id,
        {"type": "response.done", "response": {"id": "old_response", "status": "cancelled"}},
    )
    assert service._activity.active_response_ids[call_id] == "new_response"


@pytest.mark.asyncio
async def test_greeting_before_sideband_setup_resumes_without_second_listen_delay(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet)
    assert call_id not in service._opening_listen
    for kind in ["speech_started", "speech_stopped", "committed"]:
        service._observe_opening_event(call_id, {"type": f"input_audio_buffer.{kind}"})
    await service.handle_sideband_open(call_id)
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    assert service._test_realtime.events == []
    assert service._test_twilio.unmuted == []
    monkeypatch.setattr("app.call_state.OPENING_LISTEN_SECONDS", 10.0)
    await service.handle_amd(call_id, "human")
    # wait_opening is bounded at one second: a second listen delay would fail.
    await wait_opening(service, call_id)
    assert service._test_realtime.events == [("session.update", call_id), ("opening", call_id)]


@pytest.mark.asyncio
async def test_concurrent_amd_during_activation_update_cannot_inject_voicemail(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    reached = asyncio.Event()
    release = asyncio.Event()
    original = service._test_realtime.enable_automatic_responses

    async def delayed_update(candidate_call_id):
        assert service._test_twilio.unmuted == [("CF" + "a" * 32, "CA" + "a" * 32)]
        reached.set()
        await release.wait()
        return await original(candidate_call_id)

    service._test_realtime.enable_automatic_responses = delayed_update
    task = asyncio.create_task(service.handle_amd(call_id, "human"))
    await asyncio.wait_for(reached.wait(), 1)
    await service.handle_amd(call_id, "machine_end_beep")
    assert service._test_realtime.events == []
    assert (await service.db.get_call(call_id))["answer_handling"] == "human"
    release.set()
    await task
    await wait_opening(service, call_id)
    assert service._test_realtime.events == [("session.update", call_id), ("opening", call_id)]
