from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from gen_automation.db.models import (
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    OutboxEvent,
    Project,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    GenerationAttemptState,
    GenerationState,
    OutboxStatus,
    ReleasePhase,
)
from gen_automation.services.outbox import (
    GENERATION_ATTEMPT_AGGREGATE,
    SALAD_JOB_SUBMIT_TOPIC,
    ExternalEffect,
    OutboxConflictError,
    OutboxLeaseLostError,
    OutboxNotFoundError,
    OutboxValidationError,
    _fail_exhausted_salad_attempt_before_submission,
    _mark_salad_attempt_unknown,
    _repair_dead_lettered_unstarted_salad_attempts,
    claim_outbox_events,
    enqueue_outbox_event,
    extend_outbox_lease,
    fail_outbox_event,
    recover_expired_outbox_events,
    succeed_outbox_event,
)
from gen_automation.services.salad_deployments import effective_worker_min_replicas

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    value = Database(f"sqlite+aiosqlite:///{(tmp_path / 'outbox.db').as_posix()}")
    await value.create_schema()
    try:
        yield value
    finally:
        await value.dispose()


async def _enqueue(
    database: Database,
    *,
    topic: str = "internal.test",
    dedupe_key: str = "event-1",
    max_attempts: int = 3,
    aggregate_id: UUID | None = None,
) -> UUID:
    async with database.sessions() as session:
        result = await enqueue_outbox_event(
            session,
            topic=topic,
            dedupe_key=dedupe_key,
            correlation_id="correlation-1",
            aggregate_type="test",
            aggregate_id=aggregate_id or uuid4(),
            payload={"internal_id": "abc"},
            max_attempts=max_attempts,
            now=NOW,
        )
        await session.commit()
        return result.event_id


def _assert_salad_aggregate_lock_order(session: AsyncMock) -> None:
    statements = [call.args[0] for call in session.scalar.await_args_list]
    sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in statements]
    assert "FROM provider_budget_guards" in sql[0]
    assert sql[0].endswith("FOR UPDATE")
    assert "SELECT generation_attempts.job_id" in sql[1]
    assert "FOR UPDATE" not in sql[1]
    assert "FROM generation_jobs" in sql[2]
    assert sql[2].endswith("FOR UPDATE")
    assert "FROM generation_attempts" in sql[3]
    assert sql[3].endswith("FOR UPDATE")


@pytest.mark.asyncio
async def test_unknown_transition_locks_budget_guard_then_job_then_attempt() -> None:
    job_id = uuid4()
    attempt_id = uuid4()
    job = SimpleNamespace(
        id=job_id,
        state=GenerationState.SUBMITTING,
        last_error_code=None,
        last_error_detail=None,
        lock_version=1,
    )
    attempt = SimpleNamespace(
        id=attempt_id,
        job_id=job_id,
        provider="salad",
        state=GenerationAttemptState.SUBMITTING,
        unknown_since=None,
        error_code=None,
        error_detail=None,
        lock_version=1,
    )
    event = SimpleNamespace(
        id=uuid4(),
        topic=SALAD_JOB_SUBMIT_TOPIC,
        aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
        aggregate_id=attempt_id,
        correlation_id="lock-order",
    )
    session = AsyncMock()
    session.scalar.side_effect = [uuid4(), job_id, job, attempt]
    session.add = MagicMock()

    changed = await _mark_salad_attempt_unknown(
        session,
        event=event,
        occurred_at=NOW,
        actor="test",
    )

    assert changed is True
    _assert_salad_aggregate_lock_order(session)


@pytest.mark.asyncio
async def test_definite_failure_locks_budget_guard_then_job_then_attempt() -> None:
    job_id = uuid4()
    attempt_id = uuid4()
    job = SimpleNamespace(
        id=job_id,
        state=GenerationState.CLAIMED,
        attempt_count=1,
        max_attempts=3,
        retry_at=None,
        lease_owner=None,
        lease_expires_at=None,
        last_error_code=None,
        last_error_detail=None,
        lock_version=1,
    )
    attempt = SimpleNamespace(
        id=attempt_id,
        job_id=job_id,
        provider="salad",
        state=GenerationAttemptState.CREATED,
        provider_external_id=None,
        submit_started_at=None,
        submitted_at=None,
        started_at=None,
        last_observed_at=None,
        unknown_since=None,
        provider_state=None,
        response_metadata=None,
        cost_reservation_microusd=0,
        reservation_released_at=None,
        attempt_no=1,
        completed_at=None,
        error_code=None,
        error_detail=None,
        lock_version=1,
    )
    event = SimpleNamespace(
        id=uuid4(),
        topic=SALAD_JOB_SUBMIT_TOPIC,
        aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
        aggregate_id=attempt_id,
        correlation_id="lock-order",
    )
    session = AsyncMock()
    session.scalar.side_effect = [uuid4(), job_id, job, attempt, None]
    session.add = MagicMock()

    changed = await _fail_exhausted_salad_attempt_before_submission(
        session,
        event=event,
        occurred_at=NOW,
        actor="test",
        error_code="definitely_unstarted",
        safe_error_detail="No provider request was sent.",
    )

    assert changed is True
    _assert_salad_aggregate_lock_order(session)


