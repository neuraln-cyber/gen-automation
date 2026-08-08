"""Low-interference operator handoff from review to exact destinations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    DerivativeJob,
    DerivativeOutput,
    FinishedSetArchive,
    MegaSetDelivery,
    PublicationAttempt,
    PublicationIntent,
    PublicationPackage,
    Release,
    ReleaseSelection,
    ReleaseVersion,
    ReviewTask,
    ReviewXSelection,
)
from gen_automation.domain.enums import (
    AdminRole,
    DerivativeJobState,
    FinishedSetArchiveState,
    MegaDeliveryState,
    PublicationAttemptState,
    PublicationIntentState,
    PublicationTarget,
    ReleasePhase,
    ReviewTaskState,
)
from gen_automation.services.publication import (
    PUBLICATION_EFFECT_APPROVAL_ATTESTATION,
    PublicationConflictError,
    PublicationDisabledError,
    PublicationInputError,
    PublicationIntentResult,
    PublicationNotFoundError,
    _load_frozen_outputs,
    _normalize_configuration,
    _normalize_credential_reference,
    approve_publication_intent,
    get_publication_guard,
    plan_publication_intent,
)


class OperatorDeliveryError(Exception):
    """Base error for the completed-set operator flow."""


class OperatorDeliveryNotFoundError(OperatorDeliveryError):
    pass


class OperatorDeliveryConflictError(OperatorDeliveryError):
    pass


class OperatorDeliveryInputError(OperatorDeliveryError, ValueError):
    pass


_TARGET_PUBLISHABLE_RELEASE_PHASES = frozenset(
    {
        ReleasePhase.RENDERING,
        ReleasePhase.READY_TO_PUBLISH,
        ReleasePhase.PUBLISHING,
        ReleasePhase.PUBLISHED,
    }
)


@dataclass(frozen=True, slots=True)
class DeliveryOutput:
    output_id: UUID
    selection_id: UUID
    display_order: int
    target: str
    object_key: str
    object_version_id: str
    width: int
    height: int
    source_asset_id: UUID | None = None
    source_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class DerivativeProgress:
    planned: bool
    total_jobs: int
    requested: int
    running: int
    retrying: int
    succeeded: int
    failed: int
    expected_full_outputs: int
    ready_full_outputs: int
    expected_x_teasers: int
    ready_x_teasers: int
    ready_for_destinations: bool
    cancelled: int = 0

    @property
    def active_jobs(self) -> int:
        """Jobs which can still make forward progress without operator repair."""

        return self.requested + self.running + self.retrying

    @property
    def outputs_ready(self) -> bool:
        """Whether every planned job and exact derivative output is complete."""

        return (
            self.planned
            and self.total_jobs == self.succeeded
            and self.active_jobs == 0
            and self.failed == 0
            and self.ready_full_outputs == self.expected_full_outputs
            and self.ready_x_teasers == self.expected_x_teasers
        )

    @property
    def full_outputs_ready(self) -> bool:
        """Whether clean full-set copies are ready, independent of X teasers."""

        return (
            self.planned
            and self.expected_full_outputs > 0
            and self.ready_full_outputs == self.expected_full_outputs
        )

    @property
    def terminal_failures(self) -> bool:
        """Whether output preparation ended with failures and no live work."""

        return self.failed - self.cancelled > 0 and self.active_jobs == 0

    @property
    def stalled(self) -> bool:
        """Whether planned work stopped short without an explicit failure."""

        return (
            self.planned
            and not self.outputs_ready
            and self.active_jobs == 0
            and not self.terminal_failures
        )


@dataclass(frozen=True, slots=True)
class DestinationState:
    key: str
    label: str
    state: str
    detail: str
    intent_id: UUID | None = None
    intent_digest: str | None = None
    intent_lock_version: int | None = None
    package_id: UUID | None = None
    package_parts: tuple[DeliveryPackagePart, ...] = ()
    completed_items: int | None = None
    total_items: int | None = None
    remote_path: str | None = None
    scheduled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DeliveryPackagePart:
    package_id: UUID
    part_number: int
    part_count: int
    first_ordinal: int
    last_ordinal: int


def package_parts_ready(parts: tuple[DeliveryPackagePart, ...]) -> bool:
    """Return whether every deterministic archive part is present exactly once."""

    expected_part_count = len(parts)
    return (
        expected_part_count > 0
        and tuple(sorted(part.part_number for part in parts))
        == tuple(range(1, expected_part_count + 1))
        and all(part.part_count == expected_part_count for part in parts)
    )


@dataclass(frozen=True, slots=True)
class OperatorDeliverySnapshot:
    review_task_id: UUID
    review_state: ReviewTaskState
    release_id: UUID
    release_version_id: UUID
    release_title: str
    release_phase: ReleasePhase
    x_selected_count: int
    progress: DerivativeProgress
    full_outputs: tuple[DeliveryOutput, ...]
    x_outputs: tuple[DeliveryOutput, ...]
    publishing_guard_enabled: bool
    publishing_guard_epoch: int | None
    publishing_guard_lock_version: int | None
    publishing_guard_changed_at: datetime | None
    destinations: tuple[DestinationState, ...]


@dataclass(frozen=True, slots=True)
class PreparedDestinations:
    patreon_intent_id: UUID
    x_intent_id: UUID | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class PreparedDestination:
    intent_id: UUID
    replayed: bool


async def load_operator_delivery(
    session: AsyncSession,
    *,
    review_task_id: UUID,
) -> OperatorDeliverySnapshot:
    """Load one exact, read-only delivery projection for the dashboard."""

    review_row = (
        await session.execute(
            select(ReviewTask, ReleaseVersion, Release)
            .join(ReleaseVersion, ReleaseVersion.id == ReviewTask.release_version_id)
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(ReviewTask.id == review_task_id)
        )
    ).one_or_none()
    if review_row is None:
        raise OperatorDeliveryNotFoundError("review task was not found")
    review_task, release_version, release = review_row

    selections = tuple(
        (
            await session.scalars(
                select(ReleaseSelection)
                .where(ReleaseSelection.review_task_id == review_task.id)
                .order_by(ReleaseSelection.display_order)
            )
        ).all()
    )
    x_selected_asset_ids = frozenset(
        (
            await session.scalars(
                select(ReviewXSelection.asset_id).where(
                    ReviewXSelection.review_task_id == review_task.id
                )
            )
        ).all()
    )
    jobs = tuple(
        (
            await session.scalars(
                select(DerivativeJob)
                .where(DerivativeJob.release_version_id == release_version.id)
                .order_by(DerivativeJob.requested_at, DerivativeJob.id)
            )
        ).all()
    )
    output_rows = (
        await session.execute(
            select(DerivativeOutput, DerivativeJob)
            .join(DerivativeJob, DerivativeJob.id == DerivativeOutput.derivative_job_id)
            .where(DerivativeJob.release_version_id == release_version.id)
        )
    ).all()
    succeeded_outputs = [
        output for output, job in output_rows if job.state == DerivativeJobState.SUCCEEDED
    ]
    selection_order = {selection.id: selection.display_order for selection in selections}
    selection_sources = {
        selection.id: (selection.asset_id, selection.source_sha256) for selection in selections
    }
    full_outputs = _ordered_outputs(
        succeeded_outputs,
        target="full",
        selection_order=selection_order,
        selection_sources=selection_sources,
    )
    x_outputs = _ordered_outputs(
        succeeded_outputs,
        target="x_teaser",
        selection_order=selection_order,
        selection_sources=selection_sources,
    )
    expected_x_count = sum(selection.asset_id in x_selected_asset_ids for selection in selections)
    state_counts = {state: sum(job.state == state for job in jobs) for state in DerivativeJobState}
    ready_for_destinations = (
        review_task.state == ReviewTaskState.COMPLETED
        and release.phase == ReleasePhase.READY_TO_PUBLISH
        and len(full_outputs) == len(selections)
        and len(x_outputs) == expected_x_count
        and not _duplicate_selection_targets(full_outputs)
        and not _duplicate_selection_targets(x_outputs)
    )
    progress = DerivativeProgress(
        planned=bool(jobs),
        total_jobs=len(jobs),
        requested=state_counts[DerivativeJobState.REQUESTED],
        running=(
            state_counts[DerivativeJobState.CLAIMED] + state_counts[DerivativeJobState.PROCESSING]
        ),
        retrying=state_counts[DerivativeJobState.RETRY_WAIT],
        succeeded=state_counts[DerivativeJobState.SUCCEEDED],
        failed=(
            state_counts[DerivativeJobState.FAILED] + state_counts[DerivativeJobState.CANCELLED]
        ),
        expected_full_outputs=len(selections),
        ready_full_outputs=len(full_outputs),
        expected_x_teasers=expected_x_count,
        ready_x_teasers=len(x_outputs),
        ready_for_destinations=ready_for_destinations,
        cancelled=state_counts[DerivativeJobState.CANCELLED],
    )

    try:
        guard = await get_publication_guard(session)
        guard_enabled = guard.enabled
        guard_epoch = guard.epoch
        guard_lock_version = guard.lock_version
        guard_changed_at = guard.changed_at
    except PublicationDisabledError:
        guard_enabled = False
        guard_epoch = None
        guard_lock_version = None
        guard_changed_at = None
    destinations = await _destination_states(
        session,
        release_id=release.id,
        release_version_id=release_version.id,
        x_selected_count=expected_x_count,
    )
    return OperatorDeliverySnapshot(
        review_task_id=review_task.id,
        review_state=review_task.state,
        release_id=release.id,
        release_version_id=release_version.id,
        release_title=release.title,
        release_phase=release.phase,
        x_selected_count=expected_x_count,
        progress=progress,
        full_outputs=full_outputs,
        x_outputs=x_outputs,
        publishing_guard_enabled=guard_enabled,
        publishing_guard_epoch=guard_epoch,
        publishing_guard_lock_version=guard_lock_version,
        publishing_guard_changed_at=guard_changed_at,
        destinations=destinations,
    )


async def prepare_operator_destinations(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    patreon_title: str,
    patreon_body: str,
    patreon_tier: str,
    patreon_tags: tuple[str, ...],
    public_preview_output_id: UUID,
    public_preview_attester_name: str,
    public_preview_attested_at: datetime | None = None,
    x_text: str,
    x_credential_reference: str | None,
    actor_user_id: UUID,
    actor_role: AdminRole,
    idempotency_key: str,
    now: datetime | None = None,
) -> PreparedDestinations:
    """Preflight, freeze, and authorize Patreon plus the selected X teasers."""

    prepared_at = _as_utc(now or datetime.now(UTC))
    preview_attested_at = _as_utc(public_preview_attested_at or prepared_at)
    snapshot = await load_operator_delivery(session, review_task_id=review_task_id)
    if snapshot.review_state != ReviewTaskState.COMPLETED:
        raise OperatorDeliveryConflictError("complete the review before preparing destinations")
    if not snapshot.progress.ready_for_destinations:
        raise OperatorDeliveryConflictError("all exact derivative outputs must be ready")
    if not snapshot.publishing_guard_enabled:
        raise OperatorDeliveryConflictError("the global publication guard is stopped")
    if public_preview_output_id not in {output.output_id for output in snapshot.full_outputs}:
        raise OperatorDeliveryInputError(
            "the Patreon public preview must be one accepted clean full output"
        )
    if snapshot.x_selected_count and x_credential_reference is None:
        raise OperatorDeliveryConflictError("the X credential reference is not configured")
    if snapshot.x_selected_count and not x_text.strip():
        raise OperatorDeliveryInputError("X post text is required for selected teasers")

    patreon_configuration = {
        "title": patreon_title,
        "body": patreon_body,
        "tier": patreon_tier,
        "tags": list(patreon_tags),
    }
    x_configuration = {"text": x_text}
    full_output_ids = tuple(output.output_id for output in snapshot.full_outputs)
    x_output_ids = tuple(output.output_id for output in snapshot.x_outputs)

    # Run every provider validator before the first durable commit. The service
    # calls below still repeat these checks while freezing their exact manifests.
    try:
        _normalize_configuration(PublicationTarget.PATREON, patreon_configuration)
        await _load_frozen_outputs(
            session,
            release_version_id=snapshot.release_version_id,
            target=PublicationTarget.PATREON,
            derivative_output_ids=full_output_ids,
            public_preview_output_id=public_preview_output_id,
        )
        if snapshot.x_selected_count:
            _normalize_configuration(PublicationTarget.X, x_configuration)
            _normalize_credential_reference(
                PublicationTarget.X,
                x_credential_reference,
            )
            await _load_frozen_outputs(
                session,
                release_version_id=snapshot.release_version_id,
                target=PublicationTarget.X,
                derivative_output_ids=x_output_ids,
                public_preview_output_id=None,
            )
    except PublicationInputError as error:
        raise OperatorDeliveryInputError(str(error)) from error
    except (PublicationNotFoundError, PublicationConflictError) as error:
        raise OperatorDeliveryConflictError(str(error)) from error

    try:
        patreon = await plan_publication_intent(
            session,
            release_version_id=snapshot.release_version_id,
            target=PublicationTarget.PATREON,
            configuration=patreon_configuration,
            derivative_output_ids=full_output_ids,
            planned_by_user_id=actor_user_id,
            idempotency_key=f"{idempotency_key}:patreon:plan",
            public_preview_output_id=public_preview_output_id,
            public_preview_attester_name=public_preview_attester_name,
            public_preview_attested_at=preview_attested_at,
            public_preview_attestation_timezone="UTC",
            now=prepared_at,
        )
        patreon_replayed = await _approve_if_needed(
            session,
            result=patreon,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            idempotency_key=f"{idempotency_key}:patreon:approve",
            now=prepared_at,
        )

        x_intent_id: UUID | None = None
        x_replayed = True
        if snapshot.x_selected_count:
            x_intent = await plan_publication_intent(
                session,
                release_version_id=snapshot.release_version_id,
                target=PublicationTarget.X,
                configuration=x_configuration,
                derivative_output_ids=x_output_ids,
                planned_by_user_id=actor_user_id,
                idempotency_key=f"{idempotency_key}:x:plan",
                credential_reference=x_credential_reference,
                now=prepared_at,
            )
            x_intent_id = x_intent.intent_id
            x_replayed = await _approve_if_needed(
                session,
                result=x_intent,
                actor_user_id=actor_user_id,
                actor_role=actor_role,
                idempotency_key=f"{idempotency_key}:x:approve",
                now=prepared_at,
            )
    except PublicationInputError as error:
        raise OperatorDeliveryInputError(str(error)) from error
    except (
        PublicationNotFoundError,
        PublicationConflictError,
        PublicationDisabledError,
    ) as error:
        raise OperatorDeliveryConflictError(str(error)) from error

    return PreparedDestinations(
        patreon_intent_id=patreon.intent_id,
        x_intent_id=x_intent_id,
        replayed=patreon.replayed and patreon_replayed and x_replayed,
    )


async def prepare_operator_patreon_destination(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    patreon_title: str,
    patreon_body: str,
    patreon_tier: str,
    patreon_tags: tuple[str, ...],
    public_preview_output_id: UUID,
    public_preview_attester_name: str,
    public_preview_attested_at: datetime | None = None,
    actor_user_id: UUID,
    actor_role: AdminRole,
    idempotency_key: str,
    now: datetime | None = None,
) -> PreparedDestination:
    """Freeze and authorize Patreon without creating or changing an X intent."""

    prepared_at = _as_utc(now or datetime.now(UTC))
    preview_attested_at = _as_utc(public_preview_attested_at or prepared_at)
    snapshot = await load_operator_delivery(session, review_task_id=review_task_id)
    _require_target_ready(snapshot, target=PublicationTarget.PATREON)
    if public_preview_output_id not in {output.output_id for output in snapshot.full_outputs}:
        raise OperatorDeliveryInputError(
            "the Patreon public preview must be one accepted clean full output"
        )

    configuration = {
        "title": patreon_title,
        "body": patreon_body,
        "tier": patreon_tier,
        "tags": list(patreon_tags),
    }
    output_ids = tuple(output.output_id for output in snapshot.full_outputs)
    try:
        _normalize_configuration(PublicationTarget.PATREON, configuration)
        await _load_frozen_outputs(
            session,
            release_version_id=snapshot.release_version_id,
            target=PublicationTarget.PATREON,
            derivative_output_ids=output_ids,
            public_preview_output_id=public_preview_output_id,
        )
        result = await plan_publication_intent(
            session,
            release_version_id=snapshot.release_version_id,
            target=PublicationTarget.PATREON,
            configuration=configuration,
            derivative_output_ids=output_ids,
            planned_by_user_id=actor_user_id,
            idempotency_key=f"{idempotency_key}:patreon:plan",
            public_preview_output_id=public_preview_output_id,
            public_preview_attester_name=public_preview_attester_name,
            public_preview_attested_at=preview_attested_at,
            public_preview_attestation_timezone="UTC",
            now=prepared_at,
        )
        approved_replayed = await _approve_if_needed(
            session,
            result=result,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            idempotency_key=f"{idempotency_key}:patreon:approve",
            now=prepared_at,
        )
    except PublicationInputError as error:
        raise OperatorDeliveryInputError(str(error)) from error
    except (
        PublicationNotFoundError,
        PublicationConflictError,
        PublicationDisabledError,
    ) as error:
        raise OperatorDeliveryConflictError(str(error)) from error
    return PreparedDestination(
        intent_id=result.intent_id,
        replayed=result.replayed and approved_replayed,
    )


async def prepare_operator_x_destination(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    x_text: str,
    x_credential_reference: str | None,
    actor_user_id: UUID,
    actor_role: AdminRole,
    idempotency_key: str,
    x_adult_content: bool = True,
    scheduled_at: datetime | None = None,
    now: datetime | None = None,
) -> PreparedDestination:
    """Freeze and authorize X without creating or changing a Patreon intent."""

    prepared_at = _as_utc(now or datetime.now(UTC))
    snapshot = await load_operator_delivery(session, review_task_id=review_task_id)
    _require_target_ready(snapshot, target=PublicationTarget.X)
    if not x_text.strip():
        raise OperatorDeliveryInputError("X post text is required for selected teasers")
    if x_credential_reference is None:
        raise OperatorDeliveryConflictError("the X credential reference is not configured")

    configuration = {"text": x_text, "adult_content": x_adult_content}
    output_ids = tuple(output.output_id for output in snapshot.x_outputs)
    try:
        _normalize_configuration(PublicationTarget.X, configuration)
        _normalize_credential_reference(PublicationTarget.X, x_credential_reference)
        await _load_frozen_outputs(
            session,
            release_version_id=snapshot.release_version_id,
            target=PublicationTarget.X,
            derivative_output_ids=output_ids,
            public_preview_output_id=None,
        )
        result = await plan_publication_intent(
            session,
            release_version_id=snapshot.release_version_id,
            target=PublicationTarget.X,
            configuration=configuration,
            derivative_output_ids=output_ids,
            planned_by_user_id=actor_user_id,
            idempotency_key=f"{idempotency_key}:x:plan",
            scheduled_at=scheduled_at,
            credential_reference=x_credential_reference,
            now=prepared_at,
        )
        approved_replayed = await _approve_if_needed(
            session,
            result=result,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            idempotency_key=f"{idempotency_key}:x:approve",
            now=prepared_at,
        )
    except PublicationInputError as error:
        raise OperatorDeliveryInputError(str(error)) from error
    except (
        PublicationNotFoundError,
        PublicationConflictError,
        PublicationDisabledError,
    ) as error:
        raise OperatorDeliveryConflictError(str(error)) from error
    return PreparedDestination(
        intent_id=result.intent_id,
        replayed=result.replayed and approved_replayed,
    )


def _require_target_ready(
    snapshot: OperatorDeliverySnapshot,
    *,
    target: PublicationTarget,
) -> None:
    if snapshot.review_state != ReviewTaskState.COMPLETED:
        raise OperatorDeliveryConflictError("complete the review before preparing destinations")
    if snapshot.release_phase not in _TARGET_PUBLISHABLE_RELEASE_PHASES:
        raise OperatorDeliveryConflictError("the current release is not publishable")
    if not snapshot.publishing_guard_enabled:
        raise OperatorDeliveryConflictError("the global publication guard is stopped")
    if target == PublicationTarget.PATREON:
        if not snapshot.progress.full_outputs_ready:
            raise OperatorDeliveryConflictError("all clean full outputs must be ready")
        return
    if snapshot.x_selected_count < 1:
        raise OperatorDeliveryInputError("select at least one X teaser before preparing X")
    if len(snapshot.x_outputs) != snapshot.x_selected_count:
        raise OperatorDeliveryConflictError("all selected X teaser outputs must be ready")


def operator_target_is_ready(
    snapshot: OperatorDeliverySnapshot,
    *,
    target: PublicationTarget,
) -> bool:
    """Return whether one target's exact inputs may be independently frozen."""

    try:
        _require_target_ready(snapshot, target=target)
    except OperatorDeliveryError:
        return False
    return True


