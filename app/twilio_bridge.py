from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlencode

from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from app.models import ContextPacket
from app.settings import Settings

T = TypeVar("T")

# Twilio SDK calls are synchronous and block on network I/O (up to the configured
# HTTP timeout). Running them on the process-wide default `to_thread` executor
# would let a slow Twilio round-trip for one call starve other calls' Twilio
# operations (and any other unrelated `to_thread` user) that share that pool.
# A dedicated executor isolates this bridge's blocking calls.
TWILIO_EXECUTOR_MAX_WORKERS = 8


@dataclass(slots=True)
class ParticipantInfo:
    call_sid: str
    conference_sid: str | None


class TwilioBridge:
    def __init__(self, settings: Settings, client: Client | None = None):
        self.settings = settings
        self.client = client or Client(
            settings.twilio_account_sid,
            Settings.reveal(settings.twilio_auth_token),
            http_client=TwilioHttpClient(
                pool_connections=True,
                timeout=settings.twilio_http_timeout_seconds,
                max_retries=0,
            ),
        )
        self._executor = ThreadPoolExecutor(
            max_workers=TWILIO_EXECUTOR_MAX_WORKERS, thread_name_prefix="twilio"
        )

    async def _run_blocking(self, fn: Callable[[], T]) -> T:
        # Note: unlike `asyncio.to_thread`, `run_in_executor` does not propagate
        # contextvars into the worker thread. None of the wrapped Twilio SDK calls
        # read or depend on contextvars, so this is safe.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn))

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _callback(self, path: str, *, call_id: str, plan_id: str, **extra: str) -> str:
        base = (self.settings.public_base_url or "").rstrip("/")
        query = urlencode({"call_id": call_id, "plan_id": plan_id, **extra})
        return f"{base}{path}?{query}"

    async def create_agent_participant(
        self, *, call_id: str, plan_id: str, conference_name: str
    ) -> ParticipantInfo:
        custom = urlencode({"X-Plan-Id": plan_id, "X-Bridge-Call-Id": call_id})
        sip_uri = f"sip:{self.settings.openai_project_id}@sip.api.openai.com;transport=tls?{custom}"

        def create() -> Any:
            return self.client.conferences(conference_name).participants.create(
                from_=self.settings.twilio_caller_id,
                to=sip_uri,
                label="agent",
                start_conference_on_enter=False,
                end_conference_on_exit=False,
                time_limit=720,
                wait_url="",
                beep="false",
                # Match Twilio's OpenAI SIP conference pattern: avoid early media
                # while the agent is alone on hold before the callee joins.
                early_media=False,
                muted=False,
                jitter_buffer_size="small",
                status_callback=self._callback(
                    "/webhooks/twilio/participant-status",
                    call_id=call_id,
                    plan_id=plan_id,
                    leg="agent",
                ),
                status_callback_event=["initiated", "ringing", "answered", "completed"],
                conference_status_callback=self._callback(
                    "/webhooks/twilio/conference",
                    call_id=call_id,
                    plan_id=plan_id,
                ),
                conference_status_callback_event=["start", "end", "join", "leave", "mute"],
            )

        participant = await self._run_blocking(create)
        return ParticipantInfo(participant.call_sid, participant.conference_sid)

    async def create_callee_participant(
        self,
        *,
        call_id: str,
        plan_id: str,
        conference_sid_or_name: str,
        packet: ContextPacket,
    ) -> ParticipantInfo:
        def create() -> Any:
            return self.client.conferences(conference_sid_or_name).participants.create(
                from_=self.settings.twilio_caller_id,
                to=packet.target.phone,
                label="callee",
                start_conference_on_enter=True,
                end_conference_on_exit=True,
                time_limit=self.settings.max_call_seconds,
                wait_url="",
                beep="false",
                timeout=self.settings.setup_deadline_seconds,
                machine_detection="DetectMessageEnd",
                jitter_buffer_size="small",
                amd_status_callback=self._callback(
                    "/webhooks/twilio/amd", call_id=call_id, plan_id=plan_id
                ),
                amd_status_callback_method="POST",
                status_callback=self._callback(
                    "/webhooks/twilio/participant-status",
                    call_id=call_id,
                    plan_id=plan_id,
                    leg="callee",
                ),
                status_callback_event=["initiated", "ringing", "answered", "completed"],
            )

        participant = await self._run_blocking(create)
        return ParticipantInfo(participant.call_sid, participant.conference_sid)

    async def create_owner_participant(
        self,
        *,
        call_id: str,
        plan_id: str,
        conference_sid_or_name: str,
        owner_phone: str,
    ) -> ParticipantInfo:
        def create() -> Any:
            return self.client.conferences(conference_sid_or_name).participants.create(
                from_=self.settings.twilio_caller_id,
                to=owner_phone,
                label="owner",
                start_conference_on_enter=True,
                end_conference_on_exit=False,
                time_limit=self.settings.max_call_seconds,
                timeout=30,
                beep="false",
                jitter_buffer_size="small",
                status_callback=self._callback(
                    "/webhooks/twilio/participant-status",
                    call_id=call_id,
                    plan_id=plan_id,
                    leg="owner",
                ),
                status_callback_event=["initiated", "ringing", "answered", "completed"],
            )

        participant = await self._run_blocking(create)
        return ParticipantInfo(participant.call_sid, participant.conference_sid)

    async def complete_conference(self, conference_sid_or_name: str | None) -> None:
        if not conference_sid_or_name:
            return

        def complete() -> None:
            self.client.conferences(conference_sid_or_name).update(status="completed")

        try:
            await self._run_blocking(complete)
        except TwilioRestException as exc:
            # A missing conference is already fully torn down, which is the desired
            # idempotent outcome for retry and startup recovery paths.
            if exc.status == 404:
                return
            raise

    async def remove_participant(
        self, conference_sid_or_name: str | None, participant_call_sid: str | None
    ) -> None:
        if not conference_sid_or_name or not participant_call_sid:
            return

        def remove() -> None:
            self.client.conferences(conference_sid_or_name).participants(
                participant_call_sid
            ).delete()

        await self._run_blocking(remove)

    async def enable_end_conference_on_exit(
        self, conference_sid_or_name: str | None, participant_call_sid: str | None
    ) -> None:
        """Make the transferred owner leg the conference lifetime owner."""

        if not conference_sid_or_name or not participant_call_sid:
            return

        def enable() -> None:
            self.client.conferences(conference_sid_or_name).participants(
                participant_call_sid
            ).update(end_conference_on_exit=True)

        await self._run_blocking(enable)

    async def unmute_participant(
        self, conference_sid_or_name: str | None, participant_call_sid: str | None
    ) -> None:
        """Force-unmute a participant after conference start.

        Twilio mutes participants that join with start_conference_on_enter=False
        until the conference starts. Explicit unmute ensures the OpenAI SIP leg
        can inject TTS into the mix even if the automatic unmute races activation.
        """
        if not conference_sid_or_name or not participant_call_sid:
            return

        def unmute() -> None:
            self.client.conferences(conference_sid_or_name).participants(
                participant_call_sid
            ).update(muted=False)

        await self._run_blocking(unmute)

    async def send_dtmf(
        self,
        conference_sid_or_name: str,
        participant_call_sid: str,
        *,
        call_id: str,
        plan_id: str,
        digits: str,
    ) -> None:
        """Play DTMF tones into the callee leg only, via a signed announce webhook.

        Twilio's participant update has no digit-sending parameter, and updating the
        callee's call directly with TwiML would pull it out of the conference. An
        announce_url plays TwiML into just that participant's leg instead.
        """
        url = self._callback(
            "/webhooks/twilio/announce-dtmf", call_id=call_id, plan_id=plan_id, digits=digits
        )

        def announce() -> None:
            self.client.conferences(conference_sid_or_name).participants(
                participant_call_sid
            ).update(announce_url=url, announce_method="POST")

        await self._run_blocking(announce)
