from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

from scripts.live_phone.audio import (
    DigitDetector,
    decode_mulaw,
    encode_mulaw,
    pcm24_to_8,
    rms,
    tone,
    wav_bytes,
)
from scripts.live_phone.config import Config
from scripts.live_phone.provider import Provider
from scripts.live_phone.report import grade, write_report
from scripts.live_phone.runner import packet, run_call
from scripts.live_phone.scenarios import SCENARIOS
from scripts.live_phone.server import create_app
from scripts.live_phone.session import Session
from scripts.live_phone.store import Store


@pytest.fixture
def phone_config(tmp_path):
    return Config(
        _env_file=None,
        public_url="https://phones.example.test",
        app_url="https://app.example.test",
        instance_id="isolated-phone-tests",
        token="t" * 32,
        mcp_token="mcp-test",
        agent_user_id="test-owner",
        debug_token="debug-test",
        twilio_account_sid="AC" + "1" * 32,
        twilio_auth_token="test-signing-token",
        openai_api_key="sk-test",
        caller_number="+14155550100",
        callee_number="+14155550101",
        owner_number="+14155550102",
        artifacts=tmp_path,
    )


def test_phone_configuration_requires_distinct_numbers_and_https(phone_config):
    for changes in (
        {"owner_number": phone_config.callee_number},
        {"app_url": "http://app.example.test"},
        {"public_url": "https://user:secret@example.test"},
        {"public_url": phone_config.app_url},
    ):
        with pytest.raises(ValidationError):
            Config(**{**phone_config.model_dump(), **changes})


def test_mulaw_roundtrip_and_wav_are_real_audio():
    pcm = tone(0.5, (440,))
    restored = decode_mulaw(encode_mulaw(pcm))
    assert abs(rms(pcm) - rms(restored)) < 100
    assert decode_mulaw(bytes([255, 127])) == bytes(4)
    assert wav_bytes(pcm).startswith(b"RIFF")
    assert len(pcm24_to_8(pcm * 3)) == len(pcm)


@pytest.mark.parametrize(
    "digit,frequencies", [("2", (697, 1336)), ("#", (941, 1477)), ("0", (941, 1336))]
)
def test_dtmf_detects_tones_and_debounces(digit, frequencies):
    detector = DigitDetector()
    pcm = decode_mulaw(encode_mulaw(tone(0.3, frequencies)))
    found = []
    for offset in range(0, len(pcm), 320):
        found.extend(detector.feed(pcm[offset : offset + 320]))
    assert found == [digit]
    assert detector.feed(bytes(1600)) == []
    assert detector.feed(pcm) == [digit]
    assert DigitDetector().feed(tone(1, (440,))) == []


def test_registry_survives_reopen_and_blocks_overlapping_runs(tmp_path):
    store = Store(tmp_path)
    store.create("run_one", {"scenario": "conversation"}, 60)
    store.add_call("run_one", "CA" + "1" * 32)
    store.add_call("run_one", "CA" + "1" * 32)
    reopened = Store(tmp_path)
    assert reopened.get("run_one")["calls"] == ["CA" + "1" * 32]
    with pytest.raises(ValueError):
        reopened.create("run_two", {}, 60)
    reopened.update("run_one", done=True)
    with pytest.raises(ValueError):
        reopened.add_call("run_one", "CA" + "2" * 32)
    reopened.create("run_two", {}, 60)


def test_plan_never_contains_callee_only_nonce(phone_config):
    context = packet(phone_config, SCENARIOS["conversation"])
    assert "{nonce}" not in json.dumps(context)
    assert context["target"]["phone"] == phone_config.callee_number
    assert context["escalation"]["owner_phone"] == phone_config.owner_number
    assert packet(phone_config, SCENARIOS["transfer"])["escalation"]["mode"] == "transfer_to_owner"


async def test_preflight_rejects_identity_before_number_api(phone_config):
    async with httpx.AsyncClient() as http:
        provider = Provider(phone_config, http)
        provider.debug = AsyncMock(return_value={"instance_id": "production"})
        provider.twilio = AsyncMock()
        with pytest.raises(ValueError):
            await provider.preflight(())
        provider.twilio.assert_not_called()


