"""R04: a cancelled provider create must not lose its late SID (F04).

Twilio's sync SDK runs in a worker thread. Cancelling the awaiting coroutine does
not cancel the remote create, so treating cancellation as a definite failure (and
retrying, or simply walking away) can orphan a billable leg. These tests pin the
shield-and-compensate behaviour for the agent leg, the callee leg, and the
carrier media stream.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models import CallState, PreparePhoneCallInput
from app.twilio_bridge import ParticipantInfo
from tests.conftest import seed_call, wait_background


async def _prepare(service, packet):
    return await service.prepare(
        PreparePhoneCallInput(
            context=packet,
            authority_basis="Owner explicitly requested this call",
            requested_by_owner=True,
        )
    )


async def _start(service, prepared):
    return await service.start(
        prepared.plan_id,
        explicit_confirmation=True,
        confirmation_text=prepared.confirmation_summary,
    )


@pytest.mark.asyncio
async def test_cancelled_agent_create_persists_late_sid_and_cleans_up(service, packet):
    prepared = await _prepare(service, packet)
    entered = asyncio.Event()
    release = asyncio.Event()
    late = ParticipantInfo("CA" + "z" * 32, "CF" + "z" * 32)

    async def slow_create(**_kwargs):
        entered.set()
        await release.wait()
        return late

    service.twilio.create_agent_participant = slow_create
    task = asyncio.create_task(_start(service, prepared))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_background()

    calls = await service.db.list_calls()
    assert len(calls) == 1
    call = calls[0]
    assert call["twilio_ai_call_sid"] == late.call_sid
    assert call["conference_sid"] == late.conference_sid
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "agent_leg_setup_failed"
    assert service.twilio.completed == [late.conference_sid]


@pytest.mark.asyncio
async def test_cancelled_callee_create_persists_late_sid_and_cleans_up(service, packet):
    call_id = await seed_call(service.db, packet)
    entered = asyncio.Event()
    release = asyncio.Event()
    late = ParticipantInfo("CA" + "y" * 32, "CF" + "y" * 32)

    async def slow_create(**_kwargs):
        entered.set()
        await release.wait()
        return late

    service.twilio.create_callee_participant = slow_create
    task = asyncio.create_task(service.handle_sideband_open(call_id))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_background()

    call = await service.db.get_call(call_id)
    assert call["twilio_callee_call_sid"] == late.call_sid
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "callee_leg_setup_failed"
    assert service.twilio.completed == [late.conference_sid]


@pytest.mark.asyncio
async def test_definite_agent_create_failure_is_not_treated_as_unknown(service, packet):
    prepared = await _prepare(service, packet)

    async def fail(**_kwargs):
        raise RuntimeError("twilio rejected the create")

    service.twilio.create_agent_participant = fail
    with pytest.raises(RuntimeError, match="twilio rejected the create"):
        await _start(service, prepared)
    await wait_background()

    calls = await service.db.list_calls()
    assert len(calls) == 1
    call = calls[0]
    # A definite RPC failure has no remote SID to record and must not be retried
    # as if the result were merely unknown.
    assert call["twilio_ai_call_sid"] is None
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "agent_leg_setup_failed"


@pytest.mark.asyncio
async def test_cancelled_media_stream_create_persists_late_sid(service, packet):
    call_id = await seed_call(service.db, packet)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_stream(**_kwargs):
        entered.set()
        await release.wait()
        return "MZ" + "q" * 32

    service.twilio.start_audio_monitor = slow_stream
    task = asyncio.create_task(service._start_media_monitor(await service.db.get_call(call_id)))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_background()

    call = await service.db.get_call(call_id)
    assert call["media_stream_sid"] == "MZ" + "q" * 32
