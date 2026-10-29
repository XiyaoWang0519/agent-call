from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.openai_live import LiveBridge
from scripts.live_phone.audio import add_speech_noise, rms, speech_bounds, tone
from scripts.live_phone.report import grade
from scripts.live_phone.runner import semantic_grade
from scripts.live_phone.scenarios import SCENARIOS, Step
from scripts.live_phone.session import Session
from tests.test_call_audio import SILENCE
from tests.test_voice_closing import frames, prepared_goodbye, settle


@pytest.mark.parametrize(
    "transcript,overlap,expected",
    [("Stop please", True, 1), ("", True, 0), ("...", True, 0), ("Stop please", False, 0)],
)
async def test_yield_requires_heard_words_and_real_overlap(
    service, packet, transcript, overlap, expected
):
    call_id = await prepared_goodbye(service, packet)
    if overlap:
        frames(service, call_id, track="inbound")
    for _ in range(2):
        service._observe_live_event(
            call_id, {"type": "session.input_transcript.delta", "delta": transcript}
        )
    await settle()
    assert service.live.events.count(("yield_to_callee", call_id)) == expected


async def test_stale_yield_is_discarded(service, packet):
    call_id = await prepared_goodbye(service, packet)
    frames(service, call_id, track="inbound")
    await service._yield_to_interruption(call_id, 0)
    assert ("yield_to_callee", call_id) not in service.live.events


async def test_late_input_transcript_cannot_interrupt_a_new_answer(service, packet):
    call_id = await prepared_goodbye(service, packet)
    audio = service._call_audio[call_id]
    audio.outbound.speaking = False
    frames(service, call_id, track="inbound")
    audio.outbound.speaking = True
    service._observe_live_event(
        call_id, {"type": "session.input_transcript.delta", "delta": "My question"}
    )
    await settle()
    assert ("yield_to_callee", call_id) not in service.live.events


async def test_silence_hangover_cannot_interrupt_a_new_answer(service, packet):
    call_id = await prepared_goodbye(service, packet)
    frames(service, call_id, track="inbound")
    frames(service, call_id, track="inbound", start=40, end=60, payload=SILENCE)
    assert call_id in service._callee_speaking
    service._observe_live_event(
        call_id, {"type": "session.input_transcript.delta", "delta": "My question"}
    )
    await settle()
    assert ("yield_to_callee", call_id) not in service.live.events


@pytest.mark.parametrize("end,expected", [(40, 0), (180, 1)])
async def test_sustained_overlap_yields_silently_but_short_spike_does_not(
    service, packet, end, expected
):
    call_id = await prepared_goodbye(service, packet)
    frames(service, call_id, track="inbound", end=end)
    await asyncio.sleep(0.2)
    assert service.live.events.count(("yield_to_callee", call_id)) == expected
    assert service.live.tool_results == []


async def test_yield_uses_supported_instruction_command(settings):
    bridge = LiveBridge(
        settings, SimpleNamespace(), on_event=AsyncMock(), on_open=AsyncMock(), on_fatal=AsyncMock()
    )
    bridge.send = AsyncMock()
    await bridge.yield_to_callee("call")
    event = bridge.send.call_args.args[1]
    assert event["type"] == "session.instructions.append"
    assert event["delegation_id"] is None
    assert "Stay silent while they speak" in event["content"]


def test_noisy_fixture_preserves_timing_and_silence():
    source = bytes(3200) + tone(0.4) + bytes(3200)
    noisy = add_speech_noise(source, 20)
    assert len(noisy) == len(source)
    assert noisy[:3200] == noisy[-3200:] == bytes(3200)
    assert noisy != source
    assert noisy == add_speech_noise(source, 20)
    assert speech_bounds(source) == pytest.approx((0.2, 0.6))
    assert rms(noisy) > 0


async def test_latency_waits_for_answer_after_question_not_earlier_matching_words(tmp_path):
    session = Session(
        "callee",
        tmp_path,
        {"question": b""},
        {},
        SimpleNamespace(),
        "test",
        AsyncMock(),
        {},
        latency_steps=(0,),
    )
    session.ready.set()

    async def play(*args):
        session.events.append({"type": "source_speech", "end": 10.0})
        session.transcripts.append({"start": 9.0, "end": 9.5, "text": "four"})
        session.voiced.append((11.0, 0.02))
        return 8.0, 10.0

    async def until(predicate, seconds):
        assert not predicate()
        session.transcripts.append({"start": 11.0, "end": 12.0, "text": "four"})
        assert predicate()

    session.play = play
    session.until = until
    await session.execute((Step("say", "question"), Step("expect", "four")))
    assert session.error is None
    assert next(e["seconds"] for e in session.events if e["type"] == "reply_audio_latency") == 1.0


async def test_semantic_grader_receives_rendered_caller_stimuli():
    parse = AsyncMock(
        return_value=SimpleNamespace(
            output_parsed=SimpleNamespace(
                evidence_ids=["callee-0"], model_dump=lambda: {"passed": True}
            )
        )
    )
    session = SimpleNamespace(
        transcripts=[{"text": "violet garden eagle", "start": 1, "end": 2}],
        events=[{"type": "playback_started", "at": 0, "label": "Repeat violet garden eagle"}],
    )
    await semantic_grade(
        SimpleNamespace(responses=SimpleNamespace(parse=parse)),
        SCENARIOS["closing-follow-up"],
        {"callee": session},
    )
    evidence = json.loads(parse.call_args.kwargs["input"])
    assert evidence["sent_stimuli"]["callee"][0]["text"] == "Repeat violet garden eagle"
    assert "{nonce}" in str(evidence["scenario"])


@pytest.mark.parametrize(
    "samples,passes",
    [([0.8] * 8, True), ([1.1] * 8, False), ([0.8] * 7 + [2.1], False), ([0.8] * 7, False)],
)
def test_received_audio_latency_gate_requires_complete_fast_samples(samples, passes):
    evidence = {
        "sessions": {
            "callee": {
                "events": [{"type": "reply_audio_latency", "seconds": value} for value in samples]
            }
        }
    }
    checks = grade(SCENARIOS["approved-facts-clean"], evidence)
    assert all(v for k, v in checks.items() if k.startswith("reply_latency_")) == passes