async def test_cleanup_reconciles_ambiguous_start_and_only_own_calls(phone_config):
    store = Store(phone_config.artifacts)
    store.create("run_one", {"scenario": "conversation", "plan_id": "own-plan"}, 120)
    own = "CA" + "a" * 32
    other = "CA" + "b" * 32
    async with httpx.AsyncClient() as http:
        provider = Provider(phone_config, http)
        provider.debug = AsyncMock(
            side_effect=[
                [
                    {"call_id": "own", "plan_id": "own-plan"},
                    {"call_id": "other", "plan_id": "other-plan"},
                ],
                {"canary_evidence": {"twilio_ai_call_sid": own}},
            ]
        )
        provider.twilio = AsyncMock(
            side_effect=[{"status": "in-progress"}, {}, {"status": "completed"}]
        )
        result = await provider.cleanup(store, "run_one")
        assert result == {"verified": True, "forced": [own], "states": {own: "completed"}}
        assert all(other not in str(call) for call in provider.twilio.call_args_list)


async def test_cleanup_cannot_release_lease_when_debug_is_unavailable(phone_config):
    store = Store(phone_config.artifacts)
    store.create("run_one", {"plan_id": "ambiguous"}, -1)
    async with httpx.AsyncClient() as http:
        provider = Provider(phone_config, http)
        provider.debug = AsyncMock(side_effect=httpx.ConnectError("unavailable"))
        result = await provider.reap(store)
    assert result[0]["verified"] is False
    assert store.unfinished()[0]["id"] == "run_one"


def signed(config, path, form):
    return {
        "X-Twilio-Signature": RequestValidator(
            config.twilio_auth_token.get_secret_value()
        ).compute_signature(config.public_url + path, form)
    }


def incoming_form(config, role="callee"):
    return {
        "AccountSid": config.twilio_account_sid,
        "From": config.caller_number,
        "To": config.callee_number if role == "callee" else config.owner_number,
        "CallSid": "CA" + "a" * 32,
    }


def test_harness_auth_and_signed_busy_endpoint(phone_config):
    app = create_app(phone_config)
    with TestClient(app) as client:
        assert client.get("/runs/unknown").status_code == 401
        app.state.store.create(
            "run_one", {"scenario": "busy", "plan_id": "plan", "bindings": {}}, 120
        )
        form = incoming_form(phone_config)
        assert client.post("/incoming", data=form).status_code == 403
        result = client.post(
            "/incoming", data=form, headers=signed(phone_config, "/incoming", form)
        )
        assert result.status_code == 200
        assert '<Reject reason="busy"' in result.text
        assert app.state.store.get("run_one")["calls"] == [form["CallSid"]]
        form["CallSid"] = "CA" + "b" * 32
        assert (
            client.post(
                "/incoming", data=form, headers=signed(phone_config, "/incoming", form)
            ).status_code
            == 409
        )
        assert (
            client.get(
                "/runs/run_one/artifacts/runs.db", headers={"Authorization": "Bearer " + "t" * 32}
            ).status_code
            == 404
        )


def test_signed_websocket_receives_audio_and_independent_asr(phone_config, monkeypatch):
    app = create_app(phone_config)
    monkeypatch.setattr(app.state.provider, "twilio", AsyncMock(return_value={}))
    monkeypatch.setattr(
        app.state.speech_client.audio.transcriptions,
        "create",
        AsyncMock(return_value=SimpleNamespace(text="amber river robin")),
    )
    with TestClient(app) as client:
        store = app.state.store
        store.create(
            "run_one", {"scenario": "conversation", "plan_id": "plan", "bindings": {}}, 120
        )
        session = Session(
            "callee",
            store.root / "run_one",
            {},
            {},
            app.state.speech_client,
            "test-asr",
            AsyncMock(),
            {},
        )
        app.state.sessions["run_one", "callee"] = session
        # Supply a reservation through the same ticket mapping used by the server.
        app.state.tickets["run_one", "callee"] = "ticket-value"
        form = incoming_form(phone_config)
        response = client.post(
            "/incoming", data=form, headers=signed(phone_config, "/incoming", form)
        )
        assert response.status_code == 200
        path = "/media/run_one/callee"
        sid = "MZ" + "1" * 32
        with client.websocket_connect(path, headers=signed(phone_config, path, {})) as ws:
            ws.send_json({"event": "connected"})
            ws.send_json(
                {
                    "event": "start",
                    "start": {
                        "streamSid": sid,
                        "accountSid": phone_config.twilio_account_sid,
                        "callSid": form["CallSid"],
                        "mediaFormat": {
                            "encoding": "audio/x-mulaw",
                            "sampleRate": 8000,
                            "channels": 1,
                        },
                        "customParameters": {"ticket": "ticket-value"},
                    },
                }
            )
            payload = base64.b64encode(encode_mulaw(tone(0.4))).decode()
            ws.send_json(
                {
                    "event": "media",
                    "streamSid": sid,
                    "media": {"track": "inbound", "timestamp": "0", "payload": payload},
                }
            )
            ws.send_json({"event": "stop", "streamSid": sid})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        assert session.drained.is_set()
        assert session.transcripts[0]["text"] == "amber river robin"
        assert session.evidence()["voiced_seconds"] > 0
        assert (store.root / "run_one/callee-received.wav").read_bytes().startswith(b"RIFF")


