from __future__ import annotations

import json
from collections.abc import Callable

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from app.call_state import CallService
from app.models import ContextPacket, PreparePhoneCallInput

CONTEXT_GUIDANCE = (
    "Assemble only call-relevant facts from Poke memory and integrations. Resolve relative dates "
    "to explicit datetimes in the owner's timezone. Never invent phone numbers or facts. Never "
    "include passwords, authentication codes, payment credentials, or government identifiers."
)


def register_tools(mcp: FastMCP, get_service: Callable[[], CallService]) -> None:
    @mcp.tool(
        name="prepare_phone_call",
        description=(
            "Validate and store a call plan without dialing. "
            + CONTEXT_GUIDANCE
            + " Read the returned confirmation_summary to the owner and collect missing fields."
        ),
    )
    async def prepare_phone_call(
        context: ContextPacket,
        authority_basis: str | None = None,
        requested_by_owner: bool = False,
    ) -> dict:
        try:
            output = await get_service().prepare(
                PreparePhoneCallInput(
                    context=context,
                    authority_basis=authority_basis,
                    requested_by_owner=requested_by_owner,
                )
            )
            return output.model_dump(mode="json")
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(
        name="start_phone_call",
        description=(
            "Start only a valid, unexpired prepared plan after explicit owner confirmation. "
            "Pass the confirmation text that was read back. Then poll get_call_result until terminal. "
            + CONTEXT_GUIDANCE
        ),
    )
    async def start_phone_call(
        plan_id: str, explicit_confirmation: bool, confirmation_text: str
    ) -> dict:
        try:
            result = await get_service().start(
                plan_id,
                explicit_confirmation=explicit_confirmation,
                confirmation_text=confirmation_text,
            )
            return result.model_dump(mode="json")
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(
        name="get_call_result",
        description=(
            "Poll a call until state is completed, failed, timed_out, or transferred; terminal calls "
            "return the stored deterministic result and raw-transcript availability. "
            + CONTEXT_GUIDANCE
        ),
    )
    async def get_call_result(call_id: str) -> dict:
        try:
            return await get_service().get_result(call_id)
        except LookupError as exc:
            raise ToolError(json.dumps({"code": "call_not_found", "message": str(exc)})) from exc

    @mcp.tool(
        name="end_phone_call",
        description="End an active call at the owner's request. " + CONTEXT_GUIDANCE,
    )
    async def end_phone_call(call_id: str) -> dict:
        ended = await get_service().terminate_call(call_id, "owner_request")
        return {"call_id": call_id, "termination_started": ended}

    @mcp.tool(
        name="get_phone_call",
        description="Return lightweight call status and timing fields. " + CONTEXT_GUIDANCE,
    )
    async def get_phone_call(call_id: str) -> dict:
        try:
            snapshot = await get_service().get_snapshot(call_id)
        except LookupError as exc:
            raise ToolError(json.dumps({"code": "call_not_found", "message": str(exc)})) from exc
        data = snapshot.model_dump(mode="json", exclude={"result"})
        return data
