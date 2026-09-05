from __future__ import annotations

import html
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from scripts.live_phone.scenarios import Scenario


def grade(scenario: Scenario, evidence: dict[str, Any]) -> dict[str, bool]:
    debug = evidence.get("debug", {})
    audit = debug.get("canary_evidence", {})
    latency = debug.get("latency_events", [])
    tools = {
        e["event_key"].split(":", 1)[0]: e["event_key"].split(":", 1)[1]
        for e in latency
        if e["stage"] == "tool_dispatched" and ":" in e["event_key"]
    }
    failed = {e["event_key"] for e in latency if e["stage"] == "tool_result_failed"}
    returned = {e["event_key"] for e in latency if e["stage"] == "tool_result_sent"}
    final = evidence.get("result", {})
    checks = {
        "runner_completed": evidence.get("error") is None,
        "expected_terminal_state": final.get("state") in scenario.states,
        "result_persisted": bool(final.get("result")),
        "provider_cleanup_verified": evidence.get("cleanup", {}).get("verified") is True,
        "no_forced_cleanup": evidence.get("cleanup", {}).get("forced") == [],
        "mcp_snapshot_observed": bool(evidence.get("snapshots")),
        "callee_reservation_consumed": "callee" in evidence.get("receivers", []),
    }
    if scenario.owner_reject or scenario.owner_steps:
        checks["owner_reservation_consumed"] = "owner" in evidence.get("receivers", [])
    if not scenario.reject:
        checks.update(
            semantic_grounding=evidence.get("semantic", {}).get("passed") is True,
            accept_2xx=isinstance(audit.get("openai_accept_status"), int)
            and 200 <= audit["openai_accept_status"] < 300,
            transcript_retained=final.get("result", {}).get("raw_transcript_available") is True,
            transcription_verified=audit.get("transcription_verified") == 1,
            vad_verified=audit.get("semantic_vad_verified") == 1,
        )
        required_roles = ["callee"] + (["owner"] if scenario.owner_steps else [])
        for role in required_roles:
            session = evidence.get("sessions", {}).get(role, {})
            checks[f"{role}:connected"] = session.get("connected") is True
            checks[f"{role}:audio_received"] = session.get("voiced_seconds", 0) > 0.1
            checks[f"{role}:no_media_gaps"] = session.get("media_gaps") == 0
            checks[f"{role}:scenario_complete"] = (
                session.get("steps_finished") is True and session.get("error") is None
            )
            checks[f"{role}:closed"] = session.get("closed") is True
    for name in scenario.tools:
        checks[f"tool:{name}"] = name in tools
        if name != "transfer_to_owner":
            checks[f"tool_result:{name}"] = tools.get(name) in returned
        if name == "search_web":
            checks["search_succeeded"] = name in tools and tools[name] not in failed
    for name in scenario.forbidden_tools:
        checks[f"tool_absent:{name}"] = name not in tools
    for key, expected in scenario.audit.items():
        checks[f"audit:{key}"] = key in audit and audit[key] == expected
    if scenario.advisory:
        checks["advisory_outcome"] = (audit.get("advisory_outcome") is not None) == (
            scenario.advisory == "present"
        )
    if scenario.name == "hold":
        for stage in ("hold_entered", "hold_exited"):
            checks[stage] = any(e["stage"] == stage for e in latency)
    if scenario.answer:
        checks["mcp_answer_accepted"] = any(
            e.get("status") == "accepted" for e in evidence.get("answers", [])
        )
    if scenario.name == "transfer":
        checks["transfer_promoted"] = str(audit.get("transfer_outcome", "")).startswith(
            "completed:"
        )
    # Compare only timestamps from the same app process clock.
    prewarm = next((e for e in latency if e["stage"] == "initial_session_ack"), None)
    dial = next((e for e in latency if e["stage"] == "twilio_callee_request"), None)
    checks["prewarm_before_dial"] = bool(
        prewarm
        and dial
        and prewarm["clock_id"] == dial["clock_id"]
        and prewarm["monotonic_ns"] <= dial["monotonic_ns"]
    )
    return checks


def write_report(root: Path, scenario: Scenario, evidence: dict[str, Any]) -> dict[str, Any]:
    checks = grade(scenario, evidence)
    result = {
        "scenario": scenario.name,
        "passed": all(checks.values()),
        "checks": checks,
        "evidence": evidence,
    }
    (root / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    suite = ET.Element(
        "testsuite",
        name=scenario.name,
        tests=str(len(checks)),
        failures=str(sum(not v for v in checks.values())),
    )
    for name, passed in checks.items():
        case = ET.SubElement(suite, "testcase", name=name, classname="live_phone")
        if not passed:
            ET.SubElement(case, "failure", message="Required evidence missing or assertion failed")
    ET.ElementTree(suite).write(root / "junit.xml", encoding="unicode")
    rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{'PASS' if passed else 'FAIL'}</td></tr>"
        for name, passed in checks.items()
    )
    audio = "".join(
        f'<h2>{role}</h2><p>Received at phone</p><audio controls src="{role}-received.wav"></audio>'
        f'<p>Sent fixture</p><audio controls src="{role}-sent.wav"></audio>'
        for role in evidence.get("sessions", {})
    )
    transcripts = html.escape(json.dumps(evidence.get("sessions", {}), indent=2))
    (root / "report.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        "<title>Live phone test</title><style>body{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 20px}"
        "td{padding:6px 20px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap}audio{width:100%}</style>"
        f"<h1>{html.escape(scenario.name)}: {'PASS' if result['passed'] else 'FAIL'}</h1>"
        f"<table>{rows}</table>{audio}<h2>Timestamped evidence</h2><pre>{transcripts}</pre></html>"
    )
    for name in ("report.json", "junit.xml", "report.html"):
        (root / name).chmod(0o600)
    return result
