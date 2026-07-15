from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app import prompts
from app.models import (
    CONTEXT_PACKET_MAX_BYTES,
    ContextPacket,
    PreparePhoneCallInput,
    SemanticVad,
)
from app.prompts import REALTIME_INSTRUCTIONS_MAX_BYTES, realtime_instructions


def _oversized_packet_data(packet: ContextPacket, value: str) -> dict:
    data = packet.model_dump(mode="json")
    data["relevant_facts"] = [value]
    return data


def test_context_packet_rejects_oversized_ascii(packet: ContextPacket):
    data = _oversized_packet_data(packet, "x" * CONTEXT_PACKET_MAX_BYTES)

    with pytest.raises(ValidationError, match=r"exceeds 16384 UTF-8 bytes"):
        ContextPacket.model_validate(data)


def test_context_packet_counts_multibyte_utf8_bytes(packet: ContextPacket):
    # Fewer than 16,384 characters, but three UTF-8 bytes per character plus JSON overhead.
    value = "界" * (CONTEXT_PACKET_MAX_BYTES // 3)
    assert len(value) < CONTEXT_PACKET_MAX_BYTES

    with pytest.raises(ValidationError, match=r"exceeds 16384 UTF-8 bytes"):
        ContextPacket.model_validate(_oversized_packet_data(packet, value))


def test_normal_context_uses_compact_approved_json(packet: ContextPacket):
    expected = json.dumps(
        packet.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )

    instructions = realtime_instructions(packet)

    assert packet.approved_context_json() == expected
    assert f"# Approved context\n{expected}\n" in instructions
    assert len(instructions.encode("utf-8")) <= REALTIME_INSTRUCTIONS_MAX_BYTES


def test_ending_instructions_gate_on_callee_engagement(packet: ContextPacket):
    """Regression: the model once armed end_call while the callee's request (a joke) was
    still pending, folding the answer into the goodbye. Ending must require both a complete
    objective and a callee with nothing further."""

    flattened = realtime_instructions(packet).replace("\n", " ")

    assert "the callee has nothing further" in flattened
    assert (
        "A pending question or request from the callee means the conversation is not finished"
        in flattened
    )
    assert "answer it fully as a normal turn first" in flattened


def test_realtime_instructions_encode_sassy_personal_assistant_voice(packet: ContextPacket):
    flattened = realtime_instructions(packet).replace("\n", " ")

    assert "# Personality and tone" in realtime_instructions(packet)
    assert "sassy personal assistant" in flattened
    assert "I'd be happy to help" in flattened
    assert "sentence fragments" in flattened
    assert "You have opinions" in flattened
    assert "dry sarcasm" in flattened
    assert "# Preambles" in realtime_instructions(packet)
    assert "Speak naturally, briefly, and professionally" not in flattened


def test_realtime_instructions_enforce_final_byte_limit(
    packet: ContextPacket, monkeypatch: pytest.MonkeyPatch
):
    size_bytes = len(realtime_instructions(packet).encode("utf-8"))
    monkeypatch.setattr(prompts, "REALTIME_INSTRUCTIONS_MAX_BYTES", size_bytes - 1)

    with pytest.raises(ValueError, match=r"Realtime instructions exceed"):
        realtime_instructions(packet)


@pytest.mark.parametrize("eagerness", ["low", "medium", "high", "auto"])
def test_semantic_vad_accepts_supported_eagerness(eagerness: str):
    vad = SemanticVad(
        eagerness=eagerness,
        create_response=True,
        interrupt_response=True,
    )

    assert vad.eagerness == eagerness


def test_semantic_vad_defaults_to_auto_and_rejects_unknown_value():
    vad = SemanticVad(create_response=True, interrupt_response=True)

    assert vad.eagerness == "auto"
    with pytest.raises(ValidationError):
        SemanticVad(
            eagerness="fast",
            create_response=True,
            interrupt_response=True,
        )


@pytest.mark.asyncio
async def test_oversized_context_is_rejected_before_plan_persistence(service, packet):
    raw_request = {
        "context": _oversized_packet_data(packet, "x" * CONTEXT_PACKET_MAX_BYTES),
        "authority_basis": "Owner asked Poke to place this call",
        "requested_by_owner": True,
    }

    async def validate_and_prepare():
        request = PreparePhoneCallInput.model_validate(raw_request)
        return await service.prepare(request)

    with pytest.raises(ValidationError, match=r"exceeds 16384 UTF-8 bytes"):
        await validate_and_prepare()

    row = await service.db.fetch_one("SELECT COUNT(*) AS count FROM plans")
    assert row == {"count": 0}
