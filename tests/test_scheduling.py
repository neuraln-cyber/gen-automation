from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from gen_automation.db.models import (
    ComplianceCheck,
    GenerationAttempt,
    GenerationJob,
    OutboxEvent,
    Project,
    Release,
    ReleaseVersion,
    SaladDeployment,
    SubjectApproval,
)
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    ApprovalStatus,
    ComplianceResult,
    DesiredDeploymentState,
    GenerationAttemptState,
    GenerationState,
    ReleasePhase,
    ResourceHealth,
    SaladDeploymentState,
)
from gen_automation.domain.release_spec import ReleaseSpecification
from gen_automation.services.compliance import validate_release_approvals
from gen_automation.services.scheduling import (
    GenerationSchedulingConflictError,
    dispatch_generation_jobs,
)
from tests.factories import seed_release_approvals, valid_release_payload

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class SchedulingContext:
    database: Database
    release_id: UUID
    deployment_id: UUID
    job_ids: tuple[UUID, ...]


@pytest.fixture
async def scheduling_context(tmp_path: Path) -> AsyncIterator[SchedulingContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'scheduling.db').as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        payload = valid_release_payload()
        specification = ReleaseSpecification.model_validate(payload["specification"])
        await seed_release_approvals(session, payload)
        approval_snapshot = await validate_release_approvals(session, specification)
        project = Project(slug="scheduling", name="Scheduling")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="release",
            title="Release",
            phase=ReleasePhase.READY,
            desired_accepted_count=2,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification=specification.model_dump(mode="json"),
            specification_sha256=canonical_sha256(specification),
            created_by="test",
            created_at=NOW,
        )
        session.add(version)
        await session.flush()
        session.add_all(
            [
                ComplianceCheck(
                    release_version_id=version.id,
                    check_type=check_type,
                    result=ComplianceResult.PASSED,
                    evidence=approval_snapshot.checks[check_type],
                    checked_by="test",
                    checked_at=NOW,
                )
                for check_type in (
                    "adult_subject_gate",
                    "artifact_license_gate",
                    "workflow_integrity_gate",
                )
            ]
        )
        jobs: list[GenerationJob] = []
        for index in range(3):
            parameters = {
                "schema_version": 1,
                "ordinal": index,
                "approval_snapshot_sha256": approval_snapshot.sha256,
            }
            job = GenerationJob(
                release_version_id=version.id,
                logical_key=f"{index + 1:064x}",
                parameters=parameters,
                parameters_sha256=canonical_sha256(parameters),
                provider="salad",
                state=GenerationState.QUEUED,
                priority=100 + index,
                expected_output_count=2,
                attempt_count=0,
                max_attempts=3,
            )
            session.add(job)
            jobs.append(job)
        deployment = SaladDeployment(
            version_no=1,
            config_sha256="b" * 64,
            provider_configuration={},
            worker_image_digest=f"registry.example/worker@sha256:{'c' * 64}",
            organization_name="organization",
            project_name="project",
            queue_name="queue-v1",
            provider_queue_id="queue-id",
            container_group_name="group-v1",
            provider_container_group_id="group-id",
            state=SaladDeploymentState.ACTIVE,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=True,
            min_replicas=0,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=1_000_000,
        )
        session.add(deployment)
        await session.commit()
        result = SchedulingContext(
            database=database,
            release_id=release.id,
            deployment_id=deployment.id,
            job_ids=tuple(job.id for job in jobs),
        )
    try:
        yield result
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_dispatch_prepares_stable_outbox_work_up_to_capacity(
    scheduling_context: SchedulingContext,
) -> None:
    async with scheduling_context.database.sessions() as session:
        result = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=2,
            now=NOW,
        )

    assert len(result.dispatched) == 2
    assert result.available_slots == 0
    assert [item.generation_job_id for item in result.dispatched] == list(
        scheduling_context.job_ids[:2]
    )
    async with scheduling_context.database.sessions() as session:
        attempts = list(
            (
                await session.scalars(
                    select(GenerationAttempt).order_by(GenerationAttempt.created_at)
                )
            ).all()
        )
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
        release = await session.get(Release, scheduling_context.release_id)

    assert len(attempts) == 2
    assert all(attempt.state == GenerationAttemptState.CREATED for attempt in attempts)
    assert outbox_count == 2
    assert release is not None
    assert release.phase == ReleasePhase.GENERATING


