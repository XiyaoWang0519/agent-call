from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.db import Database
from app.models import EvidenceValue, PreparePhoneCallInput
from app.policy import validate_context, validate_destination
from app.settings import Settings


@pytest.mark.parametrize(
    "phone",
    ["+1911", "+1933", "+1988", "+1211", "+19005550123", "14155550100"],
)
def test_blocklisted_destinations_are_rejected(settings, phone):
    assert not validate_destination(phone, settings).allowed


def test_outside_country_allowlist_is_rejected(settings):
    assert not validate_destination("+442079460018", settings).allowed


def test_context_owner_callback_must_match_configured_owner(settings, packet):
    changed = packet.model_copy(deep=True)
    changed.owner.callback_number = "+14155550102"
    changed.escalation.owner_phone = "+14155550102"
    errors = validate_context(changed, settings)
    assert {error.code for error in errors} == {"configured_owner_phone_mismatch"}


def test_context_owner_identity_is_fixed_for_single_user_service(settings, packet):
    changed = packet.model_copy(deep=True)
    changed.owner.display_name = "Someone Else"
    assert "owner_identity_mismatch" in {
        error.code for error in validate_context(changed, settings)
    }


@pytest.mark.parametrize(
    "sensitive_fact",
    [
        "The card number is 4111 1111 1111 1111.",
        "Use bank account 123456789.",
        "Their government ID is A12345.",
    ],
)
def test_sensitive_context_is_rejected(settings, packet, sensitive_fact):
    changed = packet.model_copy(deep=True)
    changed.relevant_facts.append(sensitive_fact)
    assert "sensitive_data" in {error.code for error in validate_context(changed, settings)}


def test_blank_required_runtime_values_are_missing(settings):
    values = settings.model_dump()
    values["public_base_url"] = ""
    blank = Settings(**values)
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        blank.require_runtime_configuration()


@pytest.mark.parametrize(
    "url",
    ["http://example.test", "https://example.test/a-path", "https://example.test?x=1"],
)
def test_public_base_url_must_be_exact_https_origin(settings, url):
    values = settings.model_dump()
    values["public_base_url"] = url
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        Settings(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("setup_deadline_seconds", 61),
        ("watchdog_stale_seconds", 16),
        ("plan_ttl_seconds", 601),
        ("max_call_seconds", 601),
    ],
)
def test_v1_timing_invariants_cannot_be_overridden(settings, field, value):
    values = settings.model_dump()
    values[field] = value
    with pytest.raises(ValueError):
        Settings(**values)


@pytest.mark.asyncio
async def test_prepare_requires_authority_and_owner_request(service, packet):
    output = await service.prepare(
        PreparePhoneCallInput(context=packet, authority_basis=None, requested_by_owner=False)
    )
    assert output.plan_id is None
    assert output.missing_fields == ["requested_by_owner", "authority_basis"]
    assert service._test_twilio.agent_creates == 0


@pytest.mark.asyncio
async def test_plan_can_only_be_claimed_once(service, packet):
    prepared = await service.prepare(
        PreparePhoneCallInput(
            context=packet,
            authority_basis="Owner asked Poke to place this call",
            requested_by_owner=True,
        )
    )
    first = await service.start(
        prepared.plan_id,
        explicit_confirmation=True,
        confirmation_text=prepared.confirmation_summary,
    )
    with pytest.raises(ValueError):
        await service.start(
            prepared.plan_id,
            explicit_confirmation=True,
            confirmation_text=prepared.confirmation_summary,
        )
    assert first.state.value == "prewarming"
    assert service._test_twilio.agent_creates == 1


@pytest.mark.asyncio
async def test_start_rejects_confirmation_text_from_another_plan(service, packet):
    prepared = await service.prepare(
        PreparePhoneCallInput(
            context=packet,
            authority_basis="Owner asked Poke to place this call",
            requested_by_owner=True,
        )
    )
    with pytest.raises(ValueError, match="confirmation_mismatch"):
        await service.start(
            prepared.plan_id,
            explicit_confirmation=True,
            confirmation_text="Yes, make some call.",
        )
    assert service._test_twilio.agent_creates == 0
    assert (await service.db.get_plan(prepared.plan_id))["state"] == "prepared"


def test_confirmation_number_requires_evidence():
    with pytest.raises(ValidationError):
        EvidenceValue(value="ABC-123", evidence_turn_ids=[])


@pytest.mark.asyncio
async def test_transcript_order_and_source_id_are_idempotent(settings):
    db = Database(settings.database_path)
    await db.initialize()
    # Minimal rows for the transcript FK.
    await db.create_plan(
        "plan_db",
        {},
        "authority",
        datetime.now(UTC) + timedelta(minutes=10),
    )
    assert await db.claim_plan_and_create_call(
        plan_id="plan_db",
        call_id="call_db",
        conference_name="conference_db",
        confirmation_text="confirmed",
    )
    first = await db.add_transcript_turn(
        call_id="call_db",
        turn_id="turn_1",
        speaker="callee",
        text="Hello",
        source_event_type="transcription.completed",
        source_event_id="evt_1",
    )
    duplicate = await db.add_transcript_turn(
        call_id="call_db",
        turn_id="turn_1_dup",
        speaker="callee",
        text="Hello",
        source_event_type="transcription.completed",
        source_event_id="evt_1",
    )
    second = await db.add_transcript_turn(
        call_id="call_db",
        turn_id="turn_2",
        speaker="assistant",
        text="Hi",
        source_event_type="audio_transcript.done",
        source_event_id="evt_2",
    )
    assert first.sequence_number == 1
    assert duplicate is None
    assert second.sequence_number == 2
