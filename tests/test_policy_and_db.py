from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from pydantic import ValidationError

from app.db import Database, DeploymentLockedError, LatencyMark, LatencyStage
from app.models import CallState, EvidenceValue, PreparePhoneCallInput
from app.policy import validate_context, validate_destination
from app.settings import Settings


async def _pragma_value(connection: aiosqlite.Connection, name: str):
    cursor = await connection.execute(f"PRAGMA {name}")
    row = await cursor.fetchone()
    return row[0]


@pytest.mark.asyncio
async def test_database_reuses_reader_and_writer_with_durable_pragmas(database):
    writer = database._writer
    reader = database._reader
    assert writer is not None
    assert reader is not None
    assert writer is not reader

    for connection in (writer, reader):
        assert await _pragma_value(connection, "journal_mode") == "wal"
        assert await _pragma_value(connection, "synchronous") == 2
        assert await _pragma_value(connection, "foreign_keys") == 1
        assert await _pragma_value(connection, "busy_timeout") == 5000
    assert await _pragma_value(writer, "query_only") == 0
    assert await _pragma_value(reader, "query_only") == 1

    await database.execute("CREATE TABLE connection_reuse (value TEXT NOT NULL)")
    await database.execute("INSERT INTO connection_reuse(value) VALUES (?)", ("visible",))
    assert await database.fetch_one("SELECT value FROM connection_reuse") == {"value": "visible"}
    assert database._writer is writer
    assert database._reader is reader

    # Re-running migrations is idempotent and does not churn live connections.
    await database.initialize()
    assert database._writer is writer
    assert database._reader is reader


@pytest.mark.asyncio
async def test_database_close_is_idempotent_and_initialize_reopens(settings):
    db = Database(settings.database_path)
    await db.initialize()
    first_writer = db._writer
    first_reader = db._reader

    await db.close()
    await db.close()
    assert db._writer is None
    assert db._reader is None
    with pytest.raises(RuntimeError, match="not initialized"):
        await db.fetch_one("SELECT 1")

    await db.initialize()
    try:
        assert db._writer is not first_writer
        assert db._reader is not first_reader
        assert await db.fetch_one("SELECT 1 AS value") == {"value": 1}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_persistent_writer_rolls_back_failed_operation(database):
    await database.execute("CREATE TABLE unique_values (value TEXT PRIMARY KEY)")
    await database.execute("INSERT INTO unique_values(value) VALUES (?)", ("first",))

    with pytest.raises(aiosqlite.IntegrityError):
        await database.execute("INSERT INTO unique_values(value) VALUES (?)", ("first",))

    # The failed transaction cannot poison the next user of the shared writer.
    await database.execute("INSERT INTO unique_values(value) VALUES (?)", ("second",))
    rows = await database.fetch_all("SELECT value FROM unique_values ORDER BY value")
    assert rows == [{"value": "first"}, {"value": "second"}]


@pytest.mark.asyncio
async def test_cancelled_queued_write_rolls_back_before_releasing_writer(database):
    await database.execute("CREATE TABLE cancelled_writes (value TEXT NOT NULL)")
    writer = database._writer
    assert writer is not None

    worker_started = threading.Event()
    release_worker = threading.Event()

    def block_worker() -> int:
        worker_started.set()
        if not release_worker.wait(timeout=5):
            raise TimeoutError("test worker was not released")
        return 1

    await writer.create_function("block_worker", 0, block_worker)

    async def occupy_worker() -> None:
        async with writer.execute("SELECT block_worker()") as cursor:
            await cursor.fetchone()

    blocker = asyncio.create_task(occupy_worker())
    write: asyncio.Task[int] | None = None
    try:
        assert await asyncio.to_thread(worker_started.wait, 1)
        write = asyncio.create_task(
            database.execute("INSERT INTO cancelled_writes(value) VALUES (?)", ("abandoned",))
        )
        for _ in range(100):
            if writer._tx.qsize() >= 1:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("write was not queued behind the blocked SQLite worker")

        write.cancel()
        for _ in range(100):
            if writer._tx.qsize() >= 2:
                break
            await asyncio.sleep(0)

        # The cancelled caller stays inside the write lock until its rollback is queued behind
        # the abandoned INSERT and has actually completed.
        assert not write.done()
    finally:
        release_worker.set()
        await asyncio.gather(blocker, return_exceptions=True)

    assert write is not None
    with pytest.raises(asyncio.CancelledError):
        await write
    assert await database.fetch_all("SELECT value FROM cancelled_writes") == []

    await database.execute("INSERT INTO cancelled_writes(value) VALUES (?)", ("healthy",))
    assert await database.fetch_all("SELECT value FROM cancelled_writes") == [{"value": "healthy"}]


