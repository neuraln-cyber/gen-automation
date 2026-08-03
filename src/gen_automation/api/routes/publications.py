from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from sqlalchemy import select

from gen_automation.api.security import (
    PublicationMutationOwner,
    PublicationMutationPrincipal,
    PublicationPrincipal,
    PublicationReader,
    Session,
    require_recent_principal,
)
from gen_automation.db.models import (
    PublicationAttempt,
    PublicationInput,
    PublicationIntent,
    PublicationPackage,
    PublicationStep,
)
from gen_automation.domain.publication import (
    PatreonPackageDownloadCreate,
    PatreonPackageDownloadRead,
    PublicationApprovalCreate,
    PublicationApprovalRead,
    PublicationAttemptRead,
    PublicationConfirmAbsent,
    PublicationConfirmPresent,
    PublicationGuardChange,
    PublicationGuardRead,
    PublicationInputRead,
    PublicationIntentCreate,
    PublicationIntentMutationRead,
    PublicationIntentRead,
    PublicationPackageRead,
    PublicationReconciliationRead,
    PublicationRevocationCreate,
    PublicationRevocationRead,
    PublicationStepRead,
)
from gen_automation.services.publication import (
    PatreonPackageDownloadResult,
    PublicationApprovalResult,
    PublicationConflictError,
    PublicationDisabledError,
    PublicationGuardResult,
    PublicationInputError,
    PublicationIntentResult,
    PublicationNotFoundError,
    PublicationReconciliationResult,
    PublicationRevocationResult,
    approve_publication_intent,
    get_publication_guard,
    plan_publication_intent,
    presign_patreon_package_download,
    reconcile_publication_absent,
    reconcile_publication_present,
    revoke_publication_intent,
    set_publication_guard,
)
from gen_automation.storage.base import ObjectStore

router = APIRouter(tags=["publications"])
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
        description="Stable unique key for this logical publication mutation",
    ),
]