async def _approve_if_needed(
    session: AsyncSession,
    *,
    result: PublicationIntentResult,
    actor_user_id: UUID,
    actor_role: AdminRole,
    idempotency_key: str,
    now: datetime,
) -> bool:
    intent = await session.get(PublicationIntent, result.intent_id)
    if intent is None:
        raise PublicationNotFoundError("publication intent was not found")
    if intent.state not in {
        PublicationIntentState.AWAITING_APPROVAL,
        PublicationIntentState.FAILED,
        PublicationIntentState.CANCELLED,
    }:
        return True
    approval = await approve_publication_intent(
        session,
        intent_id=intent.id,
        expected_intent_digest=intent.intent_digest,
        expected_lock_version=intent.lock_version,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        attestation=PUBLICATION_EFFECT_APPROVAL_ATTESTATION,
        approval_seconds=3_600,
        idempotency_key=idempotency_key,
        now=now,
    )
    return approval.replayed


def _ordered_outputs(
    outputs: list[DerivativeOutput],
    *,
    target: str,
    selection_order: dict[UUID, int],
    selection_sources: dict[UUID, tuple[UUID, str]],
) -> tuple[DeliveryOutput, ...]:
    matching = sorted(
        (
            output
            for output in outputs
            if output.target == target
            and output.release_selection_id in selection_sources
            and output.release_selection_id in selection_order
        ),
        key=lambda output: (selection_order.get(output.release_selection_id, 2**31), output.id),
    )
    return tuple(
        DeliveryOutput(
            output_id=output.id,
            selection_id=output.release_selection_id,
            display_order=selection_order.get(output.release_selection_id, 2**31),
            target=output.target,
            object_key=output.asset_object_key,
            object_version_id=output.asset_object_version_id,
            width=output.asset_width,
            height=output.asset_height,
            source_asset_id=selection_sources[output.release_selection_id][0],
            source_sha256=selection_sources[output.release_selection_id][1],
        )
        for output in matching
    )