@pytest.mark.asyncio
async def test_legacy_repair_candidate_locks_only_outbox_row_on_postgresql() -> None:
    session = AsyncMock()
    session.scalars.return_value = SimpleNamespace(all=lambda: ())

    await _repair_dead_lettered_unstarted_salad_attempts(
        session,
        now=NOW,
        limit=1,
        actor="test",
    )

    statement = session.scalars.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE OF outbox_events SKIP LOCKED" in sql
    assert "FOR UPDATE OF generation_attempts" not in sql
    assert "FOR UPDATE OF generation_jobs" not in sql


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_rejects_dedupe_conflicts(
    database: Database,
) -> None:
    aggregate_id = uuid4()
    async with database.sessions() as session:
        first = await enqueue_outbox_event(
            session,
            topic="asset.available",
            dedupe_key="asset:123",
            correlation_id="release:123",
            aggregate_type="asset",
            aggregate_id=aggregate_id,
            payload={"asset_id": str(aggregate_id)},
            now=NOW,
        )
        replay = await enqueue_outbox_event(
            session,
            topic="asset.available",
            dedupe_key="asset:123",
            correlation_id="release:123",
            aggregate_type="asset",
            aggregate_id=aggregate_id,
            payload={"asset_id": str(aggregate_id)},
            now=NOW,
        )
        assert first.created is True
        assert replay == type(replay)(event_id=first.event_id, created=False)

        with pytest.raises(OutboxConflictError):
            await enqueue_outbox_event(
                session,
                topic="asset.available",
                dedupe_key="asset:123",
                correlation_id="release:123",
                aggregate_type="asset",
                aggregate_id=aggregate_id,
                payload={"asset_id": "different"},
                now=NOW,
            )
        await session.commit()

    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 1


@pytest.mark.asyncio
async def test_claim_commits_lease_and_success_requires_its_owner(
    database: Database,
) -> None:
    first_id = await _enqueue(database, dedupe_key="first")
    await _enqueue(database, dedupe_key="second")

    async with database.sessions() as session:
        claimed = await claim_outbox_events(
            session,
            worker_id="worker-a",
            limit=1,
            lease_seconds=60,
            now=NOW,
        )
    assert [item.id for item in claimed] == [first_id]
    assert claimed[0].attempt == 1

    async with database.sessions() as session:
        with pytest.raises(OutboxLeaseLostError):
            await succeed_outbox_event(
                session,
                event_id=first_id,
                worker_id="worker-b",
                now=NOW + timedelta(seconds=1),
            )
        await session.rollback()

        extended = await extend_outbox_lease(
            session,
            event_id=first_id,
            worker_id="worker-a",
            lease_seconds=120,
            now=NOW + timedelta(seconds=1),
        )
        assert extended == NOW + timedelta(seconds=121)

        result = await succeed_outbox_event(
            session,
            event_id=first_id,
            worker_id="worker-a",
            now=NOW + timedelta(seconds=2),
        )
        assert result.status == OutboxStatus.SUCCEEDED
        assert result.changed is True
        replay = await succeed_outbox_event(
            session,
            event_id=first_id,
            worker_id="worker-a",
            now=NOW + timedelta(seconds=3),
        )
        assert replay.changed is False

    async with database.sessions() as session:
        event = await session.get(OutboxEvent, first_id)
        assert event is not None
        assert event.processed_at is not None
        assert event.lease_owner is None


