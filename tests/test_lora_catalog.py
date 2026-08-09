from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AdminUser,
    AuditEvent,
    LoraImportJob,
    ManagedLoraArtifact,
    ModelArtifactApproval,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AdminRole,
    ApprovalStatus,
    LoraImportJobState,
    LoraImportSource,
    ManagedLoraLifecycle,
    ModelArtifactKind,
)
from gen_automation.domain.lora_catalog import (
    CivitaiLoraImportCreate,
    LoraDependencySummary,
    ManualLoraImportCreate,
    ManualUploadCompletion,
    VerifiedLoraArtifact,
)
from gen_automation.services.lora_catalog import (
    LoraCatalogConflictError,
    cancel_lora_import_job,
    claim_next_lora_import_job,
    complete_lora_import_job,
    complete_static_lora_import_duplicate,
    create_civitai_import_job,
    create_manual_import_job,
    fail_lora_import_job,
    list_lora_import_jobs,
    list_managed_loras,
    mark_managed_lora_active,
    mark_managed_lora_purged,
    mark_managed_lora_retired,
    mark_manual_upload_complete,
    recover_exhausted_lora_import_lease,
    restore_managed_lora,
    retire_managed_lora,
    retry_lora_import_job,
)

NOW = datetime(2026, 8, 9, 14, 0, tzinfo=UTC)
MODEL_BUCKET = "gen-automation-staging-model-artifacts"
SHA = "a" * 64
FINAL_KEY = f"worker/managed-loras/sha256/{SHA}.safetensors"


@pytest.fixture
async def lora_database(
    tmp_path: Path,
) -> AsyncIterator[tuple[Database, UUID, UUID]]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'lora-catalog.db').as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        owner = _user("owner@example.test", AdminRole.OWNER)
        reviewer = _user("reviewer@example.test", AdminRole.REVIEWER)
        session.add_all((owner, reviewer))
        await session.commit()
        owner_id = owner.id
        reviewer_id = reviewer.id
    try:
        yield database, owner_id, reviewer_id
    finally:
        await database.dispose()


def _user(username: str, role: AdminRole) -> AdminUser:
    return AdminUser(
        username_normalized=username,
        display_name=username,
        password_hash="disabled-test-password-hash",  # noqa: S106
        role=role,
        is_active=True,
        failed_login_count=0,
        password_changed_at=NOW,
        credential_version=1,
        lock_version=1,
    )


def _manual() -> ManualLoraImportCreate:
    return ManualLoraImportCreate(
        display_name="Akali style",
        canonical_source_url="https://models.example.test/akali-lora",
        license_url="https://models.example.test/akali-lora/license",
        commercial_use_attested=True,
        adult_use_attested=True,
        target_filename="akali-style.safetensors",
        expected_sha256=SHA,
        expected_byte_size=1_024,
        expected_metadata={"operator_note": "Owner supplied file."},
        trigger_words=["akali style"],
    )


def _civitai() -> CivitaiLoraImportCreate:
    return CivitaiLoraImportCreate(
        display_name="Akali Civitai",
        canonical_source_url="https://civitai.com/models/1234/akali",
        license_url="https://civitai.com/models/1234/akali",
        commercial_use_attested=True,
        adult_use_attested=True,
        target_filename="akali-civitai.safetensors",
        civitai_model_id=1234,
        civitai_version_id=5678,
        civitai_file_id=9012,
        expected_sha256=SHA,
        expected_byte_size=1_024,
        expected_metadata={"provider": "civitai"},
        trigger_words=["akali"],
    )


def _approval(
    owner_id: UUID,
    *,
    artifact_sha256: str = SHA,
    storage_key: str | None = None,
    source_url: str = "https://models.example.test/akali-lora",
    license_url: str = "https://models.example.test/akali-lora/license",
) -> ModelArtifactApproval:
    resolved_storage_key = storage_key or (
        f"worker/managed-loras/sha256/{artifact_sha256}.safetensors"
    )
    return ModelArtifactApproval(
        artifact_sha256=artifact_sha256,
        name="Akali style",
        kind=ModelArtifactKind.LORA,
        source_url=source_url,
        storage_key=resolved_storage_key,
        license_url=license_url,
        commercial_use_approved=True,
        adult_use_approved=True,
        safetensors_verified=True,
        evidence={"summary": "Test approval"},
        evidence_sha256="b" * 64,
        status=ApprovalStatus.APPROVED,
        is_current=True,
        approval_version=1,
        approved_by_user_id=owner_id,
        approved_at=NOW,
        revoked_by_user_id=None,
        revoked_at=None,
    )