@pytest.mark.asyncio
async def test_initialize_migrates_legacy_opening_column_and_state(database, packet):
    db = database
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    await db.create_plan(
        "plan_legacy",
        packet.model_dump(mode="json"),
        "Owner explicitly requested the call",
        expires_at,
    )
    assert await db.claim_plan_and_create_call(
        plan_id="plan_legacy",
        call_id="call_legacy",
        conference_name="conference_legacy",
        confirmation_text="Confirmed",
    )
    await db.update_call(
        "call_legacy",
        state="greeting_started",
        opening_sent=1,
    )
    await db.execute("ALTER TABLE calls RENAME COLUMN opening_sent TO greeting_sent")

    await db.initialize()

    columns = {row["name"] for row in await db.fetch_all("PRAGMA table_info(calls)")}
    call = await db.get_call("call_legacy")
    assert "opening_sent" in columns
    assert "greeting_sent" not in columns
    assert call["opening_sent"] == 1
    assert call["state"] == CallState.ACTIVE.value


@pytest.mark.asyncio
async def test_initialize_migrates_xai_provider_columns_after_openai_rollback(database, packet):
    db = database
    await db.create_plan(
        "plan_provider_rollback",
        packet.model_dump(mode="json"),
        "Owner explicitly requested the call",
        datetime.now(UTC) + timedelta(minutes=10),
    )
    assert await db.claim_plan_and_create_call(
        plan_id="plan_provider_rollback",
        call_id="call_provider_rollback",
        conference_name="conference_provider_rollback",
        confirmation_text="Confirmed",
    )
    await db.update_call(
        "call_provider_rollback",
        openai_call_id="rtc_provider_rollback",
        openai_accept_status=200,
        semantic_vad_verified=1,
    )

    await db.execute("ALTER TABLE calls RENAME COLUMN openai_call_id TO xai_call_id")
    await db.execute("ALTER TABLE calls RENAME COLUMN openai_accept_status TO xai_connect_status")
    await db.execute("ALTER TABLE calls RENAME COLUMN semantic_vad_verified TO vad_verified")
    # Match production after the first rolled-back startup partially recreated OpenAI columns.
    await db.execute("ALTER TABLE calls ADD COLUMN openai_accept_status INTEGER")
    await db.execute(
        "ALTER TABLE calls ADD COLUMN semantic_vad_verified INTEGER NOT NULL DEFAULT 0"
    )

    await db.initialize()
    await db.initialize()

    columns = {row["name"] for row in await db.fetch_all("PRAGMA table_info(calls)")}
    call = await db.get_call("call_provider_rollback")
    assert "openai_call_id" in columns
    assert "xai_call_id" not in columns
    assert call["openai_call_id"] == "rtc_provider_rollback"
    assert call["openai_accept_status"] == 200
    assert call["semantic_vad_verified"] == 1


@pytest.mark.asyncio
async def test_latency_events_are_migration_safe_idempotent_and_correlated(database, packet):
    db = database
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    await db.create_plan(
        "plan_latency",
        packet.model_dump(mode="json"),
        "Owner explicitly requested the call",
        expires_at,
    )
    assert await db.claim_plan_and_create_call(
        plan_id="plan_latency",
        call_id="call_latency",
        conference_name="conference_latency",
        confirmation_text="Confirmed",
    )

    first = LatencyMark("2026-07-14T12:00:00+00:00", 100)
    later = LatencyMark("2026-07-14T12:00:01+00:00", 200)
    earliest = LatencyMark("2026-07-14T11:59:59+00:00", 50)
    await db.record_latency_events(
        "call_latency",
        [
            (LatencyStage.SIDEBAND_OPEN, first, ""),
            (LatencyStage.TOOL_CALL_RECEIVED, first, "tool_1"),
            (LatencyStage.TOOL_CALL_RECEIVED, later, "tool_2"),
        ],
    )
    await db.record_latency_event(
        "call_latency",
        LatencyStage.SIDEBAND_OPEN,
        later,
    )
    await db.record_latency_event(
        "call_latency",
        LatencyStage.SIDEBAND_OPEN,
        earliest,
    )

    events = await db.get_latency_events("call_latency")
    assert [(event["stage"], event["event_key"]) for event in events] == [
        ("sideband_open", ""),
        ("tool_call_received", "tool_1"),
        ("tool_call_received", "tool_2"),
    ]
    assert events[0]["occurred_at"] == earliest.occurred_at
    assert events[0]["monotonic_ns"] == earliest.monotonic_ns
    assert len({event["clock_id"] for event in events}) == 1

    # Existing databases receive this table through CREATE TABLE IF NOT EXISTS.
    await db.initialize()
    tables = {row["name"] for row in await db.fetch_all("SELECT name FROM sqlite_master")}
    assert "call_latency_events" in tables


