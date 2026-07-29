import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import batched
from types import MappingProxyType
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    Asset,
    AssetRanking,
    AssetScore,
    GenerationJob,
    Project,
    Release,
    ReleaseVersion,
    ReviewTask,
    ScoringRun,
)
from gen_automation.domain.enums import AssetKind, AssetScoreState, ScoringRunState
from gen_automation.services.ranking_manifest import (
    RankingManifestIntegrityError,
    validate_completed_ranking_manifest,
)
from gen_automation.storage.base import ObjectStore, ObjectStoreError

_MAX_SIGNING_CONCURRENCY = 32
_MAX_SIGNED_URL_LENGTH = 16_384
_SAFE_FILENAME_PART = re.compile(r"[^a-zA-Z0-9_-]+")
_SAFE_IMAGE_FORMAT = re.compile(r"^[a-zA-Z0-9]{1,10}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_SIGNED_URL_SCHEMES = frozenset({"http", "https", "memory"})
_RANKABLE_SCORE_STATES = frozenset(
    {
        AssetScoreState.SCORED,
        AssetScoreState.FLAGGED_BLANK,
        AssetScoreState.FLAGGED_CORRUPT,
    }
)


class RankedDashboardError(Exception):
    """Base error for read-only ranked-dashboard queries."""


class RankedReleaseNotFoundError(RankedDashboardError):
    pass


class RankingUnavailableError(RankedDashboardError):
    pass


class RankingIntegrityError(RankedDashboardError):
    pass


class RankingStorageError(RankedDashboardError):
    pass


@dataclass(frozen=True)
class DashboardRelease:
    id: UUID
    project_slug: str
    project_name: str
    slug: str
    title: str
    phase: str
    health: str
    current_version_no: int
    updated_at: datetime
    completed_scoring_run_id: UUID | None
    ranked_asset_count: int
    ranking_is_complete: bool


@dataclass(frozen=True)
class RankedMaster:
    asset_id: UUID
    rank: int
    aggregate_score_micros: int
    score_percent: str
    disposition: str
    explanation: Mapping[str, object]
    explanation_summary: tuple[str, ...]
    width: int
    height: int
    image_format: str
    byte_size: int
    checksum_prefix: str
    view_url: str
    download_url: str
    download_name: str


@dataclass(frozen=True)
class RankedRelease:
    release_id: UUID
    release_version_id: UUID
    scoring_run_id: UUID
    project_slug: str
    project_name: str
    release_slug: str
    release_title: str
    release_phase: str
    current_version_no: int
    release_version_no: int
    scorer_version: str
    completed_at: datetime
    assets: tuple[RankedMaster, ...]


@dataclass(frozen=True)
class ReviewTaskNavigation:
    task_id: UUID
    scoring_run_id: UUID
    release_version_id: UUID
    release_id: UUID


async def list_dashboard_releases(
    session: AsyncSession,
    *,
    limit: int = 200,
) -> tuple[DashboardRelease, ...]:
    if not 1 <= limit <= 500:
        raise ValueError("dashboard release limit must be between 1 and 500")
    release_rows = list(
        (
            await session.execute(
                select(Release, Project, ReleaseVersion)
                .join(Project, Project.id == Release.project_id)
                .join(
                    ReleaseVersion,
                    (ReleaseVersion.release_id == Release.id)
                    & (ReleaseVersion.version_no == Release.current_version_no),
                )
                .order_by(Project.slug, Release.updated_at.desc(), Release.id)
                .limit(limit)
            )
        ).all()
    )
    if not release_rows:
        return ()

    version_ids = [version.id for _, _, version in release_rows]
    completed_runs = list(
        (
            await session.scalars(
                select(ScoringRun)
                .where(
                    ScoringRun.release_version_id.in_(version_ids),
                    ScoringRun.state == ScoringRunState.COMPLETED,
                )
                .order_by(
                    ScoringRun.release_version_id,
                    ScoringRun.completed_at.desc(),
                    ScoringRun.created_at.desc(),
                    ScoringRun.id.desc(),
                )
            )
        ).all()
    )
    latest_by_version: dict[UUID, ScoringRun] = {}
    for run in completed_runs:
        latest_by_version.setdefault(run.release_version_id, run)

    run_ids = [run.id for run in latest_by_version.values()]
    ranking_counts: dict[UUID, int] = {}
    if run_ids:
        count_rows = (
            await session.execute(
                select(
                    AssetRanking.scoring_run_id,
                    func.count(AssetRanking.id),
                )
                .where(AssetRanking.scoring_run_id.in_(run_ids))
                .group_by(AssetRanking.scoring_run_id)
            )
        ).all()
        ranking_counts = {run_id: int(count) for run_id, count in count_rows}

    return tuple(
        _dashboard_release(
            release,
            project,
            version,
            latest_by_version.get(version.id),
            ranking_counts,
        )
        for release, project, version in release_rows
    )


async def load_ranked_release(
    session: AsyncSession,
    *,
    store: ObjectStore,
    release_id: UUID,
    expires_in: int,
) -> RankedRelease:
    _validate_expiry(expires_in)
    release_row = (
        await session.execute(
            select(Release, Project, ReleaseVersion)
            .join(Project, Project.id == Release.project_id)
            .join(
                ReleaseVersion,
                (ReleaseVersion.release_id == Release.id)
                & (ReleaseVersion.version_no == Release.current_version_no),
            )
            .where(Release.id == release_id)
        )
    ).one_or_none()
    if release_row is None:
        raise RankedReleaseNotFoundError("release was not found")
    release, project, release_version = release_row

    run = await session.scalar(
        select(ScoringRun)
        .where(
            ScoringRun.release_version_id == release_version.id,
            ScoringRun.state == ScoringRunState.COMPLETED,
        )
        .order_by(
            ScoringRun.completed_at.desc(),
            ScoringRun.created_at.desc(),
            ScoringRun.id.desc(),
        )
        .limit(1)
    )
    if run is None or run.completed_at is None:
        raise RankingUnavailableError("the current release version has no completed ranking")

    return await _load_ranked_snapshot(
        session,
        store=store,
        release=release,
        project=project,
        release_version=release_version,
        run=run,
        expires_in=expires_in,
    )


async def load_ranked_scoring_run(
    session: AsyncSession,
    *,
    store: ObjectStore,
    scoring_run_id: UUID,
    expires_in: int,
) -> RankedRelease:
    """Load and sign the exact frozen ranking snapshot selected by a review task."""

    _validate_expiry(expires_in)
    row = (
        await session.execute(
            select(ScoringRun, ReleaseVersion, Release, Project)
            .join(
                ReleaseVersion,
                ReleaseVersion.id == ScoringRun.release_version_id,
            )
            .join(Release, Release.id == ReleaseVersion.release_id)
            .join(Project, Project.id == Release.project_id)
            .where(ScoringRun.id == scoring_run_id)
        )
    ).one_or_none()
    if row is None:
        raise RankedReleaseNotFoundError("scoring run was not found")
    run, release_version, release, project = row
    if run.state != ScoringRunState.COMPLETED or run.completed_at is None:
        raise RankingUnavailableError("the scoring run has no completed ranking")
    return await _load_ranked_snapshot(
        session,
        store=store,
        release=release,
        project=project,
        release_version=release_version,
        run=run,
        expires_in=expires_in,
    )


async def load_current_completed_scoring_run_id(
    session: AsyncSession,
    *,
    release_id: UUID,
) -> UUID:
    release_version_id = await session.scalar(
        select(ReleaseVersion.id)
        .join(
            Release,
            (Release.id == ReleaseVersion.release_id)
            & (Release.current_version_no == ReleaseVersion.version_no),
        )
        .where(Release.id == release_id)
    )
    if release_version_id is None:
        raise RankedReleaseNotFoundError("release was not found")
    scoring_run_id = await session.scalar(
        select(ScoringRun.id)
        .where(
            ScoringRun.release_version_id == release_version_id,
            ScoringRun.state == ScoringRunState.COMPLETED,
            ScoringRun.completed_at.is_not(None),
        )
        .order_by(
            ScoringRun.completed_at.desc(),
            ScoringRun.created_at.desc(),
            ScoringRun.id.desc(),
        )
        .limit(1)
    )
    if scoring_run_id is None:
        raise RankingUnavailableError("the current release version has no completed ranking")
    return cast(UUID, scoring_run_id)


async def find_review_task_id(
    session: AsyncSession,
    *,
    scoring_run_id: UUID,
) -> UUID | None:
    return await session.scalar(
        select(ReviewTask.id).where(ReviewTask.scoring_run_id == scoring_run_id)
    )


async def load_review_task_navigation(
    session: AsyncSession,
    *,
    review_task_id: UUID,
) -> ReviewTaskNavigation:
    row = (
        await session.execute(
            select(ReviewTask, ReleaseVersion)
            .join(
                ReleaseVersion,
                ReleaseVersion.id == ReviewTask.release_version_id,
            )
            .where(ReviewTask.id == review_task_id)
        )
    ).one_or_none()
    if row is None:
        raise RankedReleaseNotFoundError("review task was not found")
    task, version = row
    return ReviewTaskNavigation(
        task_id=task.id,
        scoring_run_id=task.scoring_run_id,
        release_version_id=task.release_version_id,
        release_id=version.release_id,
    )


async def _load_ranked_snapshot(
    session: AsyncSession,
    *,
    store: ObjectStore,
    release: Release,
    project: Project,
    release_version: ReleaseVersion,
    run: ScoringRun,
    expires_in: int,
) -> RankedRelease:
    if run.completed_at is None:
        raise RankingUnavailableError("the scoring run has no completion timestamp")
    result = await session.execute(
        select(AssetRanking, AssetScore, Asset)
        .join(
            AssetScore,
            (AssetScore.id == AssetRanking.asset_score_id)
            & (AssetScore.scoring_run_id == AssetRanking.scoring_run_id)
            & (AssetScore.asset_id == AssetRanking.asset_id),
        )
        .join(Asset, Asset.id == AssetRanking.asset_id)
        .join(
            GenerationJob,
            GenerationJob.id == Asset.generation_job_id,
        )
        .where(
            AssetRanking.scoring_run_id == run.id,
            Asset.release_id == release.id,
            Asset.kind == AssetKind.RAW_MASTER,
            GenerationJob.release_version_id == release_version.id,
        )
        .order_by(AssetRanking.rank)
    )
    rows = [(ranking, score, asset) for ranking, score, asset in result.all()]
    _validate_ranked_rows(rows, run=run, store=store)

    signed_assets: list[RankedMaster] = []
    try:
        for row_batch in batched(rows, _MAX_SIGNING_CONCURRENCY):
            signed_assets.extend(
                await asyncio.gather(
                    *(
                        _sign_ranked_master(
                            store,
                            release_slug=release.slug,
                            ranking=ranking,
                            score=score,
                            expires_in=expires_in,
                        )
                        for ranking, score, _asset in row_batch
                    )
                )
            )
    except ObjectStoreError as error:
        raise RankingStorageError("raw-master access could not be signed") from error

    return RankedRelease(
        release_id=release.id,
        release_version_id=release_version.id,
        scoring_run_id=run.id,
        project_slug=project.slug,
        project_name=project.name,
        release_slug=release.slug,
        release_title=release.title,
        release_phase=release.phase.value,
        current_version_no=release.current_version_no,
        release_version_no=release_version.version_no,
        scorer_version=run.scorer_version,
        completed_at=run.completed_at,
        assets=tuple(signed_assets),
    )


def _validate_expiry(expires_in: int) -> None:
    if not 60 <= expires_in <= 900:
        raise ValueError("dashboard signed URLs must expire within 60 to 900 seconds")


def _dashboard_release(
    release: Release,
    project: Project,
    _version: ReleaseVersion,
    run: ScoringRun | None,
    ranking_counts: Mapping[UUID, int],
) -> DashboardRelease:
    count = ranking_counts.get(run.id, 0) if run is not None else 0
    return DashboardRelease(
        id=release.id,
        project_slug=project.slug,
        project_name=project.name,
        slug=release.slug,
        title=release.title,
        phase=release.phase.value,
        health=release.health.value,
        current_version_no=release.current_version_no,
        updated_at=release.updated_at,
        completed_scoring_run_id=run.id if run is not None else None,
        ranked_asset_count=count,
        ranking_is_complete=run is not None and count == run.asset_count,
    )


def _validate_ranked_rows(
    rows: Sequence[tuple[AssetRanking, AssetScore, Asset]],
    *,
    run: ScoringRun,
    store: ObjectStore,
) -> None:
    if len(rows) != run.asset_count or [row[0].rank for row in rows] != list(
        range(1, run.asset_count + 1)
    ):
        raise RankingIntegrityError("the completed ranking is incomplete")
    for ranking, score, asset in rows:
        if (
            ranking.asset_id != asset.id
            or ranking.asset_id != score.asset_id
            or score.state not in _RANKABLE_SCORE_STATES
            or score.completed_at is None
            or ranking.aggregate_score_micros < 0
            or ranking.aggregate_score_micros > 1_000_000
            or (
                score.aggregate_score_micros is not None
                and score.aggregate_score_micros != ranking.aggregate_score_micros
            )
            or ranking.scorer_version != run.scorer_version
            or score.scorer_version != run.scorer_version
            or ranking.config_sha256 != run.config_sha256
            or score.config_sha256 != run.config_sha256
            or not isinstance(ranking.explanation, dict)
            or score.asset_width <= 0
            or score.asset_height <= 0
            or score.asset_byte_size <= 0
            or not _SHA256.fullmatch(score.asset_sha256)
            or not score.asset_object_key
            or not score.asset_object_version_id
            or score.asset_storage_backend != store.backend
            or score.asset_storage_bucket != store.bucket
        ):
            raise RankingIntegrityError("a ranked raw-master snapshot is invalid")
    try:
        validate_completed_ranking_manifest(
            run,
            [(ranking, score) for ranking, score, _asset in rows],
        )
    except RankingManifestIntegrityError as error:
        raise RankingIntegrityError("the completed ranking manifest changed") from error


async def _sign_ranked_master(
    store: ObjectStore,
    *,
    release_slug: str,
    ranking: AssetRanking,
    score: AssetScore,
    expires_in: int,
) -> RankedMaster:
    download_name = _download_name(
        release_slug=release_slug,
        rank=ranking.rank,
        asset_id=ranking.asset_id,
        image_format=score.asset_image_format,
    )
    view_url, download_url = await asyncio.gather(
        store.presign_download(
            key=score.asset_object_key,
            version_id=score.asset_object_version_id,
            expires_in=expires_in,
        ),
        store.presign_download(
            key=score.asset_object_key,
            version_id=score.asset_object_version_id,
            expires_in=expires_in,
            download_name=download_name,
        ),
    )
    _validate_signed_url(view_url)
    _validate_signed_url(download_url)
    explanation = dict(ranking.explanation)
    return RankedMaster(
        asset_id=ranking.asset_id,
        rank=ranking.rank,
        aggregate_score_micros=ranking.aggregate_score_micros,
        score_percent=f"{ranking.aggregate_score_micros / 10_000:.1f}%",
        disposition=ranking.disposition.value,
        explanation=MappingProxyType(explanation),
        explanation_summary=_explanation_summary(explanation),
        width=score.asset_width,
        height=score.asset_height,
        image_format=score.asset_image_format,
        byte_size=score.asset_byte_size,
        checksum_prefix=score.asset_sha256[:12],
        view_url=view_url,
        download_url=download_url,
        download_name=download_name,
    )


def _download_name(
    *,
    release_slug: str,
    rank: int,
    asset_id: UUID,
    image_format: str,
) -> str:
    safe_slug = _SAFE_FILENAME_PART.sub("-", release_slug).strip("-_")[:80] or "release"
    safe_format = image_format.lower()
    extension = safe_format if _SAFE_IMAGE_FORMAT.fullmatch(safe_format) else "img"
    return f"{safe_slug}-rank-{rank:04d}-{str(asset_id)[:8]}.{extension}"


def _validate_signed_url(url: str) -> None:
    if (
        not isinstance(url, str)
        or not url
        or len(url) > _MAX_SIGNED_URL_LENGTH
        or any(ord(character) < 32 for character in url)
    ):
        raise RankingStorageError("object storage returned an invalid signed URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in _ALLOWED_SIGNED_URL_SCHEMES
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RankingStorageError("object storage returned an invalid signed URL")


def _explanation_summary(explanation: Mapping[str, object]) -> tuple[str, ...]:
    summary: list[str] = []
    quality_state = explanation.get("quality_state")
    if isinstance(quality_state, str) and quality_state:
        summary.append(f"Quality state: {quality_state.replace('_', ' ')}")
    signal = explanation.get("signal")
    if isinstance(signal, dict):
        classification = signal.get("classification")
        if isinstance(classification, str) and classification:
            summary.append(f"Signal: {classification.replace('_', ' ')}")
        reason_code = signal.get("reason_code")
        if isinstance(reason_code, str) and reason_code:
            summary.append(f"Reason: {reason_code.replace('_', ' ')}")
    duplicate = explanation.get("duplicate")
    if isinstance(duplicate, dict):
        is_representative = duplicate.get("is_representative")
        summary.append(
            "Duplicate cluster representative"
            if is_representative is True
            else "Near-duplicate member"
        )
    if not summary:
        summary.append("No additional scoring notes")
    return tuple(summary)