def _duplicate_selection_targets(outputs: tuple[DeliveryOutput, ...]) -> bool:
    return len({output.selection_id for output in outputs}) != len(outputs)


async def _destination_states(
    session: AsyncSession,
    *,
    release_id: UUID,
    release_version_id: UUID,
    x_selected_count: int,
) -> tuple[DestinationState, ...]:
    intents = tuple(
        (
            await session.scalars(
                select(PublicationIntent)
                .where(PublicationIntent.release_id == release_id)
                .order_by(
                    case(
                        (
                            PublicationIntent.state.in_(
                                (
                                    PublicationIntentState.FAILED,
                                    PublicationIntentState.CANCELLED,
                                )
                            ),
                            1,
                        ),
                        else_=0,
                    ),
                    PublicationIntent.planned_at.desc(),
                    PublicationIntent.id.desc(),
                )
            )
        ).all()
    )
    latest_by_target: dict[PublicationTarget, PublicationIntent] = {}
    for intent in intents:
        latest_by_target.setdefault(intent.target, intent)
    patreon_intent = latest_by_target.get(PublicationTarget.PATREON)
    x_intent = latest_by_target.get(PublicationTarget.X)
    patreon = await _publication_destination(
        session,
        key="patreon",
        label="Patreon",
        intent=patreon_intent,
    )
    x = await _publication_destination(
        session,
        key="x",
        label="X",
        intent=x_intent,
        not_requested_detail=(
            "No teaser images were selected."
            if x_selected_count == 0
            else "Selected teasers have not been prepared."
        ),
    )
    mega = await _mega_destination(
        session,
        release_version_id=release_version_id,
    )
    return (patreon, mega, x)


