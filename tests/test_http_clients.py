from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.call_state import CallService
from app.db import Database
from app.exa_search import ExaSearchClient
from app.main import create_app
from app.openai_client import create_openai_client
from app.openai_realtime import RealtimeBridge
from app.settings import Settings
from app.twilio_bridge import TwilioBridge


def _leaf_exceptions(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in _leaf_exceptions(child)]
    return [error]


def test_default_twilio_client_uses_bounded_pooled_transport(settings, monkeypatch):
    observed: dict[str, object] = {}
    transport = object()

    def fake_http_client(**kwargs):
        observed["transport_options"] = kwargs
        return transport

    def fake_client(account_sid, auth_token, **kwargs):
        observed["credentials"] = (account_sid, auth_token)
        observed["client_options"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr("app.twilio_bridge.TwilioHttpClient", fake_http_client)
    monkeypatch.setattr("app.twilio_bridge.Client", fake_client)

    TwilioBridge(settings)

    assert observed["transport_options"] == {
        "pool_connections": True,
        "timeout": 10.0,
        "max_retries": 0,
    }
    assert observed["client_options"] == {"http_client": transport}


def test_twilio_http_timeout_is_configurable(settings, monkeypatch):
    settings.twilio_http_timeout_seconds = 12
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        "app.twilio_bridge.TwilioHttpClient",
        lambda **kwargs: observed.update(kwargs) or object(),
    )
    monkeypatch.setattr("app.twilio_bridge.Client", lambda *args, **kwargs: SimpleNamespace())

    TwilioBridge(settings)

    assert observed["timeout"] == 12


def test_openai_client_has_bounded_timeouts_and_no_sdk_retries(settings, monkeypatch):
    observed: dict[str, object] = {}
    transport = object()

    def fake_http_client(**kwargs):
        observed["transport_options"] = kwargs
        return transport

    def fake_openai(**kwargs):
        observed["client_options"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr("app.openai_client.httpx.AsyncClient", fake_http_client)
    monkeypatch.setattr("app.openai_client.AsyncOpenAI", fake_openai)

    create_openai_client(settings)

    client_options = observed["client_options"]
    timeout = client_options["timeout"]
    assert timeout.connect == 3.0
    assert timeout.read == 10.0
    assert timeout.write == 10.0
    assert timeout.pool == 10.0
    assert client_options["max_retries"] == 0
    assert client_options["http_client"] is transport
    assert observed["transport_options"]["limits"].keepalive_expiry == 60


def test_openai_control_timeouts_are_configurable(settings, monkeypatch):
    values = settings.model_dump()
    values["openai_connect_timeout_seconds"] = 4
    values["openai_http_timeout_seconds"] = 20
    configured = Settings(**values)
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        "app.openai_client.httpx.AsyncClient",
        lambda **kwargs: observed.update(transport_options=kwargs) or object(),
    )
    monkeypatch.setattr(
        "app.openai_client.AsyncOpenAI",
        lambda **kwargs: observed.update(kwargs) or SimpleNamespace(),
    )

    create_openai_client(configured)

    timeout = observed["timeout"]
    assert timeout.connect == 4
    assert timeout.read == 20


def test_openai_keepalive_expiry_is_configurable(settings, monkeypatch):
    values = settings.model_dump()
    values["openai_keepalive_expiry_seconds"] = 90
    configured = Settings(**values)
    observed: dict[str, object] = {}
    transport = object()

    def fake_http_client(**kwargs):
        observed["transport_options"] = kwargs
        return transport

    def fake_openai(**kwargs):
        observed["client_options"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr("app.openai_client.httpx.AsyncClient", fake_http_client)
    monkeypatch.setattr("app.openai_client.AsyncOpenAI", fake_openai)

    create_openai_client(configured)

    transport_options = observed["transport_options"]
    assert transport_options["limits"].keepalive_expiry == 90
    assert transport_options["limits"].max_connections == 1000
    assert transport_options["limits"].max_keepalive_connections == 100
    assert transport_options["follow_redirects"] is True
    assert observed["client_options"]["http_client"] is transport


def test_exa_client_has_bounded_pooled_transport(settings, monkeypatch):
    observed: dict[str, object] = {}
    transport = SimpleNamespace()

    def fake_http_client(**kwargs):
        observed.update(kwargs)
        return transport

    monkeypatch.setattr("app.exa_search.httpx.AsyncClient", fake_http_client)

    ExaSearchClient(settings)

    timeout = observed["timeout"]
    assert timeout.connect == 1.0
    assert timeout.read == 3.0
    assert timeout.write == 3.0
    assert timeout.pool == 3.0
    assert observed["limits"].max_connections == 20
    assert observed["limits"].max_keepalive_connections == 10
    assert observed["limits"].keepalive_expiry == 60
    assert observed["follow_redirects"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("openai_connect_timeout_seconds", 0),
        ("openai_http_timeout_seconds", 61),
        ("openai_keepalive_expiry_seconds", 4),
        ("openai_extraction_timeout_seconds", 121),
    ],
)
def test_openai_transport_settings_are_bounded(settings, field, value):
    values = settings.model_dump()
    values[field] = value
    with pytest.raises(ValueError):
        Settings(**values)


