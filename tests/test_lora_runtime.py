import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from gen_automation.config import Settings
from gen_automation.db.models import (
    AdminUser,
    ExperimentWarmLease,
    GenerationAttempt,
    GenerationJob,
    LoraImportJob,
    ManagedLoraArtifact,
    ModelArtifactApproval,
    Project,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AdminRole,
    ApprovalStatus,
    DesiredDeploymentState,
    ExperimentWarmLeaseState,
    GenerationAttemptState,
    GenerationState,
    LoraImportJobState,
    ManagedLoraLifecycle,
    ModelArtifactKind,
    SaladDeploymentState,
)
from gen_automation.domain.lora_catalog import (
    CivitaiLoraImportCreate,
    LoraDependencySummary,
    ManualLoraImportCreate,
    ManualUploadCompletion,
)
from gen_automation.gpu_worker.artifacts import (
    ArtifactKind,
    ModelArtifactSpec,
    create_artifact_manifest,
)
from gen_automation.integrations.civitai.client import CivitaiClient
from gen_automation.integrations.civitai.models import (
    CivitaiFileScan,
    CivitaiLicenseTerms,
    CivitaiModelType,
    CivitaiResolvedLora,
)
from gen_automation.services.lora_catalog import (
    create_civitai_import_job,
    create_manual_import_job,
    mark_manual_upload_complete,
    retire_managed_lora,
)
from gen_automation.services.lora_runtime import LoraRuntime
from gen_automation.services.managed_lora_dependencies import (
    managed_lora_dependency_summary,
)
from gen_automation.storage.base import ObjectStoreError
from gen_automation.storage.memory import MemoryObjectStore
from gen_automation.storage.model_artifacts import (
    FINAL_KEY_PREFIX,
    QUARANTINE_CONTENT_TYPE,
    ModelArtifactStore,
)

NOW = datetime(2026, 8, 9, 19, 0, tzinfo=UTC)
BUCKET = "test-managed-models"


def _safetensors_bytes() -> bytes:
    data = b"\x00\x00\x00\x00"
    header = json.dumps(
        {
            "__metadata__": {"format": "pt"},
            "lora_A.weight": {
                "dtype": "F32",
                "shape": [1],
                "data_offsets": [0, len(data)],
            },
        },
        separators=(",", ":"),
    ).encode()
    return len(header).to_bytes(8, "little") + header + data


@pytest.fixture
async def runtime_context(
    tmp_path: Path,
) -> AsyncIterator[tuple[Database, UUID, MemoryObjectStore, LoraRuntime]]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'lora-runtime.db').as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        owner = AdminUser(
            username_normalized="runtime-owner@example.test",
            display_name="Runtime Owner",
            password_hash="disabled-test-password-hash",  # noqa: S106
            role=AdminRole.OWNER,
            is_active=True,
            failed_login_count=0,
            password_changed_at=NOW,
            credential_version=1,
            lock_version=1,
        )
        session.add(owner)
        await session.commit()
        owner_id = owner.id
    baseline = create_artifact_manifest(
        (
            ModelArtifactSpec(
                logical_name="test-checkpoint",
                kind=ArtifactKind.CHECKPOINT,
                source_object_id="models/checkpoint.safetensors",
                source_object_version_id="version-1",
                sha256="f" * 64,
                exact_size_bytes=1024,
                max_size_bytes=1024,
                target_filename="checkpoint.safetensors",
            ),
        )
    )
    settings = Settings(
        background_runtime_enabled=True,
        lora_manager_enabled=True,
        civitai_api_key=SecretStr("test-civitai-key"),
        salad_worker_artifact_bucket=SecretStr(BUCKET),
        salad_worker_artifact_region=SecretStr("eu-central-1"),
        salad_worker_model_manifest_json=SecretStr(baseline.model_dump_json()),
        salad_worker_model_manifest_sha256=SecretStr(baseline.manifest_sha256),
    )
    memory = MemoryObjectStore(bucket=BUCKET)
    runtime = LoraRuntime(
        settings=settings,
        sessions=database.sessions,
        store=ModelArtifactStore(memory),
        civitai=cast(CivitaiClient, object()),
        worker_id="test-lora-runtime",
    )
    try:
        yield database, owner_id, memory, runtime
    finally:
        await database.dispose()


