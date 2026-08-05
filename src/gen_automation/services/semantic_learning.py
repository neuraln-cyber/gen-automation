"""Autonomous, owner-scoped semantic anatomy learning orchestration."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.db.models import (
    AdminUser,
    AuditEvent,
    SemanticLearningPolicy,
    SemanticModelPromotion,
    SemanticTrainingRun,
)
from gen_automation.domain.canonical import canonical_json_bytes, canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    SemanticPromotionDecision,
    SemanticTrainingKind,
    SemanticTrainingState,
    SemanticVerdict,
)
from gen_automation.services.semantic_feedback import (
    DEFAULT_CONFIGURED_BASELINE_THRESHOLD_MICROS,
    load_effective_semantic_threshold_micros,
)
from gen_automation.services.semantic_learning_readiness import (
    META_EVALUATION_MINIMUM_HOLDOUT_DEFECT,
    META_EVALUATION_MINIMUM_TRAINING_DEFECT,
    META_EVALUATION_MINIMUM_TRAINING_GOOD,
    SEMANTIC_LEARNING_READINESS_SCHEMA_VERSION,
    SemanticLearningSample,
    SemanticProfileLearningReadiness,
    load_semantic_learning_samples,
    summarize_semantic_learning_readiness,
)
from gen_automation.services.semantic_meta_classifier import (
    SEMANTIC_META_MODEL_SCHEMA_VERSION,
    SEMANTIC_META_SPLIT_SCHEMA_VERSION,
    SemanticMetaClassifierError,
    SemanticMetaDatasetSplit,
    SemanticMetaEvaluation,
    SemanticMetaGroupBy,
    SemanticMetaModel,
    SemanticMetaPromotionDecision,
    SemanticMetaTrainingParameters,
    build_semantic_meta_split_from_learning_samples,
    chronological_semantic_meta_split_from_learning_samples,
    compare_semantic_meta_challenger,
    deserialize_semantic_meta_model,
    evaluate_semantic_meta_model,
    evaluate_semantic_meta_predictions,
    fit_semantic_meta_classifier,
)

SEMANTIC_LEARNING_POLICY_SCHEMA_VERSION = "semantic-learning-policy/v1"
SEMANTIC_META_TRAINING_CONFIG_SCHEMA_VERSION = "semantic-meta-training-config/v1"
DEFAULT_VISUAL_RUN_COST_MICROUSD = 10_000_000
DEFAULT_MINIMUM_NEW_LABELS_FOR_RETRAIN = 50
DEFAULT_META_MAX_ATTEMPTS = 3
DEFAULT_META_LEASE_SECONDS = 300
_MINIMUM_HOLDOUT_GOOD_FOR_TWO_PERCENT_BOUND = 149


class SemanticLearningError(RuntimeError):
    """Base error for durable learning orchestration."""


class SemanticLearningNotReadyError(SemanticLearningError):
    """The requested owner/profile has not passed its readiness gates."""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        self.blockers = blockers
        super().__init__("semantic learning is not ready: " + "; ".join(blockers))


@dataclass(frozen=True, slots=True)
class SemanticLearningCycleResult:
    created_policies: int = 0
    queued_meta_runs: int = 0
    processed_meta_run: bool = False

    @property
    def did_work(self) -> bool:
        return bool(self.created_policies or self.queued_meta_runs or self.processed_meta_run)


@dataclass(frozen=True, slots=True)
class ClaimedSemanticTrainingRun:
    run_id: UUID
    owner_user_id: UUID
    profile_sha256: str
    dataset_sha256: str
    split_manifest_sha256: str
    worker_id: str
    lease_expires_at: datetime
    attempt: int
    max_attempts: int


@dataclass(frozen=True, slots=True)
class SemanticMetaTrainingOutput:
    model: SemanticMetaModel
    challenger: SemanticMetaEvaluation
    champion: SemanticMetaEvaluation
    promotion: SemanticMetaPromotionDecision
    model_payload: dict[str, Any]
    evaluation_report: dict[str, Any]
    evaluation_sha256: str


async def ensure_semantic_learning_policy(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    now: datetime | None = None,
) -> tuple[SemanticLearningPolicy, bool]:
    """Create the low-friction standing policy once for an active owner."""

    owner = await session.scalar(
        select(AdminUser).where(
            AdminUser.id == owner_user_id,
            AdminUser.role == AdminRole.OWNER,
            AdminUser.is_active.is_(True),
        )
    )
    if owner is None:
        raise SemanticLearningError("semantic learning policy requires an active owner")
    existing = await session.get(SemanticLearningPolicy, owner_user_id)
    if existing is not None:
        return existing, False
    created_at = _as_utc(now or datetime.now(UTC))
    policy = SemanticLearningPolicy(
        owner_user_id=owner_user_id,
        learning_enabled=True,
        auto_train_meta=True,
        auto_train_visual=True,
        auto_promote_validated=True,
        max_visual_run_microusd=DEFAULT_VISUAL_RUN_COST_MICROUSD,
        minimum_new_labels_for_retrain=DEFAULT_MINIMUM_NEW_LABELS_FOR_RETRAIN,
        lock_version=1,
        updated_at=created_at,
    )
    try:
        async with session.begin_nested():
            session.add(policy)
            await session.flush()
    except IntegrityError:
        existing = await session.get(SemanticLearningPolicy, owner_user_id)
        if existing is None:
            raise
        return existing, False
    session.add(
        AuditEvent(
            actor=str(owner_user_id),
            action="semantic_learning.policy_enabled",
            resource_type="semantic_learning_policy",
            resource_id=owner_user_id,
            correlation_id=f"semantic-learning:{owner_user_id}",
            detail={
                "schema_version": SEMANTIC_LEARNING_POLICY_SCHEMA_VERSION,
                "auto_train_meta": True,
                "auto_train_visual": True,
                "auto_promote_validated": True,
                "max_visual_run_microusd": DEFAULT_VISUAL_RUN_COST_MICROUSD,
            },
            occurred_at=created_at,
        )
    )
    await session.flush()
    return policy, True


async def ensure_owner_semantic_learning_policies(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    owner_ids = tuple(
        (
            await session.scalars(
                select(AdminUser.id).where(
                    AdminUser.role == AdminRole.OWNER,
                    AdminUser.is_active.is_(True),
                )
            )
        ).all()
    )
    created = 0
    for owner_user_id in owner_ids:
        _policy, was_created = await ensure_semantic_learning_policy(
            session,
            owner_user_id=owner_user_id,
            now=now,
        )
        created += int(was_created)
    return created


async def update_semantic_learning_policy(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    expected_lock_version: int,
    learning_enabled: bool,
    auto_train_meta: bool,
    auto_train_visual: bool,
    auto_promote_validated: bool,
    max_visual_run_microusd: int,
    minimum_new_labels_for_retrain: int,
    now: datetime | None = None,
) -> SemanticLearningPolicy:
    if not 1 <= max_visual_run_microusd <= 25_000_000:
        raise SemanticLearningError("visual training cost cap must be between $0.000001 and $25")
    if not 1 <= minimum_new_labels_for_retrain <= 10_000:
        raise SemanticLearningError("retraining label interval must be between 1 and 10000")
    policy, _created = await ensure_semantic_learning_policy(
        session,
        owner_user_id=owner_user_id,
        now=now,
    )
    if policy.lock_version != expected_lock_version:
        raise SemanticLearningError("semantic learning settings changed; reload and retry")
    policy.learning_enabled = bool(learning_enabled)
    policy.auto_train_meta = bool(auto_train_meta)
    policy.auto_train_visual = bool(auto_train_visual)
    policy.auto_promote_validated = bool(auto_promote_validated)
    policy.max_visual_run_microusd = max_visual_run_microusd
    policy.minimum_new_labels_for_retrain = minimum_new_labels_for_retrain
    policy.lock_version += 1
    policy.updated_at = _as_utc(now or datetime.now(UTC))
    session.add(
        AuditEvent(
            actor=str(owner_user_id),
            action="semantic_learning.policy_updated",
            resource_type="semantic_learning_policy",
            resource_id=owner_user_id,
            correlation_id=f"semantic-learning:{owner_user_id}",
            detail={
                "learning_enabled": policy.learning_enabled,
                "auto_train_meta": policy.auto_train_meta,
                "auto_train_visual": policy.auto_train_visual,
                "auto_promote_validated": policy.auto_promote_validated,
                "max_visual_run_microusd": policy.max_visual_run_microusd,
                "minimum_new_labels_for_retrain": (policy.minimum_new_labels_for_retrain),
                "lock_version": policy.lock_version,
            },
            occurred_at=policy.updated_at,
        )
    )
    await session.flush()
    return policy


async def enqueue_ready_meta_training_runs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    owner_user_id: UUID | None = None,
) -> int:
    """Queue at most one idempotent CPU challenger per ready owner/profile snapshot."""

    created_at = _as_utc(now or datetime.now(UTC))
    query = select(SemanticLearningPolicy).where(
        SemanticLearningPolicy.learning_enabled.is_(True),
        SemanticLearningPolicy.auto_train_meta.is_(True),
    )
    if owner_user_id is not None:
        query = query.where(SemanticLearningPolicy.owner_user_id == owner_user_id)
    policies = tuple(
        (await session.scalars(query.order_by(SemanticLearningPolicy.owner_user_id))).all()
    )
    queued = 0
    for policy in policies:
        samples = await load_semantic_learning_samples(
            session,
            owner_user_id=policy.owner_user_id,
        )
        report = summarize_semantic_learning_readiness(samples)
        samples_by_profile: dict[str, list[SemanticLearningSample]] = {}
        for sample in samples:
            samples_by_profile.setdefault(sample.profile_sha256, []).append(sample)
        for profile in report.profiles:
            if not profile.meta_classifier.ready or not profile.meta_evaluation.ready:
                continue
            profile_samples = tuple(samples_by_profile.get(profile.profile_sha256, ()))
            split = _meta_split(profile_samples, profile)
            if not await _retrain_interval_satisfied(session, policy=policy, profile=profile):
                continue
            run = await _create_meta_training_run(
                session,
                policy=policy,
                profile=profile,
                split=split,
                now=created_at,
            )
            queued += int(run is not None)
    return queued


async def request_meta_training_run(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    profile_sha256: str,
    now: datetime | None = None,
) -> SemanticTrainingRun:
    """Idempotently request the current ready snapshot, bypassing only the cadence delta."""

    policy, _created = await ensure_semantic_learning_policy(
        session,
        owner_user_id=owner_user_id,
        now=now,
    )
    samples = await load_semantic_learning_samples(
        session,
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
    )
    report = summarize_semantic_learning_readiness(samples)
    if len(report.profiles) != 1:
        raise SemanticLearningNotReadyError(("no learning data exists for this profile",))
    profile = report.profiles[0]
    blockers = (*profile.meta_classifier.blockers, *profile.meta_evaluation.blockers)
    if blockers:
        raise SemanticLearningNotReadyError(tuple(dict.fromkeys(blockers)))
    split = _meta_split(samples, profile)
    created = await _create_meta_training_run(
        session,
        policy=policy,
        profile=profile,
        split=split,
        now=_as_utc(now or datetime.now(UTC)),
    )
    if created is not None:
        return created
    existing = await session.scalar(
        select(SemanticTrainingRun).where(
            SemanticTrainingRun.owner_user_id == owner_user_id,
            SemanticTrainingRun.kind == SemanticTrainingKind.META_CLASSIFIER,
            SemanticTrainingRun.dataset_sha256 == split.dataset_sha256,
            SemanticTrainingRun.training_config_sha256
            == canonical_sha256(_meta_training_config(profile)),
        )
    )
    if existing is None:
        raise SemanticLearningError("idempotent semantic training request was not found")
    return existing


async def claim_meta_training_run(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_META_LEASE_SECONDS,
    now: datetime | None = None,
) -> ClaimedSemanticTrainingRun | None:
    claimed_at = _as_utc(now or datetime.now(UTC))
    candidate = await session.scalar(
        select(SemanticTrainingRun)
        .where(
            SemanticTrainingRun.kind == SemanticTrainingKind.META_CLASSIFIER,
            SemanticTrainingRun.attempts < SemanticTrainingRun.max_attempts,
            SemanticTrainingRun.available_at <= claimed_at,
            or_(
                SemanticTrainingRun.state == SemanticTrainingState.QUEUED,
                (SemanticTrainingRun.state == SemanticTrainingState.PREPARING)
                & (SemanticTrainingRun.lease_expires_at <= claimed_at),
            ),
        )
        .order_by(SemanticTrainingRun.available_at, SemanticTrainingRun.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if candidate is None:
        return None
    candidate.state = SemanticTrainingState.PREPARING
    candidate.attempts += 1
    candidate.lease_owner = worker_id
    candidate.lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
    candidate.started_at = claimed_at
    candidate.last_error_code = None
    candidate.last_error_detail = None
    await session.flush()
    return ClaimedSemanticTrainingRun(
        run_id=candidate.id,
        owner_user_id=candidate.owner_user_id,
        profile_sha256=candidate.profile_sha256,
        dataset_sha256=candidate.dataset_sha256,
        split_manifest_sha256=candidate.split_manifest_sha256,
        worker_id=worker_id,
        lease_expires_at=candidate.lease_expires_at,
        attempt=candidate.attempts,
        max_attempts=candidate.max_attempts,
    )


async def process_claimed_meta_training_run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    claimed: ClaimedSemanticTrainingRun,
    configured_baseline_threshold_micros: int = (DEFAULT_CONFIGURED_BASELINE_THRESHOLD_MICROS),
) -> None:
    try:
        async with sessions() as session:
            run = await session.get(SemanticTrainingRun, claimed.run_id)
            if run is None:
                raise SemanticLearningError("claimed semantic training run no longer exists")
            samples = await load_semantic_learning_samples(
                session,
                owner_user_id=claimed.owner_user_id,
                profile_sha256=claimed.profile_sha256,
            )
            split = _split_from_run_manifest(samples, run)
            threshold = await load_effective_semantic_threshold_micros(
                session,
                profile_sha256=claimed.profile_sha256,
                configured_fallback_micros=configured_baseline_threshold_micros,
            )
            champion_model = await load_effective_semantic_meta_model(
                session,
                owner_user_id=claimed.owner_user_id,
                profile_sha256=claimed.profile_sha256,
            )
        output = await asyncio.to_thread(
            _fit_and_evaluate_meta_run,
            split,
            tuple(samples),
            threshold,
            champion_model,
        )
        await _complete_meta_training_run(
            sessions,
            claimed=claimed,
            split=split,
            output=output,
        )
    except Exception as error:
        await _retry_or_fail_meta_training_run(sessions, claimed=claimed, error=error)
        if isinstance(error, (SemanticLearningError, SemanticMetaClassifierError)):
            return
        raise


async def run_semantic_learning_cycle(
    sessions: async_sessionmaker[AsyncSession],
    *,
    worker_id: str,
    configured_baseline_threshold_micros: int = (DEFAULT_CONFIGURED_BASELINE_THRESHOLD_MICROS),
    lease_seconds: int = DEFAULT_META_LEASE_SECONDS,
) -> SemanticLearningCycleResult:
    """Perform one bounded policy/enqueue/train cycle without external spending."""

    async with sessions() as session:
        created_policies = await ensure_owner_semantic_learning_policies(session)
        queued = await enqueue_ready_meta_training_runs(session)
        await session.commit()
    async with sessions() as session:
        claimed = await claim_meta_training_run(
            session,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        await session.commit()
    if claimed is not None:
        await process_claimed_meta_training_run(
            sessions,
            claimed=claimed,
            configured_baseline_threshold_micros=(configured_baseline_threshold_micros),
        )
    return SemanticLearningCycleResult(
        created_policies=created_policies,
        queued_meta_runs=queued,
        processed_meta_run=claimed is not None,
    )


async def load_effective_semantic_meta_model(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    profile_sha256: str,
) -> SemanticMetaModel | None:
    promotion = await session.scalar(
        select(SemanticModelPromotion)
        .where(
            SemanticModelPromotion.owner_user_id == owner_user_id,
            SemanticModelPromotion.profile_sha256 == profile_sha256,
            SemanticModelPromotion.kind == SemanticTrainingKind.META_CLASSIFIER,
            SemanticModelPromotion.decision.in_(
                (
                    SemanticPromotionDecision.PROMOTED,
                    SemanticPromotionDecision.ROLLED_BACK,
                )
            ),
        )
        .order_by(SemanticModelPromotion.created_at.desc(), SemanticModelPromotion.id.desc())
        .limit(1)
    )
    if promotion is None:
        return None
    run = await session.get(SemanticTrainingRun, promotion.training_run_id)
    if (
        run is None
        or run.owner_user_id != owner_user_id
        or run.profile_sha256 != profile_sha256
        or run.kind != SemanticTrainingKind.META_CLASSIFIER
        or run.state != SemanticTrainingState.SUCCEEDED
        or run.model_payload is None
        or run.artifact_sha256 != promotion.artifact_sha256
        or run.dataset_sha256 != promotion.dataset_sha256
        or run.evaluation_sha256 != promotion.evaluation_sha256
    ):
        raise SemanticLearningError("promoted semantic model artifact is unavailable")
    model = deserialize_semantic_meta_model(canonical_json_bytes(run.model_payload))
    if (
        model.artifact_sha256 != promotion.artifact_sha256
        or model.owner_user_id != str(owner_user_id)
        or model.profile_sha256 != profile_sha256
        or model.split_manifest_sha256 != run.split_manifest_sha256
    ):
        raise SemanticLearningError("promoted semantic model artifact identity is invalid")
    return model


async def rollback_semantic_meta_model(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    actor_user_id: UUID,
    profile_sha256: str,
    reason: str,
    target_training_run_id: UUID | None = None,
    now: datetime | None = None,
) -> SemanticModelPromotion:
    """Append an owner-authorized rollback to a previously active CPU model."""

    if actor_user_id != owner_user_id:
        raise SemanticLearningError("semantic model rollback requires the owning user")
    actor = await session.scalar(
        select(AdminUser).where(
            AdminUser.id == actor_user_id,
            AdminUser.role == AdminRole.OWNER,
            AdminUser.is_active.is_(True),
        )
    )
    if actor is None:
        raise SemanticLearningError("semantic model rollback requires an active owner")
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise SemanticLearningError("semantic model rollback requires a reason")
    active = await _active_promotion_record(
        session,
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
    )
    if active is None:
        raise SemanticLearningError("no active semantic model exists to roll back")
    target_run_id = target_training_run_id or active.previous_training_run_id
    if target_run_id is None:
        raise SemanticLearningError("the active semantic model has no rollback target")
    if target_run_id == active.training_run_id:
        raise SemanticLearningError("rollback target is already active")
    target_was_active = await session.scalar(
        select(SemanticModelPromotion.id).where(
            SemanticModelPromotion.owner_user_id == owner_user_id,
            SemanticModelPromotion.profile_sha256 == profile_sha256,
            SemanticModelPromotion.kind == SemanticTrainingKind.META_CLASSIFIER,
            SemanticModelPromotion.training_run_id == target_run_id,
            SemanticModelPromotion.decision.in_(
                (
                    SemanticPromotionDecision.PROMOTED,
                    SemanticPromotionDecision.ROLLED_BACK,
                )
            ),
        )
    )
    if target_was_active is None:
        raise SemanticLearningError("rollback target was never an active semantic model")
    target = await session.get(SemanticTrainingRun, target_run_id)
    if (
        target is None
        or target.owner_user_id != owner_user_id
        or target.profile_sha256 != profile_sha256
        or target.kind != SemanticTrainingKind.META_CLASSIFIER
        or target.state != SemanticTrainingState.SUCCEEDED
        or target.artifact_sha256 is None
        or target.model_payload is None
        or target.evaluation_sha256 is None
    ):
        raise SemanticLearningError("rollback target artifact is unavailable")
    model = deserialize_semantic_meta_model(canonical_json_bytes(target.model_payload))
    if (
        model.artifact_sha256 != target.artifact_sha256
        or model.owner_user_id != str(owner_user_id)
        or model.profile_sha256 != profile_sha256
        or model.split_manifest_sha256 != target.split_manifest_sha256
    ):
        raise SemanticLearningError("rollback target artifact identity is invalid")
    rolled_back_at = _as_utc(now or datetime.now(UTC))
    event = SemanticModelPromotion(
        owner_user_id=owner_user_id,
        kind=SemanticTrainingKind.META_CLASSIFIER,
        training_run_id=target.id,
        previous_training_run_id=active.training_run_id,
        profile_sha256=profile_sha256,
        artifact_sha256=model.artifact_sha256,
        dataset_sha256=target.dataset_sha256,
        evaluation_sha256=target.evaluation_sha256,
        decision=SemanticPromotionDecision.ROLLED_BACK,
        keep_threshold_micros=model.keep_threshold_micros,
        reject_threshold_micros=model.reject_threshold_micros,
        reason=normalized_reason,
        created_by_user_id=actor_user_id,
        created_at=rolled_back_at,
    )
    session.add(event)
    await session.flush()
    session.add(
        AuditEvent(
            actor=str(actor_user_id),
            action="semantic_learning.meta_model_rolled_back",
            resource_type="semantic_model_promotion",
            resource_id=event.id,
            correlation_id=f"semantic-training:{target.id}",
            detail={
                "profile_sha256": profile_sha256,
                "target_training_run_id": str(target.id),
                "previous_training_run_id": str(active.training_run_id),
                "artifact_sha256": model.artifact_sha256,
                "reason": normalized_reason,
            },
            occurred_at=rolled_back_at,
        )
    )
    await session.flush()
    return event


async def _create_meta_training_run(
    session: AsyncSession,
    *,
    policy: SemanticLearningPolicy,
    profile: SemanticProfileLearningReadiness,
    split: SemanticMetaDatasetSplit,
    now: datetime,
) -> SemanticTrainingRun | None:
    config = _meta_training_config(profile)
    config_sha256 = canonical_sha256(config)
    existing = await session.scalar(
        select(SemanticTrainingRun.id).where(
            SemanticTrainingRun.owner_user_id == policy.owner_user_id,
            SemanticTrainingRun.kind == SemanticTrainingKind.META_CLASSIFIER,
            SemanticTrainingRun.dataset_sha256 == split.dataset_sha256,
            SemanticTrainingRun.training_config_sha256 == config_sha256,
        )
    )
    if existing is not None:
        return None
    run = SemanticTrainingRun(
        owner_user_id=policy.owner_user_id,
        kind=SemanticTrainingKind.META_CLASSIFIER,
        state=SemanticTrainingState.QUEUED,
        profile_sha256=profile.profile_sha256,
        dataset_sha256=split.dataset_sha256,
        dataset_schema_version=SEMANTIC_META_SPLIT_SCHEMA_VERSION,
        split_manifest=split.manifest_wire(),
        split_manifest_sha256=split.manifest_sha256,
        training_config=config,
        training_config_sha256=config_sha256,
        attempts=0,
        max_attempts=DEFAULT_META_MAX_ATTEMPTS,
        available_at=now,
        estimated_cost_microusd=0,
        actual_cost_microusd=0,
        created_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
    except IntegrityError:
        return None
    session.add(
        AuditEvent(
            actor=str(policy.owner_user_id),
            action="semantic_learning.meta_training_queued",
            resource_type="semantic_training_run",
            resource_id=run.id,
            correlation_id=f"semantic-training:{run.id}",
            detail={
                "profile_sha256": profile.profile_sha256,
                "dataset_sha256": split.dataset_sha256,
                "split_manifest_sha256": split.manifest_sha256,
                "binary_labeled_count": profile.binary_labeled_count,
                "estimated_cost_microusd": 0,
            },
            occurred_at=now,
        )
    )
    await session.flush()
    return run


async def _retrain_interval_satisfied(
    session: AsyncSession,
    *,
    policy: SemanticLearningPolicy,
    profile: SemanticProfileLearningReadiness,
) -> bool:
    latest = await session.scalar(
        select(SemanticTrainingRun)
        .where(
            SemanticTrainingRun.owner_user_id == policy.owner_user_id,
            SemanticTrainingRun.profile_sha256 == profile.profile_sha256,
            SemanticTrainingRun.kind == SemanticTrainingKind.META_CLASSIFIER,
            SemanticTrainingRun.state == SemanticTrainingState.SUCCEEDED,
        )
        .order_by(SemanticTrainingRun.completed_at.desc(), SemanticTrainingRun.id.desc())
        .limit(1)
    )
    if latest is None:
        return True
    previous_count = latest.training_config.get("binary_labeled_count")
    if not isinstance(previous_count, int):
        return True
    return profile.binary_labeled_count - previous_count >= policy.minimum_new_labels_for_retrain


def _meta_split(
    samples: tuple[SemanticLearningSample, ...],
    profile: SemanticProfileLearningReadiness,
) -> SemanticMetaDatasetSplit:
    group_by = (
        SemanticMetaGroupBy.RELEASE_SET
        if profile.split.recommended_group_key == "release_id"
        else SemanticMetaGroupBy.GENERATION_BATCH
    )
    return chronological_semantic_meta_split_from_learning_samples(
        samples,
        group_by=group_by,
        holdout_fraction=0.20,
        minimum_training_good=META_EVALUATION_MINIMUM_TRAINING_GOOD,
        minimum_training_defect=META_EVALUATION_MINIMUM_TRAINING_DEFECT,
        minimum_holdout_good=_MINIMUM_HOLDOUT_GOOD_FOR_TWO_PERCENT_BOUND,
        minimum_holdout_defect=META_EVALUATION_MINIMUM_HOLDOUT_DEFECT,
    )


def _meta_training_config(
    profile: SemanticProfileLearningReadiness,
) -> dict[str, Any]:
    parameters = SemanticMetaTrainingParameters()
    return {
        "schema_version": SEMANTIC_META_TRAINING_CONFIG_SCHEMA_VERSION,
        "model_schema_version": SEMANTIC_META_MODEL_SCHEMA_VERSION,
        "readiness_schema_version": SEMANTIC_LEARNING_READINESS_SCHEMA_VERSION,
        "readiness_dataset_sha256": profile.dataset_sha256,
        "binary_labeled_count": profile.binary_labeled_count,
        "anatomy_good_count": profile.anatomy_good_count,
        "anatomy_defect_count": profile.anatomy_defect_count,
        "group_key": profile.split.recommended_group_key,
        "parameters": parameters.to_wire(),
    }


def _split_from_run_manifest(
    samples: tuple[SemanticLearningSample, ...],
    run: SemanticTrainingRun,
) -> SemanticMetaDatasetSplit:
    manifest = run.split_manifest
    training_rows = manifest.get("training")
    holdout_rows = manifest.get("holdout")
    if not isinstance(training_rows, list) or not isinstance(holdout_rows, list):
        raise SemanticLearningError("stored semantic split manifest is invalid")
    by_id = {str(sample.feedback_id): sample for sample in samples}
    training_ids = _manifest_sample_ids(training_rows)
    holdout_ids = _manifest_sample_ids(holdout_rows)
    if not training_ids or not holdout_ids:
        raise SemanticLearningError("stored semantic split is empty")
    try:
        training = tuple(by_id[sample_id] for sample_id in training_ids)
        holdout = tuple(by_id[sample_id] for sample_id in holdout_ids)
    except KeyError as error:
        raise SemanticLearningError("stored semantic split sample is unavailable") from error
    group_key = run.training_config.get("group_key")
    group_by = (
        SemanticMetaGroupBy.RELEASE_SET
        if group_key == "release_id"
        else SemanticMetaGroupBy.GENERATION_BATCH
    )
    split = build_semantic_meta_split_from_learning_samples(
        training=training,
        holdout=holdout,
        group_by=group_by,
    )
    if (
        split.dataset_sha256 != run.dataset_sha256
        or split.manifest_sha256 != run.split_manifest_sha256
    ):
        raise SemanticLearningError("stored semantic split identity does not match")
    return split


def _manifest_sample_ids(rows: list[object]) -> tuple[str, ...]:
    result: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise SemanticLearningError("stored semantic split row is invalid")
        sample_id = row.get("sample_id")
        if not isinstance(sample_id, str):
            raise SemanticLearningError("stored semantic split sample ID is invalid")
        result.append(sample_id)
    return tuple(result)


def _fit_and_evaluate_meta_run(
    split: SemanticMetaDatasetSplit,
    samples: tuple[SemanticLearningSample, ...],
    champion_threshold_micros: int,
    champion_model: SemanticMetaModel | None,
) -> SemanticMetaTrainingOutput:
    model = fit_semantic_meta_classifier(split)
    challenger = evaluate_semantic_meta_model(model, split.holdout)
    if champion_model is None:
        champion = _evaluate_vlm_champion(
            split,
            samples=samples,
            reject_threshold_micros=champion_threshold_micros,
        )
        champion_name = "calibrated_vlm"
    else:
        champion = evaluate_semantic_meta_model(champion_model, split.holdout)
        champion_name = champion_model.artifact_sha256
    promotion = compare_semantic_meta_challenger(champion, challenger)
    model_payload = json.loads(model.serialize())
    evaluation_report: dict[str, Any] = {
        "schema_version": "semantic-meta-evaluation/v1",
        "holdout_sha256": challenger.evaluation_sha256,
        "champion": {"identity": champion_name, **asdict(champion)},
        "challenger": asdict(challenger),
        "promotion": asdict(promotion),
    }
    return SemanticMetaTrainingOutput(
        model=model,
        challenger=challenger,
        champion=champion,
        promotion=promotion,
        model_payload=model_payload,
        evaluation_report=evaluation_report,
        evaluation_sha256=canonical_sha256(evaluation_report),
    )


def _validate_meta_training_output(
    *,
    claimed: ClaimedSemanticTrainingRun,
    split: SemanticMetaDatasetSplit,
    output: SemanticMetaTrainingOutput,
) -> None:
    """Fail closed before a challenger artifact can enter the durable ledger."""

    model = output.model
    if split.owner_user_id != str(claimed.owner_user_id):
        raise SemanticLearningError("claimed semantic owner does not match its dataset")
    if split.profile_sha256 != claimed.profile_sha256:
        raise SemanticLearningError("claimed semantic profile does not match its dataset")
    if split.dataset_sha256 != claimed.dataset_sha256:
        raise SemanticLearningError("claimed semantic dataset identity does not match")
    if split.manifest_sha256 != claimed.split_manifest_sha256:
        raise SemanticLearningError("claimed semantic split identity does not match")
    if model.owner_user_id != str(claimed.owner_user_id):
        raise SemanticLearningError("trained semantic model owner identity does not match")
    if model.profile_sha256 != claimed.profile_sha256:
        raise SemanticLearningError("trained semantic model profile identity does not match")
    if model.training_dataset_sha256 != split.training_dataset_sha256:
        raise SemanticLearningError("trained semantic model dataset identity does not match")
    if model.split_manifest_sha256 != claimed.split_manifest_sha256:
        raise SemanticLearningError("trained semantic model split identity does not match")
    expected_payload: dict[str, Any] = json.loads(model.serialize())
    if output.model_payload != expected_payload:
        raise SemanticLearningError("trained semantic model payload is not canonical")
    if output.challenger.model_sha256 != model.artifact_sha256:
        raise SemanticLearningError("challenger evaluation model identity does not match")
    if output.champion.evaluation_sha256 != output.challenger.evaluation_sha256:
        raise SemanticLearningError("champion and challenger holdout identities differ")
    expected_promotion = compare_semantic_meta_challenger(
        output.champion,
        output.challenger,
    )
    if output.promotion != expected_promotion:
        raise SemanticLearningError("semantic promotion decision is not reproducible")
    if output.evaluation_sha256 != canonical_sha256(output.evaluation_report):
        raise SemanticLearningError("semantic evaluation report identity does not match")
    if output.evaluation_report.get("holdout_sha256") != (output.challenger.evaluation_sha256):
        raise SemanticLearningError("semantic evaluation report holdout does not match")
    if output.evaluation_report.get("challenger") != asdict(output.challenger):
        raise SemanticLearningError("semantic challenger evaluation report does not match")
    if output.evaluation_report.get("promotion") != asdict(output.promotion):
        raise SemanticLearningError("semantic promotion report does not match")


def _evaluate_vlm_champion(
    split: SemanticMetaDatasetSplit,
    *,
    samples: tuple[SemanticLearningSample, ...],
    reject_threshold_micros: int,
) -> SemanticMetaEvaluation:
    by_id = {str(sample.feedback_id): sample for sample in samples}
    probabilities: dict[str, int] = {}
    for example in split.holdout:
        sample = by_id.get(example.sample_id)
        if sample is None:
            raise SemanticLearningError("champion evaluation sample is unavailable")
        if sample.verdict == SemanticVerdict.PASS:
            probability = 0
        elif sample.verdict == SemanticVerdict.REVIEW:
            probability = max(1, reject_threshold_micros - 1)
        else:
            probability = sample.confidence_micros
        probabilities[example.sample_id] = probability
    return evaluate_semantic_meta_predictions(
        split.holdout,
        probabilities,
        keep_threshold_micros=0,
        reject_threshold_micros=reject_threshold_micros,
        model_sha256=canonical_sha256(
            {
                "kind": "calibrated_vlm",
                "reject_threshold_micros": reject_threshold_micros,
            }
        ),
    )


async def _complete_meta_training_run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    claimed: ClaimedSemanticTrainingRun,
    split: SemanticMetaDatasetSplit,
    output: SemanticMetaTrainingOutput,
) -> None:
    now = datetime.now(UTC)
    async with sessions() as session:
        run = await session.scalar(
            select(SemanticTrainingRun)
            .where(
                SemanticTrainingRun.id == claimed.run_id,
                SemanticTrainingRun.state == SemanticTrainingState.PREPARING,
                SemanticTrainingRun.lease_owner == claimed.worker_id,
                SemanticTrainingRun.lease_expires_at > now,
            )
            .with_for_update()
        )
        if run is None:
            raise SemanticLearningError("semantic training lease was lost")
        if (
            run.owner_user_id != claimed.owner_user_id
            or run.profile_sha256 != claimed.profile_sha256
            or run.dataset_sha256 != claimed.dataset_sha256
            or run.split_manifest_sha256 != claimed.split_manifest_sha256
        ):
            raise SemanticLearningError("stored semantic training identity changed")
        _validate_meta_training_output(
            claimed=claimed,
            split=split,
            output=output,
        )
        policy = await session.get(SemanticLearningPolicy, claimed.owner_user_id)
        if policy is None:
            raise SemanticLearningError("semantic learning policy is unavailable")
        previous = await _active_promotion_record(
            session,
            owner_user_id=claimed.owner_user_id,
            profile_sha256=claimed.profile_sha256,
        )
        decision = (
            SemanticPromotionDecision.PROMOTED
            if output.promotion.promote and policy.auto_promote_validated
            else SemanticPromotionDecision.REJECTED
        )
        reason = (
            "Validated challenger automatically promoted for advisory triage."
            if decision == SemanticPromotionDecision.PROMOTED
            else "; ".join(output.promotion.blockers)
            or "Automatic promotion is disabled by the owner policy."
        )
        run.state = SemanticTrainingState.SUCCEEDED
        run.artifact_sha256 = output.model.artifact_sha256
        run.model_payload = output.model_payload
        run.evaluation_report = output.evaluation_report
        run.evaluation_sha256 = output.evaluation_sha256
        run.promotion_report = asdict(output.promotion)
        run.actual_cost_microusd = 0
        run.lease_owner = None
        run.lease_expires_at = None
        run.completed_at = now
        run.last_error_code = None
        run.last_error_detail = None
        promotion = SemanticModelPromotion(
            owner_user_id=claimed.owner_user_id,
            kind=SemanticTrainingKind.META_CLASSIFIER,
            training_run_id=run.id,
            previous_training_run_id=(previous.training_run_id if previous is not None else None),
            profile_sha256=claimed.profile_sha256,
            artifact_sha256=output.model.artifact_sha256,
            dataset_sha256=run.dataset_sha256,
            evaluation_sha256=output.evaluation_sha256,
            decision=decision,
            keep_threshold_micros=output.model.keep_threshold_micros,
            reject_threshold_micros=output.model.reject_threshold_micros,
            reason=reason,
            created_by_user_id=claimed.owner_user_id,
            created_at=now,
        )
        session.add(promotion)
        session.add(
            AuditEvent(
                actor=claimed.worker_id,
                action="semantic_learning.meta_training_completed",
                resource_type="semantic_training_run",
                resource_id=run.id,
                correlation_id=f"semantic-training:{run.id}",
                detail={
                    "artifact_sha256": output.model.artifact_sha256,
                    "evaluation_sha256": output.evaluation_sha256,
                    "promotion_decision": decision.value,
                    "cost_microusd": 0,
                },
                occurred_at=now,
            )
        )
        await session.commit()


async def _active_promotion_record(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    profile_sha256: str,
) -> SemanticModelPromotion | None:
    promotion: SemanticModelPromotion | None = await session.scalar(
        select(SemanticModelPromotion)
        .where(
            SemanticModelPromotion.owner_user_id == owner_user_id,
            SemanticModelPromotion.profile_sha256 == profile_sha256,
            SemanticModelPromotion.kind == SemanticTrainingKind.META_CLASSIFIER,
            SemanticModelPromotion.decision.in_(
                (
                    SemanticPromotionDecision.PROMOTED,
                    SemanticPromotionDecision.ROLLED_BACK,
                )
            ),
        )
        .order_by(SemanticModelPromotion.created_at.desc(), SemanticModelPromotion.id.desc())
        .limit(1)
    )
    return promotion


async def _retry_or_fail_meta_training_run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    claimed: ClaimedSemanticTrainingRun,
    error: Exception,
) -> None:
    now = datetime.now(UTC)
    safe_code = (
        "semantic_training_contract_error"
        if isinstance(error, (SemanticLearningError, SemanticMetaClassifierError))
        else "semantic_training_failed"
    )
    async with sessions() as session:
        run = await session.scalar(
            select(SemanticTrainingRun)
            .where(
                SemanticTrainingRun.id == claimed.run_id,
                SemanticTrainingRun.state == SemanticTrainingState.PREPARING,
                SemanticTrainingRun.lease_owner == claimed.worker_id,
            )
            .with_for_update()
        )
        if run is None:
            return
        terminal = run.attempts >= run.max_attempts or isinstance(
            error,
            (SemanticLearningError, SemanticMetaClassifierError),
        )
        run.state = SemanticTrainingState.FAILED if terminal else SemanticTrainingState.QUEUED
        run.available_at = now + timedelta(minutes=5)
        run.lease_owner = None
        run.lease_expires_at = None
        run.last_error_code = safe_code
        run.last_error_detail = "The CPU anatomy challenger could not be trained."
        run.completed_at = now if terminal else None
        session.add(
            AuditEvent(
                actor=claimed.worker_id,
                action=(
                    "semantic_learning.meta_training_failed"
                    if terminal
                    else "semantic_learning.meta_training_retry_scheduled"
                ),
                resource_type="semantic_training_run",
                resource_id=run.id,
                correlation_id=f"semantic-training:{run.id}",
                detail={"error_code": safe_code, "attempt": run.attempts},
                occurred_at=now,
            )
        )
        await session.commit()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SemanticLearningError("semantic learning timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = (
    "ClaimedSemanticTrainingRun",
    "SemanticLearningCycleResult",
    "SemanticLearningError",
    "SemanticLearningNotReadyError",
    "enqueue_ready_meta_training_runs",
    "ensure_owner_semantic_learning_policies",
    "ensure_semantic_learning_policy",
    "load_effective_semantic_meta_model",
    "process_claimed_meta_training_run",
    "request_meta_training_run",
    "rollback_semantic_meta_model",
    "run_semantic_learning_cycle",
    "update_semantic_learning_policy",
)