@pytest.mark.parametrize("value", [0, 11])
def test_exa_search_timeout_is_bounded(settings, value):
    values = settings.model_dump()
    values["exa_search_timeout_seconds"] = value
    with pytest.raises(ValueError):
        Settings(**values)


def test_openai_connect_timeout_cannot_exceed_request_timeout(settings):
    values = settings.model_dump()
    values["openai_connect_timeout_seconds"] = 11
    values["openai_http_timeout_seconds"] = 10
    with pytest.raises(ValueError, match="cannot exceed"):
        Settings(**values)


@pytest.mark.asyncio
async def test_realtime_accept_has_a_total_wall_clock_deadline(settings, packet):
    settings.openai_connect_timeout_seconds = 0.005
    settings.openai_http_timeout_seconds = 0.01

    async def hanging_accept(*args, **kwargs):
        await asyncio.Event().wait()

    client = SimpleNamespace(
        realtime=SimpleNamespace(
            calls=SimpleNamespace(
                with_raw_response=SimpleNamespace(accept=hanging_accept),
            )
        )
    )
    bridge = RealtimeBridge(
        settings,
        client,
        on_event=lambda *args: None,
        on_open=lambda *args: None,
        on_fatal=lambda *args: None,
    )

    with pytest.raises(TimeoutError):
        await bridge.accept_and_connect(
            call_id="call_1",
            openai_call_id="rtc_1",
            packet=packet,
        )


@pytest.mark.asyncio
async def test_call_service_closes_only_internally_created_api_clients(settings, monkeypatch):
    owned = SimpleNamespace(closed=False)
    owned_exa = SimpleNamespace(closed=False)

    async def close() -> None:
        owned.closed = True

    owned.close = close

    async def close_exa() -> None:
        owned_exa.closed = True

    owned_exa.close = close_exa
    monkeypatch.setattr("app.call_state.create_openai_client", lambda _: owned)
    monkeypatch.setattr("app.call_state.ExaSearchClient", lambda _: owned_exa)
    db = Database(settings.database_path)

    service = CallService(settings, db, twilio=SimpleNamespace())
    await service.stop()
    assert owned.closed is True
    assert owned_exa.closed is True

    injected = SimpleNamespace(closed=False)

    async def close_injected() -> None:
        injected.closed = True

    injected.close = close_injected
    injected_exa = SimpleNamespace(closed=False)

    async def close_injected_exa() -> None:
        injected_exa.closed = True

    injected_exa.close = close_injected_exa
    service = CallService(
        settings,
        db,
        twilio=SimpleNamespace(),
        openai=injected,
        exa=injected_exa,
    )
    await service.stop()
    assert injected.closed is False
    assert injected_exa.closed is False


@pytest.mark.asyncio
async def test_app_lifespan_closes_openai_when_service_shutdown_fails(settings, monkeypatch):
    client = SimpleNamespace(closed=False)
    captured = {}

    async def close() -> None:
        client.closed = True

    client.close = close

    class FailingStopService:
        def __init__(self, *args, **kwargs):
            captured["db"] = args[1]

        async def recover_startup(self) -> None:
            pass

        async def start_watchdog(self) -> None:
            pass

        async def stop(self) -> None:
            raise RuntimeError("stop failed")

    monkeypatch.setattr("app.main.create_openai_client", lambda _: client)
    monkeypatch.setattr("app.main.CallService", FailingStopService)
    application = create_app(settings)

    with pytest.raises(RuntimeError, match="stop failed"):
        async with application.router.lifespan_context(application):
            pass

    assert client.closed is True
    assert captured["db"]._writer is None
    assert captured["db"]._reader is None