@pytest.mark.asyncio
async def test_retry_requires_proof_effect_did_not_start_and_honors_attempt_limit(
    database: Database,
) -> None:
    event_id = await _enqueue(database, max_attempts=2)
    async with database.sessions() as session:
        claimed = await claim_outbox_events(
            session,
            worker_id="worker",
            limit=1,
            lease_seconds=60,
            now=NOW,
        )
        assert claimed[0].id == event_id
        retried = await fail_outbox_event(
            session,
            event_id=event_id,
            worker_id="worker",
            error_code="provider.rate_limited",
            safe_error_detail="No request body was sent.",
            external_effect=ExternalEffect.DEFINITELY_NOT_STARTED,
            retry_not_before=NOW + timedelta(seconds=30),
            now=NOW + timedelta(seconds=1),
        )
        assert retried.status == OutboxStatus.PENDING

        assert (
            await claim_outbox_events(
                session,
                worker_id="worker",
                limit=1,
                lease_seconds=60,
                now=NOW + timedelta(seconds=29),
            )
            == []
        )
        second = await claim_outbox_events(
            session,
            worker_id="worker",
            limit=1,
            lease_seconds=60,
            now=NOW + timedelta(seconds=30),
        )
        assert second[0].attempt == 2

        exhausted = await fail_outbox_event(
            session,
            event_id=event_id,
            worker_id="worker",
            error_code="provider.unavailable",
            safe_error_detail=None,
            external_effect=ExternalEffect.DEFINITELY_NOT_STARTED,
            retry_not_before=NOW + timedelta(minutes=2),
            now=NOW + timedelta(seconds=31),
        )
        assert exhausted.status == OutboxStatus.DEAD_LETTER


@dataclass(frozen=True)
class AttemptContext:
    attempt_id: UUID
    job_id: UUID
    deployment_id: UUID
    outbox_event_id: UUID


async def _create_salad_submit_attempt(
    database: Database,
    *,
    attempt_state: GenerationAttemptState = GenerationAttemptState.SUBMITTING,
    outbox_max_attempts: int = 10,
    job_max_attempts: int = 3,
    release_phase: ReleasePhase = ReleasePhase.DRAFT,
) -> AttemptContext:
    async with database.sessions() as session:
        project = Project(slug="outbox-project", name="Outbox Project")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="outbox-release",
            title="Outbox Release",
            desired_accepted_count=1,
            phase=release_phase,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={"schema_version": 1},
            specification_sha256="a" * 64,
            created_by="test",
            created_at=NOW,
        )
        deployment = SaladDeployment(
            version_no=1,
            config_sha256="b" * 64,
            worker_image_digest="registry.example/worker@" + "c" * 64,
            organization_name="org",
            project_name="project",
            queue_name="queue",
            container_group_name="group",
            is_current=True,
            max_hourly_cost_microusd=1_000_000,
        )
        session.add_all([version, deployment])
        await session.flush()
        job = GenerationJob(
            release_version_id=version.id,
            logical_key="d" * 64,
            parameters={"seed": 1},
            parameters_sha256="e" * 64,
            provider="salad",
            state=(
                GenerationState.CLAIMED
                if attempt_state == GenerationAttemptState.CREATED
                else GenerationState.SUBMITTING
            ),
            expected_output_count=1,
            attempt_count=1,
            max_attempts=job_max_attempts,
        )
        session.add(job)
        await session.flush()
        attempt = GenerationAttempt(
            job_id=job.id,
            salad_deployment_id=deployment.id,
            attempt_no=1,
            provider="salad",
            submission_key="f" * 64,
            request_sha256="1" * 64,
            state=attempt_state,
            worker_image_digest=deployment.worker_image_digest,
            request_metadata={"generation_attempt_id": "internal"},
            submit_started_at=(NOW if attempt_state == GenerationAttemptState.SUBMITTING else None),
            created_at=NOW,
        )
        session.add(attempt)
        await session.flush()
        event = await enqueue_outbox_event(
            session,
            topic=SALAD_JOB_SUBMIT_TOPIC,
            dedupe_key=f"submit:{attempt.id}",
            correlation_id=str(job.id),
            aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
            aggregate_id=attempt.id,
            payload={"generation_attempt_id": str(attempt.id)},
            max_attempts=outbox_max_attempts,
            now=NOW,
        )
        await session.commit()
        return AttemptContext(
            attempt_id=attempt.id,
            job_id=job.id,
            deployment_id=deployment.id,
            outbox_event_id=event.event_id,
        )