def _manual(body: bytes, *, name: str = "Test LoRA") -> ManualLoraImportCreate:
    return ManualLoraImportCreate(
        display_name=name,
        canonical_source_url="https://models.example.test/test-lora",
        license_url="https://models.example.test/test-lora/license",
        commercial_use_attested=True,
        adult_use_attested=True,
        target_filename="provider-upload.safetensors",
        expected_sha256=hashlib.sha256(body).hexdigest(),
        expected_byte_size=len(body),
        expected_metadata={"operator_note": "Test upload"},
        trigger_words=["test style"],
    )


async def _queue_manual(
    database: Database,
    memory: MemoryObjectStore,
    *,
    owner_id: UUID,
    body: bytes,
    key: str,
    name: str = "Test LoRA",
) -> UUID:
    async with database.sessions() as session:
        created = await create_manual_import_job(
            session,
            command=_manual(body, name=name),
            model_bucket=BUCKET,
            actor_user_id=owner_id,
            idempotency_key=f"{key}:create",
            now=NOW,
        )
    assert created.job.staging_object_key is not None
    memory.put_for_test(
        created.job.staging_object_key,
        body,
        content_type=QUARANTINE_CONTENT_TYPE,
        metadata={"upload-id": str(created.job.job_id)},
    )
    uploaded = await memory.head(created.job.staging_object_key)
    assert uploaded is not None
    assert uploaded.version_id is not None
    assert uploaded.etag is not None
    async with database.sessions() as session:
        await mark_manual_upload_complete(
            session,
            job_id=created.job.job_id,
            completion=ManualUploadCompletion(
                object_version_id=uploaded.version_id,
                object_etag=uploaded.etag,
                byte_size=len(body),
            ),
            expected_lock_version=created.job.lock_version,
            actor_user_id=owner_id,
            idempotency_key=f"{key}:complete-upload",
            now=NOW,
        )
    return created.job.job_id


async def _no_dependencies(
    _session: object,
    _artifact_id: UUID,
) -> LoraDependencySummary:
    return LoraDependencySummary()


async def test_manual_import_activate_delete_purge_and_reinstall(
    runtime_context: tuple[Database, UUID, MemoryObjectStore, LoraRuntime],
) -> None:
    database, owner_id, memory, runtime = runtime_context
    body = _safetensors_bytes()
    first_job_id = await _queue_manual(
        database,
        memory,
        owner_id=owner_id,
        body=body,
        key="first-import",
    )

    assert await runtime.import_once() is True
    async with database.sessions() as session:
        first_job = await session.get(LoraImportJob, first_job_id)
        artifact = await session.scalar(select(ManagedLoraArtifact))
        assert first_job is not None
        assert artifact is not None
        assert first_job.state == LoraImportJobState.COMPLETED
        assert artifact.lifecycle == ManagedLoraLifecycle.PENDING_ACTIVATION
        first_artifact_id = artifact.id
        first_version_id = artifact.object_version_id
        first_lock_version = artifact.lock_version
        final_key = artifact.object_key
    assert await runtime.lifecycle_once() is True

    async with database.sessions() as session:
        artifact = await session.get(ManagedLoraArtifact, first_artifact_id)
        assert artifact is not None
        assert artifact.lifecycle == ManagedLoraLifecycle.ACTIVE
        retiring = await retire_managed_lora(
            session,
            artifact_id=artifact.id,
            expected_lock_version=artifact.lock_version,
            purge_requested=True,
            dependency_summary_hook=_no_dependencies,
            actor_user_id=owner_id,
            idempotency_key="retire-and-purge",
            now=NOW,
        )
        assert retiring.artifact.lock_version > first_lock_version
    assert await runtime.lifecycle_once() is True
    assert await runtime.lifecycle_once() is True

    async with database.sessions() as session:
        purged = await session.get(ManagedLoraArtifact, first_artifact_id)
        assert purged is not None
        assert purged.lifecycle == ManagedLoraLifecycle.PURGED
    assert await memory.head(final_key, version_id=first_version_id) is None

    second_job_id = await _queue_manual(
        database,
        memory,
        owner_id=owner_id,
        body=body,
        key="reinstall",
        name="Reinstalled Test LoRA",
    )
    assert await runtime.import_once() is True
    async with database.sessions() as session:
        second_job = await session.get(LoraImportJob, second_job_id)
        artifacts = list(
            (
                await session.scalars(
                    select(ManagedLoraArtifact).order_by(ManagedLoraArtifact.created_at)
                )
            ).all()
        )
        assert second_job is not None
        assert second_job.state == LoraImportJobState.COMPLETED
        assert len(artifacts) == 2
        assert artifacts[0].lifecycle == ManagedLoraLifecycle.PURGED
        assert artifacts[1].lifecycle == ManagedLoraLifecycle.PENDING_ACTIVATION
        assert artifacts[1].object_version_id != first_version_id
        assert artifacts[1].artifact_sha256 == artifacts[0].artifact_sha256


