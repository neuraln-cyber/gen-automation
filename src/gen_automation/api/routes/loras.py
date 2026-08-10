"""Owner/admin API for CPU-only managed LoRA onboarding and retirement."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from sqlalchemy import select

from gen_automation.api.security import (
    ComplianceManager,
    ComplianceMutationPrincipal,
    ComplianceReader,
    Session,
)
from gen_automation.config import Settings
from gen_automation.db.models import ModelArtifactApproval
from gen_automation.domain.enums import (
    ApprovalStatus,
    LoraImportJobState,
    LoraImportSource,
    ManagedLoraLifecycle,
    ModelArtifactKind,
)
from gen_automation.domain.lora_api import (
    CivitaiFileRead,
    CivitaiResolveRead,
    CivitaiResolveRequest,
    CivitaiVersionRead,
    LoraActionRequest,
    LoraEntryRead,
    LoraImportRead,
    LoraLibraryRead,
    LoraMutationRead,
    LoraRetireRequest,
    ManualImportCreateRead,
    ManualImportUploadRead,
)
from gen_automation.domain.lora_catalog import (
    CivitaiLoraImportCreate,
    ManualLoraImportCreate,
    ManualUploadCompletion,
)
from gen_automation.integrations.civitai import (
    CivitaiClient,
    CivitaiError,
    CivitaiRateLimitError,
    CivitaiSourceSelectionError,
    CivitaiURLValidationError,
    parse_civitai_url,
)
from gen_automation.services.lora_catalog import (
    LoraCatalogConflictError,
    LoraCatalogInputError,
    LoraCatalogNotFoundError,
    LoraImportJobSnapshot,
    ManagedLoraArtifactSnapshot,
    cancel_lora_import_job,
    create_civitai_import_job,
    create_manual_import_job,
    get_lora_import_job,
    get_managed_lora,
    list_lora_import_jobs,
    list_managed_loras,
    mark_manual_upload_complete,
    restore_managed_lora,
    retire_managed_lora,
    retry_lora_import_job,
)
from gen_automation.services.managed_lora_dependencies import (
    managed_lora_dependency_summary,
    managed_lora_historical_reference_sha256s,
)
from gen_automation.storage.base import ObjectStoreError
from gen_automation.storage.model_artifacts import ModelArtifactError, ModelArtifactStore

router = APIRouter(prefix="/loras", tags=["loras"])
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
        description="Stable key for one logical LoRA mutation",
    ),
]


@router.get("", response_model=LoraLibraryRead)
async def get_lora_library(
    request: Request,
    session: Session,
    principal: ComplianceReader,
) -> LoraLibraryRead:
    _enabled_settings(request)
    managed = await list_managed_loras(session, actor_user_id=principal.user_id, limit=None)
    imports = await list_lora_import_jobs(session, actor_user_id=principal.user_id, limit=100)
    entries = await _library_entries(session, managed)
    return LoraLibraryRead(
        entries=entries,
        imports=[_import_read(item) for item in imports],
    )


@router.post("/civitai:resolve", response_model=CivitaiResolveRead)
async def resolve_civitai_lora(
    command: CivitaiResolveRequest,
    request: Request,
    _principal: ComplianceMutationPrincipal,
) -> CivitaiResolveRead:
    try:
        source = parse_civitai_url(command.url)
        client = _civitai_client(request)
        selected_version = command.version_id or source.version_id
        if source.model_id is not None and selected_version is None:
            choices = await client.list_lora_versions(source)
            return CivitaiResolveRead(
                model_id=source.model_id,
                commercial_image_allowed=True,
                versions=[
                    CivitaiVersionRead(
                        version_id=item.version_id,
                        name=item.name,
                        base_model=item.base_model,
                        target_filename=item.target_filename,
                        declared_size_bytes=item.declared_size_bytes,
                        sha256=item.sha256,
                    )
                    for item in choices
                ],
            )
        resolved = await client.resolve_lora(source, version_id=command.version_id)
    except CivitaiRateLimitError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Civitai is rate limiting checks; retry shortly",
        ) from error
    except (CivitaiSourceSelectionError, CivitaiURLValidationError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except (CivitaiError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Civitai metadata could not be verified",
        ) from error
    return CivitaiResolveRead(
        model_id=resolved.model_id,
        version_id=resolved.version_id,
        model_name=resolved.model_name,
        version_name=resolved.version_name,
        base_model=resolved.base_model,
        canonical_source_url=resolved.canonical_source_url,
        # Civitai exposes per-model usage terms in the resolved model metadata;
        # the canonical version URL is the durable human-review reference.
        license_url=resolved.canonical_source_url,
        files=[
            CivitaiFileRead(
                file_id=resolved.file_id,
                name=resolved.target_filename,
                target_filename=resolved.target_filename,
                size_bytes=resolved.declared_size_bytes,
                sha256=resolved.sha256,
            )
        ],
        trained_words=list(resolved.trained_words),
        commercial_image_allowed=True,
    )


@router.post(
    "/imports/manual",
    response_model=ManualImportCreateRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_manual_lora_import(
    command: ManualLoraImportCreate,
    request: Request,
    response: Response,
    session: Session,
    principal: ComplianceMutationPrincipal,
    idempotency_key: IdempotencyKey,
) -> ManualImportCreateRead:
    settings = _enabled_settings(request)
    bucket = _model_bucket(settings)
    try:
        result = await create_manual_import_job(
            session,
            command=command,
            model_bucket=bucket,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
            max_attempts=settings.background_lora_import_max_attempts,
        )
        upload = await _model_store(request).create_quarantine_upload(
            upload_id=result.job.job_id,
            filename=result.job.target_filename,
            expires_in=settings.lora_upload_presign_ttl_seconds,
        )
    except (LoraCatalogInputError, ValueError) as error:
        raise _catalog_http_error(error) from error
    except (LoraCatalogConflictError, ModelArtifactError, ObjectStoreError) as error:
        raise _catalog_http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return ManualImportCreateRead(
        import_=_import_read(result.job),
        upload=ManualImportUploadRead(
            url=upload.grant.url,
            method="POST",
            fields=upload.grant.fields,
            headers=upload.grant.headers,
        ),
    )


@router.post(
    "/imports/civitai",
    response_model=LoraMutationRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_civitai_lora_import(
    command: CivitaiLoraImportCreate,
    request: Request,
    response: Response,
    session: Session,
    principal: ComplianceMutationPrincipal,
    idempotency_key: IdempotencyKey,
) -> LoraMutationRead:
    settings = _enabled_settings(request)
    try:
        assert command.civitai_model_id is not None  # validated by the domain model
        assert command.civitai_version_id is not None  # validated by the domain model
        canonical_source_url = (
            f"https://civitai.com/models/{command.civitai_model_id}"
            f"?modelVersionId={command.civitai_version_id}"
        )
        command = CivitaiLoraImportCreate.model_validate(
            {
                **command.model_dump(),
                "canonical_source_url": canonical_source_url,
                "license_url": canonical_source_url,
            }
        )
        result = await create_civitai_import_job(
            session,
            command=command,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
            max_attempts=settings.background_lora_import_max_attempts,
        )
    except (LoraCatalogInputError, LoraCatalogConflictError) as error:
        raise _catalog_http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return LoraMutationRead(
        import_=_import_read(result.job),
        changed=result.changed,
        replayed=result.replayed,
    )


@router.post("/imports/{job_id}:complete", response_model=LoraMutationRead)
async def complete_manual_lora_upload(
    job_id: UUID,
    command: ManualUploadCompletion,
    request: Request,
    response: Response,
    session: Session,
    principal: ComplianceMutationPrincipal,
    idempotency_key: IdempotencyKey,
) -> LoraMutationRead:
    _enabled_settings(request)
    try:
        snapshot = await get_lora_import_job(
            session,
            job_id=job_id,
            actor_user_id=principal.user_id,
        )
        result = await mark_manual_upload_complete(
            session,
            job_id=job_id,
            completion=command,
            expected_lock_version=snapshot.lock_version,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
        )
    except (LoraCatalogInputError, LoraCatalogNotFoundError, LoraCatalogConflictError) as error:
        raise _catalog_http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return LoraMutationRead(
        import_=_import_read(result.job),
        changed=result.changed,
        replayed=result.replayed,
    )


@router.post("/imports/{job_id}:retry", response_model=LoraMutationRead)
async def retry_lora_import(
    job_id: UUID,
    command: LoraActionRequest,
    request: Request,
    response: Response,
    session: Session,
    principal: ComplianceMutationPrincipal,
    idempotency_key: IdempotencyKey,
) -> LoraMutationRead:
    _enabled_settings(request)
    try:
        expected = await _job_lock_version(session, principal.user_id, job_id, command)
        result = await retry_lora_import_job(
            session,
            job_id=job_id,
            expected_lock_version=expected,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
        )
    except (LoraCatalogInputError, LoraCatalogNotFoundError, LoraCatalogConflictError) as error:
        raise _catalog_http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return LoraMutationRead(
        import_=_import_read(result.job),
        changed=result.changed,
        replayed=result.replayed,
    )


@router.post("/imports/{job_id}:cancel", response_model=LoraMutationRead)
async def cancel_lora_import(
    job_id: UUID,
    command: LoraActionRequest,
    request: Request,
    response: Response,
    session: Session,
    principal: ComplianceMutationPrincipal,
    idempotency_key: IdempotencyKey,
) -> LoraMutationRead:
    _enabled_settings(request)
    try:
        expected = await _job_lock_version(session, principal.user_id, job_id, command)
        result = await cancel_lora_import_job(
            session,
            job_id=job_id,
            expected_lock_version=expected,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
        )
    except (LoraCatalogInputError, LoraCatalogNotFoundError, LoraCatalogConflictError) as error:
        raise _catalog_http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return LoraMutationRead(
        import_=_import_read(result.job),
        changed=result.changed,
        replayed=result.replayed,
    )


@router.post("/{artifact_id}:retire", response_model=LoraMutationRead)
async def retire_lora(
    artifact_id: UUID,
    command: LoraRetireRequest,
    request: Request,
    response: Response,
    session: Session,
    principal: ComplianceManager,
    idempotency_key: IdempotencyKey,
) -> LoraMutationRead:
    _enabled_settings(request)
    try:
        expected = await _artifact_lock_version(session, principal.user_id, artifact_id, command)
        result = await retire_managed_lora(
            session,
            artifact_id=artifact_id,
            expected_lock_version=expected,
            purge_requested=command.purge_requested,
            dependency_summary_hook=managed_lora_dependency_summary,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
        )
    except (LoraCatalogInputError, LoraCatalogNotFoundError, LoraCatalogConflictError) as error:
        raise _catalog_http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return LoraMutationRead(
        entry=_managed_entry(result.artifact),
        changed=result.changed,
        replayed=result.replayed,
    )


@router.post("/{artifact_id}:restore", response_model=LoraMutationRead)
async def restore_lora(
    artifact_id: UUID,
    command: LoraActionRequest,
    request: Request,
    response: Response,
    session: Session,
    principal: ComplianceManager,
    idempotency_key: IdempotencyKey,
) -> LoraMutationRead:
    _enabled_settings(request)
    try:
        expected = await _artifact_lock_version(session, principal.user_id, artifact_id, command)
        result = await restore_managed_lora(
            session,
            artifact_id=artifact_id,
            expected_lock_version=expected,
            dependency_summary_hook=managed_lora_dependency_summary,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
        )
    except (LoraCatalogInputError, LoraCatalogNotFoundError, LoraCatalogConflictError) as error:
        raise _catalog_http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return LoraMutationRead(
        entry=_managed_entry(result.artifact),
        changed=result.changed,
        replayed=result.replayed,
    )


async def _job_lock_version(
    session: Session,
    actor_id: UUID,
    job_id: UUID,
    command: LoraActionRequest,
) -> int:
    if command.expected_lock_version is not None:
        return command.expected_lock_version
    return (await get_lora_import_job(session, job_id=job_id, actor_user_id=actor_id)).lock_version


async def _artifact_lock_version(
    session: Session,
    actor_id: UUID,
    artifact_id: UUID,
    command: LoraActionRequest,
) -> int:
    if command.expected_lock_version is not None:
        return command.expected_lock_version
    return (
        await get_managed_lora(session, artifact_id=artifact_id, actor_user_id=actor_id)
    ).lock_version


async def _library_entries(
    session: Session,
    managed: tuple[ManagedLoraArtifactSnapshot, ...],
) -> list[LoraEntryRead]:
    historically_referenced = await managed_lora_historical_reference_sha256s(
        session,
        sha256s=(
            item.artifact_sha256
            for item in managed
            if item.purge_requested and item.lifecycle == ManagedLoraLifecycle.RETIRED
        ),
    )
    managed_by_approval: dict[UUID, ManagedLoraArtifactSnapshot] = {}
    for managed_item in managed:
        if managed_item.lifecycle != ManagedLoraLifecycle.PURGED:
            managed_by_approval.setdefault(managed_item.approval_id, managed_item)
    for managed_item in managed:
        managed_by_approval.setdefault(managed_item.approval_id, managed_item)
    approvals = list(
        (
            await session.scalars(
                select(ModelArtifactApproval)
                .where(
                    ModelArtifactApproval.kind == ModelArtifactKind.LORA,
                    ModelArtifactApproval.status == ApprovalStatus.APPROVED,
                    ModelArtifactApproval.is_current.is_(True),
                )
                .order_by(ModelArtifactApproval.name, ModelArtifactApproval.id)
            )
        ).all()
    )
    current_approval_ids = {item.id for item in approvals}
    entries: list[LoraEntryRead] = []
    seen_managed: set[UUID] = set()
    for approval in approvals:
        matched_managed = managed_by_approval.get(approval.id)
        if matched_managed is not None:
            entries.append(
                _managed_entry(
                    matched_managed,
                    historically_referenced=(
                        matched_managed.artifact_sha256 in historically_referenced
                    ),
                    approval_current=True,
                )
            )
            seen_managed.add(matched_managed.artifact_id)
            continue
        managed_prefix = approval.storage_key.startswith("worker/managed-loras/sha256/")
        entries.append(
            LoraEntryRead(
                id=f"static-{approval.id}",
                name=approval.name,
                status="catalog_error" if managed_prefix else "active",
                readiness_status="unavailable" if managed_prefix else "ready",
                size_bytes=0,
                sha256=approval.artifact_sha256,
                source_url=approval.source_url,
                source_label=(
                    "Managed catalog needs repair" if managed_prefix else "Protected baseline"
                ),
                version_name=None,
                trigger_words=[],
                updated_at=approval.approved_at,
                can_retire=False,
                can_restore=False,
                lock_version=None,
            )
        )
    entries.extend(
        _managed_entry(
            item,
            historically_referenced=item.artifact_sha256 in historically_referenced,
            approval_current=item.approval_id in current_approval_ids,
        )
        for item in managed
        if item.artifact_id not in seen_managed
    )
    return entries


def _managed_entry(
    item: ManagedLoraArtifactSnapshot,
    *,
    historically_referenced: bool = False,
    approval_current: bool = True,
) -> LoraEntryRead:
    readiness = {
        ManagedLoraLifecycle.PENDING_ACTIVATION: "activating",
        ManagedLoraLifecycle.ACTIVE: "ready",
        ManagedLoraLifecycle.RETIRING: "deleting",
        ManagedLoraLifecycle.RETIRED: "removed",
        ManagedLoraLifecycle.PURGED: "removed",
    }[item.lifecycle]
    if not approval_current and item.lifecycle != ManagedLoraLifecycle.PURGED:
        readiness = "unavailable"
    elif (
        item.lifecycle_error_code is not None
        and item.lifecycle_error_code != "historical_reference_retained"
    ):
        readiness = "failed"
    version_name = None
    raw_version = (
        item.provenance.get("verified", {}).get("version_name")
        if isinstance(item.provenance.get("verified"), dict)
        else None
    )
    if isinstance(raw_version, str):
        version_name = raw_version
    return LoraEntryRead(
        id=item.artifact_id,
        name=item.display_name,
        status=item.lifecycle.value,
        readiness_status=readiness,
        size_bytes=(0 if item.lifecycle == ManagedLoraLifecycle.PURGED else item.byte_size),
        sha256=item.artifact_sha256,
        source_url=item.canonical_source_url,
        source_label=(
            "Civitai" if item.source_type == LoraImportSource.CIVITAI else "Manual upload"
        ),
        version_name=version_name,
        trigger_words=list(item.trigger_words),
        updated_at=item.updated_at,
        can_retire=item.lifecycle
        in {
            ManagedLoraLifecycle.PENDING_ACTIVATION,
            ManagedLoraLifecycle.ACTIVE,
        },
        can_restore=item.lifecycle
        in {
            ManagedLoraLifecycle.RETIRING,
            ManagedLoraLifecycle.RETIRED,
        },
        lock_version=item.lock_version,
        purge_requested=item.purge_requested,
        storage_retained_reason=(
            "Stored bytes are retained because an existing release references this exact LoRA."
            if item.purge_requested
            and item.lifecycle == ManagedLoraLifecycle.RETIRED
            and historically_referenced
            else None
        ),
        lifecycle_error_code=item.lifecycle_error_code,
        lifecycle_error=item.lifecycle_error_detail,
        lifecycle_retry_at=item.lifecycle_retry_at,
    )


def _import_read(item: LoraImportJobSnapshot) -> LoraImportRead:
    retryable = item.state == LoraImportJobState.FAILED and item.attempts < item.max_attempts
    cancellable = item.state in {
        LoraImportJobState.AWAITING_UPLOAD,
        LoraImportJobState.QUEUED,
        LoraImportJobState.RETRY_WAIT,
        LoraImportJobState.FAILED,
    }
    return LoraImportRead(
        id=item.job_id,
        name=item.display_name,
        source_kind=item.source_type,
        status=item.state,
        bytes_transferred=item.progress_bytes,
        total_bytes=item.total_bytes,
        error=item.last_error_detail,
        error_code=item.last_error_code,
        retryable=retryable,
        cancellable=cancellable,
        lock_version=item.lock_version,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _enabled_settings(request: Request) -> Settings:
    settings = cast(Settings, request.app.state.settings)
    if not settings.lora_manager_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LoRA management is not enabled",
        )
    return settings


def _model_bucket(settings: Settings) -> str:
    value = settings.salad_worker_artifact_bucket
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LoRA private storage is unavailable",
        )
    return value.get_secret_value()


def _model_store(request: Request) -> ModelArtifactStore:
    value = getattr(request.app.state, "model_artifact_store", None)
    if not isinstance(value, ModelArtifactStore):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LoRA private storage is unavailable",
        )
    return value


def _civitai_client(request: Request) -> CivitaiClient:
    value = getattr(request.app.state, "civitai_client", None)
    if not isinstance(value, CivitaiClient):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Civitai import is unavailable",
        )
    return value


def _catalog_http_error(error: Exception) -> HTTPException:
    if isinstance(error, LoraCatalogNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, LoraCatalogConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, (LoraCatalogInputError, ValueError)):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="LoRA storage is temporarily unavailable",
    )