@pytest.mark.asyncio
async def test_exhausted_unstarted_salad_failure_repairs_attempt_and_releases_job(
    database: Database,
) -> None:
    context = await _create_salad_submit_attempt(
        database,
        attempt_state=GenerationAttemptState.CREATED,
        outbox_max_attempts=1,
    )
    async with database.sessions() as session:
        claimed = await claim_outbox_events(
            session,
            worker_id="worker",
            limit=1,
            lease_seconds=60,
            now=NOW,
        )
        assert claimed[0].id == context.outbox_event_id
        result = await fail_outbox_event(
            session,
            event_id=context.outbox_event_id,
            worker_id="worker",
            error_code="provider_unavailable",
            safe_error_detail="No provider request was sent.",
            external_effect=ExternalEffect.DEFINITELY_NOT_STARTED,
            retry_not_before=NOW + timedelta(minutes=1),
            now=NOW + timedelta(seconds=1),
        )
        assert result.status == OutboxStatus.DEAD_LETTER

    async with database.sessions() as session:
        event = await session.get(OutboxEvent, context.outbox_event_id)
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert event is not None
        assert attempt is not None
        assert job is not None
        assert event.status == OutboxStatus.DEAD_LETTER
        assert attempt.state == GenerationAttemptState.FAILED
        assert attempt.completed_at is not None
        assert attempt.error_code == "provider_unavailable"
        assert job.state == GenerationState.RETRY_WAIT
        assert job.retry_at is not None


@pytest.mark.asyncio
async def test_claim_repairs_preexisting_exhausted_unstarted_salad_event(
    database: Database,
) -> None:
    context = await _create_salad_submit_attempt(
        database,
        attempt_state=GenerationAttemptState.CREATED,
        outbox_max_attempts=1,
    )
    async with database.sessions() as session:
        event = await session.get(OutboxEvent, context.outbox_event_id)
        assert event is not None
        event.attempts = event.max_attempts
        await session.commit()

    async with database.sessions() as session:
        claimed = await claim_outbox_events(
            session,
            worker_id="repair-worker",
            limit=1,
            lease_seconds=60,
            now=NOW,
        )
        assert claimed == []

    async with database.sessions() as session:
        event = await session.get(OutboxEvent, context.outbox_event_id)
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert event is not None
        assert attempt is not None
        assert job is not None
        assert event.status == OutboxStatus.DEAD_LETTER
        assert event.last_error_code == "attempt_limit_exhausted"
        assert attempt.state == GenerationAttemptState.FAILED
        assert attempt.error_code == "outbox_attempt_limit_exhausted"
        assert job.state == GenerationState.RETRY_WAIT


@pytest.mark.asyncio
async def test_claim_repairs_legacy_dead_letter_with_clean_created_aggregate(
    database: Database,
) -> None:
    context = await _create_salad_submit_attempt(
        database,
        attempt_state=GenerationAttemptState.CREATED,
    )
    async with database.sessions() as session:
        event = await session.get(OutboxEvent, context.outbox_event_id)
        assert event is not None
        event.status = OutboxStatus.DEAD_LETTER
        event.processed_at = NOW
        event.last_error_code = "legacy_controller_failure"
        await session.commit()

    async with database.sessions() as session:
        claimed = await claim_outbox_events(
            session,
            worker_id="repair-worker",
            limit=1,
            lease_seconds=60,
            topics={SALAD_JOB_SUBMIT_TOPIC},
            now=NOW + timedelta(seconds=1),
        )
        assert claimed == []

    async with database.sessions() as session:
        event = await session.get(OutboxEvent, context.outbox_event_id)
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert event is not None
        assert attempt is not None
        assert job is not None
        assert event.status == OutboxStatus.DEAD_LETTER
        assert attempt.state == GenerationAttemptState.FAILED
        assert attempt.error_code == "outbox_dead_lettered_before_submission"
        assert job.state == GenerationState.RETRY_WAIT


@pytest.mark.asyncio
async def test_claim_repairs_terminal_markerless_submitting_legacy_dead_letter(
    database: Database,
) -> None:
    context = await _create_salad_submit_attempt(database)
    async with database.sessions() as session:
        event = await session.get(OutboxEvent, context.outbox_event_id)
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert event is not None
        assert attempt is not None
        assert job is not None
        event.status = OutboxStatus.DEAD_LETTER
        event.processed_at = NOW
        event.last_error_code = "legacy_controller_failure"
        attempt.submit_started_at = None
        job.state = GenerationState.CANCELLED
        await session.commit()

    async with database.sessions() as session:
        claimed = await claim_outbox_events(
            session,
            worker_id="repair-worker",
            limit=1,
            lease_seconds=60,
            topics={SALAD_JOB_SUBMIT_TOPIC},
            now=NOW + timedelta(seconds=1),
        )
        assert claimed == []

    async with database.sessions() as session:
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert attempt is not None
        assert job is not None
        assert attempt.state == GenerationAttemptState.FAILED
        assert attempt.error_code == "outbox_dead_lettered_before_submission"
        assert job.state == GenerationState.CANCELLED