def test_grade_never_passes_missing_audio_or_forced_cleanup(tmp_path):
    scenario = SCENARIOS["conversation"]
    checks = grade(
        scenario, {"result": {"state": "completed", "result": {"raw_transcript_available": True}}}
    )
    assert checks["callee:audio_received"] is False
    assert checks["callee:scenario_complete"] is False
    assert checks["provider_cleanup_verified"] is False
    assert checks["semantic_grounding"] is False
    report = write_report(
        tmp_path,
        scenario,
        {"sessions": {"callee": {"transcripts": [{"text": "<script>alert(1)</script>"}]}}},
    )
    assert report["passed"] is False
    html = (tmp_path / "report.html").read_text()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


async def test_session_fails_instead_of_accepting_unheard_expected_text(tmp_path):
    session = Session("callee", tmp_path, {}, {}, None, "test", AsyncMock(), {})
    session.ready.set()
    from scripts.live_phone.scenarios import Step

    await session.execute((Step("expect", "nonce never heard", 0.01),))
    assert session.error is not None
    assert session.finished.is_set()
    assert not any(event["type"] == "step_passed" for event in session.events)


async def test_runner_does_not_retry_ambiguous_start_and_always_cleans_up(
    phone_config, monkeypatch
):
    store = Store(phone_config.artifacts)
    store.create("run_one", {"scenario": "conversation"}, 120)

    class FakeMCP:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_tools(self):
            return [
                SimpleNamespace(name=name)
                for name in (
                    "prepare_phone_call",
                    "start_phone_call",
                    "wait_for_call_event",
                    "answer_call_question",
                    "get_phone_call",
                    "get_call_result",
                    "end_phone_call",
                )
            ]

        async def call_tool(self, name, args):
            calls.append(name)
            if name == "prepare_phone_call":
                return SimpleNamespace(
                    is_error=False,
                    data={"plan_id": "plan-one", "confirmation_summary": "exact approved text"},
                )
            assert store.get("run_one")["plan_id"] == "plan-one"
            assert args["confirmation_text"] == "exact approved text"
            raise TimeoutError("start response lost")

    calls = []
    monkeypatch.setattr("scripts.live_phone.runner.Client", FakeMCP)
    provider = SimpleNamespace(
        cleanup=AsyncMock(return_value={"verified": True, "forced": [], "states": {}})
    )
    await run_call(phone_config, store, provider, None, "run_one", SCENARIOS["conversation"], {})
    assert calls.count("start_phone_call") == 1
    provider.cleanup.assert_awaited_once_with(store, "run_one")
    row = store.get("run_one")
    assert row["done"] is True
    assert row["passed"] is False
    assert row["error"] == "TimeoutError"


async def test_preflight_verifies_all_destination_routes(phone_config):
    expected = {
        "instance_id": phone_config.instance_id,
        "live_calls_enabled": True,
        "caller_hash": hashlib.sha256(phone_config.caller_number.encode()).hexdigest(),
        "owner_hash": hashlib.sha256(phone_config.owner_number.encode()).hexdigest(),
        "ask_agent_enabled": True,
    }
    async with httpx.AsyncClient() as http:
        provider = Provider(phone_config, http)
        provider.debug = AsyncMock(return_value=expected)

        async def number(method, path, *, params):
            return {
                "incoming_phone_numbers": [
                    {
                        "phone_number": params["PhoneNumber"],
                        "voice_url": phone_config.public_url + "/incoming",
                        "voice_method": "POST",
                        "voice_application_sid": None,
                        "trunk_sid": None,
                    }
                ]
            }

        provider.twilio = AsyncMock(side_effect=number)
        assert await provider.preflight(("ask_agent_enabled",)) == expected
        assert provider.twilio.await_count == 3


