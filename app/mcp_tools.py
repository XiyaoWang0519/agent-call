from __future__ import annotations

import json
from collections.abc import Callable

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from app.call_state import CallService
from app.models import AnswerCallQuestionRequest, ContextPacket, PreparePhoneCallInput

CONTEXT_GUIDANCE = (
    "Assemble only call-relevant facts from Poke memory and integrations. Resolve relative dates "
    "to explicit datetimes in the owner's timezone. Never invent phone numbers or facts. Never "
    "include passwords, authentication codes, payment credentials, or government identifiers. "
    "Write the objective from the agent's perspective as the caller: who the agent calls and what "
    "the agent, acting for the owner, must achieve. Avoid parentheticals or phrasing that could "
    "reassign roles between the agent and the callee."
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
            "Pass the confirmation text that was read back. Then call wait_for_call_event; answer "
            "pending questions with answer_call_question, continue waiting with the returned cursor, "
            "and call get_call_result once the call is terminal. " + CONTEXT_GUIDANCE
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

    @mcp.tool(
        name="wait_for_call_event",
        description=(
            "Long-poll for mid-call questions or terminal state after a sequence cursor. "
            "Returns immediately when events exist; on idle timeout returns an empty events list "
            "(never an error). Re-enter with next_after_sequence. " + CONTEXT_GUIDANCE
        ),
    )
    async def wait_for_call_event(
        call_id: str,
        after_sequence: int = 0,
        timeout_seconds: float = 20.0,
    ) -> dict:
        try:
            return await get_service().wait_for_call_event(
                call_id,
                after_sequence=after_sequence,
                timeout_seconds=timeout_seconds,
            )
        except LookupError as exc:
            raise ToolError(json.dumps({"code": "call_not_found", "message": str(exc)})) from exc
        except ValueError as exc:
            raise ToolError(
                json.dumps({"code": "invalid_call_state", "message": str(exc)})
            ) from exc

    @mcp.tool(
        name="answer_call_question",
        description=(
            "Answer one pending mid-call question from the voice agent. Exactly-once: a second "
            "answer returns already_answered; late answers after timeout return expired. "
            + CONTEXT_GUIDANCE
        ),
    )
    async def answer_call_question(call_id: str, question_id: str, answer: str) -> dict:
        try:
            request = AnswerCallQuestionRequest(
                call_id=call_id,
                question_id=question_id,
                answer=answer,
            )
        except ValidationError as exc:
            raise ToolError(str(exc)) from exc
        try:
            return await get_service().answer_call_question(
                request.call_id,
                request.question_id,
                request.answer,
            )
        except LookupError as exc:
            raise ToolError("unknown question") from exc
        except ValueError as exc:
            raise ToolError(
                json.dumps({"code": "invalid_call_state", "message": str(exc)})
            ) from exc
