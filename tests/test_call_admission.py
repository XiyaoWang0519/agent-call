"""R03: single-call admission and termination-aware callee-dial claim.

The review found that single-use plan consumption did not bound *different* plans
to one live call (F02), and that the ``callee_dialed`` flag was toggled in a
separate read-then-CAS with no lifecycle predicate (F03). These tests pin the
replacement behaviour: capacity is decided inside the plan-consumption
transaction, and the dial claim itself refuses a call that termination already
owns.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.models import CallState, PreparePhoneCallInput
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


async def _start_error(service, prepared) -> dict:
    with pytest.raises(ValueError) as exc_info:
        await _start(service, prepared)
    return json.loads(str(exc_info.value))


@pytest.mark.asyncio
async def test_second_plan_is_rejected_while_first_call_is_live(service, packet):
    first = await _prepare(service, packet)
    second = await _prepare(service, packet)
    await _start(service, first)

    payload = await _start_error(service, second)

    assert payload["code"] == "call_busy"
    assert service.twilio.agent_creates == 1
    # The rejected plan was not burned and can be started once capacity frees up.
    assert (await service.db.get_plan(second.plan_id))["state"] == "prepared"


@pytest.mark.asyncio
async def test_concurrent_starts_admit_exactly_one_call(service, packet):
    first = await _prepare(service, packet)
    second = await _prepare(service, packet)

    results = await asyncio.gather(
        _start(service, first),
        _start(service, second),
        return_exceptions=True,
    )

    winners = [result for result in results if not isinstance(result, BaseException)]
    losers = [result for result in results if isinstance(result, ValueError)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert json.loads(str(losers[0]))["code"] == "call_busy"
    assert service.twilio.agent_creates == 1
    nonterminal = await service.db.list_nonterminal_calls()
    assert len(nonterminal) == 1


@pytest.mark.asyncio
async def test_terminating_call_reports_call_ending(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, state=CallState.TERMINATING.value)
    prepared = await _prepare(service, packet)

    payload = await _start_error(service, prepared)

    assert payload["code"] == "call_ending"
    assert (await service.db.get_plan(prepared.plan_id))["state"] == "prepared"


@pytest.mark.asyncio
async def test_plan_can_start_after_prior_call_is_terminal(service, packet):
    first = await _prepare(service, packet)
    first_started = await _start(service, first)
    await service.terminate_call(first_started.call_id, "callee_call_completed")
    await wait_background()

    second = await _prepare(service, packet)
    started = await _start(service, second)

    assert started.state is CallState.PREWARMING
    assert service.twilio.agent_creates == 2


@pytest.mark.asyncio
async def test_claim_callee_dial_denied_once_termination_claimed(service, packet):
    call_id = await seed_call(service.db, packet)

    assert await service.db.claim_callee_dial(call_id) is True
    # One-shot: a duplicate sideband open cannot dial twice.
    assert await service.db.claim_callee_dial(call_id) is False

    other = await seed_call(
        service.db, packet, call_id="call_terminating", openai_call_id="rtc_terminating"
    )
    await service.db.update_call(other, state=CallState.TERMINATING.value, termination_claimed=1)
    assert await service.db.claim_callee_dial(other) is False


@pytest.mark.asyncio
async def test_sideband_open_after_termination_claim_does_not_dial(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, state=CallState.TERMINATING.value, termination_claimed=1)

    await service.handle_sideband_open(call_id)

    assert service.twilio.callee_creates == 0
