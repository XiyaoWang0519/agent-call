from __future__ import annotations

import pytest

from app.models import CallState, PreparePhoneCallInput
from tests.conftest import seed_call, wait_background


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
async def test_session_created_config_mismatch_terminates_before_activation(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_realtime_event(
        call_id,
        {
            "type": "session.created",
            "event_id": "evt_session_bad",
            "transcription_ok": False,
            "vad_ok": True,
        },
    )
    await wait_background()
    call = await service.db.get_call(call_id)
    assert call["transcription_verified"] == 0
    assert call["semantic_vad_verified"] == 1
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "transcription_config_mismatch"


@pytest.mark.asyncio
async def test_telephony_gates_wait_for_verified_session_created(service, packet):
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
    assert service._test_realtime.events == []

    await service.handle_realtime_event(
        call_id,
        {
            "type": "session.created",
            "event_id": "evt_session_good",
            "transcription_ok": True,
            "vad_ok": True,
        },
    )
    assert (await service.db.get_call(call_id))["state"] == CallState.GREETING_STARTED.value
    assert service._test_realtime.events == [
        ("session.update", call_id),
        ("greeting", call_id),
    ]


@pytest.mark.asyncio
async def test_greeting_exactly_once_after_confirmed_session_update(service, packet):
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

    assert service._test_realtime.events == [
        ("session.update", call_id),
        ("greeting", call_id),
    ]
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.GREETING_STARTED.value
    assert call["greeting_sent"] == 1


@pytest.mark.asyncio
async def test_conference_start_can_satisfy_callee_ready_gate(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, sideband_open=1)
    await service.handle_amd(call_id, "human")
    await service.handle_conference_event(call_id, {"StatusCallbackEvent": "conference-start"})
    call = await service.db.get_call(call_id)
    assert call["callee_joined"] == 1
    assert call["state"] == CallState.GREETING_STARTED.value


@pytest.mark.asyncio
async def test_session_update_must_echo_both_flags(service, packet):
    call_id = await seed_call(service.db, packet)
    service._test_realtime.update_event["session"]["audio"]["input"]["turn_detection"][
        "interrupt_response"
    ] = False
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)
    await service.handle_amd(call_id, "human")
    assert (await service.db.get_call(call_id))["state"] == CallState.FAILED.value
    assert not [event for event in service._test_realtime.events if event[0] == "greeting"]


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
async def test_tool_output_is_followed_by_observed_continuation(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.GREETING_STARTED)
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
async def test_transfer_tool_returns_output_before_removing_ai(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.GREETING_STARTED)
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
    assert order[:2] == ["tool_output", "remove_participant"]


@pytest.mark.asyncio
@pytest.mark.parametrize("answered_by", ["unknown", "future_new_value"])
async def test_unknown_amd_assumes_human_and_never_hangs_up(service, packet, answered_by):
    call_id = await seed_call(service.db, packet, call_id=f"call_{answered_by}")
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)
    await service.handle_amd(call_id, answered_by)
    call = await service.db.get_call(call_id)
    assert call["answer_handling"] == "assumed_human"
    assert call["state"] == CallState.GREETING_STARTED.value
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
        ("session.update", call_id),
        ("voicemail", call_id),
    ]


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
    assert call["state"] == CallState.GREETING_STARTED.value
    assert service._test_realtime.hangups == []
