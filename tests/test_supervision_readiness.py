"""R05: supervision failures are visible and stop new calls (F05).

The watchdog loop previously had no error handling, and /healthz is a constant, so
a dead or failing supervisor could keep accepting new billable calls. These tests
pin the three behaviours: a failed pass marks the service unhealthy, new calls are
rejected with a stable code while existing call handling is untouched, and the
recovery flip is observable through /readyz without changing /healthz.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import PreparePhoneCallInput


async def _prepare(service, packet):
    return await service.prepare(
        PreparePhoneCallInput(
            context=packet,
            authority_basis="Owner explicitly requested this call",
            requested_by_owner=True,
        )
    )


@pytest.mark.asyncio
async def test_watchdog_failure_marks_not_ready_and_blocks_new_calls(service, packet, monkeypatch):
    original = service.db.list_nonterminal_calls

    async def failing_list():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(service.db, "list_nonterminal_calls", failing_list)
    await service._watchdog_iteration()

    assert service.supervision_ready() is False
    assert service._watchdog_failures == 1

    prepared = await _prepare(service, packet)
    with pytest.raises(ValueError) as exc_info:
        await service.start(
            prepared.plan_id,
            explicit_confirmation=True,
            confirmation_text=prepared.confirmation_summary,
        )
    assert json.loads(str(exc_info.value))["code"] == "supervision_unavailable"
    assert service.twilio.agent_creates == 0
    # The rejected plan is not consumed and can be started after recovery.
    assert (await service.db.get_plan(prepared.plan_id))["state"] == "prepared"

    monkeypatch.setattr(service.db, "list_nonterminal_calls", original)
    await service._watchdog_iteration()

    assert service.supervision_ready() is True
    started = await service.start(
        prepared.plan_id,
        explicit_confirmation=True,
        confirmation_text=prepared.confirmation_summary,
    )
    assert service.twilio.agent_creates == 1
    assert started.call_id


@pytest.mark.asyncio
async def test_start_watchdog_is_idempotent(service):
    await service.start_watchdog()
    first = service._watchdog_task
    assert first is not None

    await service.start_watchdog()

    assert service._watchdog_task is first


@pytest.mark.asyncio
async def test_unexpected_watchdog_exit_marks_not_ready(service):
    async def boom() -> None:
        raise RuntimeError("watchdog crashed")

    task = asyncio.create_task(boom())
    await asyncio.gather(task, return_exceptions=True)

    service._watchdog_finished(task)

    assert service.supervision_ready() is False


def test_readyz_separates_liveness_from_readiness(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").json() == {"status": "ready"}
        assert client.get("/readyz").status_code == 200

        app.state.call_service._supervision_healthy = False
        assert client.get("/healthz").status_code == 200
        unready = client.get("/readyz")
        assert unready.status_code == 503
        assert unready.json() == {"status": "not_ready"}

        app.state.call_service._supervision_healthy = True
        assert client.get("/readyz").status_code == 200