async def test_cleanup_still_stops_known_legs_when_application_is_down(phone_config):
    store = Store(phone_config.artifacts)
    store.create("run_one", {"plan_id": "plan"}, 60)
    sid = "CA" + "a" * 32
    store.remember_resources("run_one", {sid}, set())
    async with httpx.AsyncClient() as http:
        provider = Provider(phone_config, http)
        provider.debug = AsyncMock(side_effect=httpx.ConnectError("down"))
        provider.twilio = AsyncMock(
            side_effect=[{"status": "in-progress"}, {}, {"status": "completed"}]
        )
        result = await provider.cleanup(store, "run_one")
    assert result["verified"] is False
    assert result["states"][sid] == "completed"
    assert result["forced"] == [sid]


async def test_cleanup_failure_on_one_leg_does_not_skip_another(phone_config):
    store = Store(phone_config.artifacts)
    store.create("run_one", {}, 60)
    bad, good = "CA" + "a" * 32, "CA" + "b" * 32
    store.remember_resources("run_one", {bad, good}, set())
    updated = []

    async def api(method, path, **kwargs):
        if bad in path and not updated:
            raise httpx.ConnectError("transient")
        if method == "POST":
            updated.append(path)
            return {}
        return {"status": "completed" if updated else "in-progress"}

    async with httpx.AsyncClient() as http:
        provider = Provider(phone_config, http)
        provider.twilio = AsyncMock(side_effect=api)
        result = await provider.cleanup(store, "run_one")
    assert updated == [f"/Calls/{good}.json"]
    assert result["verified"] is False


def test_live_test_capabilities_are_opt_in_and_authenticated(settings):
    from app.main import create_app as application

    with TestClient(application(settings)) as client:
        assert client.get("/diagnostics/live-test").status_code == 401
        headers = {"Authorization": "Bearer debug-test"}
        assert client.get("/diagnostics/live-test", headers=headers).status_code == 404
        settings.live_test_instance_id = "isolated-phone-tests"
        result = client.get("/diagnostics/live-test", headers=headers)
        assert result.status_code == 200
        assert (
            result.json()["caller_hash"]
            == hashlib.sha256(settings.twilio_caller_id.encode()).hexdigest()
        )
        assert settings.owner_phone_e164 not in result.text


async def test_tool_audit_records_name_and_failure_without_payloads(service, packet):
    from app.models import CallState
    from tests.conftest import seed_call, wait_background

    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service.handle_realtime_event(
        call_id,
        {
            "type": "response.function_call_arguments.done",
            "name": "send_dtmf",
            "call_id": "tool_test",
            "arguments": '{"digits":"invalid-sensitive-value"}',
        },
    )
    await service.handle_realtime_send(
        call_id,
        {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": "tool_test",
                "output": '{"ok":false,"error":"invalid_dtmf_request"}',
            },
        },
    )
    await wait_background()
    events = await service.get_latency_event_records(call_id)
    assert any(
        e["stage"] == "tool_dispatched" and e["event_key"] == "send_dtmf:tool_test" for e in events
    )
    assert any(e["stage"] == "tool_result_failed" for e in events)
    assert "invalid-sensitive-value" not in json.dumps(events)