async def _no_dependencies(
    _session: object,
    _artifact_id: UUID,
) -> LoraDependencySummary:
    return LoraDependencySummary()


async def _complete_civitai_artifact(
    session: AsyncSession,
    *,
    owner_id: UUID,
    command: CivitaiLoraImportCreate,
    worker_id: str,
    key_prefix: str,
    object_version_id: str,
    object_etag: str,
    now: datetime,
) -> tuple[UUID, UUID, UUID]:
    artifact_sha256 = command.expected_sha256
    byte_size = command.expected_byte_size
    assert artifact_sha256 is not None
    assert byte_size is not None
    created = await create_civitai_import_job(
        session,
        command=command,
        actor_user_id=owner_id,
        idempotency_key=f"{key_prefix}-create",
        now=now,
    )
    claim = await claim_next_lora_import_job(
        session,
        worker_id=worker_id,
        lease_seconds=300,
        now=now + timedelta(seconds=1),
    )
    assert claim is not None
    assert claim.job.job_id == created.job.job_id
    approval = _approval(
        owner_id,
        artifact_sha256=artifact_sha256,
        source_url=str(command.canonical_source_url),
        license_url=str(command.license_url),
    )
    session.add(approval)
    await session.flush()
    completed = await complete_lora_import_job(
        session,
        job_id=created.job.job_id,
        verified=VerifiedLoraArtifact(
            artifact_sha256=artifact_sha256,
            storage_bucket=MODEL_BUCKET,
            object_key=(f"worker/managed-loras/sha256/{artifact_sha256}.safetensors"),
            object_version_id=object_version_id,
            object_etag=object_etag,
            byte_size=byte_size,
            approval_id=approval.id,
            provenance={"safetensors_header_verified": True},
        ),
        worker_id=worker_id,
        expected_attempt=1,
        now=now + timedelta(seconds=2),
    )
    assert completed.job.state == LoraImportJobState.COMPLETED
    assert completed.job.result_artifact_id is not None
    return completed.job.result_artifact_id, approval.id, created.job.job_id


async def test_manual_job_uses_server_owned_key_replays_and_freezes_exact_version(
    lora_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, reviewer_id = lora_database
    async with database.sessions() as session:
        created = await create_manual_import_job(
            session,
            command=_manual(),
            model_bucket=MODEL_BUCKET,
            actor_user_id=owner_id,
            idempotency_key="manual-create",
            now=NOW,
        )
    assert created.job.state == LoraImportJobState.AWAITING_UPLOAD
    assert created.job.staging_bucket == MODEL_BUCKET
    assert created.job.staging_object_key == (
        f"onboarding/loras/{created.job.job_id}/source.safetensors"
    )

    async with database.sessions() as session:
        replay = await create_manual_import_job(
            session,
            command=_manual(),
            model_bucket=MODEL_BUCKET,
            actor_user_id=owner_id,
            idempotency_key="manual-create",
            now=NOW,
        )
        frozen = await mark_manual_upload_complete(
            session,
            job_id=created.job.job_id,
            completion=ManualUploadCompletion(
                object_version_id="version-1",
                object_etag="c" * 32,
                byte_size=1_024,
            ),
            expected_lock_version=1,
            actor_user_id=owner_id,
            idempotency_key="manual-upload-complete",
            now=NOW + timedelta(minutes=1),
        )
    assert replay.replayed is True
    assert replay.job.job_id == created.job.job_id
    assert frozen.job.state == LoraImportJobState.QUEUED
    assert frozen.job.staging_object_version_id == "version-1"
    assert frozen.job.staging_object_etag == "c" * 32
    assert frozen.job.lock_version == 2

    async with database.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(LoraImportJob))
        audit_count = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.resource_id == created.job.job_id)
        )
        assert count == 1
        assert audit_count == 2
        with pytest.raises(LoraCatalogConflictError, match="owner or administrator"):
            await list_lora_import_jobs(session, actor_user_id=reviewer_id)


