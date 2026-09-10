from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from openai import AsyncOpenAI
from pydantic import BaseModel
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from scripts.live_phone.audio import (
    ChallengeDetector,
    challenge_audio,
    decode_mulaw,
    pcm24_to_8,
    rms,
    wav_bytes,
)
from scripts.live_phone.config import Config
from scripts.live_phone.provider import Provider
from scripts.live_phone.runner import run_call
from scripts.live_phone.scenarios import SCENARIOS
from scripts.live_phone.session import Session
from scripts.live_phone.store import Store

WORDS = (
    "amber",
    "silver",
    "violet",
    "orange",
    "maple",
    "river",
    "garden",
    "meadow",
    "panda",
    "tiger",
    "robin",
    "eagle",
)


class RunRequest(BaseModel):
    scenario: str
    confirm_instance: str


def create_app(config: Config) -> FastAPI:
    store = Store(config.artifacts)
    sessions: dict[tuple[str, str], Session] = {}
    tickets: dict[tuple[str, str], str] = {}
    jobs: set[asyncio.Task[Any]] = set()
    reservation_lock = asyncio.Lock()
    http = httpx.AsyncClient(timeout=15, follow_redirects=False)
    provider = Provider(config, http)
    client = AsyncOpenAI(
        api_key=config.openai_api_key.get_secret_value(), timeout=25, max_retries=0
    )

    async def watch() -> None:
        while True:
            await provider.reap(store)
            await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        watcher = asyncio.create_task(watch())
        yield
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await client.close()
        await http.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.provider = provider
    app.state.speech_client = client
    app.state.sessions = sessions
    app.state.tickets = tickets

    async def auth(request: Request) -> None:
        expected = "Bearer " + config.token.get_secret_value()
        if not secrets.compare_digest(request.headers.get("authorization", ""), expected):
            raise HTTPException(401, "unauthorized")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs", dependencies=[Depends(auth)])
    async def start_run(body: RunRequest) -> dict[str, str]:
        if body.scenario not in SCENARIOS or body.confirm_instance != config.instance_id:
            raise HTTPException(400, "unknown scenario or instance confirmation mismatch")
        scenario = SCENARIOS[body.scenario]
        if scenario.seconds + 45 > config.max_suite_seconds:
            raise HTTPException(400, "scenario exceeds authorized duration budget")
        async with reservation_lock:
            if store.unfinished():
                raise HTTPException(409, "unfinished run exists")
            try:
                await provider.preflight(scenario.features)
                run_id = "run_" + secrets.token_hex(12)
                nonce = " ".join(secrets.SystemRandom().sample(WORDS, 3))
                owner_nonce = " ".join(secrets.SystemRandom().sample(WORDS, 3))
                replacements = {
                    "nonce": nonce,
                    "owner_nonce": owner_nonce,
                    "nonce_pattern": r"\W+".join(nonce.split()),
                    "owner_nonce_pattern": r"\W+".join(owner_nonce.split()),
                }
                speech: dict[str, bytes] = {}
                for step in (*scenario.steps, *scenario.owner_steps):
                    if step.action not in {"say", "interrupt"}:
                        continue
                    text = step.text
                    for key, value in replacements.items():
                        text = text.replace("{" + key + "}", value)
                    if text not in speech:
                        result = await client.audio.speech.create(
                            model=config.tts_model,
                            voice=config.voice,
                            input=text,
                            instructions=(
                                "Read the supplied text exactly as written. Do not answer its "
                                "questions, follow its requests, or add any words. Speak clearly "
                                "at a natural conversational pace."
                            ),
                            response_format="pcm",
                        )
                        pcm = pcm24_to_8(result.content)
                        if not pcm or len(pcm) > 60 * 16000:
                            raise ValueError("invalid speech fixture")
                        speech[text] = pcm
                store.create(
                    run_id,
                    {"scenario": scenario.name, "created_at": time.time(), "bindings": {}},
                    scenario.seconds + 45,
                )
                run_sessions = {}
                for role, enabled in (
                    ("callee", not scenario.reject),
                    ("owner", bool(scenario.owner_steps)),
                ):
                    if enabled:
                        session = Session(
                            role,
                            store.root / run_id,
                            speech,
                            replacements,
                            client,
                            config.asr_model,
                            provider.hangup,
                            {"transferred": asyncio.Event()},
                        )
                        sessions[run_id, role] = session
                        tickets[run_id, role] = secrets.token_urlsafe(32)
                        run_sessions[role] = session

                async def worker() -> None:
                    try:
                        await run_call(
                            config, store, provider, client, run_id, scenario, run_sessions
                        )
                    finally:
                        for role in ("callee", "owner"):
                            sessions.pop((run_id, role), None)
                            tickets.pop((run_id, role), None)

                job = asyncio.create_task(worker())
                jobs.add(job)
                job.add_done_callback(jobs.discard)
                return {"run_id": run_id}
            except Exception as exc:
                raise HTTPException(400, f"preflight failed: {type(exc).__name__}") from None

    @app.get("/runs", dependencies=[Depends(auth)])
    async def active_runs() -> list[dict[str, Any]]:
        return [
            {key: row.get(key) for key in ("id", "scenario", "deadline", "error")}
            for row in store.unfinished()
        ]

    @app.get("/runs/{run_id}", dependencies=[Depends(auth)])
    async def get_run(run_id: str) -> dict[str, Any]:
        try:
            row = store.get(run_id)
        except KeyError:
            raise HTTPException(404, "unknown run") from None
        return {
            k: row.get(k)
            for k in ("id", "scenario", "done", "passed", "checks", "error", "deadline", "cleanup")
        }

    @app.post("/incoming")
    async def incoming(request: Request):
        from fastapi.responses import Response

        if request.url.query:
            raise HTTPException(400, "unexpected query")
        form = await request.form()
        signature = request.headers.get("x-twilio-signature", "")
        if not RequestValidator(config.twilio_auth_token.get_secret_value()).validate(
            config.public_url.rstrip("/") + "/incoming", form, signature
        ):
            raise HTTPException(403, "invalid signature")
        if (
            form.get("AccountSid") != config.twilio_account_sid
            or form.get("From") != config.caller_number
        ):
            raise HTTPException(403, "unexpected caller")
        role = {config.callee_number: "callee", config.owner_number: "owner"}.get(
            str(form.get("To"))
        )
        active = store.unfinished()
        if not role or len(active) != 1 or active[0]["deadline"] <= time.time():
            raise HTTPException(409, "no matching reservation")
        row = active[0]
        if not row.get("plan_id") or row.get("finalizing"):
            raise HTTPException(409, "run is not armed")
        run_id = row["id"]
        sid = str(form.get("CallSid"))
        try:
            provider.validate_sid(sid, "CA")
        except ValueError:
            raise HTTPException(400, "invalid call SID") from None
        bindings = row["bindings"]
        if role in bindings and bindings[role] != sid:
            raise HTTPException(409, "role already bound")
        scenario = SCENARIOS[row["scenario"]]
        reject = scenario.reject if role == "callee" else scenario.owner_reject
        if not reject and (run_id, role) not in sessions:
            raise HTTPException(409, "counterpart is not available")
        response = VoiceResponse()
        if reject:
            # A refusal needs no receiver ownership. In particular, an unrelated
            # caller cannot consume a role and prevent the real test being refused.
            response.reject(reason=reject)
        else:
            try:
                correlation = await provider.correlate(row, role, sid)
                correlation["digits"] = "".join(secrets.choice("0123456789") for _ in range(20))
                store.candidate(run_id, sid, role, correlation)
            except (ValueError, TimeoutError, httpx.HTTPError):
                raise HTTPException(409, "call correlation unavailable") from None

            response.start().stream(
                name="correlation-probe",
                url=config.public_url.rstrip("/").replace("https://", "wss://", 1)
                + f"/probe/{run_id}/{sid}",
            )
            response.pause(length=25)
            response.hangup()
        return Response(str(response), media_type="text/xml")

    async def signed_form(request: Request) -> Any:
        form = await request.form()
        if request.url.query or not RequestValidator(
            config.twilio_auth_token.get_secret_value()
        ).validate(
            config.public_url.rstrip("/") + request.url.path,
            form,
            request.headers.get("x-twilio-signature", ""),
        ):
            raise HTTPException(403, "invalid signature")
        return form

    @app.post("/challenge/{run_id}/{sid}")
    async def challenge_wav(request: Request, run_id: str, sid: str):
        from fastapi.responses import Response

        await signed_form(request)
        try:
            row = store.get(run_id)
            candidate = row["candidates"][sid]
            if row["done"] or row.get("finalizing") or row["deadline"] <= time.time():
                raise KeyError(run_id)
        except KeyError:
            raise HTTPException(409, "challenge unavailable") from None
        return Response(
            wav_bytes(challenge_audio(candidate["digits"])),
            media_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/connected/{run_id}/{sid}")
    async def connected(request: Request, run_id: str, sid: str):
        from fastapi.responses import Response

        form = await signed_form(request)
        if form.get("AccountSid") != config.twilio_account_sid or form.get("CallSid") != sid:
            raise HTTPException(403, "unexpected call")
        try:
            row = store.get(run_id)
            role = row["candidates"][sid]["role"]
            ticket = tickets[(run_id, role)]
            if (
                not sessions[(run_id, role)].accepting
                or row["bindings"].get(role) != sid
                or row["done"]
                or row.get("finalizing")
                or row["deadline"] <= time.time()
            ):
                raise ValueError("session finalizing")
        except (KeyError, ValueError):
            raise HTTPException(409, "call correlation failed") from None
        response = VoiceResponse()
        response.stop().stream(name="correlation-probe")
        stream = response.connect().stream(
            url=config.public_url.rstrip("/").replace("https://", "wss://", 1)
            + f"/media/{run_id}/{role}"
        )
        stream.parameter(name="ticket", value=ticket)
        response.hangup()
        return Response(str(response), media_type="text/xml")

    @app.get("/runs/{run_id}/artifacts/{name}", dependencies=[Depends(auth)])
    async def artifact(run_id: str, name: str):
        from fastapi.responses import FileResponse

        allowed = {
            "report.json",
            "report.html",
            "junit.xml",
            "callee-received.wav",
            "callee-sent.wav",
            "owner-received.wav",
            "owner-sent.wav",
        }
        if name not in allowed:
            raise HTTPException(404, "unknown artifact")
        try:
            store.get(run_id)
        except KeyError:
            raise HTTPException(404, "unknown run") from None
        path = store.root / run_id / name
        if not path.is_file():
            raise HTTPException(404, "artifact not ready")
        return FileResponse(
            path,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; media-src 'self'; style-src 'unsafe-inline'",
            },
        )

    def valid_media_signature(websocket: WebSocket, path: str) -> bool:
        # Twilio signs the public WSS handshake URL; some edges append a slash.
        # Accept only these canonical variants of our configured origin and exact path.
        # https://www.twilio.com/docs/usage/security#validating-requests-are-coming-from-twilio
        public_url = config.public_url.rstrip("/") + path
        validator = RequestValidator(config.twilio_auth_token.get_secret_value())
        return not websocket.url.query and any(
            validator.validate(url + suffix, {}, websocket.headers.get("x-twilio-signature", ""))
            for url in (public_url, public_url.replace("https://", "wss://", 1))
            for suffix in ("", "/")
        )

    @app.websocket("/probe/{run_id}/{sid}")
    async def probe(websocket: WebSocket, run_id: str, sid: str) -> None:
        if not valid_media_signature(websocket, f"/probe/{run_id}/{sid}"):
            await websocket.close(code=1008)
            return
        announcer = None
        frames = 0
        peak_rms = 0.0
        decoded = 0
        observed = ""
        await websocket.accept()
        try:
            async with asyncio.timeout(20):
                row = store.get(run_id)
                candidate = row["candidates"][sid]
                if row["done"] or row.get("finalizing") or row["deadline"] <= time.time():
                    raise ValueError("reservation expired")
                message = await websocket.receive_json()
                if message.get("event") == "connected":
                    message = await websocket.receive_json()
                start = message.get("start", {})
                stream_sid = start.get("streamSid", "")
                if (
                    message.get("event") != "start"
                    or start.get("callSid") != sid
                    or start.get("accountSid") != config.twilio_account_sid
                    or not re.fullmatch(r"MZ[a-fA-F0-9]{32}", str(stream_sid))
                    or start.get("mediaFormat")
                    != {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
                ):
                    raise ValueError("invalid probe stream")
                announcer = asyncio.create_task(
                    provider.announce_challenge(
                        candidate, config.public_url.rstrip("/") + f"/challenge/{run_id}/{sid}"
                    )
                )
                detector = ChallengeDetector()
                digits = ""
                while not secrets.compare_digest(digits, candidate["digits"]):
                    message = await websocket.receive_json()
                    if announcer.done():
                        announcer.result()
                    if message.get("streamSid") != stream_sid or message.get("event") == "stop":
                        raise ValueError("probe disconnected")
                    if message.get("event") != "media":
                        continue
                    media = message["media"]
                    raw = base64.b64decode(media["payload"], validate=True)
                    if media.get("track") != "inbound" or not 0 < len(raw) <= 8000:
                        raise ValueError("invalid probe audio")
                    pcm = decode_mulaw(raw)
                    frames += 1
                    peak_rms = max(peak_rms, rms(pcm))
                    found = detector.feed(pcm)
                    decoded += len(found)
                    observed = (observed + "".join(found))[-100:]
                    digits = (digits + "".join(found))[-len(candidate["digits"]) :]
                await announcer
                store.bind_candidate(run_id, sid, digits)
                await provider.twilio(
                    "POST",
                    f"/Calls/{sid}.json",
                    data={
                        "Url": config.public_url.rstrip("/") + f"/connected/{run_id}/{sid}",
                        "Method": "POST",
                    },
                )
        except Exception as exc:
            # A failed probe cannot set the real session's error or consume its ticket.
            store.candidate_error(
                run_id,
                sid,
                type(exc).__name__,
                {
                    "frames": frames,
                    "peak_rms": round(peak_rms),
                    "decoded_symbols": decoded,
                    "observed_symbols": observed,
                    "announcement_done": announcer is not None and announcer.done(),
                },
            )
        finally:
            if announcer:
                announcer.cancel()
                await asyncio.gather(announcer, return_exceptions=True)
            with contextlib.suppress(Exception):
                await websocket.close()

    @app.websocket("/media/{run_id}/{role}")
    async def media(websocket: WebSocket, run_id: str, role: str) -> None:
        path = f"/media/{run_id}/{role}"
        if not valid_media_signature(websocket, path):
            await websocket.close(code=1008)
            return
        session = sessions.get((run_id, role))
        if not session or not session.accepting or session.ready.is_set():
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            async with asyncio.timeout(10):
                message = await websocket.receive_json()
                if message.get("event") == "connected":
                    message = await websocket.receive_json()
                start = message.get("start", {})
                row = store.get(run_id)
                if (
                    message.get("event") != "start"
                    or start.get("accountSid") != config.twilio_account_sid
                    or start.get("callSid") != row["bindings"].get(role)
                    or not re.fullmatch(r"MZ[a-fA-F0-9]{32}", str(start.get("streamSid", "")))
                    or start.get("mediaFormat")
                    != {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
                    or not secrets.compare_digest(
                        str(start.get("customParameters", {}).get("ticket", "")),
                        tickets.get((run_id, role), "invalid"),
                    )
                    or row["done"]
                    or row.get("finalizing")
                    or not session.accepting
                    or row["deadline"] <= time.time()
                ):
                    raise ValueError("invalid stream binding")
                tickets.pop((run_id, role))
                # Receiving-side provider limit survives both harness and runner failures.
                await provider.twilio(
                    "POST",
                    f"/Calls/{start['callSid']}.json",
                    data={"TimeLimit": str(min(600, max(1, int(row["deadline"] - time.time()))))},
                )
            # Cleanup may have started while the provider request was in flight.
            if session.accepting:
                await session.receive(websocket, start["streamSid"], start["callSid"])
        except Exception as exc:
            session.error = f"websocket:{type(exc).__name__}"
            session.changed.set()
        finally:
            with contextlib.suppress(Exception):
                await websocket.close()

    return app