async def test_static_duplicate_removes_unmanaged_promoted_copy(
    runtime_context: tuple[Database, UUID, MemoryObjectStore, LoraRuntime],
) -> None:
    database, owner_id, memory, runtime = runtime_context
    body = _safetensors_bytes()
    sha256 = hashlib.sha256(body).hexdigest()
    async with database.sessions() as session:
        session.add(
            ModelArtifactApproval(
                artifact_sha256=sha256,
                name="Protected baseline LoRA",
                kind=ModelArtifactKind.LORA,
                source_url="https://models.example.test/static",
                storage_key="models/static-lora.safetensors",
                license_url="https://models.example.test/static/license",
                commercial_use_approved=True,
                adult_use_approved=True,
                safetensors_verified=True,
                evidence={"summary": "Protected baseline"},
                evidence_sha256="e" * 64,
                status=ApprovalStatus.APPROVED,
                is_current=True,
                approval_version=1,
                approved_by_user_id=owner_id,
                approved_at=NOW,
            )
        )
        await session.commit()
    job_id = await _queue_manual(
        database,
        memory,
        owner_id=owner_id,
        body=body,
        key="static-duplicate",
    )
    assert await runtime.import_once() is True

    async with database.sessions() as session:
        job = await session.get(LoraImportJob, job_id)
        managed_count = int(
            await session.scalar(select(func.count()).select_from(ManagedLoraArtifact)) or 0
        )
        assert job is not None
        assert job.state == LoraImportJobState.DUPLICATE
        assert managed_count == 0
    assert await memory.head(f"{FINAL_KEY_PREFIX}/{sha256}.safetensors") is None