@pytest.mark.asyncio
async def test_legacy_repair_does_not_rewind_parent_with_newer_running_attempt(
    database: Database,
) -> None:
    context = await _create_salad_submit_attempt(
        database,
        attempt_state=GenerationAttemptState.CREATED,
    )
    async with database.sessions() as session:
        event = await session.get(OutboxEvent, context.outbox_event_id)
        old_attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert event is not None
        assert old_attempt is not None
        assert job is not None
        event.status = OutboxStatus.DEAD_LETTER
        event.processed_at = NOW
        event.last_error_code = "legacy_controller_failure"
        newer_attempt = GenerationAttempt(
            job_id=job.id,
            salad_deployment_id=old_attempt.salad_deployment_id,
            attempt_no=2,
            provider="salad",
            provider_external_id="provider-running-attempt",
            submission_key="2" * 64,
            request_sha256="3" * 64,
            state=GenerationAttemptState.RUNNING,
            worker_image_digest=old_attempt.worker_image_digest,
            request_metadata={"generation_attempt_id": "newer"},
            provider_state="running",
            submit_started_at=NOW,
            submitted_at=NOW,
            started_at=NOW,
            last_observed_at=NOW,
            created_at=NOW,
        )
        session.add(newer_attempt)
        job.attempt_count = 2
        job.state = GenerationState.RUNNING
        await session.commit()
        newer_attempt_id = newer_attempt.id

    async with database.sessions() as session:
        assert (
            await claim_outbox_events(
                session,
                worker_id="repair-worker",
                limit=1,
                lease_seconds=60,
                topics={SALAD_JOB_SUBMIT_TOPIC},
                now=NOW + timedelta(seconds=1),
            )
            == []
        )

    async with database.sessions() as session:
        old_attempt = await session.get(GenerationAttempt, context.attempt_id)
        newer_attempt = await session.get(GenerationAttempt, newer_attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert old_attempt is not None
        assert newer_attempt is not None
        assert job is not None
        assert old_attempt.state == GenerationAttemptState.FAILED
        assert newer_attempt.state == GenerationAttemptState.RUNNING
        assert job.state == GenerationState.RUNNING
        assert job.attempt_count == 2


@pytest.mark.asyncio
async def test_legacy_repair_skips_unrepairable_head_row_with_bounded_limit(
    database: Database,
) -> None:
    repairable = await _create_salad_submit_attempt(
        database,
        attempt_state=GenerationAttemptState.CREATED,
    )
    async with database.sessions() as session:
        repairable_event = await session.get(OutboxEvent, repairable.outbox_event_id)
        repairable_job = await session.get(GenerationJob, repairable.job_id)
        repairable_attempt = await session.get(GenerationAttempt, repairable.attempt_id)
        assert repairable_event is not None
        assert repairable_job is not None
        assert repairable_attempt is not None
        repairable_event.status = OutboxStatus.DEAD_LETTER
        repairable_event.processed_at = NOW

        unrepairable_job = GenerationJob(
            release_version_id=repairable_job.release_version_id,
            logical_key="9" * 64,
            parameters={"seed": 9},
            parameters_sha256="8" * 64,
            provider="salad",
            state=GenerationState.RUNNING,
            expected_output_count=1,
            attempt_count=1,
            max_attempts=3,
        )
        session.add(unrepairable_job)
        await session.flush()
        unrepairable_attempt = GenerationAttempt(
            job_id=unrepairable_job.id,
            salad_deployment_id=repairable_attempt.salad_deployment_id,
            attempt_no=1,
            provider="salad",
            submission_key="7" * 64,
            request_sha256="6" * 64,
            state=GenerationAttemptState.CREATED,
            worker_image_digest=repairable_attempt.worker_image_digest,
            request_metadata={"generation_attempt_id": "unrepairable"},
            created_at=NOW - timedelta(seconds=1),
        )
        session.add(unrepairable_attempt)
        await session.flush()
        enqueued = await enqueue_outbox_event(
            session,
            topic=SALAD_JOB_SUBMIT_TOPIC,
            dedupe_key=f"submit:{unrepairable_attempt.id}",
            correlation_id=str(unrepairable_job.id),
            aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
            aggregate_id=unrepairable_attempt.id,
            payload={"generation_attempt_id": str(unrepairable_attempt.id)},
            now=NOW - timedelta(seconds=1),
        )
        unrepairable_event = await session.get(OutboxEvent, enqueued.event_id)
        assert unrepairable_event is not None
        unrepairable_event.status = OutboxStatus.DEAD_LETTER
        unrepairable_event.processed_at = NOW - timedelta(seconds=1)
        await session.commit()
        unrepairable_attempt_id = unrepairable_attempt.id

    async with database.sessions() as session:
        await _repair_dead_lettered_unstarted_salad_attempts(
            session,
            now=NOW + timedelta(seconds=1),
            limit=1,
            actor="repair-worker",
        )
        await session.commit()

    async with database.sessions() as session:
        repaired = await session.get(GenerationAttempt, repairable.attempt_id)
        untouched = await session.get(GenerationAttempt, unrepairable_attempt_id)
        assert repaired is not None
        assert untouched is not None
        assert repaired.state == GenerationAttemptState.FAILED
        assert untouched.state == GenerationAttemptState.CREATED


@pytest.mark.asyncio
async def test_expired_salad_submit_is_never_replayed_and_attempt_becomes_unknown(
    database: Database,
) -> None:
    context = await _create_salad_submit_attempt(database)
    async with database.sessions() as session:
        claim = await claim_outbox_events(
            session,
            worker_id="crashed-worker",
            limit=1,
            lease_seconds=10,
            now=NOW,
        )
        assert claim[0].id == context.outbox_event_id

    async with database.sessions() as session:
        claimed = await claim_outbox_events(
            session,
            worker_id="replacement-worker",
            limit=1,
            lease_seconds=60,
            definitely_safe_to_replay_topics={SALAD_JOB_SUBMIT_TOPIC},
            now=NOW + timedelta(seconds=11),
        )
        assert claimed == []

    async with database.sessions() as session:
        event = await session.get(OutboxEvent, context.outbox_event_id)
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        assert event is not None
        assert attempt is not None
        job = await session.get(GenerationJob, attempt.job_id)
        assert job is not None
        assert event.status == OutboxStatus.DEAD_LETTER
        assert event.last_error_code == "ambiguous_external_effect"
        assert attempt.state == GenerationAttemptState.UNKNOWN
        assert attempt.unknown_since is not None
        assert attempt.last_observed_at is None
        assert attempt.error_code == "submit_outcome_unknown"
        assert job.state == GenerationState.UNKNOWN


@pytest.mark.parametrize(
    ("job_max_attempts", "expected_job_state"),
    [
        (3, GenerationState.RETRY_WAIT),
        (1, GenerationState.DEAD_LETTER),
    ],
)
@pytest.mark.asyncio
async def test_expired_clean_created_salad_submit_fails_locally_without_worker_demand(
    database: Database,
    job_max_attempts: int,
    expected_job_state: GenerationState,
) -> None:
    context = await _create_salad_submit_attempt(
        database,
        attempt_state=GenerationAttemptState.CREATED,
        job_max_attempts=job_max_attempts,
        release_phase=ReleasePhase.GENERATING,
    )
    async with database.sessions() as session:
        claim = await claim_outbox_events(
            session,
            worker_id="crashed-worker",
            limit=1,
            lease_seconds=10,
            now=NOW,
        )
        assert claim[0].id == context.outbox_event_id
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=context.deployment_id,
                now=NOW + timedelta(seconds=1),
            )
            == 1
        )

    async with database.sessions() as session:
        summary = await recover_expired_outbox_events(
            session,
            definitely_safe_to_replay_topics={SALAD_JOB_SUBMIT_TOPIC},
            now=NOW + timedelta(seconds=11),
        )
        assert summary.replayed == 0
        assert summary.dead_lettered == 1
        assert summary.attempts_marked_unknown == 0

    async with database.sessions() as session:
        event = await session.get(OutboxEvent, context.outbox_event_id)
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert event is not None
        assert attempt is not None
        assert job is not None
        assert event.status == OutboxStatus.DEAD_LETTER
        assert event.last_error_code == "lease_expired_before_effect"
        assert attempt.state == GenerationAttemptState.FAILED
        assert attempt.unknown_since is None
        assert attempt.error_code == "outbox_lease_expired_before_submission"
        assert job.state == expected_job_state
        assert (job.retry_at is not None) is (expected_job_state == GenerationState.RETRY_WAIT)
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=context.deployment_id,
                now=NOW + timedelta(seconds=11),
            )
            == 0
        )


