from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.db import LatencyMark
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
    assert service._test_live.accepts == [(started.call_id, "rtc_incoming")]
    assert service._test_twilio.callee_creates == 0

    await service.handle_sideband_open(started.call_id)
    assert service._test_twilio.callee_creates == 1
    assert service._test_live.initial_updates == [started.call_id]
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
    assert service._test_live.rejects == ["rtc_unknown"]


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

    assert service._test_live.rejects == [
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
    assert len(service._test_live.accepts) == 1
    assert len(service._test_live.rejects) == 1


@pytest.mark.asyncio
async def test_realtime_error_logs_exclude_raw_provider_events(service, packet, caplog):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    provider_secret = "sk-provider-secret-123456789"

    with caplog.at_level("INFO", logger="app.call_state"):
        await service.handle_live_event(
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
    assert service._test_live.hangups == []


@pytest.mark.parametrize("bad_session", [{}, {"model": "gpt-realtime-2.1"}])
async def test_mismatched_session_fails_before_dialing(service, packet, bad_session):
    call_id = await seed_call(service.db, packet)
    service._test_live.initial_update_event = {"type": "session.updated", "session": bad_session}
    await service.handle_sideband_open(call_id)
    call = await service.db.get_call(call_id)
    assert call["termination_reason"] == "live_session_config_mismatch"
    assert service._test_twilio.callee_creates == 0


async def test_activation_requires_callee_answer_and_monitor_before_unmute(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_sideband_open(call_id)
    await service.handle_conference_event(call_id, {"StatusCallbackEvent": "conference-start"})
    assert not service._test_twilio.unmuted
    assert (await service.db.get_call(call_id))["state"] == "prewarming"
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    call = await service.db.get_call(call_id)
    assert call["state"] == "active"
    assert call["live_session_verified"] == 1
    assert call["media_stream_sid"] == "MZ" + "d" * 32
    assert service._call_audio[call_id].connected
    assert len(service._test_twilio.unmuted) == 1
    assert service._test_live.events == [("session.instructions.append", call_id)]
    # Duplicate callbacks cannot create a second monitor or introduction.
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "in-progress"})
    assert len(service._test_twilio.unmuted) == 1
    assert len(service._test_live.events) == 1


async def test_agent_is_unmuted_before_conversation_is_enabled(service, packet, monkeypatch):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)

    async def enable(candidate):
        assert candidate == call_id
        assert service._test_twilio.unmuted

    monkeypatch.setattr(service.live, "enable_conversation", enable)
    await service._check_activation_gate(call_id)
    assert (await service.db.get_call(call_id))["state"] == "active"


async def test_monitor_failure_prevents_conversation(service, packet, monkeypatch):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)

    async def fail(**kwargs):
        raise RuntimeError("monitor unavailable")

    monkeypatch.setattr(service.twilio, "start_audio_monitor", fail)
    await service._check_activation_gate(call_id)
    assert (await service.db.get_call(call_id))["termination_reason"] == "live_activation_failed"
    assert not service._test_twilio.unmuted
    assert not service._test_live.events


@pytest.mark.parametrize("amd", ["human", "unknown", "machine_start", "machine_end_other"])
async def test_ambiguous_amd_does_not_authorize_voicemail(service, packet, amd):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)
    await service.handle_amd(call_id, amd)
    assert (await service.db.get_call(call_id))["state"] == "active"
    assert ("voicemail", call_id) not in service._test_live.events


@pytest.mark.parametrize("amd", ["machine_end_beep", "machine_end_silence"])
async def test_recording_ready_amd_authorizes_one_voicemail(service, packet, amd):
    call_id = await seed_call(service.db, packet)
    await service.handle_amd(call_id, amd)
    assert not service._test_live.events
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)
    await service._check_activation_gate(call_id)
    await service.handle_amd(call_id, amd)
    assert service._test_live.events.count(("voicemail", call_id)) == 1
    assert (await service.db.get_call(call_id))["voicemail_sent"] == 1


async def test_late_recording_ready_uses_live_instruction_after_active(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, sideband_open=1, callee_joined=1)
    await service._check_activation_gate(call_id)
    await service.handle_amd(call_id, "machine_end_beep")
    assert service._test_live.events[-1] == ("voicemail", call_id)
    assert service._test_live.hangups == []


async def test_native_transcript_fragments_are_persisted_exactly(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    for i, delta in enumerate(["Thanks", ", I", " will call tomorrow."]):
        await service.handle_live_event(
            call_id,
            {
                "type": "session.output_transcript.delta",
                "event_id": f"fragment_{i}",
                "delta": delta,
                "start_ms": i * 100,
                "end_ms": (i + 1) * 100,
            },
        )
    transcript = await service.db.get_transcript(call_id)
    assert "".join(turn.text for turn in transcript) == "Thanks, I will call tomorrow."
    assert service._test_live.hangups == []