async def test_static_duplicate_cleanup_failure_retries_before_terminalizing(
    runtime_context: tuple[Database, UUID, MemoryObjectStore, LoraRuntime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, owner_id, memory, runtime = runtime_context
    body = _safetensors_bytes()
    sha256 = hashlib.sha256(body).hexdigest()
    async with database.sessions() as session:
        session.add(
            ModelArtifactApproval(
                artifact_sha256=sha256,
                name="Protected retry baseline",
                kind=ModelArtifactKind.LORA,
                source_url="https://models.example.test/static-retry",
                storage_key="models/static-retry.safetensors",
                license_url="https://models.example.test/static-retry/license",
                commercial_use_approved=True,
                adult_use_approved=True,
                safetensors_verified=True,
                evidence={"summary": "Protected baseline"},
                evidence_sha256="d" * 64,
                status=ApprovalStatus.APPROVED,
                is_current=True,
                approval_version=1,
                approved_by_user_id=owner_id,
                approved_at=NOW,
            )
        )
        await session.commit()
    job_id = await _queue_manual(
        database,
        memory,
        owner_id=owner_id,
        body=body,
        key="static-cleanup-retry",
    )
    original_delete = runtime.store.delete_exact

    async def fail_delete_once(*, key: str, version_id: str) -> None:
        del key, version_id
        raise ObjectStoreError("simulated private-storage outage")

    monkeypatch.setattr(runtime.store, "delete_exact", fail_delete_once)
    assert await runtime.import_once() is True
    async with database.sessions() as session:
        job = await session.get(LoraImportJob, job_id)
        assert job is not None
        assert job.state == LoraImportJobState.RETRY_WAIT
        job.available_at = datetime.now(UTC)
        await session.commit()
    assert await memory.head(f"{FINAL_KEY_PREFIX}/{sha256}.safetensors") is not None

    monkeypatch.setattr(runtime.store, "delete_exact", original_delete)
    assert await runtime.import_once() is True
    async with database.sessions() as session:
        job = await session.get(LoraImportJob, job_id)
        assert job is not None
        assert job.state == LoraImportJobState.DUPLICATE
    assert await memory.head(f"{FINAL_KEY_PREFIX}/{sha256}.safetensors") is None


@pytest.mark.parametrize("declared_delta", [-1, 1])
async def test_civitai_advisory_size_does_not_reject_verified_bytes(
    runtime_context: tuple[Database, UUID, MemoryObjectStore, LoraRuntime],
    declared_delta: int,
) -> None:
    database, owner_id, _memory, runtime = runtime_context
    body = _safetensors_bytes()
    sha256 = hashlib.sha256(body).hexdigest()
    resolved = CivitaiResolvedLora(
        model_id=123,
        version_id=456,
        file_id=789,
        model_type=CivitaiModelType.LORA,
        model_name="Provider LoRA",
        version_name="V1",
        target_filename="provider-lora.safetensors",
        canonical_source_url="https://civitai.com/models/123?modelVersionId=456",
        creator="creator",
        base_model="SDXL 1.0",
        trained_words=("provider style",),
        declared_size_bytes=len(body) + declared_delta,
        sha256=sha256,
        scan=CivitaiFileScan(pickle_result="Success", virus_result="Success"),
        license_terms=CivitaiLicenseTerms(
            allow_no_credit=False,
            commercial_use=("Image",),
            allow_derivatives=True,
            allow_different_license=False,
        ),
        nsfw=False,
        nsfw_level=0,
        _download_url="https://civitai.com/api/download/models/456",
    )

    class FakeCivitaiClient:
        async def resolve_lora(
            self,
            _source: object,
            *,
            version_id: int | None = None,
        ) -> CivitaiResolvedLora:
            assert version_id == resolved.version_id
            return resolved

        @asynccontextmanager
        async def open_download(
            self,
            selected: CivitaiResolvedLora,
            *,
            max_bytes: int,
        ) -> AsyncIterator[AsyncIterator[bytes]]:
            assert selected is resolved
            assert len(body) < max_bytes

            async def chunks() -> AsyncIterator[bytes]:
                yield body

            yield chunks()

    runtime.civitai = cast(CivitaiClient, FakeCivitaiClient())
    async with database.sessions() as session:
        created = await create_civitai_import_job(
            session,
            command=CivitaiLoraImportCreate(
                display_name="Provider LoRA",
                canonical_source_url=resolved.canonical_source_url,
                license_url=resolved.canonical_source_url,
                commercial_use_attested=True,
                adult_use_attested=True,
                target_filename=resolved.target_filename,
                expected_sha256=resolved.sha256,
                expected_byte_size=resolved.declared_size_bytes,
                expected_metadata={},
                trigger_words=list(resolved.trained_words),
                civitai_model_id=resolved.model_id,
                civitai_version_id=resolved.version_id,
                civitai_file_id=resolved.file_id,
            ),
            actor_user_id=owner_id,
            idempotency_key=f"civitai-advisory-size:{declared_delta}:create",
            now=NOW,
        )

    assert await runtime.import_once() is True
    async with database.sessions() as session:
        job = await session.get(LoraImportJob, created.job.job_id)
        artifact = await session.scalar(select(ManagedLoraArtifact))
        assert job is not None
        assert artifact is not None
        assert job.state == LoraImportJobState.COMPLETED
        assert job.progress_bytes == len(body)
        assert job.total_bytes == len(body)
        assert artifact.byte_size == len(body)
        assert artifact.provenance["verified"]["license_terms"]["commercial_use"] == ["Image"]


async def test_never_activated_delete_does_not_wait_for_unrelated_warm_gpu(
    runtime_context: tuple[Database, UUID, MemoryObjectStore, LoraRuntime],
) -> None:
    database, owner_id, memory, runtime = runtime_context
    body = _safetensors_bytes()
    job_id = await _queue_manual(
        database,
        memory,
        owner_id=owner_id,
        body=body,
        key="delete-before-activation",
    )
    assert await runtime.import_once() is True

    async with database.sessions() as session:
        job = await session.get(LoraImportJob, job_id)
        artifact = await session.scalar(select(ManagedLoraArtifact))
        assert job is not None
        assert artifact is not None
        assert artifact.activated_at is None
        deployment = SaladDeployment(
            version_no=1,
            config_sha256="c" * 64,
            runtime_managed_lora_sha256s=[],
            provider_configuration={"container": {}},
            worker_image_digest=f"registry.example/worker@sha256:{'a' * 64}",
            organization_name="organization",
            project_name="project",
            queue_name="generation",
            provider_queue_id="queue-id",
            container_group_name="worker",
            provider_container_group_id="group-id",
            state=SaladDeploymentState.ACTIVE,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=True,
            min_replicas=1,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=360_000,
            observed_replicas=1,
            ready_replicas=1,
            last_observed_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(deployment)
        await session.flush()
        session.add(
            ExperimentWarmLease(
                salad_deployment_id=deployment.id,
                state=ExperimentWarmLeaseState.ACTIVE,
                started_at=NOW,
                expires_at=NOW + timedelta(minutes=30),
                hard_expires_at=NOW + timedelta(minutes=90),
                ended_at=None,
                last_activity_at=NOW,
                idle_ttl_seconds=300,
                max_cost_microusd=360_000,
                provider_version=1,
                created_by="unrelated-warm-runtime-test",
                lock_version=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await retire_managed_lora(
            session,
            artifact_id=artifact.id,
            expected_lock_version=artifact.lock_version,
            purge_requested=True,
            dependency_summary_hook=_no_dependencies,
            actor_user_id=owner_id,
            idempotency_key="delete-before-activation:retire",
            now=NOW,
        )

    assert await runtime.lifecycle_once() is True
    assert await runtime.lifecycle_once() is True
    async with database.sessions() as session:
        artifact = await session.scalar(select(ManagedLoraArtifact))
        assert artifact is not None
        assert artifact.lifecycle == ManagedLoraLifecycle.PURGED


async def test_resident_lora_retirement_waits_for_active_warm_runtime(
    runtime_context: tuple[Database, UUID, MemoryObjectStore, LoraRuntime],
) -> None:
    database, owner_id, memory, runtime = runtime_context
    body = _safetensors_bytes()
    await _queue_manual(
        database,
        memory,
        owner_id=owner_id,
        body=body,
        key="resident-retirement",
    )
    assert await runtime.import_once() is True
    assert await runtime.lifecycle_once() is True

    async with database.sessions() as session:
        artifact = await session.scalar(select(ManagedLoraArtifact))
        assert artifact is not None
        assert artifact.lifecycle == ManagedLoraLifecycle.ACTIVE
        deployment = SaladDeployment(
            version_no=1,
            config_sha256="d" * 64,
            runtime_artifact_manifest_sha256="e" * 64,
            runtime_managed_lora_sha256s=[artifact.artifact_sha256],
            provider_configuration={"container": {}},
            worker_image_digest=f"registry.example/worker@sha256:{'a' * 64}",
            organization_name="organization",
            project_name="project",
            queue_name="generation",
            provider_queue_id="resident-queue-id",
            container_group_name="worker",
            provider_container_group_id="resident-group-id",
            state=SaladDeploymentState.ACTIVE,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=True,
            min_replicas=0,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=360_000,
            observed_replicas=0,
            ready_replicas=0,
            last_observed_at=datetime.now(UTC),
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(deployment)
        await session.flush()
        lease = ExperimentWarmLease(
            salad_deployment_id=deployment.id,
            state=ExperimentWarmLeaseState.ACTIVE,
            started_at=NOW,
            expires_at=NOW + timedelta(minutes=30),
            hard_expires_at=NOW + timedelta(minutes=90),
            ended_at=None,
            last_activity_at=NOW,
            idle_ttl_seconds=300,
            max_cost_microusd=360_000,
            provider_version=1,
            created_by="resident-retirement-test",
            lock_version=1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(lease)
        await retire_managed_lora(
            session,
            artifact_id=artifact.id,
            expected_lock_version=artifact.lock_version,
            purge_requested=True,
            dependency_summary_hook=_no_dependencies,
            actor_user_id=owner_id,
            idempotency_key="resident-retirement:retire",
            now=NOW,
        )
        lease_id = lease.id

    assert await runtime.lifecycle_once() is False
    async with database.sessions() as session:
        artifact = await session.scalar(select(ManagedLoraArtifact))
        lease = await session.get(ExperimentWarmLease, lease_id)
        assert artifact is not None
        assert lease is not None
        assert artifact.lifecycle == ManagedLoraLifecycle.RETIRING
        lease.state = ExperimentWarmLeaseState.ENDED
        lease.ended_at = NOW + timedelta(minutes=1)
        lease.updated_at = NOW + timedelta(minutes=1)
        await session.commit()

    assert await runtime.lifecycle_once() is True
    async with database.sessions() as session:
        artifact = await session.scalar(select(ManagedLoraArtifact))
        assert artifact is not None
        assert artifact.lifecycle == ManagedLoraLifecycle.RETIRED


async def test_rollover_resident_attempt_blocks_lora_retirement(
    runtime_context: tuple[Database, UUID, MemoryObjectStore, LoraRuntime],
) -> None:
    database, owner_id, memory, runtime = runtime_context
    await _queue_manual(
        database,
        memory,
        owner_id=owner_id,
        body=_safetensors_bytes(),
        key="rollover-resident-retirement",
    )
    assert await runtime.import_once() is True
    assert await runtime.lifecycle_once() is True

    other_lora_sha256 = "0" * 64
    async with database.sessions() as session:
        artifact = await session.scalar(select(ManagedLoraArtifact))
        assert artifact is not None
        assert artifact.lifecycle == ManagedLoraLifecycle.ACTIVE
        assert artifact.artifact_sha256 != other_lora_sha256

        project = Project(slug="lora-rollover", name="LoRA rollover")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="rollover-release",
            title="Rollover release",
            desired_accepted_count=1,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={"schema_version": 1},
            specification_sha256="5" * 64,
            created_by="rollover-test",
            created_at=NOW,
        )
        old_deployment = SaladDeployment(
            version_no=1,
            config_sha256="1" * 64,
            runtime_artifact_manifest_sha256="2" * 64,
            runtime_managed_lora_sha256s=[
                artifact.artifact_sha256,
                other_lora_sha256,
            ],
            provider_configuration={"container": {}},
            worker_image_digest=f"registry.example/worker@sha256:{'a' * 64}",
            organization_name="organization",
            project_name="project",
            queue_name="generation-v1",
            provider_queue_id="rollover-old-queue",
            container_group_name="worker-v1",
            provider_container_group_id="rollover-old-group",
            state=SaladDeploymentState.ACTIVE,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=False,
            min_replicas=0,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=360_000,
            observed_replicas=1,
            ready_replicas=1,
            last_observed_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        current_deployment = SaladDeployment(
            version_no=2,
            config_sha256="3" * 64,
            runtime_artifact_manifest_sha256="4" * 64,
            runtime_managed_lora_sha256s=[],
            provider_configuration={"container": {}},
            worker_image_digest=f"registry.example/worker@sha256:{'a' * 64}",
            organization_name="organization",
            project_name="project",
            queue_name="generation-v2",
            provider_queue_id=None,
            container_group_name="worker-v2",
            provider_container_group_id=None,
            state=SaladDeploymentState.PLANNED,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=True,
            min_replicas=0,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=360_000,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add_all([version, old_deployment, current_deployment])
        await session.flush()
        job = GenerationJob(
            release_version_id=version.id,
            logical_key="6" * 64,
            parameters={"loras": [{"sha256": other_lora_sha256}]},
            parameters_sha256="7" * 64,
            provider="salad",
            state=GenerationState.RUNNING,
            expected_output_count=1,
            attempt_count=1,
            max_attempts=3,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(job)
        await session.flush()
        attempt = GenerationAttempt(
            job_id=job.id,
            salad_deployment_id=old_deployment.id,
            attempt_no=1,
            provider="salad",
            provider_external_id="rollover-old-attempt",
            submission_key="8" * 64,
            request_sha256="9" * 64,
            state=GenerationAttemptState.RUNNING,
            worker_image_digest=f"registry.example/worker@sha256:{'a' * 64}",
            request_metadata={},
            submit_started_at=NOW,
            submitted_at=NOW,
            started_at=NOW,
            last_observed_at=NOW,
            provider_state="running",
            cost_reservation_microusd=0,
            lock_version=1,
            created_at=NOW,
        )
        session.add(attempt)
        await session.flush()

        dependencies = await managed_lora_dependency_summary(session, artifact.id)
        assert dependencies.queued_generation_jobs == 0
        assert dependencies.active_generation_attempts == 1
        assert dependencies.warm_experiment_leases == 0
        retired = await retire_managed_lora(
            session,
            artifact_id=artifact.id,
            expected_lock_version=artifact.lock_version,
            purge_requested=True,
            dependency_summary_hook=managed_lora_dependency_summary,
            actor_user_id=owner_id,
            idempotency_key="rollover-resident-retirement:retire",
            now=NOW,
        )
        assert retired.dependencies.active_generation_attempts == 1
        attempt_id = attempt.id
        job_id = job.id
        old_deployment_id = old_deployment.id
        final_key = f"{FINAL_KEY_PREFIX}/{artifact.artifact_sha256}.safetensors"

    assert await runtime.lifecycle_once() is False
    async with database.sessions() as session:
        artifact = await session.scalar(select(ManagedLoraArtifact))
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert artifact is not None
        assert attempt is not None
        assert artifact.lifecycle == ManagedLoraLifecycle.RETIRING
        attempt.state = GenerationAttemptState.SUCCEEDED
        attempt.completed_at = NOW + timedelta(minutes=1)
        await session.commit()

    assert await runtime.lifecycle_once() is True
    async with database.sessions() as session:
        artifact = await session.scalar(select(ManagedLoraArtifact))
        assert artifact is not None
        assert artifact.lifecycle == ManagedLoraLifecycle.RETIRED
        job = await session.get(GenerationJob, job_id)
        assert job is not None
        job.attempt_count = 2
        second_attempt = GenerationAttempt(
            job_id=job.id,
            salad_deployment_id=old_deployment_id,
            attempt_no=2,
            provider="salad",
            provider_external_id="rollover-old-purge-attempt",
            submission_key="a" * 64,
            request_sha256="b" * 64,
            state=GenerationAttemptState.RUNNING,
            worker_image_digest=f"registry.example/worker@sha256:{'a' * 64}",
            request_metadata={},
            submit_started_at=NOW + timedelta(minutes=2),
            submitted_at=NOW + timedelta(minutes=2),
            started_at=NOW + timedelta(minutes=2),
            last_observed_at=NOW + timedelta(minutes=2),
            provider_state="running",
            cost_reservation_microusd=0,
            lock_version=1,
            created_at=NOW + timedelta(minutes=2),
        )
        session.add(second_attempt)
        await session.commit()
        second_attempt_id = second_attempt.id

    assert await runtime.lifecycle_once() is False
    assert await memory.head(final_key) is not None
    async with database.sessions() as session:
        artifact = await session.scalar(select(ManagedLoraArtifact))
        second_attempt = await session.get(GenerationAttempt, second_attempt_id)
        assert artifact is not None
        assert second_attempt is not None
        assert artifact.lifecycle == ManagedLoraLifecycle.RETIRED
        second_attempt.state = GenerationAttemptState.SUCCEEDED
        second_attempt.completed_at = NOW + timedelta(minutes=3)
        await session.commit()

    assert await runtime.lifecycle_once() is True
    assert await memory.head(final_key) is None
    async with database.sessions() as session:
        artifact = await session.scalar(select(ManagedLoraArtifact))
        assert artifact is not None
        assert artifact.lifecycle == ManagedLoraLifecycle.PURGED