async def test_verified_completion_catalog_lifecycle_and_no_hard_delete(
    lora_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, _ = lora_database
    async with database.sessions() as session:
        created = await create_manual_import_job(
            session,
            command=_manual(),
            model_bucket=MODEL_BUCKET,
            actor_user_id=owner_id,
            idempotency_key="managed-create",
            now=NOW,
        )
    async with database.sessions() as session:
        await mark_manual_upload_complete(
            session,
            job_id=created.job.job_id,
            completion=ManualUploadCompletion(
                object_version_id="staging-version",
                object_etag="d" * 32,
                byte_size=1_024,
            ),
            expected_lock_version=1,
            actor_user_id=owner_id,
            idempotency_key="managed-upload",
            now=NOW + timedelta(minutes=1),
        )
    async with database.sessions() as session:
        claim = await claim_next_lora_import_job(
            session,
            worker_id="lora-worker-1",
            lease_seconds=300,
            now=NOW + timedelta(minutes=2),
        )
    assert claim is not None
    async with database.sessions() as session:
        approval = _approval(owner_id)
        session.add(approval)
        await session.flush()
        completed = await complete_lora_import_job(
            session,
            job_id=created.job.job_id,
            verified=VerifiedLoraArtifact(
                artifact_sha256=SHA,
                storage_bucket=MODEL_BUCKET,
                object_key=FINAL_KEY,
                object_version_id="managed-version-1",
                object_etag="e" * 32,
                byte_size=1_024,
                approval_id=approval.id,
                provenance={"safetensors_header_verified": True},
            ),
            worker_id="lora-worker-1",
            expected_attempt=1,
            now=NOW + timedelta(minutes=3),
        )
    assert completed.job.state == LoraImportJobState.COMPLETED
    assert completed.job.result_artifact_id is not None

    async with database.sessions() as session:
        artifacts = await list_managed_loras(session, actor_user_id=owner_id)
        assert len(artifacts) == 1
        artifact = artifacts[0]
        assert artifact.lifecycle == ManagedLoraLifecycle.PENDING_ACTIVATION
        active = await mark_managed_lora_active(
            session,
            artifact_id=artifact.artifact_id,
            expected_lock_version=1,
            worker_id="catalog-reconciler",
            idempotency_key="activate-1",
            now=NOW + timedelta(minutes=4),
        )
    assert active.artifact.lifecycle == ManagedLoraLifecycle.ACTIVE

    async def dependencies(
        _session: object,
        _artifact_id: UUID,
    ) -> LoraDependencySummary:
        return LoraDependencySummary(queued_generation_jobs=2)

    async with database.sessions() as session:
        retiring = await retire_managed_lora(
            session,
            artifact_id=artifact.artifact_id,
            expected_lock_version=2,
            purge_requested=False,
            dependency_summary_hook=dependencies,
            actor_user_id=owner_id,
            idempotency_key="retire-1",
            now=NOW + timedelta(minutes=5),
        )
        assert retiring.dependencies.has_dependencies is True
        assert retiring.artifact.lifecycle == ManagedLoraLifecycle.RETIRING
        with pytest.raises(LoraCatalogConflictError, match="runtime dependencies"):
            await mark_managed_lora_retired(
                session,
                artifact_id=artifact.artifact_id,
                expected_lock_version=3,
                dependency_summary_hook=dependencies,
                worker_id="catalog-reconciler",
                idempotency_key="retired-blocked",
                now=NOW + timedelta(minutes=6),
            )
        restored = await restore_managed_lora(
            session,
            artifact_id=artifact.artifact_id,
            expected_lock_version=3,
            dependency_summary_hook=_no_dependencies,
            actor_user_id=owner_id,
            idempotency_key="restore-1",
            now=NOW + timedelta(minutes=7),
        )
    assert restored.artifact.lifecycle == ManagedLoraLifecycle.ACTIVE

    async with database.sessions() as session:
        await retire_managed_lora(
            session,
            artifact_id=artifact.artifact_id,
            expected_lock_version=4,
            purge_requested=True,
            dependency_summary_hook=_no_dependencies,
            actor_user_id=owner_id,
            idempotency_key="retire-purge",
            now=NOW + timedelta(minutes=9),
        )
        retired = await mark_managed_lora_retired(
            session,
            artifact_id=artifact.artifact_id,
            expected_lock_version=5,
            dependency_summary_hook=_no_dependencies,
            worker_id="catalog-reconciler",
            idempotency_key="retired-2",
            now=NOW + timedelta(minutes=10),
        )
        assert retired.artifact.lifecycle == ManagedLoraLifecycle.RETIRED
        with pytest.raises(LoraCatalogConflictError, match="exact LoRA version"):
            await mark_managed_lora_purged(
                session,
                artifact_id=artifact.artifact_id,
                expected_lock_version=6,
                deleted_object_key=FINAL_KEY,
                deleted_object_version_id="wrong-version",
                worker_id="catalog-reconciler",
                idempotency_key="purge-wrong",
                now=NOW + timedelta(minutes=11),
            )
        purged = await mark_managed_lora_purged(
            session,
            artifact_id=artifact.artifact_id,
            expected_lock_version=6,
            deleted_object_key=FINAL_KEY,
            deleted_object_version_id="managed-version-1",
            worker_id="catalog-reconciler",
            idempotency_key="purge-exact",
            now=NOW + timedelta(minutes=12),
        )
        assert purged.artifact.lifecycle == ManagedLoraLifecycle.PURGED

        stored = await session.get(ManagedLoraArtifact, artifact.artifact_id)
        assert stored is not None
        await session.delete(stored)
        with pytest.raises(IntegrityError, match="managed LoRAs cannot be deleted"):
            await session.commit()


async def test_never_activated_retiring_restore_returns_to_pending_activation(
    lora_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, _ = lora_database
    async with database.sessions() as session:
        artifact_id, _, _ = await _complete_civitai_artifact(
            session,
            owner_id=owner_id,
            command=_civitai(),
            worker_id="lora-worker-pending-restore",
            key_prefix="pending-restore",
            object_version_id="pending-restore-version",
            object_etag="1" * 32,
            now=NOW,
        )
        retiring = await retire_managed_lora(
            session,
            artifact_id=artifact_id,
            expected_lock_version=1,
            purge_requested=False,
            dependency_summary_hook=_no_dependencies,
            actor_user_id=owner_id,
            idempotency_key="pending-restore-retire",
            now=NOW + timedelta(minutes=1),
        )
        restored = await restore_managed_lora(
            session,
            artifact_id=artifact_id,
            expected_lock_version=2,
            dependency_summary_hook=_no_dependencies,
            actor_user_id=owner_id,
            idempotency_key="pending-restore-restore",
            now=NOW + timedelta(minutes=2),
        )

    assert retiring.artifact.lifecycle == ManagedLoraLifecycle.RETIRING
    assert retiring.artifact.activated_at is None
    assert restored.artifact.lifecycle == ManagedLoraLifecycle.PENDING_ACTIVATION
    assert restored.artifact.lock_version == 3


async def test_retired_restore_requires_pending_activation_before_reinstall(
    lora_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, _ = lora_database
    async with database.sessions() as session:
        artifact_id, _, _ = await _complete_civitai_artifact(
            session,
            owner_id=owner_id,
            command=_civitai(),
            worker_id="lora-worker-retired-restore",
            key_prefix="retired-restore",
            object_version_id="retired-restore-version",
            object_etag="2" * 32,
            now=NOW,
        )
        activated = await mark_managed_lora_active(
            session,
            artifact_id=artifact_id,
            expected_lock_version=1,
            worker_id="catalog-reconciler-retired-restore",
            idempotency_key="retired-restore-activate",
            now=NOW + timedelta(seconds=3),
        )
        await retire_managed_lora(
            session,
            artifact_id=artifact_id,
            expected_lock_version=2,
            purge_requested=False,
            dependency_summary_hook=_no_dependencies,
            actor_user_id=owner_id,
            idempotency_key="retired-restore-retire",
            now=NOW + timedelta(minutes=1),
        )
        retired = await mark_managed_lora_retired(
            session,
            artifact_id=artifact_id,
            expected_lock_version=3,
            dependency_summary_hook=_no_dependencies,
            worker_id="catalog-reconciler-retired-restore",
            idempotency_key="retired-restore-finish",
            now=NOW + timedelta(minutes=2),
        )
        restored = await restore_managed_lora(
            session,
            artifact_id=artifact_id,
            expected_lock_version=4,
            dependency_summary_hook=_no_dependencies,
            actor_user_id=owner_id,
            idempotency_key="retired-restore-restore",
            now=NOW + timedelta(minutes=3),
        )

    assert activated.artifact.lifecycle == ManagedLoraLifecycle.ACTIVE
    assert retired.artifact.lifecycle == ManagedLoraLifecycle.RETIRED
    assert retired.artifact.retired_at is not None
    assert restored.artifact.lifecycle == ManagedLoraLifecycle.PENDING_ACTIVATION
    assert restored.artifact.lock_version == 5


async def test_purged_sha_can_be_reimported_as_a_new_live_installation(
    lora_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, _ = lora_database
    command = _civitai()
    async with database.sessions() as session:
        original_id, approval_id, _ = await _complete_civitai_artifact(
            session,
            owner_id=owner_id,
            command=command,
            worker_id="lora-worker-first-install",
            key_prefix="first-install",
            object_version_id="first-install-version",
            object_etag="3" * 32,
            now=NOW,
        )
        await retire_managed_lora(
            session,
            artifact_id=original_id,
            expected_lock_version=1,
            purge_requested=True,
            dependency_summary_hook=_no_dependencies,
            actor_user_id=owner_id,
            idempotency_key="first-install-retire",
            now=NOW + timedelta(minutes=1),
        )
        await mark_managed_lora_retired(
            session,
            artifact_id=original_id,
            expected_lock_version=2,
            dependency_summary_hook=_no_dependencies,
            worker_id="catalog-reconciler-first-install",
            idempotency_key="first-install-finish-retirement",
            now=NOW + timedelta(minutes=2),
        )
        purged = await mark_managed_lora_purged(
            session,
            artifact_id=original_id,
            expected_lock_version=3,
            deleted_object_key=FINAL_KEY,
            deleted_object_version_id="first-install-version",
            worker_id="catalog-reconciler-first-install",
            idempotency_key="first-install-purge",
            now=NOW + timedelta(minutes=3),
        )
        reimport = await create_civitai_import_job(
            session,
            command=command,
            actor_user_id=owner_id,
            idempotency_key="second-install-create",
            now=NOW + timedelta(minutes=4),
        )
        claim = await claim_next_lora_import_job(
            session,
            worker_id="lora-worker-second-install",
            lease_seconds=300,
            now=NOW + timedelta(minutes=4, seconds=1),
        )
        assert claim is not None
        assert claim.job.job_id == reimport.job.job_id
        completed = await complete_lora_import_job(
            session,
            job_id=reimport.job.job_id,
            verified=VerifiedLoraArtifact(
                artifact_sha256=SHA,
                storage_bucket=MODEL_BUCKET,
                object_key=FINAL_KEY,
                object_version_id="second-install-version",
                object_etag="4" * 32,
                byte_size=1_024,
                approval_id=approval_id,
                provenance={"safetensors_header_verified": True},
            ),
            worker_id="lora-worker-second-install",
            expected_attempt=1,
            now=NOW + timedelta(minutes=4, seconds=2),
        )
        artifacts = await list_managed_loras(session, actor_user_id=owner_id)

    assert purged.artifact.lifecycle == ManagedLoraLifecycle.PURGED
    assert completed.job.state == LoraImportJobState.COMPLETED
    assert completed.job.result_artifact_id not in {None, original_id}
    assert len(artifacts) == 2
    by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    assert by_id[original_id].lifecycle == ManagedLoraLifecycle.PURGED
    replacement_id = completed.job.result_artifact_id
    assert replacement_id is not None
    assert by_id[replacement_id].lifecycle == ManagedLoraLifecycle.PENDING_ACTIVATION
    assert by_id[replacement_id].approval_id == approval_id
    assert by_id[replacement_id].object_version_id == "second-install-version"


async def test_same_upstream_filename_with_different_hashes_has_distinct_runtime_names(
    lora_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, _ = lora_database
    second_sha = "d" * 64
    shared_filename = "upstream-shared-name.safetensors"
    first_command = _civitai().model_copy(update={"target_filename": shared_filename})
    second_command = _civitai().model_copy(
        update={
            "display_name": "Different bytes, same upstream filename",
            "target_filename": shared_filename,
            "expected_sha256": second_sha,
            "civitai_file_id": 9013,
        }
    )

    async with database.sessions() as session:
        first_id, _, first_job_id = await _complete_civitai_artifact(
            session,
            owner_id=owner_id,
            command=first_command,
            worker_id="lora-worker-shared-name-first",
            key_prefix="shared-name-first",
            object_version_id="shared-name-first-version",
            object_etag="5" * 32,
            now=NOW,
        )
        second_id, _, second_job_id = await _complete_civitai_artifact(
            session,
            owner_id=owner_id,
            command=second_command,
            worker_id="lora-worker-shared-name-second",
            key_prefix="shared-name-second",
            object_version_id="shared-name-second-version",
            object_etag="6" * 32,
            now=NOW + timedelta(minutes=1),
        )
        artifacts = await list_managed_loras(session, actor_user_id=owner_id)
        jobs = await list_lora_import_jobs(session, actor_user_id=owner_id)

    assert first_id != second_id
    assert {job.target_filename for job in jobs if job.job_id in {first_job_id, second_job_id}} == {
        shared_filename
    }
    by_sha = {artifact.artifact_sha256: artifact for artifact in artifacts}
    assert by_sha[SHA].target_filename == f"managed-{SHA}.safetensors"
    assert by_sha[second_sha].target_filename == f"managed-{second_sha}.safetensors"
    assert by_sha[SHA].target_filename != by_sha[second_sha].target_filename


async def test_static_duplicate_ignores_purged_history_and_never_becomes_manageable(
    lora_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, _ = lora_database
    async with database.sessions() as session:
        created = await create_civitai_import_job(
            session,
            command=_civitai(),
            actor_user_id=owner_id,
            idempotency_key="static-create",
            now=NOW,
        )
    async with database.sessions() as session:
        claim = await claim_next_lora_import_job(
            session,
            worker_id="lora-worker-static",
            lease_seconds=300,
            now=NOW + timedelta(minutes=1),
        )
    assert claim is not None
    async with database.sessions() as session:
        approval = _approval(owner_id, storage_key="worker/loras/static-akali.safetensors")
        approval.source_url = str(_civitai().canonical_source_url)
        approval.license_url = str(_civitai().license_url)
        session.add(approval)
        await session.flush()
        session.add(
            ManagedLoraArtifact(
                artifact_sha256=SHA,
                display_name="Previously purged managed LoRA",
                source_type=LoraImportSource.MANUAL,
                canonical_source_url="https://models.example.test/old-managed-lora",
                license_url="https://models.example.test/old-managed-lora/license",
                civitai_model_id=None,
                civitai_version_id=None,
                civitai_file_id=None,
                provenance={"provider": "manual"},
                storage_bucket=MODEL_BUCKET,
                object_key=FINAL_KEY,
                object_version_id="purged-static-history-version",
                object_etag="8" * 32,
                byte_size=1_024,
                target_filename=f"managed-{SHA}.safetensors",
                approval_id=approval.id,
                trigger_words=[],
                lifecycle=ManagedLoraLifecycle.PURGED,
                purge_requested=True,
                registered_by_user_id=owner_id,
                retirement_requested_by_user_id=owner_id,
                restored_by_user_id=None,
                activated_at=None,
                retirement_requested_at=NOW,
                retired_at=NOW,
                restored_at=None,
                purged_at=NOW,
                lock_version=3,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.commit()
        result = await complete_static_lora_import_duplicate(
            session,
            job_id=created.job.job_id,
            artifact_sha256=SHA,
            approval_id=approval.id,
            worker_id="lora-worker-static",
            expected_attempt=1,
            now=NOW + timedelta(minutes=2),
        )
    assert result.job.state == LoraImportJobState.DUPLICATE
    assert result.job.result_artifact_id is None
    assert result.job.last_error_code == "already_available_static"
    async with database.sessions() as session:
        managed_count = await session.scalar(select(func.count()).select_from(ManagedLoraArtifact))
    assert managed_count == 1


async def test_existing_managed_sha_deduplicates_across_different_source_metadata(
    lora_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, _ = lora_database
    async with database.sessions() as session:
        approval = _approval(owner_id)
        session.add(approval)
        await session.flush()
        existing = ManagedLoraArtifact(
            artifact_sha256=SHA,
            display_name="Existing manual LoRA",
            source_type=LoraImportSource.MANUAL,
            canonical_source_url="https://models.example.test/akali-lora",
            license_url="https://models.example.test/akali-lora/license",
            civitai_model_id=None,
            civitai_version_id=None,
            civitai_file_id=None,
            provenance={"provider": "manual"},
            storage_bucket=MODEL_BUCKET,
            object_key=FINAL_KEY,
            object_version_id="managed-version-existing",
            object_etag="9" * 32,
            byte_size=1_024,
            target_filename="akali-style.safetensors",
            approval_id=approval.id,
            trigger_words=["akali style"],
            lifecycle=ManagedLoraLifecycle.PENDING_ACTIVATION,
            purge_requested=False,
            registered_by_user_id=owner_id,
            retirement_requested_by_user_id=None,
            restored_by_user_id=None,
            activated_at=None,
            retirement_requested_at=None,
            retired_at=None,
            restored_at=None,
            purged_at=None,
            lock_version=1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(existing)
        await session.commit()
        existing_id = existing.id
        approval_id = approval.id

    # This request deliberately has a different Civitai source and licence URL.
    async with database.sessions() as session:
        created = await create_civitai_import_job(
            session,
            command=_civitai(),
            actor_user_id=owner_id,
            idempotency_key="managed-cross-source-create",
            now=NOW + timedelta(minutes=1),
        )
    async with database.sessions() as session:
        claim = await claim_next_lora_import_job(
            session,
            worker_id="lora-worker-cross-source",
            lease_seconds=300,
            now=NOW + timedelta(minutes=2),
        )
    assert claim is not None
    async with database.sessions() as session:
        result = await complete_lora_import_job(
            session,
            job_id=created.job.job_id,
            verified=VerifiedLoraArtifact(
                artifact_sha256=SHA,
                storage_bucket=MODEL_BUCKET,
                object_key=FINAL_KEY,
                object_version_id="managed-version-existing",
                object_etag="9" * 32,
                byte_size=1_024,
                approval_id=approval_id,
                provenance={"provider": "civitai"},
            ),
            worker_id="lora-worker-cross-source",
            expected_attempt=1,
            now=NOW + timedelta(minutes=3),
        )
        artifact_count = await session.scalar(select(func.count()).select_from(ManagedLoraArtifact))
        approval_count = await session.scalar(
            select(func.count()).select_from(ModelArtifactApproval)
        )
    assert result.job.state == LoraImportJobState.DUPLICATE
    assert result.job.result_artifact_id == existing_id
    assert artifact_count == 1
    assert approval_count == 1


async def test_failed_job_owner_retry_then_cancel(
    lora_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, _ = lora_database
    async with database.sessions() as session:
        created = await create_civitai_import_job(
            session,
            command=_civitai(),
            actor_user_id=owner_id,
            idempotency_key="retry-create",
            now=NOW,
        )
    async with database.sessions() as session:
        claim = await claim_next_lora_import_job(
            session,
            worker_id="lora-worker-retry",
            lease_seconds=300,
            now=NOW + timedelta(minutes=1),
        )
        assert claim is not None
    async with database.sessions() as session:
        failed = await fail_lora_import_job(
            session,
            job_id=created.job.job_id,
            worker_id="lora-worker-retry",
            expected_attempt=1,
            error_code="provider_rejected",
            error_detail="Provider rejected the bounded request.",
            retryable=False,
            now=NOW + timedelta(minutes=2),
        )
        retried = await retry_lora_import_job(
            session,
            job_id=created.job.job_id,
            expected_lock_version=failed.lock_version,
            actor_user_id=owner_id,
            idempotency_key="owner-retry",
            now=NOW + timedelta(minutes=3),
        )
        cancelled = await cancel_lora_import_job(
            session,
            job_id=created.job.job_id,
            expected_lock_version=retried.job.lock_version,
            actor_user_id=owner_id,
            idempotency_key="owner-cancel",
            now=NOW + timedelta(minutes=4),
        )
    assert retried.job.state == LoraImportJobState.QUEUED
    assert cancelled.job.state == LoraImportJobState.CANCELLED


async def test_expired_final_import_lease_is_terminalized(
    lora_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, _ = lora_database
    async with database.sessions() as session:
        created = await create_civitai_import_job(
            session,
            command=_civitai(),
            actor_user_id=owner_id,
            idempotency_key="exhausted-lease-create",
            max_attempts=1,
            now=NOW,
        )
    async with database.sessions() as session:
        claim = await claim_next_lora_import_job(
            session,
            worker_id="lora-worker-crashed",
            lease_seconds=30,
            now=NOW + timedelta(seconds=1),
        )
        assert claim is not None
        assert claim.attempt == 1
    async with database.sessions() as session:
        recovered = await recover_exhausted_lora_import_lease(
            session,
            worker_id="lora-worker-recovery",
            now=NOW + timedelta(seconds=32),
        )
        assert recovered is not None
        assert recovered.job_id == created.job.job_id
        assert recovered.state == LoraImportJobState.FAILED
        assert recovered.last_error_code == "lora_import_attempts_exhausted"
        assert recovered.completed_at == NOW + timedelta(seconds=32)
        stored = await session.get(LoraImportJob, created.job.job_id)
        assert stored is not None
        assert stored.lease_owner is None
        assert stored.lease_expires_at is None
        assert (
            await claim_next_lora_import_job(
                session,
                worker_id="lora-worker-recovery",
                lease_seconds=30,
                now=NOW + timedelta(seconds=33),
            )
            is None
        )


def test_commands_reject_secrets_and_keys_and_derive_civitai_provenance() -> None:
    with pytest.raises(ValidationError, match="credentials"):
        ManualLoraImportCreate(
            **{
                **_manual().model_dump(mode="python"),
                "expected_metadata": {"api_token": "do-not-store"},
            }
        )
    with pytest.raises(ValidationError, match="signed URLs"):
        ManualLoraImportCreate(
            **{
                **_manual().model_dump(mode="python"),
                "expected_metadata": {
                    "source": "https://bucket.example.test/file?X-Amz-Signature=secret"
                },
            }
        )
    normalized_civitai = CivitaiLoraImportCreate(
        **{
            **_civitai().model_dump(mode="python"),
            "canonical_source_url": "https://attacker.example.test/model/1",
            "license_url": "https://attacker.example.test/false-license",
        }
    )
    assert str(normalized_civitai.canonical_source_url) == (
        "https://civitai.com/models/1234?modelVersionId=5678"
    )
    assert normalized_civitai.license_url == normalized_civitai.canonical_source_url
    with pytest.raises(ValidationError, match="content-addressed"):
        VerifiedLoraArtifact(
            artifact_sha256=SHA,
            storage_bucket=MODEL_BUCKET,
            object_key="worker/managed-loras/not-content-addressed.safetensors",
            object_version_id="v1",
            object_etag="f" * 32,
            byte_size=1_024,
            approval_id=UUID(int=1),
        )
    completion = ManualUploadCompletion(
        object_version_id="v1",
        object_etag=f'"{"A" * 32}"',
        byte_size=1_024,
    )
    assert completion.object_etag == "a" * 32
