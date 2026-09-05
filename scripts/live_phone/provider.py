from __future__ import annotations

import asyncio
import hashlib
import re
import time
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
        calls = set(record["calls"])
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