@router.post(
    "/publication-intents",
    response_model=PublicationIntentMutationRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_publication_intent(
    command: PublicationIntentCreate,
    session: Session,
    principal: PublicationMutationPrincipal,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> PublicationIntentMutationRead:
    try:
        result = await plan_publication_intent(
            session,
            release_version_id=command.release_version_id,
            target=command.target,
            configuration=command.configuration,
            derivative_output_ids=command.derivative_output_ids,
            planned_by_user_id=principal.user_id,
            idempotency_key=idempotency_key,
            scheduled_at=command.scheduled_at,
            credential_reference=command.credential_reference,
            public_preview_output_id=command.public_preview_output_id,
            public_preview_attester_name=command.public_preview_attester_name,
            public_preview_attested_at=command.public_preview_attested_at,
            public_preview_attestation_timezone=(command.public_preview_attestation_timezone),
        )
    except (
        PublicationInputError,
        PublicationNotFoundError,
        PublicationConflictError,
    ) as error:
        raise _http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return _intent_mutation_read(result)


@router.get(
    "/publication-intents/{intent_id}",
    response_model=PublicationIntentRead,
)
async def get_publication_intent(
    intent_id: UUID,
    session: Session,
    _principal: PublicationReader,
) -> PublicationIntentRead:
    return await _intent_read(session, intent_id)


@router.get(
    "/releases/{release_id}/publication-intents",
    response_model=list[PublicationIntentRead],
)
async def get_release_publication_intents(
    release_id: UUID,
    session: Session,
    _principal: PublicationReader,
) -> list[PublicationIntentRead]:
    intent_ids = tuple(
        (
            await session.scalars(
                select(PublicationIntent.id)
                .where(PublicationIntent.release_id == release_id)
                .order_by(PublicationIntent.planned_at, PublicationIntent.id)
            )
        ).all()
    )
    return [await _intent_read(session, intent_id) for intent_id in intent_ids]


@router.post(
    "/publication-intents/{intent_id}:approve",
    response_model=PublicationApprovalRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_publication_approval(
    intent_id: UUID,
    command: PublicationApprovalCreate,
    session: Session,
    principal: PublicationPrincipal,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> PublicationApprovalRead:
    try:
        result = await approve_publication_intent(
            session,
            intent_id=intent_id,
            expected_intent_digest=command.expected_intent_digest,
            expected_lock_version=command.expected_lock_version,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            attestation=command.attestation,
            approval_seconds=command.approval_seconds,
            idempotency_key=idempotency_key,
        )
    except (
        PublicationInputError,
        PublicationNotFoundError,
        PublicationConflictError,
    ) as error:
        raise _http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return _approval_read(result)


@router.post(
    "/publication-intents/{intent_id}:revoke",
    response_model=PublicationRevocationRead,
)
async def post_publication_revocation(
    intent_id: UUID,
    command: PublicationRevocationCreate,
    session: Session,
    principal: PublicationMutationPrincipal,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> PublicationRevocationRead:
    try:
        result = await revoke_publication_intent(
            session,
            intent_id=intent_id,
            expected_intent_digest=command.expected_intent_digest,
            expected_lock_version=command.expected_lock_version,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            attestation=command.attestation,
            idempotency_key=idempotency_key,
        )
    except (
        PublicationInputError,
        PublicationNotFoundError,
        PublicationConflictError,
    ) as error:
        raise _http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return _revocation_read(result)


@router.post(
    "/publication-intents/{intent_id}:confirm-present",
    response_model=PublicationReconciliationRead,
)
async def post_publication_confirm_present(
    intent_id: UUID,
    command: PublicationConfirmPresent,
    session: Session,
    principal: PublicationPrincipal,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> PublicationReconciliationRead:
    try:
        result = await reconcile_publication_present(
            session,
            intent_id=intent_id,
            expected_intent_digest=command.expected_intent_digest,
            expected_lock_version=command.expected_lock_version,
            remote_identifier=command.remote_identifier,
            remote_url=command.remote_url,
            evidence=command.evidence,
            attestation=command.attestation,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            idempotency_key=idempotency_key,
        )
    except (
        PublicationInputError,
        PublicationNotFoundError,
        PublicationConflictError,
    ) as error:
        raise _http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return _reconciliation_read(result)


@router.post(
    "/publication-intents/{intent_id}:confirm-absent",
    response_model=PublicationReconciliationRead,
)
async def post_publication_confirm_absent(
    intent_id: UUID,
    command: PublicationConfirmAbsent,
    session: Session,
    principal: PublicationPrincipal,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> PublicationReconciliationRead:
    try:
        result = await reconcile_publication_absent(
            session,
            intent_id=intent_id,
            expected_intent_digest=command.expected_intent_digest,
            expected_lock_version=command.expected_lock_version,
            evidence=command.evidence,
            attestation=command.attestation,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            idempotency_key=idempotency_key,
        )
    except (
        PublicationInputError,
        PublicationNotFoundError,
        PublicationConflictError,
    ) as error:
        raise _http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return _reconciliation_read(result)


@router.post(
    "/publication-intents/{intent_id}/patreon-package:download",
    response_model=PatreonPackageDownloadRead,
)
async def post_patreon_package_download(
    intent_id: UUID,
    command: PatreonPackageDownloadCreate,
    request: Request,
    session: Session,
    principal: PublicationMutationPrincipal,
) -> PatreonPackageDownloadRead:
    store = cast(ObjectStore | None, request.app.state.object_store)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="object storage is unavailable",
        )
    try:
        result = await presign_patreon_package_download(
            session,
            store,
            intent_id=intent_id,
            expected_intent_digest=command.expected_intent_digest,
            expected_lock_version=command.expected_lock_version,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            expires_in_seconds=command.expires_in_seconds,
            part_number=command.part_number,
        )
    except (
        PublicationInputError,
        PublicationNotFoundError,
        PublicationConflictError,
    ) as error:
        raise _http_error(error) from error
    return _package_download_read(result)


@router.get(
    "/publication-guard",
    response_model=PublicationGuardRead,
)
async def get_global_publication_guard(
    session: Session,
    _principal: PublicationReader,
) -> PublicationGuardRead:
    try:
        result = await get_publication_guard(session)
    except PublicationConflictError as error:
        raise _http_error(error) from error
    return _guard_read(result)


@router.post(
    "/publication-guard",
    response_model=PublicationGuardRead,
)
async def post_global_publication_guard(
    command: PublicationGuardChange,
    request: Request,
    session: Session,
    principal: PublicationMutationOwner,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> PublicationGuardRead:
    if command.enabled:
        principal = await require_recent_principal(request, principal)
    try:
        result = await set_publication_guard(
            session,
            enabled=command.enabled,
            expected_epoch=command.expected_epoch,
            expected_lock_version=command.expected_lock_version,
            reason=command.reason,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
            idempotency_key=idempotency_key,
        )
    except (
        PublicationInputError,
        PublicationNotFoundError,
        PublicationConflictError,
    ) as error:
        raise _http_error(error) from error
    response.headers["Idempotency-Replayed"] = "false"
    return _guard_read(result)


async def _intent_read(
    session: Session,
    intent_id: UUID,
) -> PublicationIntentRead:
    intent = await session.get(PublicationIntent, intent_id)
    if intent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="publication intent was not found",
        )
    inputs = tuple(
        (
            await session.scalars(
                select(PublicationInput)
                .where(PublicationInput.intent_id == intent.id)
                .order_by(PublicationInput.ordinal)
            )
        ).all()
    )
    attempts = tuple(
        (
            await session.scalars(
                select(PublicationAttempt)
                .where(PublicationAttempt.intent_id == intent.id)
                .order_by(PublicationAttempt.attempt_no)
            )
        ).all()
    )
    attempt_reads: list[PublicationAttemptRead] = []
    for attempt in attempts:
        steps = tuple(
            (
                await session.scalars(
                    select(PublicationStep)
                    .where(PublicationStep.attempt_id == attempt.id)
                    .order_by(PublicationStep.ordinal)
                )
            ).all()
        )
        attempt_reads.append(
            PublicationAttemptRead(
                id=attempt.id,
                attempt_no=attempt.attempt_no,
                approval_id=attempt.approval_id,
                state=attempt.state,
                attempt_count=attempt.attempt_count,
                max_attempts=attempt.max_attempts,
                available_at=attempt.available_at,
                retry_at=attempt.retry_at,
                created_at=attempt.created_at,
                completed_at=attempt.completed_at,
                last_error_code=attempt.last_error_code,
                last_error_detail=attempt.last_error_detail,
                steps=[
                    PublicationStepRead(
                        id=step.id,
                        ordinal=step.ordinal,
                        kind=step.kind,
                        state=step.state,
                        retry_count=step.retry_count,
                        max_retries=step.max_retries,
                        retry_at=step.retry_at,
                        effect_started_at=step.effect_started_at,
                        effect_completed_at=step.effect_completed_at,
                        remote_identifier=step.remote_identifier,
                        remote_expires_at=step.remote_expires_at,
                        package_id=step.package_id,
                        last_error_code=step.last_error_code,
                        last_error_detail=step.last_error_detail,
                    )
                    for step in steps
                ],
            )
        )
    packages = list(
        (
            await session.scalars(
                select(PublicationPackage)
                .where(PublicationPackage.intent_id == intent.id)
                .order_by(PublicationPackage.part_number)
            )
        ).all()
    )
    package = packages[0] if packages else None

    def package_read(item: PublicationPackage) -> PublicationPackageRead:
        return PublicationPackageRead(
            id=item.id,
            part_number=item.part_number,
            part_count=item.part_count,
            first_ordinal=item.first_ordinal,
            last_ordinal=item.last_ordinal,
            sha256=item.sha256,
            manifest_sha256=item.manifest_sha256,
            byte_size=item.byte_size,
            content_type=item.content_type,
            created_at=item.created_at,
        )

    return PublicationIntentRead(
        id=intent.id,
        release_id=intent.release_id,
        release_version_id=intent.release_version_id,
        target=intent.target,
        state=intent.state,
        configuration=intent.configuration,
        configuration_sha256=intent.configuration_sha256,
        input_manifest_sha256=intent.input_manifest_sha256,
        intent_digest=intent.intent_digest,
        input_count=intent.input_count,
        scheduled_at=intent.scheduled_at,
        public_preview_attester_name=intent.public_preview_attester_name,
        public_preview_attested_at=intent.public_preview_attested_at,
        public_preview_attestation_timezone=(intent.public_preview_attestation_timezone),
        planned_at=intent.planned_at,
        lock_version=intent.lock_version,
        completed_at=intent.completed_at,
        last_error_code=intent.last_error_code,
        last_error_detail=intent.last_error_detail,
        inputs=[
            PublicationInputRead(
                id=item.id,
                ordinal=item.ordinal,
                role=item.role,
                derivative_output_id=item.derivative_output_id,
                derivative_target=item.derivative_target,
                asset_id=item.asset_id,
                asset_sha256=item.asset_sha256,
                content_type=item.asset_content_type,
                width=item.asset_width,
                height=item.asset_height,
                byte_size=item.asset_byte_size,
            )
            for item in inputs
        ],
        attempts=attempt_reads,
        package=(package_read(package) if package is not None else None),
        packages=[package_read(item) for item in packages],
    )


def _intent_mutation_read(
    result: PublicationIntentResult,
) -> PublicationIntentMutationRead:
    return PublicationIntentMutationRead(
        intent_id=result.intent_id,
        release_id=result.release_id,
        release_version_id=result.release_version_id,
        target=result.target,
        state=result.state,
        configuration_sha256=result.configuration_sha256,
        input_manifest_sha256=result.input_manifest_sha256,
        intent_digest=result.intent_digest,
        input_count=result.input_count,
        scheduled_at=result.scheduled_at,
        lock_version=result.lock_version,
        replayed=result.replayed,
    )


def _approval_read(result: PublicationApprovalResult) -> PublicationApprovalRead:
    return PublicationApprovalRead(
        intent_id=result.intent_id,
        approval_id=result.approval_id,
        attempt_id=result.attempt_id,
        approval_revision=result.approval_revision,
        intent_lock_version=result.intent_lock_version,
        expires_at=result.expires_at,
        state=result.state,
        replayed=result.replayed,
    )


def _revocation_read(
    result: PublicationRevocationResult,
) -> PublicationRevocationRead:
    return PublicationRevocationRead(
        intent_id=result.intent_id,
        approval_id=result.approval_id,
        approval_revision=result.approval_revision,
        intent_lock_version=result.intent_lock_version,
        state=result.state,
        replayed=result.replayed,
    )


def _reconciliation_read(
    result: PublicationReconciliationResult,
) -> PublicationReconciliationRead:
    return PublicationReconciliationRead(
        intent_id=result.intent_id,
        reconciliation_id=result.reconciliation_id,
        outcome=result.outcome,
        state=result.state,
        lock_version=result.lock_version,
        replayed=result.replayed,
    )


def _guard_read(result: PublicationGuardResult) -> PublicationGuardRead:
    return PublicationGuardRead(
        enabled=result.enabled,
        epoch=result.epoch,
        lock_version=result.lock_version,
        changed_at=result.changed_at,
    )


def _package_download_read(
    result: PatreonPackageDownloadResult,
) -> PatreonPackageDownloadRead:
    return PatreonPackageDownloadRead(
        intent_id=result.intent_id,
        package_id=result.package_id,
        url=result.url,
        filename=result.filename,
        sha256=result.sha256,
        manifest_sha256=result.manifest_sha256,
        byte_size=result.byte_size,
        expires_at=result.expires_at,
        part_number=result.part_number,
        part_count=result.part_count,
        first_ordinal=result.first_ordinal,
        last_ordinal=result.last_ordinal,
    )


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, PublicationInputError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="publication request is invalid",
        )
    if isinstance(error, PublicationNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="publication resource was not found",
        )
    if isinstance(error, PublicationDisabledError):
        return HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="publication is stopped",
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="publication state conflict",
    )
