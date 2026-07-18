from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

E164 = Annotated[str, StringConstraints(pattern=r"^\+[1-9]\d{1,14}$")]
CONTEXT_PACKET_MAX_BYTES = 16 * 1024


def utc_now() -> datetime:
    return datetime.now(UTC)


class CallState(StrEnum):
    PREPARED = "prepared"
    PREWARMING = "prewarming"
    READY_TO_ACTIVATE = "ready_to_activate"
    ACTIVATING = "activating"
    ACTIVE = "active"
    TERMINATING = "terminating"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    TRANSFERRED = "transferred"


TERMINAL_STATES = {
    CallState.COMPLETED,
    CallState.FAILED,
    CallState.TIMED_OUT,
    CallState.TRANSFERRED,
}


class OwnerContext(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=80)
    callback_number: E164


class TargetContext(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    organization: str | None = Field(default=None, max_length=160)
    phone: E164


class EscalationContext(BaseModel):
    mode: Literal["transfer_to_owner", "end_call"]
    owner_phone: E164


class ContextPacket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: OwnerContext
    target: TargetContext
    objective: str = Field(min_length=1, max_length=4000)
    relevant_facts: list[str] = Field(default_factory=list, max_length=100)
    preferences: list[str] = Field(default_factory=list, max_length=100)
    hard_constraints: list[str] = Field(default_factory=list, max_length=100)
    allowed_commitments: list[str] = Field(default_factory=list, max_length=100)
    prohibited_actions: list[str] = Field(default_factory=list, max_length=100)
    escalation: EscalationContext

    def approved_context_json(self) -> str:
        approved = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        size_bytes = len(approved.encode("utf-8"))
        if size_bytes > CONTEXT_PACKET_MAX_BYTES:
            raise ValueError(
                "ContextPacket compact JSON exceeds "
                f"{CONTEXT_PACKET_MAX_BYTES} UTF-8 bytes (received {size_bytes})"
            )
        return approved

    @model_validator(mode="after")
    def validate_serialized_size(self) -> ContextPacket:
        self.approved_context_json()
        return self


class PreparePhoneCallInput(BaseModel):
    context: ContextPacket
    authority_basis: str | None = Field(default=None, max_length=1000)
    requested_by_owner: bool = False


class PreparePhoneCallOutput(BaseModel):
    plan_id: str | None = None
    confirmation_summary: str
    missing_fields: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class StartPhoneCallOutput(BaseModel):
    call_id: str
    state: CallState
    poll_after_seconds: int = 2
    next_action: str = (
        "Call wait_for_call_event. For each pending question, finish all relevant checks and use "
        "answer_call_question once with only the final, ready-to-relay result, never an "
        "intermediate 'I'm checking' update. Continue waiting with next_after_sequence; when "
        "terminal, call get_call_result."
    )


class EvidenceValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    evidence_turn_ids: list[str] = Field(
        min_length=1,
        description=(
            "Exact turn_id values copied verbatim from the provided transcript entries. "
            "Never invent, alter, or abbreviate an id."
        ),
    )


class Commitment(EvidenceValue):
    status: Literal["confirmed", "proposed", "unknown"] = "unknown"


class FollowUp(EvidenceValue):
    owner_action_required: bool = True


class ExtractedCallResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal[
        "completed",
        "partially_completed",
        "needs_follow_up",
        "declined",
        "voicemail_left",
        "wrong_number",
        "transferred",
        "failed",
        "unknown",
    ]
    summary: str
    commitments: list[Commitment] = Field(default_factory=list)
    confirmation_numbers: list[EvidenceValue] = Field(default_factory=list)
    follow_ups: list[FollowUp] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class StoredCallResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    call_status: Literal["completed", "transferred", "failed", "timed_out"]
    finalization_status: Literal["succeeded", "failed", "telephony_only"]
    outcome: Literal[
        "completed",
        "partially_completed",
        "needs_follow_up",
        "declined",
        "voicemail_left",
        "wrong_number",
        "transferred",
        "failed",
        "unknown",
    ]
    result_source: Literal[
        "realtime_tool", "post_call_extractor", "telephony_only", "extraction_failed"
    ]
    summary: str
    commitments: list[Commitment] = Field(default_factory=list)
    confirmation_numbers: list[EvidenceValue] = Field(default_factory=list)
    follow_ups: list[FollowUp] = Field(default_factory=list)
    answered_by: str | None = None
    answer_handling: str | None = None
    transcript_complete: bool
    raw_transcript_available: bool
    finalized_at: datetime = Field(default_factory=utc_now)


class TranscriptTurn(BaseModel):
    call_id: str
    turn_id: str
    speaker: Literal["assistant", "callee", "owner", "system"]
    text: str
    source_event_type: str
    source_event_id: str
    sequence_number: int
    created_at: datetime = Field(default_factory=utc_now)


class InputTranscription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    delay: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None


class SemanticVad(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["semantic_vad"] = "semantic_vad"
    eagerness: Literal["low", "medium", "high", "auto"] = "auto"
    create_response: bool
    interrupt_response: bool


class RealtimeAudioInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Do not set audio format for SIP: OpenAI negotiates G.711 with the carrier.
    # Explicit format values have been observed to clobber PCMU into PCM and silence the leg.
    transcription: InputTranscription
    turn_detection: SemanticVad


class RealtimeAudioOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice: Literal["cedar"] = "cedar"
    speed: float = Field(default=1.0, ge=0.25, le=1.5)


class RealtimeAudio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: RealtimeAudioInput
    output: RealtimeAudioOutput


class RealtimeFunctionTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["function"] = "function"
    name: Literal[
        "transfer_to_owner",
        "record_call_outcome",
        "search_web",
        "send_dtmf",
        "ask_poke",
        "end_call",
    ]
    description: str = Field(min_length=1)
    parameters: dict[str, Any]


class AcceptPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["realtime"] = "realtime"
    model: Literal["gpt-realtime-2.1"] = "gpt-realtime-2.1"
    reasoning: dict[str, Literal["low"]] = Field(default_factory=lambda: {"effort": "low"})
    output_modalities: list[Literal["audio"]] = Field(default_factory=lambda: ["audio"])
    max_output_tokens: Literal["inf"] = "inf"
    parallel_tool_calls: Literal[True] = True
    tool_choice: Literal["auto"] = "auto"
    # Writes Realtime session activity to the OpenAI Traces dashboard.
    tracing: Literal["auto"] = "auto"
    instructions: str
    audio: RealtimeAudio
    tools: list[RealtimeFunctionTool]


class RealtimeIncomingData(BaseModel):
    call_id: str
    sip_headers: list[dict[str, str]] = Field(default_factory=list)


class RealtimeIncomingEvent(BaseModel):
    type: Literal["realtime.call.incoming"]
    id: str
    data: RealtimeIncomingData


class WebSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=500)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("query must contain at least two non-whitespace characters")
        return normalized


DTMF_DIGITS_PATTERN = re.compile(r"^[0-9*#w]{1,32}$")


class SendDtmfRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    digits: str

    @field_validator("digits")
    @classmethod
    def normalize_digits(cls, value: str) -> str:
        normalized = value.strip()
        if not DTMF_DIGITS_PATTERN.match(normalized):
            raise ValueError("digits must be 1-32 characters from 0-9, *, #, and w")
        return normalized


class QuestionStatus(StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class AskPokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=5, max_length=500)
    reason: str | None = Field(default=None, max_length=200)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 5:
            raise ValueError("question must contain at least five non-whitespace characters")
        return normalized

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None


class AnswerCallQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(min_length=1, max_length=120)
    question_id: str = Field(min_length=1, max_length=120)
    answer: str = Field(min_length=1, max_length=4096)

    @field_validator("answer")
    @classmethod
    def validate_answer(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("answer must be non-empty")
        if len(stripped.encode("utf-8")) > 4096:
            raise ValueError("answer exceeds 4096 UTF-8 bytes")
        return stripped


class AdvisoryOutcome(BaseModel):
    status: str
    summary: str
    commitments: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list, alias="followUps")


class VoiceEndCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal[
        "objective_completed",
        "callee_declined",
        "wrong_number",
        "unable_to_complete",
        "out_of_scope",
    ]


class CallUsage(BaseModel):
    realtime_input_text_tokens: int = 0
    realtime_input_audio_tokens: int = 0
    realtime_input_cached_text_tokens: int = 0
    realtime_input_cached_audio_tokens: int = 0
    realtime_output_text_tokens: int = 0
    realtime_output_audio_tokens: int = 0
    extractor_input_tokens: int = 0
    extractor_output_tokens: int = 0
    exa_search_count: int = 0
    twilio_reported_duration_seconds: int | None = None
    billable_duration_seconds: int | None = None


class CallCost(BaseModel):
    currency: str = "USD"
    estimated: bool = True
    usage: CallUsage
    realtime_cost_usd: float
    extractor_cost_usd: float
    twilio_cost_usd: float
    exa_cost_usd: float
    total_cost_usd: float


class CallSnapshot(BaseModel):
    call_id: str
    state: CallState
    created_at: datetime
    started_at: datetime | None = None
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    answered_by: str | None = None
    answer_handling: str | None = None
    duration_seconds: int | None = None
    result: StoredCallResult | None = None
    cost: CallCost | None = None


class ToolErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ExtractionEnvelope(BaseModel):
    result: ExtractedCallResult

    @model_validator(mode="after")
    def evidence_is_nonempty(self) -> ExtractionEnvelope:
        for group in (
            self.result.commitments,
            self.result.confirmation_numbers,
            self.result.follow_ups,
        ):
            for item in group:
                if not item.evidence_turn_ids:
                    raise ValueError("all extracted claims require transcript evidence")
        return self
