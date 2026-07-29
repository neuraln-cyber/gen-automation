import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import PIL
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    Asset,
    AssetRanking,
    AssetScore,
    GenerationJob,
    Release,
    ReleaseVersion,
    ScoringRun,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AssetKind,
    AssetScoreState,
    AssetState,
    RankingDisposition,
    ReleasePhase,
    ScoringRunState,
)
from gen_automation.domain.ids import uuid7
from gen_automation.quality import (
    DEFAULT_QUALITY_CONFIG,
    MICROS,
    SCORER_VERSION,
    DuplicateCandidate,
    QualityConfig,
    QualityResult,
    calculate_metrics,
    cluster_near_duplicates,
    quality_config_sha256,
    score_metrics,
)
from gen_automation.services.ranking_manifest import (
    RankingManifestIntegrityError,
    load_ranking_manifest_rows,
    ranking_manifest_sha256,
    validate_completed_ranking_manifest,
)

_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,99}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_TERMINAL_SCORE_STATES = frozenset(
    {
        AssetScoreState.SCORED,
        AssetScoreState.FLAGGED_BLANK,
        AssetScoreState.FLAGGED_CORRUPT,
        AssetScoreState.DEAD_LETTER,
    }
)


class QualityServiceError(Exception):
    """Base error for durable quality-scoring workflows."""


class QualityRunNotFoundError(QualityServiceError):
    pass


class QualityConflictError(QualityServiceError):
    pass