@pytest.mark.parametrize("scenario_name", ["ask-agent", "transfer"])
async def test_runner_polls_answers_and_waits_for_post_transfer_audio(
    phone_config, monkeypatch, scenario_name
):
    store = Store(phone_config.artifacts)
    store.create("run_one", {"scenario": scenario_name}, 120)
    scenario = SCENARIOS[scenario_name]
    calls = []
    expected_terminal = "transferred" if scenario_name == "transfer" else "completed"
    polled = 0

    class FakeMCP:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_tools(self):
            return [
                SimpleNamespace(name=n)
                for n in (
                    "prepare_phone_call",
                    "start_phone_call",
                    "wait_for_call_event",
                    "answer_call_question",
                    "get_phone_call",
                    "get_call_result",
                    "end_phone_call",
                )
            ]

        async def call_tool(self, name, args):
            nonlocal polled
            calls.append((name, args))
            if name == "prepare_phone_call":
                data = {"plan_id": "plan", "confirmation_summary": "approved"}
            elif name == "start_phone_call":
                data = {"call_id": "app-call"}
            elif name == "wait_for_call_event":
                polled += 1
                data = {
                    "terminal": polled > 1,
                    "state": expected_terminal if polled > 1 else "active",
                    "next_after_sequence": 7,
                    "events": []
                    if polled > 1
                    else [{"question_id": "q", "question": "color?", "status": "pending"}],
                }
            elif name == "answer_call_question":
                data = {"status": "accepted"}
            elif name == "get_phone_call":
                data = {"state": "active"}
            else:
                assert polled > 1
                data = {"state": expected_terminal, "result": {"raw_transcript_available": True}}
            return SimpleNamespace(is_error=False, data=data)

    class FakeSession:
        def __init__(self):
            self.error = None
            self.signals = {"transferred": asyncio.Event()}
            self.closed = asyncio.Event()
            self.drained = asyncio.Event()
            self.finished = False

        async def execute(self, steps):
            if scenario_name == "transfer":
                await self.signals["transferred"].wait()
            self.finished = True
            self.closed.set()
            self.drained.set()

        def evidence(self):
            return {"steps_finished": self.finished}

    sessions = {"callee": FakeSession()}
    if scenario_name == "transfer":
        sessions["owner"] = FakeSession()

    async def cleanup(*args):
        assert all(s.finished for s in sessions.values())
        return {"verified": True, "forced": [], "states": {}}

    provider = SimpleNamespace(
        cleanup=cleanup,
        debug=AsyncMock(return_value={"canary_evidence": {}}),
        remember_audit=lambda *args: None,
    )
    monkeypatch.setattr("scripts.live_phone.runner.Client", FakeMCP)
    monkeypatch.setattr(
        "scripts.live_phone.runner.semantic_grade", AsyncMock(return_value={"passed": True})
    )
    await run_call(phone_config, store, provider, None, "run_one", scenario, sessions)
    assert store.get("run_one")["error"] is None
    polls = [args for name, args in calls if name == "wait_for_call_event"]
    assert polls[1]["after_sequence"] == 7
    answers = [args for name, args in calls if name == "answer_call_question"]
    assert len(answers) == (1 if scenario_name == "ask-agent" else 0)
    if answers:
        assert answers[0]["answer"] == scenario.answer


def test_start_endpoint_prepares_audio_then_dispatches_run(phone_config, monkeypatch):
    app = create_app(phone_config)
    app.state.provider.preflight = AsyncMock(return_value={})
    app.state.speech_client.audio.speech.create = AsyncMock(
        return_value=SimpleNamespace(content=tone(0.3))
    )

    async def fake_run(config, store, provider, client, run_id, scenario, sessions):
        assert scenario.name == "conversation"
        assert sessions["callee"].speech
        assert sessions["callee"].replacements["nonce"] not in json.dumps(packet(config, scenario))
        store.update(run_id, done=True, passed=False)

    monkeypatch.setattr("scripts.live_phone.server.run_call", fake_run)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + "t" * 32}
        assert (
            client.post(
                "/runs",
                headers=headers,
                json={"scenario": "conversation", "confirm_instance": "wrong"},
            ).status_code
            == 400
        )
        result = client.post(
            "/runs",
            headers=headers,
            json={"scenario": "conversation", "confirm_instance": phone_config.instance_id},
        )
        assert result.status_code == 200
        run_id = result.json()["run_id"]
        assert client.get(f"/runs/{run_id}", headers=headers).status_code == 200
    app.state.provider.preflight.assert_awaited_once()


async def test_audio_playback_paces_mulaw_and_requires_playback_mark(tmp_path, monkeypatch):
    session = Session("callee", tmp_path, {}, {}, None, "test", AsyncMock(), {})
    session.stream_sid = "MZ" + "1" * 32
    messages = []
    clock = [0.0]
    monkeypatch.setattr(session, "now", lambda: clock[0])

    async def sleep(seconds):
        clock[0] += seconds

    async def send(message):
        messages.append(message)
        if message["event"] == "mark":
            session.marks[message["mark"]["name"]].set()

    monkeypatch.setattr("scripts.live_phone.session.asyncio.sleep", sleep)
    session.websocket = SimpleNamespace(send_json=send)
    pcm = tone(0.1)
    start, end = await session.play(pcm, "synthetic speech")
    assert start == 0
    assert end == pytest.approx(0.1)
    assert len(messages) == 6  # Five 20 ms media packets, followed by a mark.
    assert all(len(base64.b64decode(m["media"]["payload"])) == 160 for m in messages[:-1])
    assert (tmp_path / "callee-sent.wav").exists() is False  # Saved at scenario/stream completion.
    session.closed.set()
    with pytest.raises(RuntimeError):
        await session.play(pcm, "cannot speak after hangup")


