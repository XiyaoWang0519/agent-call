from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastmcp import FastMCP

from app.call_state import CallService
from app.db import Database
from app.grok_oauth.constants import GROK_MCP_PATH
from app.grok_oauth.provider import GrokOAuthProvider
from app.mcp_tools import register_tools
from app.openai_client import create_openai_client
from app.routes import debug, deployment, grok_oauth, openai_webhooks, twilio_webhooks
from app.security import MCPAuthMiddleware, WebhookBodyLimitMiddleware
from app.settings import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)

# Each cleanup step must finish within this bound so a supervisor-issued cancel
# (or a hung client) cannot stall shutdown indefinitely.
LIFESPAN_CLEANUP_STEP_TIMEOUT_SECONDS = 15.0


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    mcp = FastMCP("Agent Phone-Call Bridge")

    def get_service() -> CallService:
        # `app` is assigned below; this closure is only ever invoked once the
        # server is handling requests, by which point it is bound. Reading from
        # app.state keeps MCP tools and HTTP routes on the same access path.
        service = getattr(app.state, "call_service", None)
        if service is None:
            raise RuntimeError("service is not started")
        return service  # type: ignore[no-any-return]

    register_tools(mcp, get_service)
    mcp_http_app = mcp.http_app(
        path="/",
        transport="streamable-http",
        stateless_http=True,
        json_response=True,
    )
    protected_mcp = MCPAuthMiddleware(mcp_http_app, settings)

    grok_oauth_provider: GrokOAuthProvider | None = None
    grok_mcp_app = None
    if settings.grok_mcp_oauth_enabled:
        grok_oauth_provider = GrokOAuthProvider(settings)
        grok_mcp = FastMCP("Agent Phone-Call Bridge", auth=grok_oauth_provider)
        register_tools(grok_mcp, get_service)
        grok_mcp_app = grok_mcp.http_app(
            path=GROK_MCP_PATH,
            transport="streamable-http",
            stateless_http=True,
            json_response=True,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.require_runtime_configuration()
        if not settings.live_calls_enabled:
            logger.info("evaluation profile enabled; live calls are disabled")
        db = Database(settings.database_path)
        await db.initialize()
        openai = None
        service: CallService | None = None
        primary_error: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        try:
            openai = create_openai_client(settings)
            service = CallService(settings, db, openai=openai)
            app.state.call_service = service
            if grok_oauth_provider is not None:
                grok_oauth_provider.attach_database(db)
                await grok_oauth_provider.prepare_storage()
            # Recovery happens before the server accepts traffic.
            await service.recover_startup()
            # A successful restart completes the deployment lease. Failed or canceled
            # deployments are also bounded by the database lock's TTL.
            await db.release_deployment_lock()
            await service.start_watchdog()
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(mcp_http_app.lifespan(app))
                if grok_mcp_app is not None:
                    await stack.enter_async_context(grok_mcp_app.lifespan(app))
                yield
        except BaseException as exc:
            primary_error = exc
        finally:
            cancelled = isinstance(primary_error, asyncio.CancelledError)
            cleanup_steps = []
            if service is not None:
                cleanup_steps.append(service.stop)
            if openai is not None:
                cleanup_steps.append(openai.close)
            cleanup_steps.append(db.close)

            # Cleanup steps are individually time-bounded and cancellation is
            # tracked separately from ordinary failures: cancellation must keep
            # running the remaining (now bounded) steps and ultimately propagate
            # as CancelledError, never get swallowed or wrapped in a group, or an
            # external cancel could not bound how long shutdown takes.
            for cleanup in cleanup_steps:
                try:
                    async with asyncio.timeout(LIFESPAN_CLEANUP_STEP_TIMEOUT_SECONDS):
                        await cleanup()
                except asyncio.CancelledError:
                    cancelled = True
                except BaseException as exc:
                    cleanup_errors.append(exc)
            try:
                app.state.call_service = None
            except BaseException as exc:  # pragma: no cover - state assignment is defensive here
                cleanup_errors.append(exc)

        if cancelled:
            # Cancellation wins the raise, so anything else that went wrong must be
            # logged here or it is lost entirely.
            if primary_error is not None and not isinstance(primary_error, asyncio.CancelledError):
                logger.error("lifespan failed before cancellation", exc_info=primary_error)
            for error in cleanup_errors:
                logger.error("lifespan cleanup step failed during cancellation", exc_info=error)
            raise asyncio.CancelledError

        if primary_error is not None:
            if cleanup_errors:
                raise BaseExceptionGroup(
                    "application lifespan failed and cleanup also failed",
                    [primary_error, *cleanup_errors],
                )
            raise primary_error
        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        if cleanup_errors:
            raise BaseExceptionGroup("application lifespan cleanup failed", cleanup_errors)

    app = FastAPI(title="Agent Phone-Call Bridge", version="1.0.0", lifespan=lifespan)
    app.add_middleware(WebhookBodyLimitMiddleware)
    app.state.settings = settings
    app.state.mcp = mcp
    app.include_router(openai_webhooks.router)
    app.include_router(twilio_webhooks.router)
    app.include_router(debug.router)
    app.include_router(deployment.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/mcp", protected_mcp)
    if grok_oauth_provider is not None and grok_mcp_app is not None:
        app.state.grok_oauth = grok_oauth_provider
        app.include_router(grok_oauth.router)
        # Mounted last so FastAPI routes and /mcp keep precedence over the
        # authenticated Grok app, whose OAuth discovery lives at the host root.
        app.mount("/", grok_mcp_app)

    return app


app = create_app()