@pytest.mark.asyncio
async def test_conference_cleanup_pending_column_and_round_trip(database, packet):
    db = database
    columns = {row["name"] for row in await db.fetch_all("PRAGMA table_info(calls)")}
    assert "conference_cleanup_pending" in columns

    await db.create_plan(
        "plan_cleanup",
        packet.model_dump(mode="json"),
        "Owner explicitly requested the call",
        datetime.now(UTC) + timedelta(minutes=10),
    )
    assert await db.claim_plan_and_create_call(
        plan_id="plan_cleanup",
        call_id="call_cleanup",
        conference_name="conference_cleanup",
        confirmation_text="Confirmed",
    )
    assert (await db.get_call("call_cleanup"))["conference_cleanup_pending"] == 0
    assert await db.list_conference_cleanup_pending() == []

    await db.set_conference_cleanup_pending("call_cleanup", True)
    pending = await db.list_conference_cleanup_pending()
    assert [row["call_id"] for row in pending] == ["call_cleanup"]
    assert (await db.get_call("call_cleanup"))["conference_cleanup_pending"] == 1

    await db.set_conference_cleanup_pending("call_cleanup", False)
    assert await db.list_conference_cleanup_pending() == []


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


def test_exa_api_key_is_required_when_search_tool_is_advertised(settings):
    values = settings.model_dump()
    values["exa_api_key"] = None
    blank = Settings(**values)
    with pytest.raises(RuntimeError, match="EXA_API_KEY"):
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
            authority_basis="Owner asked the agent to place this call",
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
            authority_basis="Owner asked the agent to place this call",
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
async def test_transcript_order_and_source_id_are_idempotent(database):
    db = database
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["prewarming", "ready_to_activate", "activating", "active"],
)
async def test_claim_transfer_joining_accepts_each_eligible_state(database, state):
    db = database
    call_id = f"call_eligible_{state}"
    await db.create_plan(
        f"plan_{call_id}", {}, "authority", datetime.now(UTC) + timedelta(minutes=10)
    )
    assert await db.claim_plan_and_create_call(
        plan_id=f"plan_{call_id}",
        call_id=call_id,
        conference_name=f"conference_{call_id}",
        confirmation_text="confirmed",
    )
    await db.update_call(call_id, state=state, callee_joined=1)

    assert await db.claim_transfer_joining(call_id, "owner needed") is True
    call = await db.get_call(call_id)
    assert call["transfer_outcome"] == "joining:owner needed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["completed", "failed", "timed_out", "transferred", "terminating"],
)
async def test_claim_transfer_joining_rejects_terminal_and_terminating_states(database, state):
    db = database
    call_id = f"call_ineligible_{state}"
    await db.create_plan(
        f"plan_{call_id}", {}, "authority", datetime.now(UTC) + timedelta(minutes=10)
    )
    assert await db.claim_plan_and_create_call(
        plan_id=f"plan_{call_id}",
        call_id=call_id,
        conference_name=f"conference_{call_id}",
        confirmation_text="confirmed",
    )
    await db.update_call(call_id, state=state, callee_joined=1)

    assert await db.claim_transfer_joining(call_id, "owner needed") is False
    call = await db.get_call(call_id)
    assert call["transfer_outcome"] is None


@pytest.mark.asyncio
async def test_claim_transfer_joining_rejects_before_callee_joined(database):
    db = database
    call_id = "call_not_joined"
    await db.create_plan(
        f"plan_{call_id}", {}, "authority", datetime.now(UTC) + timedelta(minutes=10)
    )
    assert await db.claim_plan_and_create_call(
        plan_id=f"plan_{call_id}",
        call_id=call_id,
        conference_name=f"conference_{call_id}",
        confirmation_text="confirmed",
    )
    # PREWARMING is the default state after claim_plan_and_create_call and callee_joined
    # defaults to 0 until the callee actually answers.
    assert await db.claim_transfer_joining(call_id, "owner needed") is False
    call = await db.get_call(call_id)
    assert call["transfer_outcome"] is None


@pytest.mark.asyncio
async def test_deployment_lock_atomically_blocks_new_calls(database):
    db = database
    await db.create_plan(
        "plan_deploy",
        {},
        "authority",
        datetime.now(UTC) + timedelta(minutes=10),
    )

    assert await db.acquire_deployment_lock() == 0
    assert await db.deployment_lock_is_active()
    with pytest.raises(DeploymentLockedError):
        await db.claim_plan_and_create_call(
            plan_id="plan_deploy",
            call_id="call_deploy",
            conference_name="conference_deploy",
            confirmation_text="confirmed",
        )
    assert (await db.get_plan("plan_deploy"))["state"] == "prepared"

    await db.release_deployment_lock()
    assert not await db.deployment_lock_is_active()
    assert await db.claim_plan_and_create_call(
        plan_id="plan_deploy",
        call_id="call_deploy",
        conference_name="conference_deploy",
        confirmation_text="confirmed",
    )
    assert await db.acquire_deployment_lock() == 1