@pytest.mark.asyncio
async def test_expired_created_salad_submit_with_remote_job_state_stays_ambiguous(
    database: Database,
) -> None:
    context = await _create_salad_submit_attempt(
        database,
        attempt_state=GenerationAttemptState.CREATED,
    )
    async with database.sessions() as session:
        job = await session.get(GenerationJob, context.job_id)
        assert job is not None
        job.state = GenerationState.RUNNING
        await session.commit()
        claim = await claim_outbox_events(
            session,
            worker_id="crashed-worker",
            limit=1,
            lease_seconds=10,
            now=NOW,
        )
        assert claim[0].id == context.outbox_event_id

    async with database.sessions() as session:
        summary = await recover_expired_outbox_events(
            session,
            now=NOW + timedelta(seconds=11),
        )
        assert summary.attempts_marked_unknown == 1

    async with database.sessions() as session:
        event = await session.get(OutboxEvent, context.outbox_event_id)
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert event is not None
        assert attempt is not None
        assert job is not None
        assert event.last_error_code == "ambiguous_external_effect"
        assert attempt.state == GenerationAttemptState.UNKNOWN
        assert job.state == GenerationState.UNKNOWN


@pytest.mark.asyncio
async def test_expired_clean_created_salad_submit_with_terminal_parent_fails_attempt_only(
    database: Database,
) -> None:
    context = await _create_salad_submit_attempt(
        database,
        attempt_state=GenerationAttemptState.CREATED,
    )
    async with database.sessions() as session:
        claim = await claim_outbox_events(
            session,
            worker_id="crashed-worker",
            limit=1,
            lease_seconds=10,
            now=NOW,
        )
        assert claim[0].id == context.outbox_event_id
        job = await session.get(GenerationJob, context.job_id)
        assert job is not None
        job.state = GenerationState.CANCELLED
        await session.commit()

    async with database.sessions() as session:
        summary = await recover_expired_outbox_events(
            session,
            now=NOW + timedelta(seconds=11),
        )
        assert summary.attempts_marked_unknown == 0

    async with database.sessions() as session:
        event = await session.get(OutboxEvent, context.outbox_event_id)
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert event is not None
        assert attempt is not None
        assert job is not None
        assert event.last_error_code == "lease_expired_before_effect"
        assert attempt.state == GenerationAttemptState.FAILED
        assert attempt.unknown_since is None
        assert job.state == GenerationState.CANCELLED


