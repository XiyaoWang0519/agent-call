from __future__ import annotations

import asyncio

import pytest

from app.models import CallState
from tests.conftest import seed_call, wait_background


async def start_goodbye(service, call_id, suffix="first"):
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.function_call_arguments.done",
            "call_id": f"tool_{suffix}",
            "name": "end_call",
            "arguments": '{"reason":"objective_completed"}',
        },
    )
    await service.handle_realtime_event(
        call_id,
        {"type": "response.created", "response": {"id": f"goodbye_{suffix}"}},
    )


async def finish_generation(service, call_id, suffix="first"):
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.done",
            "response": {"id": f"goodbye_{suffix}", "status": "completed"},
        },
    )


async def finish_playback(service, call_id, suffix="first"):
    await service.handle_realtime_event(
        call_id,
        {"type": "output_audio_buffer.stopped", "response_id": f"goodbye_{suffix}"},
    )


async def test_reply_window_starts_after_playback_and_silent_callee_ends(
    service, packet, monkeypatch
):
    monkeypatch.setattr("app.call_state.VOICE_END_REPLY_GRACE_SECONDS", 0.15)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await start_goodbye(service, call_id)
    result = service._test_realtime.tool_results[-1][2]
    assert result["status"] == "closing_pending"
    assert result["call_connected"] is True
    await finish_generation(service, call_id)
    await asyncio.sleep(0.17)
    assert service._test_realtime.hangups == []
    await finish_playback(service, call_id)
    await asyncio.sleep(0.04)
    assert service._test_realtime.hangups == []
    assert (await service.db.get_call(call_id))["state"] == CallState.ACTIVE.value
    await asyncio.sleep(0.17)
    await wait_background()
    assert (await service.db.get_call(call_id))["termination_reason"] == "voice_model_end_call"
    assert service._test_realtime.hangups == ["rtc_test"]


@pytest.mark.parametrize("stage", ["generating", "playing", "waiting"])
async def test_reply_received_before_dispatch_cancels_every_closing_stage(
    service, packet, monkeypatch, stage
):
    monkeypatch.setattr("app.call_state.VOICE_END_REPLY_GRACE_SECONDS", 0.03)
    monkeypatch.setattr("app.call_state.TERMINATION_AUDIO_DRAIN_TIMEOUT_SECONDS", 0.04)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await start_goodbye(service, call_id)
    if stage != "generating":
        await finish_generation(service, call_id)
    if stage == "waiting":
        await finish_playback(service, call_id)

    # This is the receiver callback. The dispatcher may still be blocked on SQLite.
    service._observe_opening_event(call_id, {"type": "input_audio_buffer.speech_started"})
    if stage == "generating":
        await finish_generation(service, call_id)
    await finish_playback(service, call_id)
    await wait_background()
    assert (await service.db.get_call(call_id))["state"] == CallState.ACTIVE.value
    assert service._test_realtime.hangups == []
    assert call_id not in service._voice_end_pending
    assert call_id not in service._voice_end_reply_waits
    assert service._test_realtime.resumed_calls == [call_id]
    # More speech during the same resumed conversation sends no duplicate update.
    service._observe_opening_event(call_id, {"type": "input_audio_buffer.speech_started"})
    await wait_background()
    assert service._test_realtime.resumed_calls == [call_id]

    # An actual follow-up answer is ordinary conversation, not a new closing turn.
    await service.handle_realtime_event(
        call_id, {"type": "response.created", "response": {"id": "followup_answer"}}
    )
    await service.handle_realtime_event(
        call_id,
        {"type": "response.done", "response": {"id": "followup_answer", "status": "completed"}},
    )
    await wait_background()
    assert service._test_realtime.hangups == []


async def test_old_reply_timer_cannot_end_a_later_goodbye(service, packet, monkeypatch):
    monkeypatch.setattr("app.call_state.VOICE_END_REPLY_GRACE_SECONDS", 0.03)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await start_goodbye(service, call_id)
    await finish_generation(service, call_id)
    await finish_playback(service, call_id)
    await start_goodbye(service, call_id, "second")
    await wait_background()
    assert service._test_realtime.hangups == []
    await finish_generation(service, call_id, "second")
    await finish_playback(service, call_id, "second")
    await wait_background()
    assert service._test_realtime.hangups == ["rtc_test"]