@pytest.mark.parametrize("continues_speaking", [False, True])
async def test_interruption_requires_overlap_and_quiet_tail(
    tmp_path, monkeypatch, continues_speaking
):
    from scripts.live_phone.scenarios import Step

    session = Session("callee", tmp_path, {"stop": tone(0.1)}, {}, None, "test", AsyncMock(), {})
    session.ready.set()
    session.voiced = [(0.5, 0.8)]
    monkeypatch.setattr(session, "now", lambda: 1.0)

    async def play(*args):
        session.voiced.append((1.1, 0.1))
        if continues_speaking:
            session.voiced.append((3.0, 1.0))
        return 1.0, 6.0

    monkeypatch.setattr(session, "play", play)
    await session.execute((Step("interrupt", "stop", 1.2),))
    assert (session.error is not None) == continues_speaking
    assert any(e["type"] == "interruption_verified" for e in session.events) != continues_speaking


@pytest.mark.parametrize("spoken_seconds", [0.0, 1.0])
async def test_hold_quiet_checks_received_audio(tmp_path, monkeypatch, spoken_seconds):
    from scripts.live_phone.scenarios import Step

    session = Session("callee", tmp_path, {}, {}, None, "test", AsyncMock(), {})
    session.ready.set()
    session.voiced = [(2.0, spoken_seconds)]
    monkeypatch.setattr(session, "play", AsyncMock(return_value=(1.0, 4.0)))
    await session.execute((Step("quiet", seconds=3),))
    assert (session.error is not None) == bool(spoken_seconds)


async def test_received_digit_sequence_is_exact(tmp_path):
    from scripts.live_phone.scenarios import Step

    session = Session("callee", tmp_path, {}, {}, None, "test", AsyncMock(), {})
    session.ready.set()
    session.digits = "3"
    await session.execute((Step("digits", "2", 0.01),))
    assert session.error is not None


@pytest.mark.parametrize("passed", [True, False])
async def test_cli_suite_downloads_reports_and_preserves_failure(
    phone_config, tmp_path, respx_mock, passed
):
    from scripts.live_phone.__main__ import run_suite

    base = phone_config.public_url
    respx_mock.post(base + "/runs").mock(
        return_value=httpx.Response(200, json={"run_id": "run_one"})
    )
    respx_mock.get(base + "/runs/run_one").mock(
        return_value=httpx.Response(200, json={"id": "run_one", "done": True, "passed": passed})
    )
    for name in ("report.json", "report.html", "junit.xml"):
        respx_mock.get(base + "/runs/run_one/artifacts/" + name).mock(
            return_value=httpx.Response(200, content=b"synthetic report")
        )
    for role in ("callee", "owner"):
        for direction in ("sent", "received"):
            respx_mock.get(base + f"/runs/run_one/artifacts/{role}-{direction}.wav").mock(
                return_value=httpx.Response(404)
            )
    status = await run_suite(phone_config, ["busy"], tmp_path / "results", phone_config.instance_id)
    assert status == (0 if passed else 1)
    assert (tmp_path / "results/run_one/report.html").read_text() == "synthetic report"
    assert json.loads((tmp_path / "results/suite.json").read_text())[0]["passed"] == passed


async def test_cli_refuses_unconfirmed_or_over_budget_suite(phone_config, tmp_path):
    from scripts.live_phone.__main__ import run_suite

    with pytest.raises(ValueError):
        await run_suite(phone_config, ["conversation"], tmp_path, "wrong")
    phone_config.max_suite_seconds = 60
    with pytest.raises(ValueError):
        await run_suite(phone_config, ["conversation"], tmp_path, phone_config.instance_id)


def test_list_command_does_not_load_credentials(monkeypatch, capsys):
    from scripts.live_phone.__main__ import main

    monkeypatch.setattr("sys.argv", ["live-phone", "list"])
    assert main() == 0
    assert len(json.loads(capsys.readouterr().out)) == len(SCENARIOS)
