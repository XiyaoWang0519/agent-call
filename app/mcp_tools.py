from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from app.call_state import CallService
from app.models import AnswerCallQuestionRequest, ContextPacket, PreparePhoneCallInput

logger = logging.getLogger(__name__)

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
    ) -> dict[str, Any]:
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
            "Pass the confirmation text that was read back. Then IMMEDIATELY begin calling "
            "wait_for_call_event in a loop, calling it again right after every response, "
            "until the call reaches a terminal state; answer pending questions with "
            "answer_call_question, continue waiting with the returned cursor, and call "
            "get_call_result once the call is terminal. Do not end your turn between calls: "
            "this tool starts a live phone call, and the phone agent on the call may ask a "
            "question (via ask_poke) at any moment that only YOU can answer by staying in the "
            "wait_for_call_event loop — if you stop polling, that question times out and the "
            "call can fail its objective. " + CONTEXT_GUIDANCE
        ),
    )
    async def start_phone_call(
        plan_id: str, explicit_confirmation: bool, confirmation_text: str
    ) -> dict[str, Any]:
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
    async def get_call_result(call_id: str) -> dict[str, Any]:
        try:
            return await get_service().get_result(call_id)
        except LookupError as exc:
            raise ToolError(json.dumps({"code": "call_not_found", "message": str(exc)})) from exc

    @mcp.tool(
        name="end_phone_call",
        description="End an active call at the owner's request. " + CONTEXT_GUIDANCE,
    )
    async def end_phone_call(call_id: str) -> dict[str, Any]:
        ended = await get_service().terminate_call(call_id, "owner_request")
        return {"call_id": call_id, "termination_started": ended}

    @mcp.tool(
        name="get_phone_call",
        description="Return lightweight call status and timing fields. " + CONTEXT_GUIDANCE,
    )
    async def get_phone_call(call_id: str) -> dict[str, Any]:
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
            "(never an error) — this is NOT a stopping condition. Re-enter with "
            "next_after_sequence immediately, in a loop, for as long as the call is "
            "non-terminal; read and follow the response's next_action field every time, which "
            "tells you exactly what to do next. Never end your turn while the call is "
            "non-terminal: the phone agent can raise a mid-call question (ask_poke) at any "
            "moment, and if nothing is polling, that question times out and the call can fail "
            "its objective. Only stop once a response reports terminal=true, then call "
            "get_call_result. " + CONTEXT_GUIDANCE
        ),
    )
    async def wait_for_call_event(
        call_id: str,
        after_sequence: int = 0,
        timeout_seconds: float = 20.0,
    ) -> dict[str, Any]:
        logger.info(
            "mcp tool wait_for_call_event call_id=%s after_sequence=%s timeout_seconds=%s",
            call_id,
            after_sequence,
            timeout_seconds,
        )
        try:
            result = await get_service().wait_for_call_event(
                call_id,
                after_sequence=after_sequence,
                timeout_seconds=timeout_seconds,
            )
        except LookupError as exc:
            logger.info(
                "mcp tool wait_for_call_event call_not_found call_id=%s",
                call_id,
            )
            raise ToolError(json.dumps({"code": "call_not_found", "message": str(exc)})) from exc
        except ValueError as exc:
            logger.info(
                "mcp tool wait_for_call_event invalid_call_state call_id=%s error=%s",
                call_id,
                exc,
            )
            raise ToolError(
                json.dumps({"code": "invalid_call_state", "message": str(exc)})
            ) from exc
        logger.info(
            "mcp tool wait_for_call_event completed call_id=%s state=%s terminal=%s "
            "event_count=%s next_after_sequence=%s",
            call_id,
            result.get("state"),
            result.get("terminal"),
            len(result.get("events") or ()),
            result.get("next_after_sequence"),
        )
        return result

    @mcp.tool(
        name="answer_call_question",
        description=(
            "Submit the final answer to one pending mid-call question from the voice agent — "
            "the callee is waiting live on the phone. Finish all relevant checks in Poke "
            "memory and integrations before calling this tool: the answer is relayed to the "
            "callee immediately and accepted exactly once. The answer must contain only the "
            "final, ready-to-relay result. Never submit a progress update, partial result, or "
            "promise to keep checking (for example, 'one sec', 'I'm still looking', or 'let "
            "me check further'). If the requested fact cannot be found after checking, submit "
            "a conclusive final answer saying that and include only useful confirmed facts. "
            "A second answer returns already_answered; late answers after timeout return "
            "expired. After answering, immediately resume the wait_for_call_event loop until "
            "the call is terminal; do not end your turn. " + CONTEXT_GUIDANCE
        ),
    )
    async def answer_call_question(call_id: str, question_id: str, answer: str) -> dict[str, Any]:
        logger.info(
            "mcp tool answer_call_question call_id=%s question_id=%s answer_chars=%s",
            call_id,
            question_id,
            len(answer),
        )
        try:
            request = AnswerCallQuestionRequest(
                call_id=call_id,
                question_id=question_id,
                answer=answer,
            )
        except ValidationError as exc:
            logger.info(
                "mcp tool answer_call_question invalid_request call_id=%s question_id=%s",
                call_id,
                question_id,
            )
            raise ToolError(str(exc)) from exc
        try:
            result = await get_service().answer_call_question(
                request.call_id,
                request.question_id,
                request.answer,
            )
        except LookupError as exc:
            logger.info(
                "mcp tool answer_call_question unknown_question call_id=%s question_id=%s",
                call_id,
                question_id,
            )
            raise ToolError("unknown question") from exc
        except ValueError as exc:
            logger.info(
                "mcp tool answer_call_question invalid_call_state call_id=%s "
                "question_id=%s error=%s",
                call_id,
                question_id,
                exc,
            )
            raise ToolError(
                json.dumps({"code": "invalid_call_state", "message": str(exc)})
            ) from exc
        logger.info(
            "mcp tool answer_call_question completed call_id=%s question_id=%s status=%s",
            call_id,
            question_id,
            result.get("status"),
        )
        return result