async def test_callee_hangup_during_reply_window_cleans_up_once(service, packet, monkeypatch):
    monkeypatch.setattr("app.call_state.VOICE_END_REPLY_GRACE_SECONDS", 0.03)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await start_goodbye(service, call_id)
    await finish_generation(service, call_id)
    await finish_playback(service, call_id)
    await service.terminate_call(call_id, "callee_participant_leave")
    await wait_background()
    assert service._test_realtime.hangups == ["rtc_test"]
    assert call_id not in service._voice_end_reply_waits
    assert (await service.db.get_call(call_id))["termination_reason"] == "callee_participant_leave"
    # Late frames from a draining sideband must not recreate completed-call state.
    service._observe_opening_event(call_id, {"type": "input_audio_buffer.speech_started"})
    assert call_id not in service._callee_speech_epochs
    assert call_id not in service._callee_speaking
    assert service._test_realtime.resumed_calls == []


@pytest.mark.parametrize("still_speaking", [True, False])
async def test_followup_received_before_queued_end_tool_prevents_forced_goodbye(
    service, packet, still_speaking
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    tool = {
        "type": "response.function_call_arguments.done",
        "call_id": "stale_end",
        "name": "end_call",
        "arguments": '{"reason":"objective_completed"}',
    }
    service._observe_opening_event(call_id, tool)
    service._observe_opening_event(call_id, {"type": "input_audio_buffer.speech_started"})
    if not still_speaking:
        service._observe_opening_event(call_id, {"type": "input_audio_buffer.speech_stopped"})
    # Dispatch happens after both frames arrived; do not observe the tool a second time.
    await service.handle_realtime_event(call_id, tool, _observed=True)
    assert service._test_realtime.tool_results[-1][2] == {
        "accepted": False,
        "error": "callee_resumed",
    }
    assert service._test_realtime.tool_result_continuations[-1] is False
    assert call_id not in service._voice_end_pending


async def test_speech_during_end_tool_bookkeeping_suppresses_goodbye(service, packet, monkeypatch):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_cancellation(_call_id):
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(service.db, "cancel_pending_questions", slow_cancellation)
    tool_task = asyncio.create_task(start_goodbye(service, call_id))
    await asyncio.wait_for(entered.wait(), timeout=1)
    service._observe_opening_event(call_id, {"type": "input_audio_buffer.speech_started"})
    release.set()
    await tool_task
    assert service._test_realtime.tool_result_continuations[-1] is False
    assert service._test_realtime.tool_results[-1][2]["error"] == "callee_resumed"
    assert call_id not in service._voice_end_pending


async def test_resumed_call_can_close_again_after_followup(service, packet, monkeypatch):
    monkeypatch.setattr("app.call_state.VOICE_END_REPLY_GRACE_SECONDS", 0.03)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await start_goodbye(service, call_id)
    await finish_generation(service, call_id)
    await finish_playback(service, call_id)
    service._observe_opening_event(call_id, {"type": "input_audio_buffer.speech_started"})
    await wait_background()
    assert service._test_realtime.resumed_calls == [call_id]
    assert service._test_realtime.hangups == []
    service._observe_opening_event(call_id, {"type": "input_audio_buffer.speech_stopped"})
    await start_goodbye(service, call_id, "second")
    await finish_generation(service, call_id, "second")
    await finish_playback(service, call_id, "second")
    await wait_background()
    assert service._test_realtime.hangups == ["rtc_test"]


async def test_cancelled_close_does_not_notify_after_call_ends(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await start_goodbye(service, call_id)
    await service.terminate_call(call_id, "callee_participant_leave")
    await service._notify_call_resumed(call_id)
    assert service._test_realtime.resumed_calls == []


@pytest.mark.parametrize("order", ["tool_first", "generation_first", "playback_first"])
async def test_spoken_goodbye_ends_without_another_model_response(
    service, packet, monkeypatch, order
):
    monkeypatch.setattr("app.call_state.VOICE_END_REPLY_GRACE_SECONDS", 0.03)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service.handle_realtime_event(
        call_id, {"type": "response.created", "response": {"id": "goodbye_spoken"}}
    )
    await service.handle_realtime_event(
        call_id, {"type": "output_audio_buffer.started", "response_id": "goodbye_spoken"}
    )
    if order != "tool_first":
        await finish_generation(service, call_id, "spoken")
    if order == "playback_first":
        await finish_playback(service, call_id, "spoken")
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.function_call_arguments.done",
            "response_id": "goodbye_spoken",
            "call_id": "spoken_end",
            "name": "finish_call_after_goodbye",
            "arguments": '{"reason":"objective_completed"}',
        },
    )
    assert service._test_realtime.tool_result_continuations[-1] is False
    assert service._test_realtime.hangups == []
    if order == "tool_first":
        await finish_generation(service, call_id, "spoken")
    if order != "playback_first":
        await finish_playback(service, call_id, "spoken")
    await wait_background()
    assert service._test_realtime.hangups == ["rtc_test"]


