"""Durable orchestration for exact-version, isolated quality scoring."""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.db.models import AssetScore, Release, ReleaseVersion, ScoringRun
from gen_automation.domain.enums import AssetScoreState, ReleasePhase, ScoringRunState
from gen_automation.quality import (
    DEFAULT_QUALITY_CONFIG,
    SCORER_VERSION,
    NormalizedThumbnail,
    QualityConfig,
    QualityMetrics,
    QualityResult,
    QualityScoreBreakdown,
    UnsafeImageError,
    calculate_metrics,
    quality_config_sha256,
    score_metrics,
)
from gen_automation.services.quality import (
    AssetAnalysis,
    CorruptAssetSignal,
    FrozenScoringRun,
    QualityInputError,
    create_scoring_run,
    freeze_scoring_run,
)
from gen_automation.services.quality_isolation import (
    QualityIsolationCrashError,
    QualityIsolationMemoryError,
    QualityIsolationPolicy,
    QualityIsolationProtocolError,
    QualityIsolationTimeoutError,
    QualityIsolationUnavailableError,
    analyze_image_isolated,
)
from gen_automation.storage.base import (
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectTooLargeError,
)

_STAGED_ANALYSIS_KEY = "staged_analysis"
_SAFE_ERROR_DETAIL = "Quality analysis failed inside the bounded processing boundary."


class QualityRuntimeError(Exception):
    """Base error for durable automatic scoring."""


class QualityLeaseLostError(QualityRuntimeError):
    """The caller no longer owns an active score-processing lease."""


class QualityRuntimeContractError(QualityRuntimeError):
    """Stored scoring data violates the automatic runtime contract."""


@dataclass(frozen=True, slots=True)
class ClaimedQualityScore:
    score_id: UUID
    scoring_run_id: UUID
    release_version_id: UUID
    asset_id: UUID
    storage_backend: str
    storage_bucket: str
    object_key: str
    object_version_id: str
    sha256: str
    byte_size: int
    image_format: str
    width: int
    height: int
    attempt: int
    max_attempts: int
    lease_expires_at: datetime
    config: QualityConfig


@dataclass(frozen=True, slots=True)
class QualityFailureResult:
    staged_terminal_signal: bool
    retry_at: datetime | None
    attempt: int


@dataclass(frozen=True, slots=True)
class QualityCycleResult:
    created_run: bool = False
    processed_score: bool = False
    finalized_run: bool = False

    @property
    def did_work(self) -> bool:
        return self.created_run or self.processed_score or self.finalized_run


type QualityAnalyzer = Callable[
    [bytes, QualityConfig, QualityIsolationPolicy],
    Awaitable[QualityResult],
]