async def _publication_destination(
    session: AsyncSession,
    *,
    key: str,
    label: str,
    intent: PublicationIntent | None,
    not_requested_detail: str = "Destination has not been prepared.",
) -> DestinationState:
    if intent is None:
        return DestinationState(
            key=key,
            label=label,
            state="not_prepared",
            detail=not_requested_detail,
        )
    attempt = await session.scalar(
        select(PublicationAttempt)
        .where(PublicationAttempt.intent_id == intent.id)
        .order_by(PublicationAttempt.attempt_no.desc())
        .limit(1)
    )
    packages = (
        tuple(
            (
                await session.scalars(
                    select(PublicationPackage)
                    .where(PublicationPackage.intent_id == intent.id)
                    .order_by(PublicationPackage.part_number)
                )
            ).all()
        )
        if intent.target == PublicationTarget.PATREON
        else ()
    )
    package = packages[0] if packages else None
    state, detail = _publication_state(intent, attempt)
    return DestinationState(
        key=key,
        label=label,
        state=state,
        detail=detail,
        intent_id=intent.id,
        intent_digest=intent.intent_digest,
        intent_lock_version=intent.lock_version,
        package_id=package.id if package is not None else None,
        package_parts=tuple(
            DeliveryPackagePart(
                package_id=item.id,
                part_number=item.part_number,
                part_count=item.part_count,
                first_ordinal=item.first_ordinal,
                last_ordinal=item.last_ordinal,
            )
            for item in packages
        ),
        scheduled_at=intent.scheduled_at,
    )


