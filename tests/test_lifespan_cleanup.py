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

    class ClosingClient:
        async def close(self) -> None:
            attempts.append("openai")

    async def close_database(self) -> None:
        attempts.append("database")

    # Later cleanup steps inherit the same 50ms bound as the hung stop. Stub them
    # so a slow runner cannot turn that single TimeoutError into an ExceptionGroup.
    monkeypatch.setattr("app.main.create_openai_client", lambda _: ClosingClient())
    monkeypatch.setattr("app.main.Database.close", close_database)
    monkeypatch.setattr("app.main.CallService", HangingStopService)
    application = create_app(settings)

    async def run_lifespan():
        async with application.router.lifespan_context(application):
            pass

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(run_lifespan(), timeout=5)

    assert attempts == ["service", "openai", "database"]


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