async def ensure_next_scoring_run(
    session: AsyncSession,
    *,
    config: QualityConfig = DEFAULT_QUALITY_CONFIG,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> bool:
    """Create at most one missing run for a current release in review."""

    config_sha256 = quality_config_sha256(config)
    identity_exists = exists(
        select(ScoringRun.id).where(
            ScoringRun.release_version_id == ReleaseVersion.id,
            ScoringRun.config_sha256 == config_sha256,
            ScoringRun.scorer_version == SCORER_VERSION,
        )
    )
    release_version_id = await session.scalar(
        select(ReleaseVersion.id)
        .join(Release, Release.id == ReleaseVersion.release_id)
        .where(
            Release.phase == ReleasePhase.REVIEWING,
            Release.current_version_no == ReleaseVersion.version_no,
            ~identity_exists,
        )
        .order_by(Release.created_at, Release.id, ReleaseVersion.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if release_version_id is None:
        await session.rollback()
        return False
    result = await create_scoring_run(
        session,
        release_version_id=release_version_id,
        config=config,
        max_attempts=max_attempts,
        now=now,
    )
    return not result.replayed


async def claim_quality_score(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> ClaimedQualityScore | None:
    """Lease one due score using a database compare-and-swap."""

    normalized_worker_id = _worker_id(worker_id)
    if isinstance(lease_seconds, bool) or not 10 <= lease_seconds <= 3600:
        raise ValueError("quality lease_seconds must be between 10 and 3600")
    claimed_at = _as_utc(now or datetime.now(UTC))

    recovered = await _recover_one_exhausted_lease(session, now=claimed_at)
    if recovered:
        await session.commit()
        return None

    due_state = or_(
        and_(
            AssetScore.state == AssetScoreState.PENDING,
            AssetScore.completed_at.is_(None),
            AssetScore.available_at <= claimed_at,
        ),
        and_(
            AssetScore.state == AssetScoreState.RETRY_WAIT,
            AssetScore.available_at <= claimed_at,
        ),
        and_(
            AssetScore.state == AssetScoreState.PROCESSING,
            AssetScore.lease_expires_at.is_not(None),
            AssetScore.lease_expires_at <= claimed_at,
        ),
    )
    candidate = (
        await session.execute(
            select(AssetScore, ScoringRun)
            .join(ScoringRun, ScoringRun.id == AssetScore.scoring_run_id)
            .join(ReleaseVersion, ReleaseVersion.id == ScoringRun.release_version_id)
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(
                ScoringRun.state == ScoringRunState.RUNNING,
                Release.phase == ReleasePhase.REVIEWING,
                Release.current_version_no == ReleaseVersion.version_no,
                AssetScore.attempts < AssetScore.max_attempts,
                due_state,
            )
            .order_by(
                AssetScore.available_at,
                AssetScore.created_at,
                AssetScore.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).one_or_none()
    if candidate is None:
        await session.rollback()
        return None
    score, run = candidate
    old_state = score.state
    next_attempt = score.attempts + 1
    previous_lease_owner = score.lease_owner
    previous_lease_expires_at = score.lease_expires_at
    lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
    predicates = [
        AssetScore.id == score.id,
        AssetScore.state == old_state,
        AssetScore.attempts == score.attempts,
        AssetScore.attempts < AssetScore.max_attempts,
    ]
    if old_state == AssetScoreState.PROCESSING:
        predicates.extend(
            (
                AssetScore.lease_owner == previous_lease_owner,
                AssetScore.lease_expires_at == previous_lease_expires_at,
                AssetScore.lease_expires_at <= claimed_at,
            )
        )
    elif old_state == AssetScoreState.PENDING:
        predicates.append(AssetScore.completed_at.is_(None))
    claimed_id = await session.scalar(
        update(AssetScore)
        .where(*predicates)
        .values(
            state=AssetScoreState.PROCESSING,
            attempts=AssetScore.attempts + 1,
            lease_owner=normalized_worker_id,
            lease_expires_at=lease_expires_at,
            started_at=claimed_at,
            completed_at=None,
            last_error_code=None,
            last_error_detail=None,
        )
        .execution_options(synchronize_session=False)
        .returning(AssetScore.id)
    )
    if claimed_id is None:
        await session.rollback()
        return None
    await session.commit()
    return ClaimedQualityScore(
        score_id=score.id,
        scoring_run_id=score.scoring_run_id,
        release_version_id=run.release_version_id,
        asset_id=score.asset_id,
        storage_backend=score.asset_storage_backend,
        storage_bucket=score.asset_storage_bucket,
        object_key=score.asset_object_key,
        object_version_id=score.asset_object_version_id,
        sha256=score.asset_sha256,
        byte_size=score.asset_byte_size,
        image_format=score.asset_image_format,
        width=score.asset_width,
        height=score.asset_height,
        attempt=next_attempt,
        max_attempts=score.max_attempts,
        lease_expires_at=lease_expires_at,
        config=_stored_config(run),
    )


async def stage_quality_analysis(
    session: AsyncSession,
    *,
    claim: ClaimedQualityScore,
    worker_id: str,
    analysis: AssetAnalysis,
    now: datetime | None = None,
) -> None:
    """Persist one analysis as non-terminal data under an active lease."""

    staged_at = _as_utc(now or datetime.now(UTC))
    score, run = await _locked_current_score(
        session,
        claim=claim,
        worker_id=worker_id,
        now=staged_at,
    )
    _validate_analysis_snapshot(claim, run=run, analysis=analysis)
    score.state = AssetScoreState.PENDING
    score.lease_owner = None
    score.lease_expires_at = None
    score.completed_at = staged_at
    score.available_at = staged_at
    score.signal_detail = {_STAGED_ANALYSIS_KEY: _analysis_to_wire(analysis)}
    score.last_error_code = (
        analysis.error_code if isinstance(analysis, CorruptAssetSignal) else None
    )
    score.last_error_detail = (
        _SAFE_ERROR_DETAIL if isinstance(analysis, CorruptAssetSignal) else None
    )
    await session.commit()


async def fail_quality_score(
    session: AsyncSession,
    *,
    claim: ClaimedQualityScore,
    worker_id: str,
    error_code: str,
    retry_delay_seconds: int,
    terminal: bool = False,
    now: datetime | None = None,
) -> QualityFailureResult:
    """Safely retry, or stage a redacted terminal signal after exhaustion."""

    normalized_code = _error_code(error_code)
    if isinstance(retry_delay_seconds, bool) or not 1 <= retry_delay_seconds <= 86400:
        raise ValueError("quality retry delay must be between 1 and 86400 seconds")
    failed_at = _as_utc(now or datetime.now(UTC))
    score, _ = await _locked_current_score(
        session,
        claim=claim,
        worker_id=worker_id,
        now=failed_at,
    )
    exhausted = score.attempts >= score.max_attempts
    if terminal or exhausted:
        score.state = AssetScoreState.PENDING
        score.completed_at = failed_at
        score.available_at = failed_at
        score.signal_detail = {
            _STAGED_ANALYSIS_KEY: _analysis_to_wire(CorruptAssetSignal(error_code=normalized_code))
        }
        retry_at = None
    else:
        retry_at = failed_at + timedelta(seconds=retry_delay_seconds)
        score.state = AssetScoreState.RETRY_WAIT
        score.completed_at = None
        score.available_at = retry_at
        score.signal_detail = {}
    score.lease_owner = None
    score.lease_expires_at = None
    score.last_error_code = normalized_code
    score.last_error_detail = _SAFE_ERROR_DETAIL
    await session.commit()
    return QualityFailureResult(
        staged_terminal_signal=terminal or exhausted,
        retry_at=retry_at,
        attempt=score.attempts,
    )


async def finalize_ready_scoring_run(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> FrozenScoringRun | None:
    """Atomically freeze one run only after every score has durable staging."""

    unstaged_score_exists = exists(
        select(AssetScore.id).where(
            AssetScore.scoring_run_id == ScoringRun.id,
            or_(
                AssetScore.state != AssetScoreState.PENDING,
                AssetScore.completed_at.is_(None),
            ),
        )
    )
    run = await session.scalar(
        select(ScoringRun)
        .join(ReleaseVersion, ReleaseVersion.id == ScoringRun.release_version_id)
        .join(Release, Release.id == ReleaseVersion.release_id)
        .where(
            ScoringRun.state == ScoringRunState.RUNNING,
            Release.phase == ReleasePhase.REVIEWING,
            Release.current_version_no == ReleaseVersion.version_no,
            ~unstaged_score_exists,
        )
        .order_by(ScoringRun.created_at, ScoringRun.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if run is None:
        await session.rollback()
        return None
    scores = list(
        (
            await session.scalars(
                select(AssetScore)
                .where(AssetScore.scoring_run_id == run.id)
                .order_by(AssetScore.asset_id)
                .with_for_update()
            )
        ).all()
    )
    if len(scores) != run.asset_count:
        raise QualityRuntimeContractError("scoring run has an incomplete staged snapshot")
    analyses = {
        score.asset_id: _analysis_from_signal_detail(score.signal_detail) for score in scores
    }
    try:
        return await freeze_scoring_run(
            session,
            scoring_run_id=run.id,
            analyses=analyses,
            now=now,
        )
    except QualityInputError as error:
        raise QualityRuntimeContractError(
            "staged quality analysis failed frozen-run validation"
        ) from error


async def process_claimed_quality_score(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    claim: ClaimedQualityScore,
    worker_id: str,
    policy: QualityIsolationPolicy,
    retry_base_seconds: int,
    retry_max_seconds: int,
    analyzer: QualityAnalyzer | None = None,
    now: datetime | None = None,
) -> None:
    """Read an exact immutable object, analyze it, and durably stage the result."""

    selected_analyzer = analyzer or _default_analyzer
    terminal_code: str | None = None
    retry_code: str | None = None
    try:
        if store.backend != claim.storage_backend or store.bucket != claim.storage_bucket:
            terminal_code = "storage_location_mismatch"
            raise QualityRuntimeContractError("object store does not match the frozen snapshot")
        payload = await store.read_bytes(
            claim.object_key,
            max_bytes=claim.byte_size,
            version_id=claim.object_version_id,
        )
        if len(payload) != claim.byte_size:
            terminal_code = "asset_size_mismatch"
            raise QualityRuntimeContractError("asset bytes do not match the frozen size")
        if hashlib.sha256(payload).hexdigest() != claim.sha256:
            terminal_code = "asset_sha256_mismatch"
            raise QualityRuntimeContractError("asset bytes do not match the frozen digest")
        analysis = await selected_analyzer(payload, claim.config, policy)
        async with sessions() as session:
            await stage_quality_analysis(
                session,
                claim=claim,
                worker_id=worker_id,
                analysis=analysis,
                now=now,
            )
        return
    except UnsafeImageError:
        terminal_code = "corrupt_image"
    except ObjectNotFoundError:
        retry_code = "asset_version_unavailable"
    except ObjectTooLargeError:
        terminal_code = "asset_size_mismatch"
    except ObjectStoreError:
        retry_code = "storage_read_failed"
    except QualityIsolationTimeoutError:
        retry_code = "analysis_timeout"
    except QualityIsolationMemoryError:
        retry_code = "analysis_memory_limit"
    except QualityIsolationProtocolError:
        retry_code = "analysis_protocol_error"
    except QualityIsolationCrashError:
        retry_code = "analysis_crash"
    except QualityIsolationUnavailableError:
        retry_code = "analysis_isolation_unavailable"
    except QualityRuntimeContractError:
        if terminal_code is None:
            terminal_code = "quality_contract_error"
    except QualityLeaseLostError:
        raise
    except Exception:
        retry_code = "quality_processing_failed"

    error_code = terminal_code or retry_code or "quality_processing_failed"
    retry_delay = _retry_delay(
        attempt=claim.attempt,
        base_seconds=retry_base_seconds,
        maximum_seconds=retry_max_seconds,
    )
    async with sessions() as session:
        await fail_quality_score(
            session,
            claim=claim,
            worker_id=worker_id,
            error_code=error_code,
            retry_delay_seconds=retry_delay,
            terminal=terminal_code is not None,
            now=now,
        )


async def run_quality_cycle(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    worker_id: str,
    config: QualityConfig,
    max_attempts: int,
    lease_seconds: int,
    retry_base_seconds: int,
    retry_max_seconds: int,
    policy: QualityIsolationPolicy,
    analyzer: QualityAnalyzer | None = None,
) -> QualityCycleResult:
    """Perform one bounded automatic scoring unit, sequentially."""

    async with sessions() as session:
        finalized = await finalize_ready_scoring_run(session)
    if finalized is not None:
        return QualityCycleResult(finalized_run=True)

    async with sessions() as session:
        created = await ensure_next_scoring_run(
            session,
            config=config,
            max_attempts=max_attempts,
        )
    if created:
        return QualityCycleResult(created_run=True)

    async with sessions() as session:
        claim = await claim_quality_score(
            session,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
    if claim is None:
        async with sessions() as session:
            finalized = await finalize_ready_scoring_run(session)
        return QualityCycleResult(finalized_run=finalized is not None)

    await process_claimed_quality_score(
        sessions,
        store,
        claim=claim,
        worker_id=worker_id,
        policy=policy,
        retry_base_seconds=retry_base_seconds,
        retry_max_seconds=retry_max_seconds,
        analyzer=analyzer,
    )
    async with sessions() as session:
        finalized = await finalize_ready_scoring_run(session)
    return QualityCycleResult(processed_score=True, finalized_run=finalized is not None)


async def _default_analyzer(
    payload: bytes,
    config: QualityConfig,
    policy: QualityIsolationPolicy,
) -> QualityResult:
    return await analyze_image_isolated(payload, config=config, policy=policy)


async def _recover_one_exhausted_lease(
    session: AsyncSession,
    *,
    now: datetime,
) -> bool:
    score = await session.scalar(
        select(AssetScore)
        .join(ScoringRun, ScoringRun.id == AssetScore.scoring_run_id)
        .join(ReleaseVersion, ReleaseVersion.id == ScoringRun.release_version_id)
        .join(Release, Release.id == ReleaseVersion.release_id)
        .where(
            ScoringRun.state == ScoringRunState.RUNNING,
            Release.phase == ReleasePhase.REVIEWING,
            Release.current_version_no == ReleaseVersion.version_no,
            AssetScore.state == AssetScoreState.PROCESSING,
            AssetScore.lease_expires_at.is_not(None),
            AssetScore.lease_expires_at <= now,
            AssetScore.attempts >= AssetScore.max_attempts,
        )
        .order_by(AssetScore.lease_expires_at, AssetScore.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if score is None:
        return False
    score.state = AssetScoreState.PENDING
    score.lease_owner = None
    score.lease_expires_at = None
    score.completed_at = now
    score.available_at = now
    score.last_error_code = "analysis_lease_expired"
    score.last_error_detail = _SAFE_ERROR_DETAIL
    score.signal_detail = {
        _STAGED_ANALYSIS_KEY: _analysis_to_wire(
            CorruptAssetSignal(error_code="analysis_lease_expired")
        )
    }
    return True


async def _locked_current_score(
    session: AsyncSession,
    *,
    claim: ClaimedQualityScore,
    worker_id: str,
    now: datetime,
) -> tuple[AssetScore, ScoringRun]:
    normalized_worker_id = _worker_id(worker_id)
    row = (
        await session.execute(
            select(AssetScore, ScoringRun)
            .join(ScoringRun, ScoringRun.id == AssetScore.scoring_run_id)
            .join(ReleaseVersion, ReleaseVersion.id == ScoringRun.release_version_id)
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(
                AssetScore.id == claim.score_id,
                AssetScore.scoring_run_id == claim.scoring_run_id,
                ScoringRun.release_version_id == claim.release_version_id,
                ScoringRun.state == ScoringRunState.RUNNING,
                Release.phase == ReleasePhase.REVIEWING,
                Release.current_version_no == ReleaseVersion.version_no,
            )
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise QualityLeaseLostError("quality score is not current and reviewable")
    score, run = row
    lease_expires_at = (
        _as_utc(score.lease_expires_at) if score.lease_expires_at is not None else None
    )
    if (
        score.state != AssetScoreState.PROCESSING
        or score.lease_owner != normalized_worker_id
        or lease_expires_at is None
        or lease_expires_at <= now
        or score.attempts != claim.attempt
    ):
        raise QualityLeaseLostError("quality processing lease is not active")
    return score, run


def _validate_analysis_snapshot(
    claim: ClaimedQualityScore,
    *,
    run: ScoringRun,
    analysis: AssetAnalysis,
) -> None:
    if isinstance(analysis, CorruptAssetSignal):
        return
    if not isinstance(analysis, QualityResult):
        raise QualityRuntimeContractError("analysis outcome type is invalid")
    if (
        analysis.sha256 != claim.sha256
        or analysis.byte_size != claim.byte_size
        or analysis.width != claim.width
        or analysis.height != claim.height
        or analysis.image_format.upper() != claim.image_format
        or analysis.config != claim.config
        or analysis.config_sha256 != run.config_sha256
        or analysis.scorer_version != run.scorer_version
        or analysis.pillow_version != run.pillow_version
        or analysis.thumbnail.width != claim.config.thumbnail_size
        or analysis.thumbnail.height != claim.config.thumbnail_size
        or len(analysis.thumbnail.luminance) != claim.config.thumbnail_size**2
        or calculate_metrics(analysis.thumbnail) != analysis.metrics
        or score_metrics(analysis.metrics, config=claim.config) != analysis.score_breakdown
        or analysis.score_micros != analysis.score_breakdown.total_micros
    ):
        raise QualityRuntimeContractError("analysis conflicts with the frozen score snapshot")


def _analysis_to_wire(analysis: AssetAnalysis) -> dict[str, Any]:
    if isinstance(analysis, CorruptAssetSignal):
        return {"kind": "corrupt", "error_code": analysis.error_code}
    return {
        "kind": "quality",
        "result": {
            "sha256": analysis.sha256,
            "byte_size": analysis.byte_size,
            "width": analysis.width,
            "height": analysis.height,
            "image_format": analysis.image_format,
            "thumbnail": {
                "width": analysis.thumbnail.width,
                "height": analysis.thumbnail.height,
                "luminance": base64.b64encode(analysis.thumbnail.luminance).decode("ascii"),
            },
            "metrics": asdict(analysis.metrics),
            "dhash64": analysis.dhash64,
            "score_micros": analysis.score_micros,
            "score_breakdown": asdict(analysis.score_breakdown),
            "config": asdict(analysis.config),
            "config_sha256": analysis.config_sha256,
            "scorer_version": analysis.scorer_version,
            "pillow_version": analysis.pillow_version,
        },
    }


def _analysis_from_signal_detail(detail: object) -> AssetAnalysis:
    root = _mapping(detail)
    staged = _mapping(root.get(_STAGED_ANALYSIS_KEY))
    kind = staged.get("kind")
    if kind == "corrupt":
        return CorruptAssetSignal(error_code=_error_code(staged.get("error_code")))
    if kind != "quality":
        raise QualityRuntimeContractError("staged quality analysis has an invalid kind")
    wire = _mapping(staged.get("result"))
    thumbnail_wire = _mapping(wire.get("thumbnail"))
    try:
        luminance = base64.b64decode(
            _string(thumbnail_wire.get("luminance"), maximum=128 * 1024),
            validate=True,
        )
        config = QualityConfig(**_mapping(wire.get("config")))
        metrics = QualityMetrics(**_mapping(wire.get("metrics")))
        breakdown = QualityScoreBreakdown(**_mapping(wire.get("score_breakdown")))
        return QualityResult(
            sha256=_sha256(wire.get("sha256")),
            byte_size=_integer(wire.get("byte_size"), minimum=1, maximum=256 * 1024 * 1024),
            width=_integer(wire.get("width"), minimum=1, maximum=32_768),
            height=_integer(wire.get("height"), minimum=1, maximum=32_768),
            image_format=_string(wire.get("image_format"), maximum=20),
            thumbnail=NormalizedThumbnail(
                width=_integer(thumbnail_wire.get("width"), minimum=1, maximum=256),
                height=_integer(thumbnail_wire.get("height"), minimum=1, maximum=256),
                luminance=luminance,
            ),
            metrics=metrics,
            dhash64=_integer(wire.get("dhash64"), minimum=0, maximum=(1 << 64) - 1),
            score_micros=_integer(wire.get("score_micros"), minimum=0, maximum=1_000_000),
            score_breakdown=breakdown,
            config=config,
            config_sha256=_sha256(wire.get("config_sha256")),
            scorer_version=_string(wire.get("scorer_version"), maximum=100),
            pillow_version=_string(wire.get("pillow_version"), maximum=50),
        )
    except (binascii.Error, TypeError, ValueError):
        raise QualityRuntimeContractError("staged quality analysis is malformed") from None


def _stored_config(run: ScoringRun) -> QualityConfig:
    try:
        config = QualityConfig(**run.configuration)
    except (TypeError, ValueError):
        raise QualityRuntimeContractError("stored quality configuration is invalid") from None
    if quality_config_sha256(config) != run.config_sha256:
        raise QualityRuntimeContractError("stored quality configuration digest is invalid")
    return config


def _retry_delay(*, attempt: int, base_seconds: int, maximum_seconds: int) -> int:
    if (
        isinstance(base_seconds, bool)
        or isinstance(maximum_seconds, bool)
        or not 1 <= base_seconds <= maximum_seconds <= 86400
    ):
        raise ValueError("quality retry bounds are invalid")
    exponent = min(max(attempt - 1, 0), 20)
    return int(min(maximum_seconds, base_seconds * (2**exponent)))


def _worker_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("quality worker_id is invalid")
    return value


def _error_code(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 100
        or not value[0].islower()
        or any(
            not (character.islower() or character.isdigit() or character == "_")
            for character in value
        )
    ):
        raise QualityRuntimeContractError("quality error code is invalid")
    return value


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise QualityRuntimeContractError("staged quality analysis is malformed")
    return cast(Mapping[str, Any], value)


def _string(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise QualityRuntimeContractError("staged quality analysis is malformed")
    return value


def _sha256(value: object) -> str:
    text = _string(value, maximum=64)
    if len(text) != 64:
        raise QualityRuntimeContractError("staged quality digest is malformed")
    try:
        int(text, 16)
    except ValueError:
        raise QualityRuntimeContractError("staged quality digest is malformed") from None
    return text


def _integer(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise QualityRuntimeContractError("staged quality numeric metadata is invalid")
    return value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