def _publication_state(
    intent: PublicationIntent,
    attempt: PublicationAttempt | None,
) -> tuple[str, str]:
    if intent.state == PublicationIntentState.PUBLISHED:
        return "published", "Provider publication is confirmed."
    if intent.state == PublicationIntentState.AWAITING_HUMAN:
        return (
            "ready",
            "Package ready for the signed-in Patreon browser driver or manual upload.",
        )
    if intent.state == PublicationIntentState.AWAITING_APPROVAL:
        return (
            "failed",
            "A fresh approval is required. Prepare destinations again to authorize and "
            "resume delivery.",
        )
    if intent.state == PublicationIntentState.UNKNOWN:
        return (
            "unknown",
            "The provider result is unknown. Confirm the outcome before any retry; "
            "the system will not publish again automatically.",
        )
    if intent.state in {
        PublicationIntentState.PROCESSING,
    } or (
        attempt is not None
        and attempt.state
        in {
            PublicationAttemptState.CLAIMED,
            PublicationAttemptState.PROCESSING,
        }
    ):
        return "running", "Destination preparation is running."
    if intent.state in {
        PublicationIntentState.FAILED,
        PublicationIntentState.CANCELLED,
        PublicationIntentState.UNKNOWN,
    }:
        return "failed", "Destination needs operator attention."
    return "queued", "Destination is authorized and queued."