@pytest.mark.asyncio
async def test_expired_markerless_submitting_orphan_with_terminal_parent_fails_attempt_only(
    database: Database,
) -> None:
    context = await _create_salad_submit_attempt(database)
    async with database.sessions() as session:
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert attempt is not None
        assert job is not None
        attempt.submit_started_at = None
        job.state = GenerationState.CANCELLED
        await session.commit()
        claim = await claim_outbox_events(
            session,
            worker_id="crashed-worker",
            limit=1,
            lease_seconds=10,
            now=NOW,
        )
        assert claim[0].id == context.outbox_event_id

    async with database.sessions() as session:
        summary = await recover_expired_outbox_events(
            session,
            now=NOW + timedelta(seconds=11),
        )
        assert summary.attempts_marked_unknown == 0

    async with database.sessions() as session:
        event = await session.get(OutboxEvent, context.outbox_event_id)
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert event is not None
        assert attempt is not None
        assert job is not None
        assert event.last_error_code == "lease_expired_before_effect"
        assert attempt.state == GenerationAttemptState.FAILED
        assert attempt.unknown_since is None
        assert job.state == GenerationState.CANCELLED


@pytest.mark.asyncio
async def test_ambiguous_salad_failure_dead_letters_even_when_retry_was_requested(
    database: Database,
) -> None:
    context = await _create_salad_submit_attempt(database)
    async with database.sessions() as session:
        await claim_outbox_events(
            session,
            worker_id="worker",
            limit=1,
            lease_seconds=60,
            now=NOW,
        )
        result = await fail_outbox_event(
            session,
            event_id=context.outbox_event_id,
            worker_id="worker",
            error_code="provider.timeout",
            safe_error_detail="The provider response was not observed.",
            external_effect=ExternalEffect.MAY_HAVE_STARTED,
            retry_not_before=NOW + timedelta(minutes=1),
            now=NOW + timedelta(seconds=1),
        )
        assert result.status == OutboxStatus.DEAD_LETTER

    async with database.sessions() as session:
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        assert attempt is not None
        assert attempt.state == GenerationAttemptState.UNKNOWN


@pytest.mark.asyncio
async def test_expired_generic_event_replays_only_when_explicitly_proven_safe(
    database: Database,
) -> None:
    event_id = await _enqueue(database, topic="internal.safe")
    async with database.sessions() as session:
        await claim_outbox_events(
            session,
            worker_id="first-worker",
            limit=1,
            lease_seconds=10,
            now=NOW,
        )
        summary = await recover_expired_outbox_events(
            session,
            definitely_safe_to_replay_topics={"internal.safe"},
            now=NOW + timedelta(seconds=11),
        )
        assert summary.replayed == 1
        assert summary.dead_lettered == 0

        claimed = await claim_outbox_events(
            session,
            worker_id="second-worker",
            limit=1,
            lease_seconds=60,
            now=NOW + timedelta(seconds=11),
        )
        assert claimed[0].id == event_id
        assert claimed[0].attempt == 2