async def test_speech_after_goodbye_before_end_tool_rejects_stale_close(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service.handle_realtime_event(
        call_id, {"type": "response.created", "response": {"id": "goodbye_spoken"}}
    )
    await service.handle_realtime_event(
        call_id, {"type": "output_audio_buffer.started", "response_id": "goodbye_spoken"}
    )
    await finish_generation(service, call_id, "spoken")
    await finish_playback(service, call_id, "spoken")
    service._observe_opening_event(call_id, {"type": "input_audio_buffer.speech_started"})
    service._observe_opening_event(call_id, {"type": "input_audio_buffer.speech_stopped"})
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.function_call_arguments.done",
            "response_id": "goodbye_spoken",
            "call_id": "stale_spoken_end",
            "name": "finish_call_after_goodbye",
            "arguments": '{"reason":"objective_completed"}',
        },
    )
    assert service._test_realtime.tool_results[-1][2]["error"] == "callee_resumed"
    assert service._test_realtime.tool_result_continuations[-1] is False
    assert service._test_realtime.hangups == []


async def test_late_end_tool_does_not_restart_reply_window(service, packet, monkeypatch):
    monkeypatch.setattr("app.call_state.VOICE_END_REPLY_GRACE_SECONDS", 0.5)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service.handle_realtime_event(
        call_id, {"type": "response.created", "response": {"id": "goodbye_spoken"}}
    )
    await service.handle_realtime_event(
        call_id, {"type": "output_audio_buffer.started", "response_id": "goodbye_spoken"}
    )
    await finish_generation(service, call_id, "spoken")
    await finish_playback(service, call_id, "spoken")
    # A delayed dispatcher must use the receiver's original playback receipt.
    service._voice_response_audio[call_id]["goodbye_spoken"].stopped_at -= 1
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.function_call_arguments.done",
            "response_id": "goodbye_spoken",
            "call_id": "late_spoken_end",
            "name": "finish_call_after_goodbye",
            "arguments": '{"reason":"objective_completed"}',
        },
    )
    await wait_background()
    assert service._test_realtime.hangups == ["rtc_test"]


@pytest.mark.parametrize("part_type", ["audio", "output_audio"])
async def test_audio_content_part_marks_sip_goodbye_before_playback(service, packet, part_type):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.content_part.added",
            "response_id": "spoken",
            "part": {"type": part_type},
        },
    )
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.function_call_arguments.done",
            "response_id": "spoken",
            "call_id": "end_spoken",
            "name": "finish_call_after_goodbye",
            "arguments": '{"reason":"objective_completed"}',
        },
    )
    assert service._test_realtime.tool_result_continuations[-1] is False
    assert service._voice_end_pending[call_id] == ("end_spoken", "spoken")
    assert service._test_realtime.hangups == []


async def spoken_farewell(service, call_id):
    await service.handle_realtime_event(
        call_id, {"type": "response.created", "response": {"id": "goodbye_spoken"}}
    )
    await service.handle_realtime_event(
        call_id, {"type": "output_audio_buffer.started", "response_id": "goodbye_spoken"}
    )
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.done",
            "response": {
                "id": "goodbye_spoken",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_audio", "transcript": "Thanks, goodbye!"}],
                    }
                ],
            },
        },
    )
    await asyncio.sleep(0)
    assert service._test_realtime.closing_checks == [(call_id, "goodbye_spoken")]


def classification_event(text="FINISHED", status="completed", event_type="response.done"):
    return {
        "type": event_type,
        "response": {
            "id": "silent_check",
            "status": status,
            "metadata": {
                "agent_call_purpose": "closing_check",
                "spoken_response_id": "goodbye_spoken",
            },
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        },
    }