async def _mega_destination(
    session: AsyncSession,
    *,
    release_version_id: UUID,
) -> DestinationState:
    archive = await session.scalar(
        select(FinishedSetArchive)
        .where(FinishedSetArchive.release_version_id == release_version_id)
        .order_by(FinishedSetArchive.created_at.desc(), FinishedSetArchive.id.desc())
        .limit(1)
    )
    if archive is None:
        return DestinationState(
            key="mega",
            label="MEGA",
            state="not_prepared",
            detail="MEGA has not been requested.",
        )
    delivery = await session.scalar(
        select(MegaSetDelivery).where(MegaSetDelivery.finished_set_archive_id == archive.id)
    )
    if getattr(archive, "mega_requested_at", None) is None and delivery is None:
        return DestinationState(
            key="mega",
            label="MEGA",
            state="not_prepared",
            detail="MEGA has not been requested.",
        )
    if archive.state == FinishedSetArchiveState.FAILED:
        return DestinationState(
            key="mega",
            label="MEGA",
            state="failed",
            detail="The clean finished-set archive failed before MEGA delivery.",
            completed_items=0,
            total_items=archive.selection_count,
        )
    if archive.state != FinishedSetArchiveState.READY:
        return DestinationState(
            key="mega",
            label="MEGA",
            state="queued",
            detail="Waiting for the clean finished-set archive.",
            completed_items=0,
            total_items=archive.selection_count,
        )
    if delivery is None:
        return DestinationState(
            key="mega",
            label="MEGA",
            state="queued",
            detail="Clean set ready; the requested MEGA upload is queued.",
            completed_items=0,
            total_items=archive.selection_count,
        )
    if delivery.state == MegaDeliveryState.SUCCEEDED:
        state = "published"
        detail = (
            "Every full-resolution image is accounted for and the completion manifest "
            "is verified on MEGA."
        )
    elif delivery.state == MegaDeliveryState.FAILED:
        state, detail = "failed", "MEGA delivery needs operator attention."
    elif delivery.state == MegaDeliveryState.CLAIMED:
        state = "running"
        detail = (
            f"Uploading full-resolution images ({delivery.uploaded_item_count} / "
            f"{delivery.total_item_count})."
        )
    elif delivery.state == MegaDeliveryState.RETRY_WAIT:
        state, detail = "queued", "MEGA upload will retry automatically."
    else:
        state, detail = "queued", "MEGA upload is queued."
    return DestinationState(
        key="mega",
        label="MEGA",
        state=state,
        detail=detail,
        completed_items=delivery.uploaded_item_count,
        total_items=delivery.total_item_count,
        remote_path=delivery.remote_folder,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OperatorDeliveryInputError("delivery timestamp must include a timezone")
    return value.astimezone(UTC)