@pytest.mark.asyncio
async def test_app_lifespan_closes_database_when_openai_shutdown_fails(settings, monkeypatch):
    captured = {}

    class FailingCloseClient:
        async def close(self) -> None:
            raise RuntimeError("openai close failed")

    class Service:
        def __init__(self, *args, **kwargs):
            captured["db"] = args[1]

        async def recover_startup(self) -> None:
            pass

        async def start_watchdog(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("app.main.create_openai_client", lambda _: FailingCloseClient())
    monkeypatch.setattr("app.main.CallService", Service)
    application = create_app(settings)

    with pytest.raises(RuntimeError, match="openai close failed"):
        async with application.router.lifespan_context(application):
            pass

    assert captured["db"]._writer is None
    assert captured["db"]._reader is None


@pytest.mark.asyncio
async def test_app_lifespan_closes_database_when_openai_client_creation_fails(
    settings, monkeypatch
):
    captured = {}

    def create_database(path):
        db = Database(path)
        captured["db"] = db
        return db

    def fail_client_creation(_):
        raise RuntimeError("openai client creation failed")

    monkeypatch.setattr("app.main.Database", create_database)
    monkeypatch.setattr("app.main.create_openai_client", fail_client_creation)
    application = create_app(settings)

    with pytest.raises(RuntimeError, match="openai client creation failed"):
        async with application.router.lifespan_context(application):
            pass

    assert captured["db"]._writer is None
    assert captured["db"]._reader is None


@pytest.mark.asyncio
async def test_app_lifespan_aggregates_all_cleanup_failures(settings, monkeypatch):
    attempts: list[str] = []
    captured = {}
    service_getters = []

    class FailingCloseDatabase(Database):
        async def close(self) -> None:
            attempts.append("database")
            await super().close()
            raise RuntimeError("database close failed")

    class FailingCloseClient:
        async def close(self) -> None:
            attempts.append("openai")
            raise RuntimeError("openai close failed")

    class FailingStopService:
        def __init__(self, *args, **kwargs):
            captured["db"] = args[1]

        async def recover_startup(self) -> None:
            pass

        async def start_watchdog(self) -> None:
            pass

        async def stop(self) -> None:
            attempts.append("service")
            raise RuntimeError("service stop failed")

    monkeypatch.setattr("app.main.Database", FailingCloseDatabase)
    monkeypatch.setattr("app.main.create_openai_client", lambda _: FailingCloseClient())
    monkeypatch.setattr("app.main.CallService", FailingStopService)
    monkeypatch.setattr(
        "app.main.register_tools", lambda _mcp, get_service: service_getters.append(get_service)
    )
    application = create_app(settings)

    with pytest.raises(ExceptionGroup) as raised:
        async with application.router.lifespan_context(application):
            pass

    assert [str(error) for error in raised.value.exceptions] == [
        "service stop failed",
        "openai close failed",
        "database close failed",
    ]
    assert attempts == ["service", "openai", "database"]
    assert captured["db"]._writer is None
    assert captured["db"]._reader is None
    with pytest.raises(RuntimeError, match="service is not started"):
        service_getters[0]()


@pytest.mark.asyncio
async def test_app_lifespan_preserves_body_error_with_cleanup_failures(settings, monkeypatch):
    attempts: list[str] = []

    class FailingCloseDatabase(Database):
        async def close(self) -> None:
            attempts.append("database")
            await super().close()
            raise RuntimeError("database close failed")

    class FailingCloseClient:
        async def close(self) -> None:
            attempts.append("openai")
            raise RuntimeError("openai close failed")

    class FailingStopService:
        def __init__(self, *args, **kwargs):
            pass

        async def recover_startup(self) -> None:
            pass

        async def start_watchdog(self) -> None:
            pass

        async def stop(self) -> None:
            attempts.append("service")
            raise RuntimeError("service stop failed")

    monkeypatch.setattr("app.main.Database", FailingCloseDatabase)
    monkeypatch.setattr("app.main.create_openai_client", lambda _: FailingCloseClient())
    monkeypatch.setattr("app.main.CallService", FailingStopService)
    application = create_app(settings)

    with pytest.raises(ExceptionGroup) as raised:
        async with application.router.lifespan_context(application):
            raise ValueError("lifespan body failed")

    leaves = _leaf_exceptions(raised.value)
    assert [str(error) for error in leaves] == [
        "lifespan body failed",
        "service stop failed",
        "openai close failed",
        "database close failed",
    ]
    assert isinstance(leaves[0], ValueError)
    assert attempts == ["service", "openai", "database"]