async def test_silent_check_reuses_farewell_and_preserves_audio_owner(service, packet, monkeypatch):
    monkeypatch.setattr("app.call_state.VOICE_END_REPLY_GRACE_SECONDS", 0.03)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    service._activity.active_response_ids[call_id] = "next_audible_response"
    await service.handle_realtime_event(
        call_id, classification_event(event_type="response.created")
    )
    assert service._activity.active_response_ids[call_id] == "next_audible_response"
    await service.handle_realtime_event(call_id, classification_event())
    assert service._test_realtime.tool_results == []
    assert service._test_realtime.hangups == []
    assert service._audio_drain_terminations[call_id] == ("goodbye_spoken", "voice_model_end_call")
    await finish_playback(service, call_id, "spoken")
    await wait_background()
    assert service._test_realtime.hangups == ["rtc_test"]


@pytest.mark.parametrize(
    "text,status",
    [
        ("CONTINUE", "completed"),
        ("FINISHED", "incomplete"),
        ("FINISHED", "failed"),
        ("Probably FINISHED", "completed"),
    ],
)
async def test_silent_check_cannot_close_without_exact_completed_decision(
    service, packet, text, status
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    await service.handle_realtime_event(call_id, classification_event(text, status))
    assert call_id not in service._audio_drain_terminations
    assert service._test_realtime.hangups == []


@pytest.mark.parametrize("intervening", ["callee_speech", "cleared", "new_response"])
async def test_silent_check_cannot_close_after_conversation_changes(service, packet, intervening):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    if intervening == "callee_speech":
        await service.handle_realtime_event(call_id, {"type": "input_audio_buffer.speech_started"})
        await service.handle_realtime_event(call_id, {"type": "input_audio_buffer.speech_stopped"})
    elif intervening == "cleared":
        await service.handle_realtime_event(
            call_id, {"type": "output_audio_buffer.cleared", "response_id": "goodbye_spoken"}
        )
    else:
        await service.handle_realtime_event(
            call_id, {"type": "response.created", "response": {"id": "new_audio"}}
        )
    await service.handle_realtime_event(call_id, classification_event())
    assert call_id not in service._audio_drain_terminations
    assert service._test_realtime.hangups == []


async def test_delayed_silent_check_uses_original_playback_time(service, packet, monkeypatch):
    monkeypatch.setattr("app.call_state.VOICE_END_REPLY_GRACE_SECONDS", 0.5)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    await finish_playback(service, call_id, "spoken")
    service._voice_response_audio[call_id]["goodbye_spoken"].stopped_at -= 1
    await service.handle_realtime_event(call_id, classification_event())
    await wait_background()
    assert service._test_realtime.hangups == ["rtc_test"]


async def test_silent_check_error_does_not_hang_up_call(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service.handle_realtime_event(
        call_id,
        {
            "type": "error",
            "error": {"event_id": "closing_check_goodbye", "code": "invalid_request_error"},
        },
    )
    await asyncio.sleep(0)
    assert service._test_realtime.hangups == []


async def test_silent_check_usage_is_counted_once_without_voice_activity(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    event = classification_event("CONTINUE")
    event["response"]["usage"] = {
        "input_token_details": {"text_tokens": 20, "audio_tokens": 10},
        "output_token_details": {"text_tokens": 3, "audio_tokens": 0},
    }
    await service.handle_realtime_event(call_id, event)
    await service.handle_realtime_event(call_id, event)
    call = await service.db.get_call(call_id)
    assert call["realtime_input_text_tokens"] == 20
    assert call["realtime_input_audio_tokens"] == 10
    assert call["realtime_output_text_tokens"] == 3
    assert call_id not in service._activity.active_response_ids


async def test_speech_during_classified_close_persistence_prevents_hangup(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    original = service.db.cancel_pending_questions

    async def cancel_with_speech(cid):
        result = await original(cid)
        service._observe_opening_event(cid, {"type": "input_audio_buffer.speech_started"})
        return result

    monkeypatch.setattr(service.db, "cancel_pending_questions", cancel_with_speech)
    await service.handle_realtime_event(call_id, classification_event())
    assert call_id not in service._audio_drain_terminations
    assert service._test_realtime.hangups == []
