"""Optional semantic anatomy QC over immutable raw-master snapshots."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.db.models import (
    Asset,
    AssetRanking,
    AssetScore,
    ScoringRun,
    SemanticAssessment,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AssetKind,
    AssetScoreState,
    AssetState,
    ScoringRunState,
    SemanticAssessmentState,
    SemanticIssueCode,
    SemanticVerdict,
)
from gen_automation.integrations.semantic_vlm import (
    SemanticVlmProtocolError,
    SemanticVlmUnavailableError,
)
from gen_automation.semantic import (
    SemanticAssessmentResult,
    SemanticIssue,
    SemanticNormalizedBox,
    assessment_profile_sha256,
    prompt_sha256,
    schema_sha256,
)
from gen_automation.storage.base import (
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectTooLargeError,
)

_RANKABLE_SCORE_STATES = (
    AssetScoreState.SCORED,
    AssetScoreState.FLAGGED_BLANK,
    AssetScoreState.FLAGGED_CORRUPT,
    AssetScoreState.DEAD_LETTER,
)
_SUPPORTED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_SAFE_UNAVAILABLE_DETAIL = (
    "Semantic anatomy assessment is unavailable. The raw master remains in human review."
)


class SemanticAssessmentError(Exception):
    """Base error for semantic assessment orchestration."""


class SemanticAssessmentLeaseLostError(SemanticAssessmentError):
    """The caller no longer owns an active semantic assessment lease."""


class SemanticAssessmentContractError(SemanticAssessmentError):
    """A frozen semantic assessment input or result is inconsistent."""


@dataclass(frozen=True, slots=True)
class SemanticAssessmentProfile:
    model_name: str
    model_revision: str

    def __post_init__(self) -> None:
        _bounded_text(self.model_name, label="semantic model", maximum=200)
        _bounded_text(self.model_revision, label="semantic model revision", maximum=200)

    @property
    def profile_sha256(self) -> str:
        return assessment_profile_sha256(
            model=self.model_name,
            model_revision=self.model_revision,
        )


@dataclass(frozen=True, slots=True)
class ClaimedSemanticAssessment:
    assessment_id: UUID
    scoring_run_id: UUID
    asset_score_id: UUID
    asset_id: UUID
    storage_backend: str
    storage_bucket: str
    object_key: str
    object_version_id: str
    asset_sha256: str
    content_type: str
    byte_size: int
    profile_sha256: str
    model_name: str
    model_revision: str
    prompt_sha256: str
    schema_sha256: str
    attempt: int
    max_attempts: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class SemanticAssessmentCycleResult:
    created_assessment: bool = False
    recovered_lease: bool = False
    processed_assessment: bool = False

    @property
    def did_work(self) -> bool:
        return self.created_assessment or self.recovered_lease or self.processed_assessment


@dataclass(frozen=True, slots=True)
class SemanticReviewAssessment:
    assessment_id: UUID
    asset_id: UUID
    state: SemanticAssessmentState
    verdict: SemanticVerdict | None
    confidence_micros: int | None
    issues: tuple[SemanticIssue, ...]
    model_name: str
    model_revision: str
    completed_at: datetime | None
    error_code: str | None

    @property
    def confidence_percent(self) -> str | None:
        if self.confidence_micros is None:
            return None
        return f"{self.confidence_micros / 10_000:.1f}%"

    def is_high_confidence_severe(self, *, threshold_micros: int) -> bool:
        _confidence_threshold(threshold_micros)
        return (
            self.state == SemanticAssessmentState.COMPLETED
            and self.verdict == SemanticVerdict.SEVERE
            and self.confidence_micros is not None
            and self.confidence_micros >= threshold_micros
        )


type SemanticAnalyzer = Callable[
    [bytes, str, str],
    Awaitable[SemanticAssessmentResult],
]


async def ensure_semantic_assessment(
    session: AsyncSession,
    *,
    profile: SemanticAssessmentProfile,
    max_assessments_per_profile: int,
    asset_allowlist: Collection[UUID],
    max_attempts: int,
    now: datetime | None = None,
) -> bool:
    """Create one assessment job for a frozen, CPU-ranked raw master."""

    assessment_limit = _assessment_limit(max_assessments_per_profile)
    allowed_asset_ids = _asset_allowlist(asset_allowlist)
    _max_attempts(max_attempts)
    if assessment_limit == 0:
        return False
    created_at = _as_utc(now or datetime.now(UTC))
    profile_digest = profile.profile_sha256

    # Serialize assessment creation before counting. Without a stable row lock,
    # two controller replicas could both observe room beneath the cap and create
    # different assessment rows, exceeding the configured hard limit.
    creation_guard = await session.scalar(
        select(ScoringRun.id)
        .where(ScoringRun.state == ScoringRunState.COMPLETED)
        .order_by(ScoringRun.completed_at, ScoringRun.id)
        .limit(1)
        .with_for_update()
    )
    if creation_guard is None:
        await session.rollback()
        return False
    existing_count = await session.scalar(
        select(func.count(SemanticAssessment.id)).where(
            SemanticAssessment.profile_sha256 == profile_digest
        )
    )
    if existing_count is None or existing_count >= assessment_limit:
        await session.rollback()
        return False

    assessment_exists = exists(
        select(SemanticAssessment.id).where(
            SemanticAssessment.scoring_run_id == AssetScore.scoring_run_id,
            SemanticAssessment.asset_id == AssetScore.asset_id,
            SemanticAssessment.profile_sha256 == profile_digest,
        )
    )
    candidate = (
        select(AssetScore, Asset)
        .join(ScoringRun, ScoringRun.id == AssetScore.scoring_run_id)
        .join(
            AssetRanking,
            (AssetRanking.scoring_run_id == AssetScore.scoring_run_id)
            & (AssetRanking.asset_id == AssetScore.asset_id),
        )
        .join(Asset, Asset.id == AssetScore.asset_id)
        .where(
            ScoringRun.state == ScoringRunState.COMPLETED,
            AssetScore.state.in_(_RANKABLE_SCORE_STATES),
            AssetScore.completed_at.is_not(None),
            Asset.kind == AssetKind.RAW_MASTER,
            Asset.state == AssetState.AVAILABLE,
            ~assessment_exists,
        )
    )
    if allowed_asset_ids:
        candidate = candidate.where(AssetScore.asset_id.in_(allowed_asset_ids))
    row = (
        await session.execute(
            candidate.order_by(ScoringRun.completed_at, AssetRanking.rank, AssetScore.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).one_or_none()
    if row is None:
        await session.rollback()
        return False
    score, asset = row
    if (
        asset.object_key is None
        or asset.object_version_id is None
        or asset.sha256 is None
        or asset.content_type is None
        or asset.byte_size is None
        or score.asset_storage_backend != asset.storage_backend
        or score.asset_storage_bucket != asset.storage_bucket
        or score.asset_object_key != asset.object_key
        or score.asset_object_version_id != asset.object_version_id
        or score.asset_sha256 != asset.sha256
        or score.asset_byte_size != asset.byte_size
    ):
        raise SemanticAssessmentContractError("ranked asset snapshot is inconsistent")
    assessment = SemanticAssessment(
        scoring_run_id=score.scoring_run_id,
        asset_score_id=score.id,
        asset_id=score.asset_id,
        asset_storage_backend=score.asset_storage_backend,
        asset_storage_bucket=score.asset_storage_bucket,
        asset_object_key=score.asset_object_key,
        asset_object_version_id=score.asset_object_version_id,
        asset_sha256=score.asset_sha256,
        asset_content_type=asset.content_type,
        asset_byte_size=score.asset_byte_size,
        profile_sha256=profile_digest,
        model_name=profile.model_name,
        model_revision=profile.model_revision,
        prompt_sha256=prompt_sha256(),
        schema_sha256=schema_sha256(),
        state=SemanticAssessmentState.PENDING,
        attempts=0,
        max_attempts=max_attempts,
        available_at=created_at,
        created_at=created_at,
    )
    session.add(assessment)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True


async def claim_semantic_assessment(
    session: AsyncSession,
    *,
    worker_id: str,
    profile: SemanticAssessmentProfile,
    lease_seconds: int,
    now: datetime | None = None,
) -> ClaimedSemanticAssessment | None:
    normalized_worker_id = _worker_id(worker_id)
    if isinstance(lease_seconds, bool) or not 10 <= lease_seconds <= 3600:
        raise ValueError("semantic assessment lease must be between 10 and 3600 seconds")
    claimed_at = _as_utc(now or datetime.now(UTC))
    profile_digest = profile.profile_sha256
    due = or_(
        and_(
            SemanticAssessment.state == SemanticAssessmentState.PENDING,
            SemanticAssessment.available_at <= claimed_at,
        ),
        and_(
            SemanticAssessment.state == SemanticAssessmentState.RETRY_WAIT,
            SemanticAssessment.available_at <= claimed_at,
        ),
        and_(
            SemanticAssessment.state == SemanticAssessmentState.PROCESSING,
            SemanticAssessment.lease_expires_at.is_not(None),
            SemanticAssessment.lease_expires_at <= claimed_at,
        ),
    )
    assessment = await session.scalar(
        select(SemanticAssessment)
        .where(
            SemanticAssessment.profile_sha256 == profile_digest,
            SemanticAssessment.attempts < SemanticAssessment.max_attempts,
            due,
        )
        .order_by(
            SemanticAssessment.available_at,
            SemanticAssessment.created_at,
            SemanticAssessment.id,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if assessment is None:
        await session.rollback()
        return None
    old_state = assessment.state
    old_attempts = assessment.attempts
    previous_owner = assessment.lease_owner
    previous_expiry = assessment.lease_expires_at
    lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
    predicates = [
        SemanticAssessment.id == assessment.id,
        SemanticAssessment.state == old_state,
        SemanticAssessment.attempts == old_attempts,
        SemanticAssessment.attempts < SemanticAssessment.max_attempts,
    ]
    if old_state == SemanticAssessmentState.PROCESSING:
        predicates.extend(
            (
                SemanticAssessment.lease_owner == previous_owner,
                SemanticAssessment.lease_expires_at == previous_expiry,
                SemanticAssessment.lease_expires_at <= claimed_at,
            )
        )
    claimed_id = await session.scalar(
        update(SemanticAssessment)
        .where(*predicates)
        .values(
            state=SemanticAssessmentState.PROCESSING,
            attempts=SemanticAssessment.attempts + 1,
            lease_owner=normalized_worker_id,
            lease_expires_at=lease_expires_at,
            started_at=claimed_at,
            last_error_code=None,
            last_error_detail=None,
        )
        .execution_options(synchronize_session=False)
        .returning(SemanticAssessment.id)
    )
    if claimed_id is None:
        await session.rollback()
        return None
    await session.commit()
    return ClaimedSemanticAssessment(
        assessment_id=assessment.id,
        scoring_run_id=assessment.scoring_run_id,
        asset_score_id=assessment.asset_score_id,
        asset_id=assessment.asset_id,
        storage_backend=assessment.asset_storage_backend,
        storage_bucket=assessment.asset_storage_bucket,
        object_key=assessment.asset_object_key,
        object_version_id=assessment.asset_object_version_id,
        asset_sha256=assessment.asset_sha256,
        content_type=assessment.asset_content_type,
        byte_size=assessment.asset_byte_size,
        profile_sha256=assessment.profile_sha256,
        model_name=assessment.model_name,
        model_revision=assessment.model_revision,
        prompt_sha256=assessment.prompt_sha256,
        schema_sha256=assessment.schema_sha256,
        attempt=old_attempts + 1,
        max_attempts=assessment.max_attempts,
        lease_expires_at=lease_expires_at,
    )


async def process_claimed_semantic_assessment(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    claim: ClaimedSemanticAssessment,
    worker_id: str,
    analyzer: SemanticAnalyzer,
    retry_base_seconds: int,
    retry_max_seconds: int,
    now: datetime | None = None,
) -> None:
    operation_at = _as_utc(now or datetime.now(UTC))
    terminal_error: str | None = None
    retry_error: str | None = None
    try:
        if store.backend != claim.storage_backend or store.bucket != claim.storage_bucket:
            terminal_error = "storage_location_mismatch"
            raise SemanticAssessmentContractError("semantic object store snapshot differs")
        if claim.content_type not in _SUPPORTED_CONTENT_TYPES:
            terminal_error = "unsupported_image_type"
            raise SemanticAssessmentContractError("semantic image type is unsupported")
        payload = await store.read_bytes(
            claim.object_key,
            max_bytes=claim.byte_size,
            version_id=claim.object_version_id,
        )
        if len(payload) != claim.byte_size:
            terminal_error = "asset_size_mismatch"
            raise SemanticAssessmentContractError("semantic asset size differs")
        if hashlib.sha256(payload).hexdigest() != claim.asset_sha256:
            terminal_error = "asset_sha256_mismatch"
            raise SemanticAssessmentContractError("semantic asset digest differs")
        result = await analyzer(payload, claim.content_type, claim.asset_sha256)
        _validate_result(result)
        async with sessions() as session:
            await _complete_assessment(
                session,
                claim=claim,
                worker_id=worker_id,
                result=result,
                now=operation_at,
            )
        return
    except ObjectNotFoundError:
        retry_error = "asset_version_unavailable"
    except ObjectTooLargeError:
        terminal_error = "asset_size_mismatch"
    except ObjectStoreError:
        retry_error = "storage_read_failed"
    except SemanticVlmUnavailableError:
        retry_error = "semantic_service_unavailable"
    except SemanticVlmProtocolError:
        retry_error = "semantic_protocol_error"
    except SemanticAssessmentContractError:
        terminal_error = terminal_error or "semantic_contract_error"
    except SemanticAssessmentLeaseLostError:
        raise
    except (TypeError, ValueError):
        retry_error = "semantic_protocol_error"
    except Exception:
        retry_error = "semantic_assessment_failed"
    error_code = terminal_error or retry_error or "semantic_assessment_failed"
    async with sessions() as session:
        await _fail_assessment(
            session,
            claim=claim,
            worker_id=worker_id,
            error_code=error_code,
            retry_delay_seconds=_retry_delay(
                attempt=claim.attempt,
                base_seconds=retry_base_seconds,
                maximum_seconds=retry_max_seconds,
            ),
            terminal=terminal_error is not None,
            now=operation_at,
        )


async def run_semantic_assessment_cycle(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    worker_id: str,
    profile: SemanticAssessmentProfile,
    analyzer: SemanticAnalyzer,
    max_assessments_per_profile: int,
    asset_allowlist: Collection[UUID],
    max_attempts: int,
    lease_seconds: int,
    retry_base_seconds: int,
    retry_max_seconds: int,
    now: datetime | None = None,
) -> SemanticAssessmentCycleResult:
    cycle_at = _as_utc(now or datetime.now(UTC))
    async with sessions() as session:
        recovered = await _recover_one_exhausted_lease(
            session,
            profile_sha256=profile.profile_sha256,
            now=cycle_at,
        )
        if recovered:
            await session.commit()
        else:
            await session.rollback()
    if recovered:
        return SemanticAssessmentCycleResult(recovered_lease=True)
    async with sessions() as session:
        created = await ensure_semantic_assessment(
            session,
            profile=profile,
            max_assessments_per_profile=max_assessments_per_profile,
            asset_allowlist=asset_allowlist,
            max_attempts=max_attempts,
            now=cycle_at,
        )
    async with sessions() as session:
        claim = await claim_semantic_assessment(
            session,
            worker_id=worker_id,
            profile=profile,
            lease_seconds=lease_seconds,
            now=cycle_at,
        )
    if claim is None:
        return SemanticAssessmentCycleResult(
            created_assessment=created,
        )
    await process_claimed_semantic_assessment(
        sessions,
        store,
        claim=claim,
        worker_id=worker_id,
        analyzer=analyzer,
        retry_base_seconds=retry_base_seconds,
        retry_max_seconds=retry_max_seconds,
        now=cycle_at,
    )
    return SemanticAssessmentCycleResult(
        created_assessment=created,
        processed_assessment=True,
    )


async def load_semantic_review_assessments(
    session: AsyncSession,
    *,
    scoring_run_id: UUID,
    profile_sha256: str | None = None,
) -> dict[UUID, SemanticReviewAssessment]:
    """Load the newest configured assessment per ranked asset without blocking review."""

    predicates = [SemanticAssessment.scoring_run_id == scoring_run_id]
    if profile_sha256 is not None:
        if (
            not isinstance(profile_sha256, str)
            or len(profile_sha256) != 64
            or any(character not in "0123456789abcdef" for character in profile_sha256)
        ):
            raise ValueError("semantic profile digest is invalid")
        predicates.append(SemanticAssessment.profile_sha256 == profile_sha256)
    rows = list(
        (
            await session.scalars(
                select(SemanticAssessment)
                .where(*predicates)
                .order_by(
                    SemanticAssessment.created_at.desc(),
                    SemanticAssessment.id.desc(),
                )
            )
        ).all()
    )
    result: dict[UUID, SemanticReviewAssessment] = {}
    for assessment in rows:
        if assessment.asset_id in result:
            continue
        result[assessment.asset_id] = _review_assessment(assessment)
    return result


async def _complete_assessment(
    session: AsyncSession,
    *,
    claim: ClaimedSemanticAssessment,
    worker_id: str,
    result: SemanticAssessmentResult,
    now: datetime,
) -> None:
    assessment = await _locked_assessment(
        session,
        claim=claim,
        worker_id=worker_id,
        now=now,
    )
    wire = result.to_wire()
    assessment.state = SemanticAssessmentState.COMPLETED
    assessment.verdict = result.verdict
    assessment.confidence_micros = result.confidence_micros
    assessment.issues = cast(list[dict[str, Any]], wire["issues"])
    assessment.response_sha256 = canonical_sha256(wire)
    assessment.lease_owner = None
    assessment.lease_expires_at = None
    assessment.completed_at = now
    assessment.last_error_code = None
    assessment.last_error_detail = None
    await session.commit()


async def _fail_assessment(
    session: AsyncSession,
    *,
    claim: ClaimedSemanticAssessment,
    worker_id: str,
    error_code: str,
    retry_delay_seconds: int,
    terminal: bool,
    now: datetime,
) -> None:
    assessment = await _locked_assessment(
        session,
        claim=claim,
        worker_id=worker_id,
        now=now,
    )
    code = _error_code(error_code)
    assessment.lease_owner = None
    assessment.lease_expires_at = None
    assessment.last_error_code = code
    assessment.last_error_detail = _SAFE_UNAVAILABLE_DETAIL
    if terminal or assessment.attempts >= assessment.max_attempts:
        assessment.state = SemanticAssessmentState.UNAVAILABLE
        assessment.completed_at = now
        assessment.available_at = now
    else:
        if not 1 <= retry_delay_seconds <= 86400:
            raise ValueError("semantic retry delay is invalid")
        assessment.state = SemanticAssessmentState.RETRY_WAIT
        assessment.available_at = now + timedelta(seconds=retry_delay_seconds)
        assessment.completed_at = None
    await session.commit()


async def _locked_assessment(
    session: AsyncSession,
    *,
    claim: ClaimedSemanticAssessment,
    worker_id: str,
    now: datetime,
) -> SemanticAssessment:
    assessment = await session.scalar(
        select(SemanticAssessment)
        .where(
            SemanticAssessment.id == claim.assessment_id,
            SemanticAssessment.scoring_run_id == claim.scoring_run_id,
            SemanticAssessment.asset_score_id == claim.asset_score_id,
            SemanticAssessment.asset_id == claim.asset_id,
        )
        .with_for_update()
    )
    expiry = (
        _as_utc(assessment.lease_expires_at)
        if assessment is not None and assessment.lease_expires_at is not None
        else None
    )
    if (
        assessment is None
        or assessment.state != SemanticAssessmentState.PROCESSING
        or assessment.lease_owner != _worker_id(worker_id)
        or expiry is None
        or expiry <= now
        or assessment.attempts != claim.attempt
        or assessment.profile_sha256 != claim.profile_sha256
        or assessment.prompt_sha256 != claim.prompt_sha256
        or assessment.schema_sha256 != claim.schema_sha256
    ):
        raise SemanticAssessmentLeaseLostError("semantic assessment lease is not active")
    return assessment


async def _recover_one_exhausted_lease(
    session: AsyncSession,
    *,
    profile_sha256: str,
    now: datetime,
) -> bool:
    assessment = await session.scalar(
        select(SemanticAssessment)
        .where(
            SemanticAssessment.profile_sha256 == profile_sha256,
            SemanticAssessment.state == SemanticAssessmentState.PROCESSING,
            SemanticAssessment.lease_expires_at.is_not(None),
            SemanticAssessment.lease_expires_at <= now,
            SemanticAssessment.attempts >= SemanticAssessment.max_attempts,
        )
        .order_by(SemanticAssessment.lease_expires_at, SemanticAssessment.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if assessment is None:
        return False
    assessment.state = SemanticAssessmentState.UNAVAILABLE
    assessment.lease_owner = None
    assessment.lease_expires_at = None
    assessment.completed_at = now
    assessment.available_at = now
    assessment.last_error_code = "semantic_lease_expired"
    assessment.last_error_detail = _SAFE_UNAVAILABLE_DETAIL
    return True


def _review_assessment(assessment: SemanticAssessment) -> SemanticReviewAssessment:
    issues: tuple[SemanticIssue, ...] = ()
    state = assessment.state
    verdict = assessment.verdict
    confidence = assessment.confidence_micros
    error_code = assessment.last_error_code
    if state == SemanticAssessmentState.COMPLETED:
        try:
            if verdict is None or confidence is None or assessment.issues is None:
                raise ValueError("semantic result is incomplete")
            issues = tuple(_issue_from_wire(value) for value in assessment.issues)
            _validate_result(
                SemanticAssessmentResult(
                    verdict=verdict,
                    confidence_micros=confidence,
                    issues=issues,
                )
            )
        except (TypeError, ValueError):
            state = SemanticAssessmentState.UNAVAILABLE
            verdict = None
            confidence = None
            issues = ()
            error_code = "stored_semantic_assessment_invalid"
    return SemanticReviewAssessment(
        assessment_id=assessment.id,
        asset_id=assessment.asset_id,
        state=state,
        verdict=verdict,
        confidence_micros=confidence,
        issues=issues,
        model_name=assessment.model_name,
        model_revision=assessment.model_revision,
        completed_at=assessment.completed_at,
        error_code=error_code,
    )


def _issue_from_wire(value: object) -> SemanticIssue:
    if not isinstance(value, dict) or set(value) not in (
        {"code", "confidence_micros"},
        {"code", "confidence_micros", "box"},
    ):
        raise ValueError("stored semantic issue is malformed")
    box_value = value.get("box")
    box = None
    if box_value is not None:
        if not isinstance(box_value, dict) or set(box_value) != {
            "x_min_micros",
            "y_min_micros",
            "x_max_micros",
            "y_max_micros",
        }:
            raise ValueError("stored semantic issue box is malformed")
        box = SemanticNormalizedBox(
            x_min_micros=_int(box_value["x_min_micros"]),
            y_min_micros=_int(box_value["y_min_micros"]),
            x_max_micros=_int(box_value["x_max_micros"]),
            y_max_micros=_int(box_value["y_max_micros"]),
        )
    return SemanticIssue(
        code=SemanticIssueCode(str(value["code"])),
        confidence_micros=_int(value["confidence_micros"]),
        box=box,
    )


def _validate_result(result: SemanticAssessmentResult) -> None:
    if not isinstance(result, SemanticAssessmentResult):
        raise SemanticAssessmentContractError("semantic analyzer result type is invalid")
    try:
        SemanticAssessmentResult(
            verdict=result.verdict,
            confidence_micros=result.confidence_micros,
            issues=tuple(result.issues),
        )
    except (TypeError, ValueError) as error:
        raise SemanticAssessmentContractError("semantic analyzer result is invalid") from error


def _retry_delay(*, attempt: int, base_seconds: int, maximum_seconds: int) -> int:
    if (
        isinstance(base_seconds, bool)
        or isinstance(maximum_seconds, bool)
        or not 1 <= base_seconds <= maximum_seconds <= 86400
    ):
        raise ValueError("semantic retry bounds are invalid")
    exponent = min(max(attempt - 1, 0), 20)
    return int(min(maximum_seconds, base_seconds * (2**exponent)))


def _worker_id(value: object) -> str:
    return _bounded_text(value, label="semantic worker ID", maximum=200)


def _error_code(value: object) -> str:
    code = _bounded_text(value, label="semantic error code", maximum=100)
    if not code[0].islower() or any(
        not (character.islower() or character.isdigit() or character == "_") for character in code
    ):
        raise ValueError("semantic error code is invalid")
    return code


def _bounded_text(value: object, *, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _max_attempts(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
        raise ValueError("semantic max attempts must be between 1 and 10")
    return value


def _assessment_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
        raise ValueError("semantic per-profile assessment limit must be between 0 and 10000")
    return value


def _asset_allowlist(value: Collection[UUID]) -> tuple[UUID, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError("semantic asset allowlist must contain UUID values")
    normalized = tuple(value)
    if len(normalized) > 10_000 or any(not isinstance(asset_id, UUID) for asset_id in normalized):
        raise ValueError("semantic asset allowlist must contain at most 10000 UUID values")
    return tuple(dict.fromkeys(normalized))


def _confidence_threshold(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
        raise ValueError("semantic confidence threshold is invalid")
    return value


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("stored semantic integer is invalid")
    return value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
