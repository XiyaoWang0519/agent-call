from __future__ import annotations

import asyncio
import hashlib
import re
import time
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from scripts.live_phone.config import Config
from scripts.live_phone.store import Store

CALL_TERMINAL = {"completed", "busy", "failed", "no-answer", "canceled"}


class Provider:
    def __init__(self, config: Config, http: httpx.AsyncClient):
        self.config = config
        self.http = http
        self.base = f"https://api.twilio.com/2010-04-01/Accounts/{config.twilio_account_sid}"

    async def twilio(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self.http.request(
            method,
            self.base + path,
            auth=(self.config.twilio_account_sid, self.config.twilio_auth_token.get_secret_value()),
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    async def debug(self, path: str) -> Any:
        response = await self.http.get(
            self.config.app_url.rstrip("/") + path,
            headers={"Authorization": f"Bearer {self.config.debug_token.get_secret_value()}"},
        )
        response.raise_for_status()
        return response.json()

    async def preflight(self, features: tuple[str, ...]) -> dict[str, Any]:
        result = await self.debug("/diagnostics/live-test")
        expected = {
            "instance_id": self.config.instance_id,
            "live_calls_enabled": True,
            "caller_hash": hashlib.sha256(self.config.caller_number.encode()).hexdigest(),
            "owner_hash": hashlib.sha256(self.config.owner_number.encode()).hexdigest(),
            **dict.fromkeys(features, True),
        }
        if any(result.get(key) != value for key, value in expected.items()):
            raise ValueError("test instance, numbers, or required feature flags do not match")
        # Verify all three numbers belong to this isolated account before authorizing dialing.
        for number in (
            self.config.caller_number,
            self.config.callee_number,
            self.config.owner_number,
        ):
            result = await self.twilio(
                "GET", "/IncomingPhoneNumbers.json", params={"PhoneNumber": number}
            )
            rows = result.get("incoming_phone_numbers", [])
            if len(rows) != 1 or rows[0].get("phone_number") != number:
                raise ValueError("test numbers must belong to the configured Twilio account")
            if number != self.config.caller_number and (
                rows[0].get("voice_url") != self.config.public_url.rstrip("/") + "/incoming"
                or rows[0].get("voice_method") != "POST"
                or rows[0].get("voice_application_sid")
                or rows[0].get("trunk_sid")
            ):
                raise ValueError("automated destination webhook configuration does not match")
        return expected

    async def hangup(self, sid: str) -> None:
        self.validate_sid(sid, "CA")
        call = await self.twilio("GET", f"/Calls/{sid}.json")
        if call["status"] not in CALL_TERMINAL:
            await self.twilio("POST", f"/Calls/{sid}.json", data={"Status": "completed"})

    async def correlate(self, row: dict[str, Any], role: str, sid: str) -> dict[str, str]:
        """Find this plan's outbound leg; caller ID is only an initial filter."""
        self.validate_sid(sid, "CA")
        inbound = await self.twilio("GET", f"/Calls/{sid}.json")
        destination = self.config.callee_number if role == "callee" else self.config.owner_number
        if (
            inbound.get("account_sid") != self.config.twilio_account_sid
            or inbound.get("direction") != "inbound"
            or inbound.get("from") != self.config.caller_number
            or inbound.get("to") != destination
            or inbound.get("status") not in {"ringing", "in-progress"}
            or parsedate_to_datetime(inbound["date_created"]).timestamp() < int(row["created_at"])
        ):
            raise ValueError("unexpected inbound call")
        # The signed incoming request can arrive before start_phone_call returns.
        async with asyncio.timeout(8):
            while True:
                calls = await self.debug("/calls")
                owned = [call for call in calls if call.get("plan_id") == row["plan_id"]]
                if len(owned) > 1:
                    raise ValueError("ambiguous application call")
                if owned:
                    call_id = owned[0]["call_id"]
                    if row.get("app_call_id", call_id) != call_id:
                        raise ValueError("application call mismatch")
                    detail = await self.debug(f"/calls/{call_id}")
                    audit = detail["canary_evidence"]
                    outbound_sid = audit.get(f"twilio_{role}_call_sid")
                    conference_sid = audit.get("conference_sid")
                    if outbound_sid and conference_sid:
                        break
                await asyncio.sleep(0.2)
        self.validate_sid(outbound_sid, "CA")
        self.validate_sid(conference_sid, "CF")
        outbound = await self.twilio("GET", f"/Calls/{outbound_sid}.json")
        if (
            outbound.get("account_sid") != self.config.twilio_account_sid
            or outbound.get("direction") != "outbound-api"
            or outbound.get("from") != self.config.caller_number
            or outbound.get("to") != destination
            or outbound.get("status") not in {"queued", "ringing", "in-progress"}
        ):
            raise ValueError("unexpected outbound leg")
        return {
            "app_call_id": call_id,
            "incoming_call_sid": sid,
            "outbound_call_sid": outbound_sid,
            "conference_sid": conference_sid,
        }

    async def announce_challenge(self, correlation: dict[str, Any], url: str) -> None:
        path = (
            f"/Conferences/{correlation['conference_sid']}/Participants/"
            f"{correlation['outbound_call_sid']}.json"
        )
        async with asyncio.timeout(12):
            while True:
                try:
                    participant = await self.twilio("GET", path)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
                    participant = {}
                if participant.get("status") == "connected":
                    break
                await asyncio.sleep(0.2)
        await self.twilio(
            "POST", f"/Calls/{correlation['incoming_call_sid']}.json", data={"TimeLimit": "30"}
        )
        await self.twilio("POST", path, data={"AnnounceUrl": url, "AnnounceMethod": "POST"})

    @staticmethod
    def validate_sid(sid: str, prefix: str) -> None:
        if not re.fullmatch(prefix + r"[a-fA-F0-9]{32}", sid):
            raise ValueError("invalid provider SID")

    @staticmethod
    def remember_audit(store: Store, run_id: str, audit: dict[str, Any]) -> None:
        calls = {
            audit[key]
            for key in ("twilio_ai_call_sid", "twilio_callee_call_sid", "twilio_owner_call_sid")
            if audit.get(key)
        }
        conferences = {audit["conference_sid"]} if audit.get("conference_sid") else set()
        for sid in calls:
            Provider.validate_sid(sid, "CA")
        for sid in conferences:
            Provider.validate_sid(sid, "CF")
        store.remember_resources(run_id, calls, conferences)

    async def cleanup(self, store: Store, run_id: str) -> dict[str, Any]:
        """Reconcile a lost start; attempt every owned resource even when another API fails."""
        record = store.get(run_id)
        errors: list[str] = []
        if record.get("plan_id"):
            try:
                rows = await self.debug("/calls")
                owned = [row for row in rows if row.get("plan_id") == record["plan_id"]]
                if len(owned) > 1:
                    raise RuntimeError("multiple calls for single-use plan")
                for row in owned:
                    evidence = await self.debug(f"/calls/{row['call_id']}")
                    audit = evidence["canary_evidence"]
                    self.remember_audit(store, run_id, audit)
                    if not audit.get("conference_sid") and audit.get("conference_name"):
                        result = await self.twilio(
                            "GET",
                            "/Conferences.json",
                            params={"FriendlyName": audit["conference_name"]},
                        )
                        found = {item["sid"] for item in result.get("conferences", [])}
                        for sid in found:
                            self.validate_sid(sid, "CF")
                        store.remember_resources(run_id, set(), found)
            except Exception as exc:
                errors.append("discovery:" + type(exc).__name__)
        record = store.get(run_id)
        # Provisional inbound legs were admitted by this harness but are not
        # evidence receivers until the private outbound challenge is returned.
        calls = set(record["calls"]) | set(record.get("candidates", {}))
        conferences = set(record.get("conferences", []))
        forced: list[str] = []
        states: dict[str, str] = {}
        for sid in conferences:
            try:
                self.validate_sid(sid, "CF")
                result = await self.twilio("GET", f"/Conferences/{sid}/Participants.json")
                found = {item["call_sid"] for item in result.get("participants", [])}
                for call in found:
                    self.validate_sid(call, "CA")
                calls.update(found)
                store.remember_resources(run_id, found, set())
            except Exception as exc:
                errors.append("participants:" + type(exc).__name__)
            try:
                self.validate_sid(sid, "CF")
                result = await self.twilio("GET", f"/Conferences/{sid}.json")
                if result["status"] != "completed":
                    forced.append(sid)
                    await self.twilio(
                        "POST", f"/Conferences/{sid}.json", data={"Status": "completed"}
                    )
            except Exception as exc:
                errors.append("conference:" + type(exc).__name__)
        for sid in sorted(calls):
            try:
                self.validate_sid(sid, "CA")
                result = await self.twilio("GET", f"/Calls/{sid}.json")
                if result["status"] not in CALL_TERMINAL:
                    forced.append(sid)
                    await self.twilio("POST", f"/Calls/{sid}.json", data={"Status": "completed"})
            except Exception as exc:
                errors.append("call:" + type(exc).__name__)
        # Fresh reads prove termination; successful update responses alone do not.
        for _ in range(5):
            pending = False
            for sid in sorted(calls | conferences):
                try:
                    resource = "Calls" if sid in calls else "Conferences"
                    self.validate_sid(sid, "CA" if sid in calls else "CF")
                    result = await self.twilio("GET", f"/{resource}/{sid}.json")
                    states[sid] = result["status"]
                    pending = pending or result["status"] not in CALL_TERMINAL
                except Exception:
                    states[sid] = "unverified"
                    pending = True
            if not pending:
                return {
                    "verified": not errors,
                    "forced": forced,
                    "states": states,
                    **({"errors": errors} if errors else {}),
                }
            await asyncio.sleep(1)
        return {"verified": False, "forced": forced, "states": states, "errors": errors}

    async def reap(self, store: Store) -> list[dict[str, Any]]:
        results = []
        for record in store.unfinished():
            if record["deadline"] > time.time():
                continue
            try:
                result = await self.cleanup(store, record["id"])
                if store.get(record["id"]).get("finalizing"):
                    # The runner owns artifact publication while receive handlers drain.
                    # A crashed runner requires explicit reconciliation; a reaper must
                    # never turn unfinished audio into a completed, downloadable report.
                    store.update(record["id"], cleanup=result)
                    results.append({"id": record["id"], **result})
                    continue
                from scripts.live_phone.report import write_report
                from scripts.live_phone.scenarios import SCENARIOS

                scenario = SCENARIOS.get(record.get("scenario", ""))
                if scenario:
                    write_report(
                        store.root / record["id"],
                        scenario,
                        {"error": "deadline_reaped", "cleanup": result},
                    )
                store.update(
                    record["id"],
                    cleanup=result,
                    done=result["verified"],
                    passed=False,
                    error="deadline_reaped",
                )
            except Exception as exc:
                result = {"verified": False, "error": type(exc).__name__}
                store.update(record["id"], cleanup=result, error="cleanup_pending")
            results.append({"id": record["id"], **result})
        return results