@pytest.mark.asyncio
async def test_dispatch_preserves_frozen_generation_queue_order_for_equal_priority_jobs(
    scheduling_context: SchedulingContext,
) -> None:
    ordinals = (2, 0, 1)
    async with scheduling_context.database.sessions() as session:
        for job_id, ordinal in zip(scheduling_context.job_ids, ordinals, strict=True):
            job = await session.get(GenerationJob, job_id)
            assert job is not None
            parameters = {**job.parameters, "ordinal": ordinal}
            job.parameters = parameters
            job.parameters_sha256 = canonical_sha256(parameters)
            job.priority = 100
        await session.commit()

    async with scheduling_context.database.sessions() as session:
        result = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=3,
            now=NOW,
        )

    assert [item.generation_job_id for item in result.dispatched] == [
        scheduling_context.job_ids[1],
        scheduling_context.job_ids[2],
        scheduling_context.job_ids[0],
    ]


@pytest.mark.asyncio
async def test_dispatch_respects_existing_inflight_capacity(
    scheduling_context: SchedulingContext,
) -> None:
    async with scheduling_context.database.sessions() as session:
        first = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=1,
            now=NOW,
        )
    async with scheduling_context.database.sessions() as session:
        second = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=1,
            now=NOW,
        )

    assert len(first.dispatched) == 1
    assert second.available_slots == 0
    assert second.dispatched == ()


@pytest.mark.asyncio
async def test_dispatch_ignores_clean_created_attempt_owned_by_terminal_job(
    scheduling_context: SchedulingContext,
) -> None:
    async with scheduling_context.database.sessions() as session:
        first = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=1,
            now=NOW,
        )
        assert len(first.dispatched) == 1

    async with scheduling_context.database.sessions() as session:
        job = await session.get(GenerationJob, scheduling_context.job_ids[0])
        attempt = await session.scalar(
            select(GenerationAttempt).where(
                GenerationAttempt.job_id == scheduling_context.job_ids[0]
            )
        )
        assert job is not None
        assert attempt is not None
        assert attempt.state == GenerationAttemptState.CREATED
        job.state = GenerationState.CANCELLED
        await session.commit()

    async with scheduling_context.database.sessions() as session:
        second = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=1,
            now=NOW,
        )

    assert [item.generation_job_id for item in second.dispatched] == [scheduling_context.job_ids[1]]
    assert second.available_slots == 0


@pytest.mark.asyncio
async def test_dispatch_ignores_markerless_submitting_orphan_from_terminal_job(
    scheduling_context: SchedulingContext,
) -> None:
    async with scheduling_context.database.sessions() as session:
        first = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=1,
            now=NOW,
        )
        assert len(first.dispatched) == 1

    async with scheduling_context.database.sessions() as session:
        job = await session.get(GenerationJob, scheduling_context.job_ids[0])
        attempt = await session.scalar(
            select(GenerationAttempt).where(
                GenerationAttempt.job_id == scheduling_context.job_ids[0]
            )
        )
        assert job is not None
        assert attempt is not None
        job.state = GenerationState.CANCELLED
        attempt.state = GenerationAttemptState.SUBMITTING
        await session.commit()

    async with scheduling_context.database.sessions() as session:
        second = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=1,
            now=NOW,
        )

    assert [item.generation_job_id for item in second.dispatched] == [scheduling_context.job_ids[1]]


@pytest.mark.asyncio
async def test_dispatch_keeps_marked_submitting_attempt_inflight_after_parent_terminal(
    scheduling_context: SchedulingContext,
) -> None:
    async with scheduling_context.database.sessions() as session:
        first = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=1,
            now=NOW,
        )
        assert len(first.dispatched) == 1

    async with scheduling_context.database.sessions() as session:
        job = await session.get(GenerationJob, scheduling_context.job_ids[0])
        attempt = await session.scalar(
            select(GenerationAttempt).where(
                GenerationAttempt.job_id == scheduling_context.job_ids[0]
            )
        )
        assert job is not None
        assert attempt is not None
        job.state = GenerationState.CANCELLED
        attempt.state = GenerationAttemptState.SUBMITTING
        attempt.submit_started_at = NOW
        await session.commit()

    async with scheduling_context.database.sessions() as session:
        second = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=1,
            now=NOW,
        )

    assert second.dispatched == ()
    assert second.available_slots == 0


