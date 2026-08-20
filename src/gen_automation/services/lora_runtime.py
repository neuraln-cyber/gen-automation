"""Bounded CPU-only import, activation, retirement, and purge cycles for LoRAs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.config import Settings
from gen_automation.db.models import (
    AuditEvent,
    ManagedLoraArtifact,
    ModelArtifactApproval,
    ProviderBudgetGuard,
    SaladDeployment,
)
from gen_automation.domain.compliance_registry import (
    ApprovalEvidence,
    ModelArtifactApprovalCreate,
)
from gen_automation.domain.enums import (
    ApprovalStatus,
    DesiredDeploymentState,
    GenerationModelFamily,
    LoraImportSource,
    ManagedLoraLifecycle,
    ModelArtifactKind,
    SaladDeploymentPurpose,
    SaladDeploymentState,
)
from gen_automation.domain.lora_catalog import (
    LORA_MODEL_FAMILY_METADATA_KEY,
    VerifiedLoraArtifact,
)
from gen_automation.integrations.civitai import (
    CivitaiAPIError,
    CivitaiError,
    CivitaiRateLimitError,
    CivitaiTransportError,
)
from gen_automation.integrations.civitai.client import CivitaiClient
from gen_automation.services.compliance_registry import (
    ComplianceRegistryConflictError,
    approve_model_artifact,
)
from gen_automation.services.lora_catalog import (
    LoraCatalogConflictError,
    LoraImportClaim,
    claim_next_lora_import_job,
    complete_lora_import_job,
    complete_static_lora_import_duplicate,
    fail_lora_import_job,
    heartbeat_lora_import_job,
    mark_managed_lora_active,
    mark_managed_lora_purged,
    mark_managed_lora_retired,
    recover_exhausted_lora_import_lease,
)
from gen_automation.services.managed_artifact_manifest import (
    ManagedArtifactManifestError,
    effective_artifact_manifest_from_settings,
)
from gen_automation.services.managed_lora_dependencies import (
    managed_lora_dependency_summary,
    managed_lora_has_historical_reference,
    runtime_has_active_work,
)
from gen_automation.storage.base import (
    ObjectConflictError,
    ObjectNotFoundError,
    ObjectStoreError,
    ObjectTooLargeError,
)
from gen_automation.storage.model_artifacts import (
    ModelArtifactCleanupError,
    ModelArtifactError,
    ModelArtifactIntegrityError,
    ModelArtifactStore,
    ModelArtifactValidationError,
    StoredModelArtifact,
)

logger = structlog.get_logger(__name__)


class LoraRuntimeConfigurationError(RuntimeError):
    """The enabled LoRA runtime lacks a validated immutable dependency."""


_PROVIDER_IDLE_FRESHNESS_SECONDS = 120


def _import_model_family(metadata: dict[str, object]) -> GenerationModelFamily:
    value = metadata.get(LORA_MODEL_FAMILY_METADATA_KEY)
    if value is None:
        return GenerationModelFamily.ILLUSTRIOUS
    if not isinstance(value, str):
        raise ModelArtifactValidationError("LoRA import model family is invalid")
    try:
        return GenerationModelFamily(value)
    except ValueError as error:
        raise ModelArtifactValidationError("LoRA import model family is invalid") from error


class LoraRuntime:
    """Run one bounded durable LoRA unit without allocating generation compute."""

    def __init__(
        self,
        *,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        store: ModelArtifactStore,
        civitai: CivitaiClient,
        worker_id: str,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.store = store
        self.civitai = civitai
        self.worker_id = worker_id

    async def import_once(self) -> bool:
        async with self.sessions() as session:
            recovered = await recover_exhausted_lora_import_lease(
                session,
                worker_id=self.worker_id,
            )
        if recovered is not None:
            return True
        async with self.sessions() as session:
            claim = await claim_next_lora_import_job(
                session,
                worker_id=self.worker_id,
                lease_seconds=self.settings.background_lora_import_lease_seconds,
            )
        if claim is None:
            return False
        try:
            await self._process_claim(claim)
        except Exception as error:
            await self._record_failure(claim, error)
        return True

    async def lifecycle_once(self) -> bool:
        checked_at = datetime.now(UTC)
        async with self.sessions() as session:
            artifacts = list(
                (
                    await session.execute(
                        select(
                            ManagedLoraArtifact.id,
                            ManagedLoraArtifact.lifecycle,
                            ManagedLoraArtifact.purge_requested,
                        )
                        .where(
                            or_(
                                ManagedLoraArtifact.lifecycle.in_(
                                    (
                                        ManagedLoraLifecycle.PENDING_ACTIVATION,
                                        ManagedLoraLifecycle.RETIRING,
                                    )
                                ),
                                (ManagedLoraArtifact.lifecycle == ManagedLoraLifecycle.RETIRED)
                                & ManagedLoraArtifact.purge_requested.is_(True),
                            ),
                            or_(
                                ManagedLoraArtifact.lifecycle_retry_at.is_(None),
                                ManagedLoraArtifact.lifecycle_retry_at <= checked_at,
                            ),
                        )
                        .order_by(
                            case(
                                (
                                    ManagedLoraArtifact.lifecycle
                                    == ManagedLoraLifecycle.PENDING_ACTIVATION,
                                    0,
                                ),
                                (
                                    ManagedLoraArtifact.lifecycle == ManagedLoraLifecycle.RETIRING,
                                    1,
                                ),
                                else_=2,
                            ),
                            ManagedLoraArtifact.updated_at,
                            ManagedLoraArtifact.id,
                        )
                        .limit(50)
                    )
                ).all()
            )
            await session.rollback()
        for artifact in artifacts:
            try:
                if artifact.lifecycle == ManagedLoraLifecycle.PENDING_ACTIVATION:
                    if await self._activate_if_idle(artifact.id):
                        return True
                elif artifact.lifecycle == ManagedLoraLifecycle.RETIRING:
                    if await self._finish_retirement_if_idle(artifact.id):
                        return True
                elif (
                    artifact.lifecycle == ManagedLoraLifecycle.RETIRED
                    and artifact.purge_requested
                    and await self._purge_if_safe(artifact.id)
                ):
                    return True
            except (
                LoraCatalogConflictError,
                LoraRuntimeConfigurationError,
                ManagedArtifactManifestError,
                ModelArtifactError,
                ObjectStoreError,
            ) as error:
                await self._record_lifecycle_failure(artifact.id, error)
        return False

    async def _process_claim(self, claim: LoraImportClaim) -> None:
        if claim.job.source_type == LoraImportSource.MANUAL:
            if (
                claim.job.staging_object_key is None
                or claim.job.staging_object_version_id is None
                or claim.job.staging_object_etag is None
            ):
                raise ModelArtifactIntegrityError(
                    "manual LoRA import has no immutable uploaded object"
                )
            stored = await self.store.promote_quarantine(
                quarantine_key=claim.job.staging_object_key,
                version_id=claim.job.staging_object_version_id,
                etag=claim.job.staging_object_etag,
                target_filename=claim.job.target_filename,
                provenance={
                    "provider": "manual",
                    "source_url": claim.job.canonical_source_url,
                    "license_url": claim.job.license_url,
                },
                expected_sha256=claim.job.expected_sha256,
                expected_size_bytes=claim.job.staging_byte_size,
            )
        elif claim.job.source_type == LoraImportSource.CIVITAI:
            resolved = await self.civitai.resolve_lora(
                claim.job.canonical_source_url,
                version_id=claim.job.civitai_version_id,
                allow_commercial_use_override=(claim.job.commercial_use_override_attested),
            )
            if (
                claim.job.civitai_model_id != resolved.model_id
                or claim.job.civitai_version_id != resolved.version_id
                or claim.job.civitai_file_id != resolved.file_id
                or claim.job.target_filename != resolved.target_filename
                or claim.job.canonical_source_url != resolved.canonical_source_url
                or claim.job.license_url != resolved.canonical_source_url
                or (
                    claim.job.expected_sha256 is not None
                    and claim.job.expected_sha256 != resolved.sha256
                )
            ):
                raise ModelArtifactIntegrityError(
                    "Civitai metadata changed after the import was approved"
                )
            static = await self._static_duplicate(resolved.sha256)
            if static is not None:
                await self._complete_static_duplicate(claim, static)
                return
            stored = await self.store.ingest_civitai(
                resolved,
                self.civitai,
                provenance={
                    "import_job_id": str(claim.job.job_id),
                    "license_url": resolved.canonical_source_url,
                    "license_terms": resolved.license_terms.as_json(),
                    "commercial_use_override_attested": (
                        claim.job.commercial_use_override_attested
                    ),
                },
                progress=lambda transferred: self._record_import_progress(
                    claim,
                    transferred,
                ),
            )
        else:
            raise ModelArtifactValidationError("LoRA import source is unsupported")

        existing_approval_id = await self._managed_duplicate(stored.sha256)
        if existing_approval_id is not None:
            await self._complete_managed_duplicate(
                claim,
                stored=stored,
                approval_id=existing_approval_id,
            )
            await self._cleanup_manual_quarantine(claim)
            return
        static = await self._static_duplicate(stored.sha256)
        if static is not None:
            await self._cleanup_unmanaged_promotion(stored)
            await self._complete_static_duplicate(claim, static)
            await self._cleanup_manual_quarantine(claim)
            return
        await self._approve_and_complete(claim, stored)
        await self._cleanup_manual_quarantine(claim)

    async def _record_import_progress(
        self,
        claim: LoraImportClaim,
        transferred: int,
    ) -> None:
        async with self.sessions() as session:
            await heartbeat_lora_import_job(
                session,
                job_id=claim.job.job_id,
                worker_id=claim.worker_id,
                expected_attempt=claim.attempt,
                progress_bytes=transferred,
                lease_seconds=self.settings.background_lora_import_lease_seconds,
            )

    async def _cleanup_manual_quarantine(self, claim: LoraImportClaim) -> None:
        if (
            claim.job.source_type != LoraImportSource.MANUAL
            or claim.job.staging_object_key is None
            or claim.job.staging_object_version_id is None
        ):
            return
        try:
            await self.store.delete_quarantine_exact(
                key=claim.job.staging_object_key,
                version_id=claim.job.staging_object_version_id,
            )
        except (ModelArtifactCleanupError, ObjectStoreError):
            logger.warning(
                "managed_lora_quarantine_cleanup_deferred",
                lora_import_job_id=str(claim.job.job_id),
            )

    async def _cleanup_unmanaged_promotion(self, stored: StoredModelArtifact) -> None:
        """Remove a managed-prefix copy after a protected static duplicate wins."""

        async with self.sessions() as session:
            static_approval = await session.scalar(
                select(ModelArtifactApproval)
                .where(
                    ModelArtifactApproval.artifact_sha256 == stored.sha256,
                    ModelArtifactApproval.kind == ModelArtifactKind.LORA,
                    ModelArtifactApproval.status == ApprovalStatus.APPROVED,
                    ModelArtifactApproval.is_current.is_(True),
                    ModelArtifactApproval.storage_key.not_like("worker/managed-loras/sha256/%"),
                )
                .with_for_update()
            )
            if static_approval is None:
                await session.rollback()
                return
            referenced = await session.scalar(
                select(ManagedLoraArtifact.id).where(
                    ManagedLoraArtifact.object_key == stored.key,
                    ManagedLoraArtifact.object_version_id == stored.version_id,
                    ManagedLoraArtifact.lifecycle != ManagedLoraLifecycle.PURGED,
                )
            )
            if referenced is not None:
                await session.rollback()
                return
            try:
                await self.store.delete_exact(key=stored.key, version_id=stored.version_id)
            finally:
                await session.rollback()

    async def _approve_and_complete(
        self,
        claim: LoraImportClaim,
        stored: StoredModelArtifact,
    ) -> None:
        source_urls = list(dict.fromkeys((claim.job.canonical_source_url, claim.job.license_url)))
        command = ModelArtifactApprovalCreate(
            artifact_sha256=stored.sha256,
            name=claim.job.display_name,
            kind=ModelArtifactKind.LORA,
            model_family=_import_model_family(claim.job.expected_metadata),
            source_url=claim.job.canonical_source_url,
            storage_key=stored.key,
            license_url=claim.job.license_url,
            commercial_use_approved=True,
            adult_use_approved=True,
            safetensors_verified=True,
            evidence=ApprovalEvidence(
                summary=(
                    (
                        "Owner-reviewed Civitai commercial-use metadata override and "
                        "adult-use attestation; "
                        if claim.job.commercial_use_override_attested
                        else "Owner-attested commercial/adult-use LoRA; "
                    )
                    + "exact Safetensors bytes, size, SHA-256, and private object version "
                    "verified by the managed onboarding runtime."
                ),
                source_urls=source_urls,
                document_sha256s=[stored.sha256],
                internal_reference=f"managed-lora-import:{claim.job.job_id}",
            ),
        )
        async with self.sessions() as outer_session:
            async with outer_session.begin():
                connection = await outer_session.connection()
                session = AsyncSession(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                try:
                    approval = await approve_model_artifact(
                        session,
                        command=command,
                        actor_user_id=claim.job.requested_by_user_id,
                        idempotency_key=(
                            f"managed-lora-approval:{claim.job.job_id}:{stored.sha256}"
                        ),
                        allow_managed_registration=True,
                    )
                    verified = VerifiedLoraArtifact(
                        artifact_sha256=stored.sha256,
                        storage_bucket=stored.bucket,
                        object_key=stored.key,
                        object_version_id=stored.version_id,
                        object_etag=stored.etag,
                        byte_size=stored.size_bytes,
                        approval_id=approval.approval_id,
                        provenance=stored.provenance,
                    )
                    await complete_lora_import_job(
                        session,
                        job_id=claim.job.job_id,
                        verified=verified,
                        worker_id=claim.worker_id,
                        expected_attempt=claim.attempt,
                    )
                finally:
                    await session.close()

    async def _static_duplicate(self, sha256: str | None) -> UUID | None:
        if sha256 is None:
            return None
        async with self.sessions() as session:
            return await session.scalar(
                select(ModelArtifactApproval.id).where(
                    ModelArtifactApproval.artifact_sha256 == sha256,
                    ModelArtifactApproval.kind == ModelArtifactKind.LORA,
                    ModelArtifactApproval.status == ApprovalStatus.APPROVED,
                    ModelArtifactApproval.is_current.is_(True),
                    ModelArtifactApproval.commercial_use_approved.is_(True),
                    ModelArtifactApproval.adult_use_approved.is_(True),
                    ModelArtifactApproval.safetensors_verified.is_(True),
                    ModelArtifactApproval.storage_key.not_like("worker/managed-loras/sha256/%"),
                )
            )

    async def _managed_duplicate(self, sha256: str) -> UUID | None:
        async with self.sessions() as session:
            value: UUID | None = await session.scalar(
                select(ManagedLoraArtifact.approval_id).where(
                    ManagedLoraArtifact.artifact_sha256 == sha256,
                    ManagedLoraArtifact.lifecycle != ManagedLoraLifecycle.PURGED,
                )
            )
            return value

    async def _complete_static_duplicate(
        self,
        claim: LoraImportClaim,
        approval_id: UUID,
    ) -> None:
        sha256 = claim.job.expected_sha256
        if sha256 is None:
            async with self.sessions() as session:
                sha256 = await session.scalar(
                    select(ModelArtifactApproval.artifact_sha256).where(
                        ModelArtifactApproval.id == approval_id
                    )
                )
        if sha256 is None:
            raise LoraCatalogConflictError("static duplicate identity disappeared")
        async with self.sessions() as session:
            await complete_static_lora_import_duplicate(
                session,
                job_id=claim.job.job_id,
                artifact_sha256=sha256,
                approval_id=approval_id,
                worker_id=claim.worker_id,
                expected_attempt=claim.attempt,
            )

    async def _complete_managed_duplicate(
        self,
        claim: LoraImportClaim,
        *,
        stored: StoredModelArtifact,
        approval_id: UUID,
    ) -> None:
        async with self.sessions() as session:
            await complete_lora_import_job(
                session,
                job_id=claim.job.job_id,
                verified=VerifiedLoraArtifact(
                    artifact_sha256=stored.sha256,
                    storage_bucket=stored.bucket,
                    object_key=stored.key,
                    object_version_id=stored.version_id,
                    object_etag=stored.etag,
                    byte_size=stored.size_bytes,
                    approval_id=approval_id,
                    provenance=stored.provenance,
                ),
                worker_id=claim.worker_id,
                expected_attempt=claim.attempt,
            )

    async def _record_failure(self, claim: LoraImportClaim, error: Exception) -> None:
        retryable = _retryable_import_error(error)
        code = _error_code(error)
        detail = _safe_error_detail(error)
        delay = min(
            self.settings.background_lora_import_retry_base_seconds
            * (2 ** max(claim.attempt - 1, 0)),
            self.settings.background_lora_import_retry_max_seconds,
        )
        async with self.sessions() as session:
            try:
                await fail_lora_import_job(
                    session,
                    job_id=claim.job.job_id,
                    worker_id=claim.worker_id,
                    expected_attempt=claim.attempt,
                    error_code=code,
                    error_detail=detail,
                    retryable=retryable,
                    retry_at=datetime.now(UTC) + timedelta(seconds=delay),
                )
            except LoraCatalogConflictError:
                await session.rollback()

    async def _activate_if_idle(self, artifact_id: UUID) -> bool:
        async with self.sessions() as session:
            artifact = await self._lock_idle_artifact(
                session,
                artifact_id=artifact_id,
                lifecycle=ManagedLoraLifecycle.PENDING_ACTIVATION,
                require_runtime_idle=False,
            )
            if artifact is None:
                return False
            await effective_artifact_manifest_from_settings(
                session,
                settings=self.settings,
                additional_artifact_ids=(artifact.id,),
                required_lora_sha256s=(),
            )
            await mark_managed_lora_active(
                session,
                artifact_id=artifact.id,
                expected_lock_version=artifact.lock_version,
                worker_id=self.worker_id,
                idempotency_key=f"lora-runtime-activate:{artifact.id}:{artifact.lock_version}",
            )
            return True

    async def _finish_retirement_if_idle(self, artifact_id: UUID) -> bool:
        async with self.sessions() as session:
            artifact = await self._lock_idle_artifact(
                session,
                artifact_id=artifact_id,
                lifecycle=ManagedLoraLifecycle.RETIRING,
            )
            if artifact is None:
                return False
            await mark_managed_lora_retired(
                session,
                artifact_id=artifact.id,
                expected_lock_version=artifact.lock_version,
                dependency_summary_hook=managed_lora_dependency_summary,
                worker_id=self.worker_id,
                idempotency_key=f"lora-runtime-retire:{artifact.id}:{artifact.lock_version}",
            )
            return True

    async def _purge_if_safe(self, artifact_id: UUID) -> bool:
        async with self.sessions() as session:
            artifact = await self._lock_idle_artifact(
                session,
                artifact_id=artifact_id,
                lifecycle=ManagedLoraLifecycle.RETIRED,
            )
            if artifact is None or not artifact.purge_requested:
                return False
            if await managed_lora_has_historical_reference(
                session,
                sha256=artifact.artifact_sha256,
            ):
                now = datetime.now(UTC)
                artifact.lifecycle_error_code = "historical_reference_retained"
                artifact.lifecycle_error_detail = (
                    "Stored bytes are retained because a frozen release references this LoRA."
                )
                artifact.lifecycle_retry_at = now + timedelta(hours=24)
                artifact.lock_version += 1
                artifact.updated_at = now
                session.add(
                    AuditEvent(
                        actor=self.worker_id,
                        action="lora.artifact.purge_deferred",
                        resource_type="managed_lora_artifact",
                        resource_id=artifact.id,
                        correlation_id=f"managed-lora:{artifact.id}",
                        detail={"reason": "historical_reference_retained"},
                        occurred_at=now,
                    )
                )
                await session.commit()
                return True
            key = artifact.object_key
            version_id = artifact.object_version_id
            lock_version = artifact.lock_version
            await self.store.delete_exact(key=key, version_id=version_id)
            await mark_managed_lora_purged(
                session,
                artifact_id=artifact.id,
                expected_lock_version=lock_version,
                deleted_object_key=key,
                deleted_object_version_id=version_id,
                worker_id=self.worker_id,
                idempotency_key=f"lora-runtime-purge:{artifact.id}:{lock_version}",
            )
            return True

    async def _record_lifecycle_failure(
        self,
        artifact_id: UUID,
        error: Exception,
    ) -> None:
        occurred_at = datetime.now(UTC)
        async with self.sessions() as session:
            artifact = await session.scalar(
                select(ManagedLoraArtifact)
                .where(
                    ManagedLoraArtifact.id == artifact_id,
                    ManagedLoraArtifact.lifecycle.in_(
                        (
                            ManagedLoraLifecycle.PENDING_ACTIVATION,
                            ManagedLoraLifecycle.RETIRING,
                            ManagedLoraLifecycle.RETIRED,
                        )
                    ),
                )
                .with_for_update()
            )
            if artifact is None:
                await session.rollback()
                return
            code, detail, base_delay = _lifecycle_error(error)
            artifact.lifecycle_error_count += 1
            delay = min(
                base_delay * (2 ** min(max(artifact.lifecycle_error_count - 1, 0), 6)),
                3600,
            )
            artifact.lifecycle_error_code = code
            artifact.lifecycle_error_detail = detail
            artifact.lifecycle_retry_at = occurred_at + timedelta(seconds=delay)
            artifact.lock_version += 1
            artifact.updated_at = occurred_at
            session.add(
                AuditEvent(
                    actor=self.worker_id,
                    action="lora.artifact.lifecycle_deferred",
                    resource_type="managed_lora_artifact",
                    resource_id=artifact.id,
                    correlation_id=f"managed-lora:{artifact.id}",
                    detail={
                        "error_code": code,
                        "retry_at": artifact.lifecycle_retry_at.isoformat(),
                    },
                    occurred_at=occurred_at,
                )
            )
            await session.commit()

    async def _lock_idle_artifact(
        self,
        session: AsyncSession,
        *,
        artifact_id: UUID,
        lifecycle: ManagedLoraLifecycle,
        require_runtime_idle: bool = True,
    ) -> ManagedLoraArtifact | None:
        artifact: ManagedLoraArtifact | None
        if not require_runtime_idle:
            artifact = await session.scalar(
                select(ManagedLoraArtifact)
                .where(
                    ManagedLoraArtifact.id == artifact_id,
                    ManagedLoraArtifact.lifecycle == lifecycle,
                )
                .with_for_update()
            )
            if artifact is None:
                await session.rollback()
            return artifact
        await session.scalar(
            select(ProviderBudgetGuard.id)
            .where(ProviderBudgetGuard.provider == "salad")
            .with_for_update()
        )
        deployment = await session.scalar(
            select(SaladDeployment)
            .where(
                SaladDeployment.is_current.is_(True),
                SaladDeployment.purpose == SaladDeploymentPurpose.IMAGE,
            )
            .with_for_update()
        )
        artifact = await session.scalar(
            select(ManagedLoraArtifact)
            .where(
                ManagedLoraArtifact.id == artifact_id,
                ManagedLoraArtifact.lifecycle == lifecycle,
            )
            .with_for_update()
        )
        if artifact is None:
            await session.rollback()
            return None
        dependencies = await managed_lora_dependency_summary(session, artifact.id)
        now = datetime.now(UTC)
        observed_at = (
            deployment.last_observed_at.replace(tzinfo=UTC)
            if deployment is not None
            and deployment.last_observed_at is not None
            and deployment.last_observed_at.tzinfo is None
            else (deployment.last_observed_at if deployment is not None else None)
        )
        provider_observation_unknown = (
            deployment is not None
            and deployment.provider_container_group_id is not None
            and (
                deployment.billing_observation_stale
                or deployment.unknown_since is not None
                or observed_at is None
                or now - observed_at > timedelta(seconds=_PROVIDER_IDLE_FRESHNESS_SECONDS)
                or deployment.observed_replicas is None
                or deployment.ready_replicas is None
            )
        )
        deployment_proven_stopped = (
            deployment is not None
            and deployment.state == SaladDeploymentState.STOPPED
            and deployment.desired_state == DesiredDeploymentState.STOPPED
            and deployment.stopped_at is not None
            and deployment.billing_active_instance_id is None
        )
        deployment_busy = (
            deployment is not None
            and not deployment_proven_stopped
            and (
                provider_observation_unknown
                or (deployment.observed_replicas or 0) > 0
                or (deployment.ready_replicas or 0) > 0
                or deployment.billing_active_instance_id is not None
                or deployment.min_replicas > 0
            )
        )
        # A never-activated artifact cannot be resident on a worker. Its
        # retirement/purge therefore need not wait behind an unrelated warm
        # Experiment lease; dependency checks still serialize any planned use.
        requires_provider_idle = artifact.activated_at is not None
        resident_sha256s = frozenset(
            deployment.runtime_managed_lora_sha256s or () if deployment is not None else ()
        )
        resident_has_active_work = (
            requires_provider_idle
            and artifact.artifact_sha256 in resident_sha256s
            and await runtime_has_active_work(session)
        )
        if (
            dependencies.has_dependencies
            or resident_has_active_work
            or (requires_provider_idle and deployment_busy)
        ):
            await session.rollback()
            return None
        return artifact


def _retryable_import_error(error: Exception) -> bool:
    if isinstance(error, CivitaiRateLimitError):
        return True
    if isinstance(error, CivitaiAPIError):
        return error.status_code in {408, 425, 429} or error.status_code >= 500
    if isinstance(error, CivitaiTransportError):
        return True
    if isinstance(error, ModelArtifactCleanupError):
        return True
    if isinstance(error, (ObjectNotFoundError, ObjectTooLargeError, ObjectConflictError)):
        return False
    if isinstance(error, ObjectStoreError):
        return True
    return isinstance(error, ComplianceRegistryConflictError)


def _lifecycle_error(error: Exception) -> tuple[str, str, int]:
    if isinstance(error, ManagedArtifactManifestError):
        return (
            "worker_manifest_rejected",
            "This LoRA cannot fit the current safe worker-artifact selection.",
            3600,
        )
    if isinstance(error, LoraRuntimeConfigurationError):
        return (
            "worker_manifest_unavailable",
            "The pinned worker-artifact configuration is unavailable.",
            300,
        )
    if isinstance(error, LoraCatalogConflictError):
        return (
            "lora_catalog_conflict",
            "The LoRA approval or lifecycle changed before activation completed.",
            3600,
        )
    return (
        "lora_storage_unavailable",
        "Private storage could not complete this LoRA lifecycle transition.",
        60,
    )


def _error_code(error: Exception) -> str:
    if isinstance(error, CivitaiRateLimitError):
        return "civitai_rate_limited"
    if isinstance(error, CivitaiTransportError):
        return "civitai_transport"
    if isinstance(error, CivitaiError):
        return "civitai_rejected"
    if isinstance(error, ModelArtifactValidationError):
        return "lora_safetensors_invalid"
    if isinstance(error, ModelArtifactIntegrityError):
        return "lora_integrity_failed"
    if isinstance(error, ModelArtifactCleanupError):
        return "lora_storage_cleanup"
    if isinstance(error, (ModelArtifactError, ObjectStoreError)):
        return "lora_storage_failed"
    if isinstance(error, ComplianceRegistryConflictError):
        return "lora_registry_conflict"
    return "lora_import_failed"


def _safe_error_detail(error: Exception) -> str:
    messages = {
        "civitai_rate_limited": "Civitai is rate limiting the import; it will retry.",
        "civitai_transport": "The Civitai transfer was interrupted; it will retry.",
        "civitai_rejected": "Civitai could not verify this exact LoRA version.",
        "lora_safetensors_invalid": "The file is not a structurally valid Safetensors LoRA.",
        "lora_integrity_failed": "The immutable file size or SHA-256 did not match.",
        "lora_storage_cleanup": "Private-storage cleanup did not finish; it will retry.",
        "lora_storage_failed": "Private storage could not finish the import.",
        "lora_registry_conflict": "The LoRA catalog changed during registration; it will retry.",
        "lora_import_failed": "The LoRA import failed safely before activation.",
    }
    return messages[_error_code(error)]
