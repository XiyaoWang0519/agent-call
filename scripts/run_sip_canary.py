from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from app.models import TERMINAL_STATES
from app.settings import Settings


def tool_data(result) -> dict[str, Any]:
    if result.is_error:
        raise RuntimeError(str(result.content))
    if isinstance(result.data, dict):
        return result.data
    if result.structured_content:
        return result.structured_content
    raise RuntimeError("tool returned no structured data")


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    settings.require_runtime_configuration()
    base_url = (args.base_url or settings.public_base_url or "").rstrip("/")
    target = args.target or settings.owner_phone_e164
    nonce = f"POKE-{secrets.token_hex(3).upper()}"
    use_outcome_tool = args.mode == "full"
    tool_instruction = (
        "After hearing the nonce, call record_call_outcome once before wrapping up."
        if use_outcome_tool
        else "Do not call record_call_outcome during this canary."
    )
    context = {
        "owner": {
            "display_name": "Irvin",
            "timezone": settings.owner_timezone,
            "callback_number": settings.owner_phone_e164,
        },
        "target": {"name": "Irvin", "organization": None, "phone": target},
        "objective": (
            "Run a SIP canary. Ask the callee to say the displayed nonce, acknowledge it, "
            "then explain in one sentence that the canary is complete. " + tool_instruction
        ),
        "relevant_facts": [f"The required spoken nonce is {nonce}."],
        "preferences": ["Keep the canary under one minute."],
        "hard_constraints": [tool_instruction],
        "allowed_commitments": ["Confirm that the technical canary was heard."],
        "prohibited_actions": ["Do not make any real-world commitment."],
        "escalation": {"mode": "end_call", "owner_phone": settings.owner_phone_e164},
    }
    headers = {
        "Authorization": f"Bearer {Settings.reveal(settings.mcp_bearer_token)}",
        "X-Poke-User-Id": settings.allowed_poke_user_id or "",
    }
    transport = StreamableHttpTransport(f"{base_url}/mcp/", headers=headers)
    async with Client(transport, timeout=30) as client:
        prepared = tool_data(
            await client.call_tool(
                "prepare_phone_call",
                {
                    "context": context,
                    "authority_basis": "Owner is running the documented live SIP canary.",
                    "requested_by_owner": True,
                },
            )
        )
        print("Confirmation read-back:")
        print(prepared["confirmation_summary"])
        started = tool_data(
            await client.call_tool(
                "start_phone_call",
                {
                    "plan_id": prepared["plan_id"],
                    "explicit_confirmation": True,
                    "confirmation_text": prepared["confirmation_summary"],
                },
            )
        )
        call_id = started["call_id"]
        print(f"Call started: {call_id}")
        print(f"When asked, say this nonce clearly: {nonce}")
        print("Also interrupt the assistant once while it is speaking, then let the call finish.")
        terminal = None
        for _ in range(args.poll_attempts):
            await asyncio.sleep(2)
            polled = tool_data(await client.call_tool("get_call_result", {"call_id": call_id}))
            if polled["state"] in {state.value for state in TERMINAL_STATES}:
                terminal = polled
                break
        if terminal is None:
            await client.call_tool("end_phone_call", {"call_id": call_id})
            raise TimeoutError("canary did not become terminal before the polling deadline")

    async with httpx.AsyncClient(timeout=15) as http:
        debug = await http.get(
            f"{base_url}/calls/{call_id}",
            headers={"Authorization": f"Bearer {Settings.reveal(settings.debug_api_token)}"},
        )
        debug.raise_for_status()
        evidence = debug.json()
    audit = evidence["canary_evidence"]
    transcript = evidence["transcript"]
    interruption_confirmed = args.interruption_confirmed or (
        (
            await asyncio.to_thread(
                input,
                "Did the assistant's audible speech stop promptly when you interrupted it? [y/N] ",
            )
        )
        .strip()
        .lower()
        in {"y", "yes"}
    )
    nonce_turns = [
        turn for turn in transcript if turn["speaker"] == "callee" and nonce in turn["text"].upper()
    ]
    responded_after_nonce = bool(nonce_turns) and any(
        turn["speaker"] == "assistant"
        and turn["sequence_number"] > nonce_turns[0]["sequence_number"]
        for turn in transcript
    )
    checks = {
        "xai_sideband_connected": audit["xai_connect_status"] == 101,
        "transcription_echoed": audit["transcription_verified"] == 1,
        "server_vad_echoed": audit["vad_verified"] == 1,
        "spoken_nonce_transcribed": bool(nonce_turns),
        "automatic_response_after_nonce": responded_after_nonce,
        "record_outcome_tool_behavior": audit["advisory_outcome"] is not None
        if use_outcome_tool
        else audit["advisory_outcome"] is None,
        "tool_output_continuation": audit["tool_continuation_observed"] == 1
        if use_outcome_tool
        else True,
        "interruption_cancel_event": audit["interruption_observed"] == 1,
        "interruption_audio_stopped": interruption_confirmed,
        "terminal_result_available": terminal.get("result") is not None,
        "raw_transcript_available": terminal.get("result", {}).get("raw_transcript_available")
        is True,
    }
    print(json.dumps({"call_id": call_id, "mode": args.mode, "checks": checks}, indent=2))
    return 0 if all(checks.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the live xAI SIP/Twilio canary")
    parser.add_argument("--base-url", help="Deployed public base URL; defaults to PUBLIC_BASE_URL")
    parser.add_argument("--target", help="Canary phone in E.164; defaults to OWNER_PHONE_E164")
    parser.add_argument("--mode", choices=("full", "no-outcome-tool"), default="full")
    parser.add_argument("--poll-attempts", type=int, default=60)
    parser.add_argument(
        "--interruption-confirmed",
        action="store_true",
        help="Skip the post-call prompt only when the operator already confirmed audio stopped.",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
