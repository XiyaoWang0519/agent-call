from __future__ import annotations

import asyncio

import pytest

from app.main import create_app


@pytest.mark.asyncio
async def test_app_lifespan_cleanup_step_failure_propagates_as_single_error(settings, monkeypatch):
    attempts: list[str] = []

    class FailingStopService:
        def __init__(self, *args, **kwargs):
            pass

        async def recover_startup(self) -> None:
            pass

        async def start_watchdog(self) -> None:
            pass

        async def stop(self) -> None:
            attempts.append("service")
            raise RuntimeError("stop failed")

    monkeypatch.setattr("app.main.CallService", FailingStopService)
    application = create_app(settings)

    with pytest.raises(RuntimeError, match="stop failed"):
        async with application.router.lifespan_context(application):
            pass

    assert attempts == ["service"]


@pytest.mark.asyncio
async def test_app_lifespan_bounds_a_hung_cleanup_step(settings, monkeypatch):
    monkeypatch.setattr("app.main.LIFESPAN_CLEANUP_STEP_TIMEOUT_SECONDS", 0.05)
    attempts: list[str] = []

    class HangingStopService:
        def __init__(self, *args, **kwargs):
            pass

        async def recover_startup(self) -> None:
            pass

        async def start_watchdog(self) -> None:
            pass

        async def stop(self) -> None:
            attempts.append("service")
            await asyncio.Event().wait()

    monkeypatch.setattr("app.main.CallService", HangingStopService)
    application = create_app(settings)

    async def run_lifespan():
        async with application.router.lifespan_context(application):
            pass

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(run_lifespan(), timeout=5)

    assert attempts == ["service"]


@pytest.mark.asyncio
async def test_app_lifespan_cancellation_from_cleanup_step_propagates_and_runs_remaining_steps(
    settings, monkeypatch
):
    attempts: list[str] = []

    class CancelledStopService:
        def __init__(self, *args, **kwargs):
            pass

        async def recover_startup(self) -> None:
            pass

        async def start_watchdog(self) -> None:
            pass

        async def stop(self) -> None:
            attempts.append("service")
            raise asyncio.CancelledError

    class ClosingClient:
        async def close(self) -> None:
            attempts.append("openai")

    monkeypatch.setattr("app.main.create_openai_client", lambda _: ClosingClient())
    monkeypatch.setattr("app.main.CallService", CancelledStopService)
    application = create_app(settings)

    with pytest.raises(asyncio.CancelledError):
        async with application.router.lifespan_context(application):
            pass

    # Cancellation propagates as CancelledError alone (never wrapped in a
    # BaseExceptionGroup) but the remaining, now individually bounded, cleanup
    # steps still ran.
    assert attempts == ["service", "openai"]


@pytest.mark.asyncio
async def test_app_lifespan_cancellation_during_body_propagates_and_runs_cleanup(
    settings, monkeypatch
):
    attempts: list[str] = []

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        async def recover_startup(self) -> None:
            raise asyncio.CancelledError

        async def start_watchdog(self) -> None:
            pass

        async def stop(self) -> None:
            attempts.append("service")

    class ClosingClient:
        async def close(self) -> None:
            attempts.append("openai")

    monkeypatch.setattr("app.main.create_openai_client", lambda _: ClosingClient())
    monkeypatch.setattr("app.main.CallService", Service)
    application = create_app(settings)

    with pytest.raises(asyncio.CancelledError):
        async with application.router.lifespan_context(application):
            pass

    assert attempts == ["service", "openai"]


@pytest.mark.asyncio
async def test_app_lifespan_closes_twilio_bridge_and_poke_http_client(settings, monkeypatch):
    attempts: list[str] = []

    class FakeTwilioBridge:
        async def close(self) -> None:
            attempts.append("twilio")

    class Service:
        def __init__(self, *args, **kwargs):
            self.twilio = FakeTwilioBridge()

        async def recover_startup(self) -> None:
            pass

        async def start_watchdog(self) -> None:
            pass

        async def stop(self) -> None:
            attempts.append("service")

    async def fake_close_poke_http_client() -> None:
        attempts.append("poke")

    monkeypatch.setattr("app.main.CallService", Service)
    monkeypatch.setattr("app.main.close_poke_http_client", fake_close_poke_http_client)
    application = create_app(settings)

    async with application.router.lifespan_context(application):
        pass

    # The Twilio bridge's executor is only torn down once service.stop() (which
    # may still issue Twilio calls, e.g. conference teardown) has completed.
    assert attempts == ["service", "twilio", "poke"]


@pytest.mark.asyncio
async def test_app_lifespan_tolerates_a_call_service_without_a_twilio_attribute(
    settings, monkeypatch
):
    # Several tests substitute minimal CallService fakes with no `.twilio`
    # attribute; the lifespan's twilio-bridge close step must not blow up on
    # those fakes.
    class MinimalService:
        def __init__(self, *args, **kwargs):
            pass

        async def recover_startup(self) -> None:
            pass

        async def start_watchdog(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("app.main.CallService", MinimalService)
    application = create_app(settings)

    async with application.router.lifespan_context(application):
        pass