@pytest.mark.asyncio
async def test_dispatch_ignores_clean_created_attempt_from_superseded_release_version(
    scheduling_context: SchedulingContext,
) -> None:
    async with scheduling_context.database.sessions() as session:
        first = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=1,
            now=NOW,
        )
        assert len(first.dispatched) == 1

    async with scheduling_context.database.sessions() as session:
        release = await session.get(Release, scheduling_context.release_id)
        old_version = await session.scalar(
            select(ReleaseVersion).where(ReleaseVersion.release_id == scheduling_context.release_id)
        )
        old_job = await session.get(GenerationJob, scheduling_context.job_ids[0])
        assert release is not None
        assert old_version is not None
        assert old_job is not None
        new_version = ReleaseVersion(
            release_id=release.id,
            version_no=2,
            specification=old_version.specification,
            specification_sha256=old_version.specification_sha256,
            created_by="test",
            created_at=NOW + timedelta(seconds=1),
        )
        session.add(new_version)
        await session.flush()
        checks = list(
            (
                await session.scalars(
                    select(ComplianceCheck).where(
                        ComplianceCheck.release_version_id == old_version.id
                    )
                )
            ).all()
        )
        session.add_all(
            [
                ComplianceCheck(
                    release_version_id=new_version.id,
                    check_type=check.check_type,
                    result=check.result,
                    evidence=check.evidence,
                    checked_by="test",
                    checked_at=NOW + timedelta(seconds=1),
                )
                for check in checks
            ]
        )
        parameters = {**old_job.parameters, "ordinal": 0}
        new_job = GenerationJob(
            release_version_id=new_version.id,
            logical_key="9" * 64,
            parameters=parameters,
            parameters_sha256=canonical_sha256(parameters),
            provider="salad",
            state=GenerationState.QUEUED,
            priority=50,
            expected_output_count=2,
            attempt_count=0,
            max_attempts=3,
        )
        session.add(new_job)
        release.current_version_no = 2
        await session.commit()
        new_job_id = new_job.id

    async with scheduling_context.database.sessions() as session:
        second = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=1,
            now=NOW + timedelta(seconds=2),
        )

    assert [item.generation_job_id for item in second.dispatched] == [new_job_id]


@pytest.mark.asyncio
async def test_dispatch_keeps_ambiguous_provider_attempt_as_inflight_after_parent_terminal(
    scheduling_context: SchedulingContext,
) -> None:
    async with scheduling_context.database.sessions() as session:
        first = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=1,
            now=NOW,
        )
        assert len(first.dispatched) == 1

    async with scheduling_context.database.sessions() as session:
        job = await session.get(GenerationJob, scheduling_context.job_ids[0])
        attempt = await session.scalar(
            select(GenerationAttempt).where(
                GenerationAttempt.job_id == scheduling_context.job_ids[0]
            )
        )
        assert job is not None
        assert attempt is not None
        job.state = GenerationState.CANCELLED
        attempt.state = GenerationAttemptState.UNKNOWN
        attempt.unknown_since = NOW
        await session.commit()

    async with scheduling_context.database.sessions() as session:
        second = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=1,
            now=NOW,
        )

    assert second.dispatched == ()
    assert second.available_slots == 0


@pytest.mark.asyncio
async def test_dispatch_waits_for_retry_time_and_release_phase(
    scheduling_context: SchedulingContext,
) -> None:
    async with scheduling_context.database.sessions() as session:
        first = await session.get(GenerationJob, scheduling_context.job_ids[0])
        second = await session.get(GenerationJob, scheduling_context.job_ids[1])
        release = await session.get(Release, scheduling_context.release_id)
        assert first is not None
        assert second is not None
        assert release is not None
        first.state = GenerationState.RETRY_WAIT
        first.retry_at = NOW + timedelta(minutes=5)
        second.state = GenerationState.RETRY_WAIT
        second.retry_at = NOW - timedelta(seconds=1)
        release.phase = ReleasePhase.PAUSED
        await session.commit()

        paused = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=3,
            now=NOW,
        )
        assert paused.dispatched == ()

        release = await session.get(Release, scheduling_context.release_id)
        assert release is not None
        release.phase = ReleasePhase.GENERATING
        await session.commit()
        ready = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=3,
            now=NOW,
        )

    assert [item.generation_job_id for item in ready.dispatched] == [
        scheduling_context.job_ids[1],
        scheduling_context.job_ids[2],
    ]


