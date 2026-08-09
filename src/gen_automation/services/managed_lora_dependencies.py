"""Reference checks used by managed-LoRA retirement and safe purge."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    ExperimentWarmLease,
    GenerationAttempt,
    GenerationJob,
    ManagedLoraArtifact,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.domain.enums import (
    ExperimentWarmLeaseState,
    GenerationAttemptState,
    GenerationState,
)
from gen_automation.domain.lora_catalog import LoraDependencySummary

_ACTIVE_JOB_STATES = frozenset(
    {
        GenerationState.QUEUED,
        GenerationState.CLAIMED,
        GenerationState.SUBMITTING,
        GenerationState.RUNNING,
        GenerationState.COLLECTING,
        GenerationState.VERIFYING,
        GenerationState.UNKNOWN,
        GenerationState.RETRY_WAIT,
        GenerationState.CANCEL_REQUESTED,
    }
)
_ACTIVE_ATTEMPT_STATES = frozenset(
    {
        GenerationAttemptState.CREATED,
        GenerationAttemptState.SUBMITTING,
        GenerationAttemptState.SUBMITTED,
        GenerationAttemptState.RUNNING,
        GenerationAttemptState.UNKNOWN,
        GenerationAttemptState.CANCEL_REQUESTED,
    }
)
_LIVE_WARM_STATES = frozenset(
    {
        ExperimentWarmLeaseState.STARTING,
        ExperimentWarmLeaseState.ACTIVE,
        ExperimentWarmLeaseState.ENDING,
    }
)


async def managed_lora_dependency_summary(
    session: AsyncSession,
    artifact_id: UUID,
) -> LoraDependencySummary:
    sha256 = await session.scalar(
        select(ManagedLoraArtifact.artifact_sha256).where(ManagedLoraArtifact.id == artifact_id)
    )
    if sha256 is None:
        return LoraDependencySummary()

    active_jobs = list(
        (
            await session.scalars(
                select(GenerationJob).where(GenerationJob.state.in_(_ACTIVE_JOB_STATES))
            )
        ).all()
    )
    matching_job_ids = {
        job.id for job in active_jobs if _parameters_reference_sha(job.parameters, sha256)
    }
    active_attempt_ids: set[UUID] = set()
    if matching_job_ids:
        active_attempt_ids.update(
            (
                await session.scalars(
                    select(GenerationAttempt.id).where(
                        GenerationAttempt.job_id.in_(matching_job_ids),
                        GenerationAttempt.state.in_(_ACTIVE_ATTEMPT_STATES),
                    )
                )
            ).all()
        )

    # A deployment rollover can leave active work on a non-current deployment.
    # That work may use another LoRA from the same resident manifest, so job
    # parameters alone cannot prove that removing this artifact is safe. Read
    # attempts and their deployment residency in one lock-free statement: the
    # lifecycle caller may already hold the managed-artifact row, while provider
    # controllers use the budget -> deployment -> attempt lock order.
    resident_attempt_rows = list(
        (
            await session.execute(
                select(
                    GenerationAttempt.id,
                    SaladDeployment.runtime_managed_lora_sha256s,
                )
                .join(
                    SaladDeployment,
                    SaladDeployment.id == GenerationAttempt.salad_deployment_id,
                )
                .where(GenerationAttempt.state.in_(_ACTIVE_ATTEMPT_STATES))
            )
        ).all()
    )
    active_attempt_ids.update(
        attempt_id
        for attempt_id, resident in resident_attempt_rows
        if _resident_manifest_references_sha(resident, sha256)
    )
    warm_runtime_hashes = list(
        (
            await session.scalars(
                select(SaladDeployment.runtime_managed_lora_sha256s)
                .join(
                    ExperimentWarmLease,
                    ExperimentWarmLease.salad_deployment_id == SaladDeployment.id,
                )
                .where(ExperimentWarmLease.state.in_(_LIVE_WARM_STATES))
            )
        ).all()
    )
    warm_leases = sum(
        _resident_manifest_references_sha(resident, sha256) for resident in warm_runtime_hashes
    )
    return LoraDependencySummary(
        queued_generation_jobs=len(matching_job_ids),
        active_generation_attempts=len(active_attempt_ids),
        warm_experiment_leases=warm_leases,
    )


async def managed_lora_has_historical_reference(
    session: AsyncSession,
    *,
    sha256: str,
) -> bool:
    specifications = await session.scalars(select(ReleaseVersion.specification))
    return any(_specification_references_sha(value, sha256) for value in specifications)


async def managed_lora_historical_reference_sha256s(
    session: AsyncSession,
    *,
    sha256s: Iterable[str],
) -> frozenset[str]:
    """Return managed hashes frozen into at least one historical release version."""

    candidates = frozenset(sha256s)
    if not candidates:
        return frozenset()
    specifications = list((await session.scalars(select(ReleaseVersion.specification))).all())
    return frozenset(
        sha256
        for sha256 in candidates
        if any(_specification_references_sha(value, sha256) for value in specifications)
    )


async def runtime_has_active_work(session: AsyncSession) -> bool:
    attempt = await session.scalar(
        select(GenerationAttempt.id)
        .where(GenerationAttempt.state.in_(_ACTIVE_ATTEMPT_STATES))
        .limit(1)
    )
    if attempt is not None:
        return True
    lease = await session.scalar(
        select(ExperimentWarmLease.id)
        .where(ExperimentWarmLease.state.in_(_LIVE_WARM_STATES))
        .limit(1)
    )
    return lease is not None


def _parameters_reference_sha(parameters: object, sha256: str) -> bool:
    if not isinstance(parameters, dict):
        return False
    return _loras_reference_sha(parameters.get("loras"), sha256)


def _specification_references_sha(specification: object, sha256: str) -> bool:
    if not isinstance(specification, dict):
        return False
    return _loras_reference_sha(specification.get("loras"), sha256)


def _loras_reference_sha(value: object, sha256: str) -> bool:
    if not isinstance(value, list):
        return False
    return any(isinstance(item, dict) and item.get("sha256") == sha256 for item in value)


def _resident_manifest_references_sha(value: object, sha256: str) -> bool:
    if not isinstance(value, list):
        return False
    return any(item == sha256 for item in value)
