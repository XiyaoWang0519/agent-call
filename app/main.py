from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastmcp import FastMCP
from openai import AsyncOpenAI

from app.call_state import CallService
from app.db import Database
from app.mcp_tools import register_tools
from app.routes import debug, openai_webhooks, twilio_webhooks
from app.security import MCPAuthMiddleware
from app.settings import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    holder: dict[str, CallService] = {}
    mcp = FastMCP("Poke Phone-Call Bridge")

    def get_service() -> CallService:
        if "service" not in holder:
            raise RuntimeError("service is not started")
        return holder["service"]

    register_tools(mcp, get_service)
    mcp_http_app = mcp.http_app(
        path="/",
        transport="streamable-http",
        stateless_http=True,
        json_response=True,
    )
    protected_mcp = MCPAuthMiddleware(mcp_http_app, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.require_runtime_configuration()
        db = Database(settings.database_path)
        await db.initialize()
        openai = AsyncOpenAI(
            api_key=Settings.reveal(settings.openai_api_key),
            webhook_secret=Settings.reveal(settings.openai_webhook_secret),
        )
        service = CallService(settings, db, openai=openai)
        holder["service"] = service
        app.state.call_service = service
        # Recovery happens before the server accepts traffic.
        await service.recover_startup()
        await service.start_watchdog()
        async with mcp_http_app.lifespan(app):
            yield
        await service.stop()
        await openai.close()
        holder.clear()

    app = FastAPI(title="Poke Phone-Call Bridge", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.mcp = mcp
    app.include_router(openai_webhooks.router)
    app.include_router(twilio_webhooks.router)
    app.include_router(debug.router)
    app.mount("/mcp", protected_mcp)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