@pytest.mark.asyncio
async def test_dispatch_fails_closed_for_stopped_deployment(
    scheduling_context: SchedulingContext,
) -> None:
    async with scheduling_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, scheduling_context.deployment_id)
        assert deployment is not None
        deployment.desired_state = DesiredDeploymentState.STOPPED
        await session.commit()

        with pytest.raises(GenerationSchedulingConflictError, match="not dispatchable"):
            await dispatch_generation_jobs(
                session,
                salad_deployment_id=scheduling_context.deployment_id,
                gpu_allocation_enabled=True,
                max_inflight=2,
                now=NOW,
            )


@pytest.mark.asyncio
async def test_dispatch_allows_exact_provider_start_pending_state(
    scheduling_context: SchedulingContext,
) -> None:
    async with scheduling_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, scheduling_context.deployment_id)
        assert deployment is not None
        deployment.state = SaladDeploymentState.PROVISIONING
        deployment.last_error_code = "provider_start_pending"
        await session.commit()

        result = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=2,
            now=NOW,
        )

    assert len(result.dispatched) == 2


@pytest.mark.asyncio
async def test_dispatch_requires_feature_flag_health_and_all_compliance_checks(
    scheduling_context: SchedulingContext,
) -> None:
    async with scheduling_context.database.sessions() as session:
        with pytest.raises(GenerationSchedulingConflictError, match="disabled"):
            await dispatch_generation_jobs(
                session,
                salad_deployment_id=scheduling_context.deployment_id,
                gpu_allocation_enabled=False,
                max_inflight=2,
                now=NOW,
            )

        release = await session.get(Release, scheduling_context.release_id)
        assert release is not None
        release.health = ResourceHealth.BLOCKED
        await session.commit()
        blocked = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=2,
            now=NOW,
        )
        assert blocked.dispatched == ()

        release = await session.get(Release, scheduling_context.release_id)
        assert release is not None
        release.health = ResourceHealth.HEALTHY
        version = await session.scalar(
            select(ReleaseVersion).where(ReleaseVersion.release_id == release.id)
        )
        assert version is not None
        session.add(
            ComplianceCheck(
                release_version_id=version.id,
                check_type="adult_subject_gate",
                result=ComplianceResult.FAILED,
                evidence={"reason": "revoked"},
                checked_by="test",
                checked_at=NOW + timedelta(seconds=1),
            )
        )
        await session.commit()
        noncompliant = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=2,
            now=NOW,
        )

    assert noncompliant.dispatched == ()


@pytest.mark.asyncio
async def test_dispatch_revalidates_current_subject_registry_status(
    scheduling_context: SchedulingContext,
) -> None:
    async with scheduling_context.database.sessions() as session:
        subject = await session.scalar(
            select(SubjectApproval).where(SubjectApproval.is_current.is_(True)).with_for_update()
        )
        assert subject is not None
        subject.is_current = False
        await session.flush()
        session.add(
            SubjectApproval(
                slug=subject.slug,
                display_name=subject.display_name,
                canonical_source_url=subject.canonical_source_url,
                canonical_source_sha256=subject.canonical_source_sha256,
                canonical_age=subject.canonical_age,
                clearly_adult=subject.clearly_adult,
                is_fictional=subject.is_fictional,
                is_aged_up_minor=subject.is_aged_up_minor,
                distribution_rights_approved=subject.distribution_rights_approved,
                adult_derivative_rights_approved=(subject.adult_derivative_rights_approved),
                evidence=subject.evidence,
                evidence_sha256=subject.evidence_sha256,
                status=ApprovalStatus.REVOKED,
                is_current=True,
                approval_version=subject.approval_version + 1,
                approved_by_user_id=subject.approved_by_user_id,
                approved_at=subject.approved_at,
                revoked_by_user_id=subject.approved_by_user_id,
                revoked_at=subject.approved_at + timedelta(seconds=1),
            )
        )
        await session.commit()

    async with scheduling_context.database.sessions() as session:
        result = await dispatch_generation_jobs(
            session,
            salad_deployment_id=scheduling_context.deployment_id,
            gpu_allocation_enabled=True,
            max_inflight=2,
            now=NOW + timedelta(seconds=2),
        )
        release = await session.get(Release, scheduling_context.release_id)
        revisions = list(
            (
                await session.scalars(
                    select(SubjectApproval).order_by(SubjectApproval.approval_version)
                )
            ).all()
        )

    assert result.dispatched == ()
    assert release is not None
    assert release.health == ResourceHealth.BLOCKED
    assert [revision.status for revision in revisions] == [
        ApprovalStatus.APPROVED,
        ApprovalStatus.REVOKED,
    ]
    assert [revision.is_current for revision in revisions] == [False, True]