@pytest.mark.asyncio
async def test_expired_generic_event_dead_letters_by_default(database: Database) -> None:
    event_id = await _enqueue(database, topic="external.unknown")
    async with database.sessions() as session:
        await claim_outbox_events(
            session,
            worker_id="crashed-worker",
            limit=1,
            lease_seconds=10,
            now=NOW,
        )
        summary = await recover_expired_outbox_events(
            session,
            now=NOW + timedelta(seconds=11),
        )
        assert summary.replayed == 0
        assert summary.dead_lettered == 1

    async with database.sessions() as session:
        event = await session.get(OutboxEvent, event_id)
        assert event is not None
        assert event.status == OutboxStatus.DEAD_LETTER


@pytest.mark.asyncio
async def test_claim_filters_topics_and_validates_worker_controls(database: Database) -> None:
    await _enqueue(database, topic="topic.a", dedupe_key="a")
    selected_id = await _enqueue(database, topic="topic.b", dedupe_key="b")
    async with database.sessions() as session:
        assert (
            await claim_outbox_events(
                session,
                worker_id="worker",
                limit=1,
                lease_seconds=30,
                topics=set(),
                now=NOW,
            )
            == []
        )
        claimed = await claim_outbox_events(
            session,
            worker_id="worker",
            limit=1,
            lease_seconds=30,
            topics={"topic.b"},
            now=NOW,
        )
        assert claimed[0].id == selected_id

        with pytest.raises(OutboxValidationError):
            await claim_outbox_events(
                session,
                worker_id="worker",
                limit=0,
                lease_seconds=30,
                now=NOW,
            )
        with pytest.raises(OutboxValidationError):
            await claim_outbox_events(
                session,
                worker_id="worker",
                limit=1,
                lease_seconds=0,
                now=NOW,
            )
        with pytest.raises(OutboxValidationError):
            await recover_expired_outbox_events(session, limit=0, now=NOW)
        with pytest.raises(OutboxValidationError):
            await extend_outbox_lease(
                session,
                event_id=selected_id,
                worker_id="worker",
                lease_seconds=0,
                now=NOW,
            )


@pytest.mark.asyncio
async def test_stale_and_invalid_transitions_fail_closed(database: Database) -> None:
    event_id = await _enqueue(database)
    async with database.sessions() as session:
        await claim_outbox_events(
            session,
            worker_id="worker",
            limit=1,
            lease_seconds=10,
            now=NOW,
        )
        with pytest.raises(OutboxLeaseLostError):
            await succeed_outbox_event(
                session,
                event_id=event_id,
                worker_id="worker",
                now=NOW + timedelta(seconds=11),
            )
        await session.rollback()

        with pytest.raises(OutboxValidationError):
            await fail_outbox_event(
                session,
                event_id=event_id,
                worker_id="worker",
                error_code="INVALID CODE",
                safe_error_detail=None,
                external_effect=ExternalEffect.DEFINITELY_NOT_STARTED,
                retry_not_before=None,
                now=NOW + timedelta(seconds=1),
            )
        with pytest.raises(OutboxValidationError):
            await fail_outbox_event(
                session,
                event_id=event_id,
                worker_id="worker",
                error_code="provider.failed",
                safe_error_detail="safe",
                external_effect=ExternalEffect.DEFINITELY_NOT_STARTED,
                retry_not_before=NOW,
                now=NOW + timedelta(seconds=1),
            )

        result = await fail_outbox_event(
            session,
            event_id=event_id,
            worker_id="worker",
            error_code="provider.ambiguous",
            safe_error_detail="No durable result was observed.",
            external_effect=ExternalEffect.MAY_HAVE_STARTED,
            retry_not_before=NOW + timedelta(minutes=1),
            now=NOW + timedelta(seconds=1),
        )
        assert result.status == OutboxStatus.DEAD_LETTER
        replay = await fail_outbox_event(
            session,
            event_id=event_id,
            worker_id="worker",
            error_code="provider.ambiguous",
            safe_error_detail=None,
            external_effect=ExternalEffect.MAY_HAVE_STARTED,
            retry_not_before=None,
            now=NOW + timedelta(seconds=2),
        )
        assert replay.changed is False

        with pytest.raises(OutboxNotFoundError):
            await succeed_outbox_event(
                session,
                event_id=uuid4(),
                worker_id="worker",
                now=NOW,
            )