class QualityInputError(QualityServiceError, ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CorruptAssetSignal:
    """Typed fail-closed outcome for bytes that could not be analyzed safely."""

    error_code: str = "corrupt_image"

    def __post_init__(self) -> None:
        if not _ERROR_CODE_PATTERN.fullmatch(self.error_code):
            raise ValueError("error_code must be a lowercase machine identifier")


type AssetAnalysis = QualityResult | CorruptAssetSignal


@dataclass(frozen=True, slots=True)
class ScoringRunResult:
    run_id: UUID
    release_version_id: UUID
    state: ScoringRunState
    asset_count: int
    config_sha256: str
    input_manifest_sha256: str
    scorer_version: str
    pillow_version: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class RankedAsset:
    asset_id: UUID
    rank: int
    aggregate_score_micros: int
    disposition: RankingDisposition
    duplicate_cluster_id: str | None
    duplicate_representative_asset_id: UUID | None
    is_duplicate_representative: bool


@dataclass(frozen=True, slots=True)
class FrozenScoringRun:
    run_id: UUID
    rankings: tuple[RankedAsset, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class _CandidateSnapshot:
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

    def manifest_entry(self) -> dict[str, str | int]:
        return {
            "asset_id": str(self.asset_id),
            "storage_backend": self.storage_backend,
            "storage_bucket": self.storage_bucket,
            "object_key": self.object_key,
            "object_version_id": self.object_version_id,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "image_format": self.image_format,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True)
class _PreparedScore:
    score: AssetScore
    state: AssetScoreState
    aggregate_score_micros: int
    signal_detail: dict[str, Any]
    result: QualityResult | None


async def create_scoring_run(
    session: AsyncSession,
    *,
    release_version_id: UUID,
    config: QualityConfig = DEFAULT_QUALITY_CONFIG,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> ScoringRunResult:
    """Create one immutable run identity and its exact raw-master snapshot."""

    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= 100
    ):
        raise QualityInputError("max_attempts must be between 1 and 100")
    created_at = _as_utc(now or datetime.now(UTC))
    configuration = asdict(config)
    config_sha256 = quality_config_sha256(config)
    candidates = await _load_candidates(session, release_version_id)
    _validate_candidate_batch(candidates, config)
    manifest_sha256 = _manifest_sha256(release_version_id, candidates)

    existing = await _find_run_by_identity(
        session,
        release_version_id=release_version_id,
        config_sha256=config_sha256,
    )
    if existing is not None:
        await _assert_run_replay(
            session,
            existing,
            candidates=candidates,
            configuration=configuration,
            manifest_sha256=manifest_sha256,
            max_attempts=max_attempts,
        )
        return _run_result(existing, replayed=True)

    run = ScoringRun(
        id=uuid7(),
        release_version_id=release_version_id,
        configuration=configuration,
        config_sha256=config_sha256,
        input_manifest_sha256=manifest_sha256,
        scorer_version=SCORER_VERSION,
        pillow_version=PIL.__version__,
        state=ScoringRunState.RUNNING,
        asset_count=len(candidates),
        max_attempts=max_attempts,
        created_at=created_at,
        started_at=created_at,
        completed_at=None,
    )
    scores = [
        _new_asset_score(
            run_id=run.id,
            candidate=candidate,
            max_attempts=max_attempts,
            created_at=created_at,
            config_sha256=config_sha256,
        )
        for candidate in candidates
    ]
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
            session.add_all(scores)
            await session.flush()
    except IntegrityError as error:
        await session.rollback()
        candidates = await _load_candidates(session, release_version_id)
        _validate_candidate_batch(candidates, config)
        manifest_sha256 = _manifest_sha256(release_version_id, candidates)
        winner = await _find_run_by_identity(
            session,
            release_version_id=release_version_id,
            config_sha256=config_sha256,
        )
        if winner is None:
            raise QualityConflictError("scoring-run identity could not be reserved") from error
        await _assert_run_replay(
            session,
            winner,
            candidates=candidates,
            configuration=configuration,
            manifest_sha256=manifest_sha256,
            max_attempts=max_attempts,
        )
        return _run_result(winner, replayed=True)

    await session.commit()
    return _run_result(run, replayed=False)


async def freeze_scoring_run(
    session: AsyncSession,
    *,
    scoring_run_id: UUID,
    analyses: Mapping[UUID, AssetAnalysis],
    now: datetime | None = None,
) -> FrozenScoringRun:
    """Validate all outcomes and atomically freeze signals and a total ranking."""

    frozen_at = _as_utc(now or datetime.now(UTC))
    run = await session.scalar(
        select(ScoringRun).where(ScoringRun.id == scoring_run_id).with_for_update()
    )
    if run is None:
        raise QualityRunNotFoundError("scoring run was not found")

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
    _validate_analysis_keyset(scores, analyses)
    config = _stored_config(run)

    if run.state == ScoringRunState.COMPLETED:
        await _validate_completed_replay(session, run, scores, analyses, config)
        rankings = await _load_rankings(session, run.id)
        return FrozenScoringRun(
            run_id=run.id,
            rankings=tuple(_ranked_asset(row) for row in rankings),
            replayed=True,
        )
    if run.state != ScoringRunState.RUNNING:
        raise QualityConflictError("scoring run is not open for finalization")
    if len(scores) != run.asset_count:
        raise QualityConflictError("scoring run has an incomplete asset-score snapshot")
    if any(score.state != AssetScoreState.PENDING for score in scores):
        raise QualityConflictError("scoring run contains partially processed scores")

    candidates = await _load_candidates_locked(session, run.release_version_id)
    _validate_candidate_batch(candidates, config)
    if _manifest_sha256(run.release_version_id, candidates) != run.input_manifest_sha256:
        raise QualityConflictError("eligible raw-master manifest changed before finalization")
    _assert_score_snapshots(scores, candidates)

    prepared = [
        _prepare_score(score, analyses[score.asset_id], run=run, config=config) for score in scores
    ]
    duplicate_metadata = _duplicate_metadata(prepared, config)
    ordered = sorted(
        prepared,
        key=lambda item: (-item.aggregate_score_micros, str(item.score.asset_id)),
    )

    ranking_rows: list[tuple[AssetRanking, AssetScore]] = []
    for rank, item in enumerate(ordered, start=1):
        _apply_prepared_score(item, frozen_at=frozen_at)
        duplicate = duplicate_metadata.get(item.score.asset_id)
        disposition = _disposition(item.state, duplicate)
        ranking = AssetRanking(
            id=uuid7(),
            scoring_run_id=run.id,
            asset_score_id=item.score.id,
            asset_id=item.score.asset_id,
            rank=rank,
            aggregate_score_micros=item.aggregate_score_micros,
            disposition=disposition,
            explanation=_ranking_explanation(item, duplicate),
            duplicate_cluster_id=duplicate[0] if duplicate is not None else None,
            duplicate_representative_asset_id=(duplicate[1] if duplicate is not None else None),
            is_duplicate_representative=(duplicate[2] if duplicate is not None else False),
            scorer_version=run.scorer_version,
            pillow_version=run.pillow_version,
            config_sha256=run.config_sha256,
            frozen_at=frozen_at,
        )
        session.add(ranking)
        ranking_rows.append((ranking, item.score))

    try:
        # Rankings must exist while the parent run is still open. Database
        # triggers reject every late ranking insert after this transition.
        await session.flush()
        run.ranking_manifest_sha256 = ranking_manifest_sha256(run, ranking_rows)
        run.state = ScoringRunState.COMPLETED
        run.completed_at = frozen_at
        await session.commit()
    except RankingManifestIntegrityError as error:
        await session.rollback()
        raise QualityConflictError("scoring ranking manifest could not be frozen") from error
    except IntegrityError as error:
        await session.rollback()
        raise QualityConflictError("scoring rankings were finalized concurrently") from error

    rankings = await _load_rankings(session, run.id)
    return FrozenScoringRun(
        run_id=run.id,
        rankings=tuple(_ranked_asset(row) for row in rankings),
        replayed=False,
    )


async def _find_run_by_identity(
    session: AsyncSession,
    *,
    release_version_id: UUID,
    config_sha256: str,
) -> ScoringRun | None:
    return cast(
        ScoringRun | None,
        await session.scalar(
            select(ScoringRun).where(
                ScoringRun.release_version_id == release_version_id,
                ScoringRun.config_sha256 == config_sha256,
                ScoringRun.scorer_version == SCORER_VERSION,
            )
        ),
    )


async def _assert_run_replay(
    session: AsyncSession,
    run: ScoringRun,
    *,
    candidates: Sequence[_CandidateSnapshot],
    configuration: dict[str, Any],
    manifest_sha256: str,
    max_attempts: int,
) -> None:
    if (
        run.configuration != configuration
        or run.input_manifest_sha256 != manifest_sha256
        or run.pillow_version != PIL.__version__
        or run.asset_count != len(candidates)
        or run.max_attempts != max_attempts
    ):
        raise QualityConflictError("scoring-run replay conflicts with frozen inputs")
    scores = list(
        (
            await session.scalars(
                select(AssetScore)
                .where(AssetScore.scoring_run_id == run.id)
                .order_by(AssetScore.asset_id)
            )
        ).all()
    )
    _assert_score_snapshots(scores, candidates)


async def _load_candidates(
    session: AsyncSession,
    release_version_id: UUID,
) -> list[_CandidateSnapshot]:
    row = (
        await session.execute(
            select(ReleaseVersion, Release)
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(ReleaseVersion.id == release_version_id)
        )
    ).one_or_none()
    if row is None:
        raise QualityRunNotFoundError("release version was not found")
    release_version, release = row
    if (
        release.phase != ReleasePhase.REVIEWING
        or release.current_version_no != release_version.version_no
    ):
        raise QualityConflictError("quality scoring requires the current release version in review")
    assets = list(
        (
            await session.scalars(
                select(Asset)
                .join(GenerationJob, GenerationJob.id == Asset.generation_job_id)
                .where(
                    GenerationJob.release_version_id == release_version.id,
                    Asset.release_id == release_version.release_id,
                    Asset.kind == AssetKind.RAW_MASTER,
                    Asset.state == AssetState.AVAILABLE,
                )
                .order_by(Asset.id)
            )
        ).all()
    )
    return [_candidate_snapshot(asset) for asset in assets]


async def _load_candidates_locked(
    session: AsyncSession,
    release_version_id: UUID,
) -> list[_CandidateSnapshot]:
    row = (
        await session.execute(
            select(ReleaseVersion, Release)
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(ReleaseVersion.id == release_version_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise QualityRunNotFoundError("release version was not found")
    release_version, release = row
    if (
        release.phase != ReleasePhase.REVIEWING
        or release.current_version_no != release_version.version_no
    ):
        raise QualityConflictError("quality scoring requires the current release version in review")
    job_ids = list(
        (
            await session.scalars(
                select(GenerationJob.id)
                .where(GenerationJob.release_version_id == release_version.id)
                .order_by(GenerationJob.id)
                .with_for_update()
            )
        ).all()
    )
    if not job_ids:
        return []
    assets = list(
        (
            await session.scalars(
                select(Asset)
                .where(Asset.generation_job_id.in_(job_ids))
                .order_by(Asset.id)
                .with_for_update()
            )
        ).all()
    )
    return [
        _candidate_snapshot(asset)
        for asset in assets
        if (
            asset.release_id == release_version.release_id
            and asset.kind == AssetKind.RAW_MASTER
            and asset.state == AssetState.AVAILABLE
        )
    ]


def _candidate_snapshot(asset: Asset) -> _CandidateSnapshot:
    if not asset.object_version_id:
        raise QualityConflictError("available raw master has no exact object version")
    if (
        asset.object_key is None
        or asset.sha256 is None
        or asset.image_format is None
        or asset.width is None
        or asset.height is None
        or asset.byte_size is None
    ):
        raise QualityConflictError("available raw master is missing immutable metadata")
    if (
        not isinstance(asset.sha256, str)
        or not _SHA256_PATTERN.fullmatch(asset.sha256)
        or not asset.object_key
        or not asset.storage_backend
        or not asset.storage_bucket
        or not isinstance(asset.byte_size, int)
        or asset.byte_size <= 0
        or not isinstance(asset.width, int)
        or asset.width <= 0
        or not isinstance(asset.height, int)
        or asset.height <= 0
    ):
        raise QualityConflictError("available raw master metadata is invalid")
    return _CandidateSnapshot(
        asset_id=asset.id,
        storage_backend=asset.storage_backend,
        storage_bucket=asset.storage_bucket,
        object_key=asset.object_key,
        object_version_id=asset.object_version_id,
        sha256=asset.sha256,
        byte_size=asset.byte_size,
        image_format=asset.image_format.upper(),
        width=asset.width,
        height=asset.height,
    )


def _validate_candidate_batch(
    candidates: Sequence[_CandidateSnapshot],
    config: QualityConfig,
) -> None:
    if not candidates:
        raise QualityConflictError("release version has no available raw masters")
    if len(candidates) > config.max_batch_size:
        raise QualityConflictError("raw-master count exceeds the configured scoring bound")
    if len({candidate.asset_id for candidate in candidates}) != len(candidates):
        raise QualityConflictError("raw-master candidate manifest contains duplicates")


def _manifest_sha256(
    release_version_id: UUID,
    candidates: Sequence[_CandidateSnapshot],
) -> str:
    return canonical_sha256(
        {
            "release_version_id": str(release_version_id),
            "assets": [
                candidate.manifest_entry()
                for candidate in sorted(candidates, key=lambda item: str(item.asset_id))
            ],
        }
    )


def _new_asset_score(
    *,
    run_id: UUID,
    candidate: _CandidateSnapshot,
    max_attempts: int,
    created_at: datetime,
    config_sha256: str,
) -> AssetScore:
    return AssetScore(
        id=uuid7(),
        scoring_run_id=run_id,
        asset_id=candidate.asset_id,
        asset_storage_backend=candidate.storage_backend,
        asset_storage_bucket=candidate.storage_bucket,
        asset_sha256=candidate.sha256,
        asset_object_key=candidate.object_key,
        asset_object_version_id=candidate.object_version_id,
        asset_byte_size=candidate.byte_size,
        asset_image_format=candidate.image_format,
        asset_width=candidate.width,
        asset_height=candidate.height,
        state=AssetScoreState.PENDING,
        attempts=0,
        max_attempts=max_attempts,
        available_at=created_at,
        lease_owner=None,
        lease_expires_at=None,
        signal_detail={},
        scorer_version=SCORER_VERSION,
        pillow_version=PIL.__version__,
        config_sha256=config_sha256,
        created_at=created_at,
    )


def _assert_score_snapshots(
    scores: Sequence[AssetScore],
    candidates: Sequence[_CandidateSnapshot],
) -> None:
    if len(scores) != len(candidates):
        raise QualityConflictError("asset-score snapshot count is inconsistent")
    candidate_by_id = {candidate.asset_id: candidate for candidate in candidates}
    for score in scores:
        candidate = candidate_by_id.get(score.asset_id)
        if candidate is None or _score_snapshot(score) != candidate:
            raise QualityConflictError("asset-score snapshot conflicts with raw-master metadata")


def _score_snapshot(score: AssetScore) -> _CandidateSnapshot:
    return _CandidateSnapshot(
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
    )


def _validate_analysis_keyset(
    scores: Sequence[AssetScore],
    analyses: Mapping[UUID, AssetAnalysis],
) -> None:
    expected = {score.asset_id for score in scores}
    supplied = set(analyses)
    if supplied != expected:
        raise QualityInputError("analyses must cover the exact frozen asset set")


def _stored_config(run: ScoringRun) -> QualityConfig:
    try:
        config = QualityConfig(**run.configuration)
    except (TypeError, ValueError) as error:
        raise QualityConflictError("stored quality configuration is invalid") from error
    if quality_config_sha256(config) != run.config_sha256:
        raise QualityConflictError("stored quality configuration digest does not match")
    return config


def _prepare_score(
    score: AssetScore,
    analysis: AssetAnalysis,
    *,
    run: ScoringRun,
    config: QualityConfig,
) -> _PreparedScore:
    if isinstance(analysis, CorruptAssetSignal):
        return _PreparedScore(
            score=score,
            state=AssetScoreState.FLAGGED_CORRUPT,
            aggregate_score_micros=0,
            signal_detail={
                "classification": "corrupt",
                "reason_code": analysis.error_code,
                "requires_review": True,
            },
            result=None,
        )
    if not isinstance(analysis, QualityResult):
        raise QualityInputError("analysis outcome has an unsupported type")
    _validate_quality_result(score, analysis, run=run, config=config)
    blank = _is_high_confidence_blank(analysis)
    return _PreparedScore(
        score=score,
        state=(AssetScoreState.FLAGGED_BLANK if blank else AssetScoreState.SCORED),
        aggregate_score_micros=analysis.score_micros,
        signal_detail={
            "classification": "blank" if blank else "scored",
            "requires_review": blank,
        },
        result=analysis,
    )


def _validate_quality_result(
    score: AssetScore,
    result: QualityResult,
    *,
    run: ScoringRun,
    config: QualityConfig,
) -> None:
    if (
        result.sha256 != score.asset_sha256
        or result.byte_size != score.asset_byte_size
        or result.width != score.asset_width
        or result.height != score.asset_height
        or result.image_format.upper() != score.asset_image_format
    ):
        raise QualityInputError("quality result does not match the frozen asset bytes")
    if (
        result.config != config
        or result.config_sha256 != run.config_sha256
        or result.scorer_version != run.scorer_version
        or result.pillow_version != run.pillow_version
    ):
        raise QualityInputError("quality result version contract does not match the run")
    metrics = result.metrics
    bounded_values = (
        (metrics.luminance_mean_micros, 0, MICROS),
        (metrics.luminance_std_micros, 0, MICROS),
        (metrics.dynamic_range_micros, 0, MICROS),
        (metrics.entropy_bits_micros, 0, 8 * MICROS),
        (metrics.entropy_normalized_micros, 0, MICROS),
        (metrics.sharpness_micros, 0, MICROS),
        (result.score_micros, 0, MICROS),
        (result.dhash64, 0, (1 << 64) - 1),
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum
        for value, minimum, maximum in bounded_values
    ):
        raise QualityInputError("quality result contains an invalid numeric signal")
    if result.score_breakdown.total_micros != result.score_micros:
        raise QualityInputError("quality result aggregate does not match its breakdown")
    if (
        result.thumbnail.width != config.thumbnail_size
        or result.thumbnail.height != config.thumbnail_size
        or len(result.thumbnail.luminance) != config.thumbnail_size**2
    ):
        raise QualityInputError("quality result thumbnail contract is invalid")
    if calculate_metrics(result.thumbnail) != result.metrics:
        raise QualityInputError("quality result metrics do not match its normalized thumbnail")
    if score_metrics(result.metrics, config=config) != result.score_breakdown:
        raise QualityInputError("quality result breakdown is not deterministic")


def _is_high_confidence_blank(result: QualityResult) -> bool:
    metrics = result.metrics
    return (
        metrics.dynamic_range_micros == 0
        and metrics.entropy_normalized_micros == 0
        and metrics.sharpness_micros == 0
    )


def _duplicate_metadata(
    prepared: Sequence[_PreparedScore],
    config: QualityConfig,
) -> dict[UUID, tuple[str, UUID, bool, int]]:
    candidates = [
        DuplicateCandidate(
            identifier=str(item.score.asset_id),
            dhash64=item.result.dhash64,
            quality_score_micros=item.aggregate_score_micros,
        )
        for item in prepared
        if item.result is not None
    ]
    result: dict[UUID, tuple[str, UUID, bool, int]] = {}
    for cluster in cluster_near_duplicates(candidates, config=config):
        if len(cluster.member_ids) < 2:
            continue
        representative_id = UUID(cluster.representative_id)
        for member_id in cluster.member_ids:
            asset_id = UUID(member_id)
            result[asset_id] = (
                cluster.cluster_id,
                representative_id,
                asset_id == representative_id,
                cluster.max_pairwise_hamming,
            )
    return result


def _disposition(
    state: AssetScoreState,
    duplicate: tuple[str, UUID, bool, int] | None,
) -> RankingDisposition:
    if state != AssetScoreState.SCORED:
        return RankingDisposition.FLAGGED_REVIEW
    if duplicate is not None and not duplicate[2]:
        return RankingDisposition.NEAR_DUPLICATE
    return RankingDisposition.REVIEW_CANDIDATE


def _apply_prepared_score(
    item: _PreparedScore,
    *,
    frozen_at: datetime,
) -> None:
    score = item.score
    score.state = item.state
    score.signal_detail = item.signal_detail
    score.aggregate_score_micros = item.aggregate_score_micros
    score.lease_owner = None
    score.lease_expires_at = None
    score.completed_at = frozen_at
    if item.result is None:
        score.last_error_code = str(item.signal_detail["reason_code"])
        score.last_error_detail = "Image bytes require manual review."
        return

    result = item.result
    score.luminance_mean_micros = result.metrics.luminance_mean_micros
    score.luminance_std_micros = result.metrics.luminance_std_micros
    score.dynamic_range_micros = result.metrics.dynamic_range_micros
    score.entropy_bits_micros = result.metrics.entropy_bits_micros
    score.entropy_normalized_micros = result.metrics.entropy_normalized_micros
    score.sharpness_micros = result.metrics.sharpness_micros
    score.dhash_hex = result.dhash_hex
    score.score_breakdown = asdict(result.score_breakdown)
    score.last_error_code = None
    score.last_error_detail = None


def _ranking_explanation(
    item: _PreparedScore,
    duplicate: tuple[str, UUID, bool, int] | None,
) -> dict[str, Any]:
    duplicate_detail: dict[str, Any] | None = None
    if duplicate is not None:
        duplicate_detail = {
            "cluster_id": duplicate[0],
            "representative_asset_id": str(duplicate[1]),
            "is_representative": duplicate[2],
            "max_pairwise_hamming": duplicate[3],
        }
    return {
        "aggregate_score_micros": item.aggregate_score_micros,
        "quality_state": item.state.value,
        "signal": item.signal_detail,
        "duplicate": duplicate_detail,
    }


async def _validate_completed_replay(
    session: AsyncSession,
    run: ScoringRun,
    scores: Sequence[AssetScore],
    analyses: Mapping[UUID, AssetAnalysis],
    config: QualityConfig,
) -> None:
    if run.completed_at is None or len(scores) != run.asset_count:
        raise QualityConflictError("completed scoring run is internally inconsistent")
    for score in scores:
        prepared = _prepare_score(score, analyses[score.asset_id], run=run, config=config)
        if (
            score.state != prepared.state
            or score.aggregate_score_micros != prepared.aggregate_score_micros
            or score.signal_detail != prepared.signal_detail
        ):
            raise QualityConflictError("completed-run replay conflicts with frozen signals")
        if prepared.result is not None and (
            score.dhash_hex != prepared.result.dhash_hex
            or score.score_breakdown != asdict(prepared.result.score_breakdown)
        ):
            raise QualityConflictError("completed-run replay conflicts with frozen metrics")
    ranking_rows = await load_ranking_manifest_rows(session, run.id)
    try:
        validate_completed_ranking_manifest(run, ranking_rows)
    except RankingManifestIntegrityError as error:
        raise QualityConflictError("completed scoring run has an invalid frozen ranking") from error


async def _load_rankings(
    session: AsyncSession,
    run_id: UUID,
) -> list[AssetRanking]:
    return list(
        (
            await session.scalars(
                select(AssetRanking)
                .where(AssetRanking.scoring_run_id == run_id)
                .order_by(AssetRanking.rank)
            )
        ).all()
    )


def _ranked_asset(row: AssetRanking) -> RankedAsset:
    return RankedAsset(
        asset_id=row.asset_id,
        rank=row.rank,
        aggregate_score_micros=row.aggregate_score_micros,
        disposition=row.disposition,
        duplicate_cluster_id=row.duplicate_cluster_id,
        duplicate_representative_asset_id=row.duplicate_representative_asset_id,
        is_duplicate_representative=row.is_duplicate_representative,
    )


def _run_result(run: ScoringRun, *, replayed: bool) -> ScoringRunResult:
    return ScoringRunResult(
        run_id=run.id,
        release_version_id=run.release_version_id,
        state=run.state,
        asset_count=run.asset_count,
        config_sha256=run.config_sha256,
        input_manifest_sha256=run.input_manifest_sha256,
        scorer_version=run.scorer_version,
        pillow_version=run.pillow_version,
        replayed=replayed,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
