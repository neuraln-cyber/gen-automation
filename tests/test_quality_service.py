import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import func, select

from gen_automation.db.models import (
    Asset,
    AssetRanking,
    AssetScore,
    GenerationJob,
    Project,
    Release,
    ReleaseVersion,
    ScoringRun,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AssetKind,
    AssetScoreState,
    AssetState,
    GenerationState,
    RankingDisposition,
    ReleasePhase,
    ScoringRunState,
)
from gen_automation.quality import QualityResult, analyze_image
from gen_automation.services.quality import (
    CorruptAssetSignal,
    QualityConflictError,
    QualityInputError,
    create_scoring_run,
    freeze_scoring_run,
)


@dataclass(frozen=True)
class QualityContext:
    database: Database
    release_version_id: UUID
    asset_ids: tuple[UUID, ...]
    payloads: dict[UUID, bytes]


def _png(*, blank: bool = False) -> bytes:
    image = Image.new("L", (64, 64), 128 if blank else 16)
    if not blank:
        draw = ImageDraw.Draw(image)
        draw.rectangle((7, 9, 49, 45), fill=232)
        draw.ellipse((19, 16, 42, 39), fill=48)
        draw.line((2, 60, 60, 2), fill=170, width=3)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _asset(
    *,
    asset_id: UUID,
    release_id: UUID,
    job_id: UUID,
    output_index: int,
    payload: bytes,
    kind: AssetKind = AssetKind.RAW_MASTER,
    state: AssetState = AssetState.AVAILABLE,
) -> Asset:
    return Asset(
        id=asset_id,
        release_id=release_id,
        generation_job_id=job_id,
        output_index=output_index,
        kind=kind,
        state=state,
        storage_backend="s3",
        storage_bucket="quality-test",
        object_key=f"masters/{asset_id}.png",
        object_version_id=f"version-{asset_id}",
        sha256=hashlib.sha256(payload).hexdigest(),
        content_type="image/png",
        image_format="PNG",
        width=64,
        height=64,
        byte_size=len(payload),
        asset_metadata={},
        available_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
    )


@pytest.fixture
async def quality_context(tmp_path: Path) -> AsyncIterator[QualityContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'quality-service.db').as_posix()}")
    await database.create_schema()
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    duplicate_payload = _png()
    blank_payload = _png(blank=True)
    corrupt_payload = b"not-an-image"
    current_ids = (
        UUID("00000000-0000-4000-8000-000000000011"),
        UUID("00000000-0000-4000-8000-000000000012"),
        UUID("00000000-0000-4000-8000-000000000013"),
        UUID("00000000-0000-4000-8000-000000000014"),
    )
    payloads = {
        current_ids[0]: duplicate_payload,
        current_ids[1]: duplicate_payload,
        current_ids[2]: blank_payload,
        current_ids[3]: corrupt_payload,
    }

    async with database.sessions() as session:
        project = Project(slug="quality", name="Quality")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="release",
            title="Quality release",
            phase=ReleasePhase.REVIEWING,
            current_version_no=2,
            desired_accepted_count=2,
        )
        session.add(release)
        await session.flush()
        old_version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={"version": 1},
            specification_sha256="1" * 64,
            created_by="test",
            created_at=now,
        )
        current_version = ReleaseVersion(
            release_id=release.id,
            version_no=2,
            specification={"version": 2},
            specification_sha256="2" * 64,
            created_by="test",
            created_at=now,
        )
        session.add_all([old_version, current_version])
        await session.flush()
        old_job = GenerationJob(
            release_version_id=old_version.id,
            logical_key="3" * 64,
            parameters={"version": 1},
            parameters_sha256="4" * 64,
            state=GenerationState.SUCCEEDED,
            expected_output_count=1,
        )
        current_job = GenerationJob(
            release_version_id=current_version.id,
            logical_key="5" * 64,
            parameters={"version": 2},
            parameters_sha256="6" * 64,
            state=GenerationState.SUCCEEDED,
            expected_output_count=4,
        )
        session.add_all([old_job, current_job])
        await session.flush()
        session.add_all(
            [
                *[
                    _asset(
                        asset_id=asset_id,
                        release_id=release.id,
                        job_id=current_job.id,
                        output_index=index,
                        payload=payloads[asset_id],
                    )
                    for index, asset_id in enumerate(current_ids)
                ],
                _asset(
                    asset_id=UUID("00000000-0000-4000-8000-000000000021"),
                    release_id=release.id,
                    job_id=old_job.id,
                    output_index=0,
                    payload=duplicate_payload,
                ),
                _asset(
                    asset_id=UUID("00000000-0000-4000-8000-000000000022"),
                    release_id=release.id,
                    job_id=current_job.id,
                    output_index=0,
                    payload=duplicate_payload,
                    kind=AssetKind.DERIVATIVE,
                ),
                _asset(
                    asset_id=UUID("00000000-0000-4000-8000-000000000023"),
                    release_id=release.id,
                    job_id=current_job.id,
                    output_index=4,
                    payload=duplicate_payload,
                    state=AssetState.ARCHIVED,
                ),
            ]
        )
        await session.commit()
        context = QualityContext(
            database=database,
            release_version_id=current_version.id,
            asset_ids=current_ids,
            payloads=payloads,
        )
    try:
        yield context
    finally:
        await database.dispose()


async def _create(context: QualityContext):
    async with context.database.sessions() as session:
        return await create_scoring_run(
            session,
            release_version_id=context.release_version_id,
            now=datetime(2026, 7, 28, 12, 1, tzinfo=UTC),
        )


def _analyses(context: QualityContext) -> dict[UUID, QualityResult | CorruptAssetSignal]:
    return {
        context.asset_ids[0]: analyze_image(context.payloads[context.asset_ids[0]]),
        context.asset_ids[1]: analyze_image(context.payloads[context.asset_ids[1]]),
        context.asset_ids[2]: analyze_image(context.payloads[context.asset_ids[2]]),
        context.asset_ids[3]: CorruptAssetSignal(),
    }


@pytest.mark.asyncio
async def test_create_run_snapshots_exact_version_and_replays(
    quality_context: QualityContext,
) -> None:
    first = await _create(quality_context)
    second = await _create(quality_context)

    assert first.asset_count == 4
    assert not first.replayed
    assert second.run_id == first.run_id
    assert second.replayed
    assert len(first.input_manifest_sha256) == 64

    async with quality_context.database.sessions() as session:
        scores = list(
            (
                await session.scalars(
                    select(AssetScore)
                    .where(AssetScore.scoring_run_id == first.run_id)
                    .order_by(AssetScore.asset_id)
                )
            ).all()
        )
        assert [score.asset_id for score in scores] == list(quality_context.asset_ids)
        assert all(score.asset_object_version_id.startswith("version-") for score in scores)
        assert all(score.asset_storage_backend == "s3" for score in scores)
        assert all(score.asset_image_format == "PNG" for score in scores)
        assert all(score.state == AssetScoreState.PENDING for score in scores)


@pytest.mark.asyncio
async def test_create_run_rejects_unversioned_master_and_manifest_drift(
    quality_context: QualityContext,
) -> None:
    async with quality_context.database.sessions() as session:
        asset = await session.get(Asset, quality_context.asset_ids[0])
        assert asset is not None
        asset.object_version_id = None
        await session.commit()
    async with quality_context.database.sessions() as session:
        with pytest.raises(QualityConflictError, match="exact object version"):
            await create_scoring_run(
                session,
                release_version_id=quality_context.release_version_id,
            )
        assert int(await session.scalar(select(func.count()).select_from(ScoringRun)) or 0) == 0

    async with quality_context.database.sessions() as session:
        asset = await session.get(Asset, quality_context.asset_ids[0])
        assert asset is not None
        asset.object_version_id = f"version-{asset.id}"
        await session.commit()
    created = await _create(quality_context)
    async with quality_context.database.sessions() as session:
        asset = await session.get(Asset, quality_context.asset_ids[0])
        assert asset is not None
        asset.object_version_id = "replacement-version"
        await session.commit()
    async with quality_context.database.sessions() as session:
        with pytest.raises(QualityConflictError, match="frozen inputs"):
            await create_scoring_run(
                session,
                release_version_id=quality_context.release_version_id,
            )
        assert (
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(AssetScore)
                    .where(AssetScore.scoring_run_id == created.run_id)
                )
                or 0
            )
            == 4
        )


@pytest.mark.asyncio
async def test_freeze_persists_deterministic_ranking_flags_and_duplicates(
    quality_context: QualityContext,
) -> None:
    created = await _create(quality_context)
    analyses = _analyses(quality_context)
    async with quality_context.database.sessions() as session:
        assets_before = {
            asset.id: (
                asset.state,
                asset.object_key,
                asset.object_version_id,
                asset.sha256,
            )
            for asset in (
                await session.scalars(select(Asset).where(Asset.id.in_(quality_context.asset_ids)))
            ).all()
        }
        frozen = await freeze_scoring_run(
            session,
            scoring_run_id=created.run_id,
            analyses=analyses,
            now=datetime(2026, 7, 28, 12, 2, tzinfo=UTC),
        )

    scores_by_asset = {
        asset_id: (analysis.score_micros if isinstance(analysis, QualityResult) else 0)
        for asset_id, analysis in analyses.items()
    }
    expected_order = sorted(
        quality_context.asset_ids,
        key=lambda asset_id: (-scores_by_asset[asset_id], str(asset_id)),
    )
    assert [row.asset_id for row in frozen.rankings] == expected_order
    assert [row.rank for row in frozen.rankings] == [1, 2, 3, 4]

    duplicate_rows = {
        row.asset_id: row for row in frozen.rankings if row.duplicate_cluster_id is not None
    }
    assert set(duplicate_rows) == set(quality_context.asset_ids[:2])
    representative_id = min(quality_context.asset_ids[:2], key=str)
    assert duplicate_rows[representative_id].is_duplicate_representative
    nonrepresentative_id = next(
        asset_id for asset_id in quality_context.asset_ids[:2] if asset_id != representative_id
    )
    assert duplicate_rows[nonrepresentative_id].disposition == RankingDisposition.NEAR_DUPLICATE

    by_asset = {row.asset_id: row for row in frozen.rankings}
    assert by_asset[quality_context.asset_ids[2]].disposition == (RankingDisposition.FLAGGED_REVIEW)
    assert by_asset[quality_context.asset_ids[3]].disposition == (RankingDisposition.FLAGGED_REVIEW)

    async with quality_context.database.sessions() as session:
        run = await session.get(ScoringRun, created.run_id)
        assert run is not None
        assert run.state == ScoringRunState.COMPLETED
        scores = {
            score.asset_id: score
            for score in (
                await session.scalars(
                    select(AssetScore).where(AssetScore.scoring_run_id == created.run_id)
                )
            ).all()
        }
        assert scores[quality_context.asset_ids[0]].state == AssetScoreState.SCORED
        assert scores[quality_context.asset_ids[2]].state == AssetScoreState.FLAGGED_BLANK
        assert scores[quality_context.asset_ids[3]].state == AssetScoreState.FLAGGED_CORRUPT
        assert scores[quality_context.asset_ids[0]].dhash_hex is not None
        assert len(scores[quality_context.asset_ids[0]].dhash_hex or "") == 16
        assets_after = {
            asset.id: (
                asset.state,
                asset.object_key,
                asset.object_version_id,
                asset.sha256,
            )
            for asset in (
                await session.scalars(select(Asset).where(Asset.id.in_(quality_context.asset_ids)))
            ).all()
        }
        assert assets_after == assets_before
        assert (
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(AssetRanking)
                    .where(AssetRanking.scoring_run_id == created.run_id)
                )
                or 0
            )
            == 4
        )

    async with quality_context.database.sessions() as session:
        replay = await freeze_scoring_run(
            session,
            scoring_run_id=created.run_id,
            analyses=analyses,
        )
        assert replay.replayed
        assert replay.rankings == frozen.rankings
    conflicting = dict(analyses)
    conflicting[quality_context.asset_ids[3]] = CorruptAssetSignal("decode_contract_failure")
    async with quality_context.database.sessions() as session:
        with pytest.raises(QualityConflictError, match="frozen signals"):
            await freeze_scoring_run(
                session,
                scoring_run_id=created.run_id,
                analyses=conflicting,
            )


@pytest.mark.asyncio
async def test_partial_freeze_fails_without_partial_writes(
    quality_context: QualityContext,
) -> None:
    created = await _create(quality_context)
    analyses = _analyses(quality_context)
    analyses.pop(quality_context.asset_ids[-1])

    async with quality_context.database.sessions() as session:
        with pytest.raises(QualityInputError, match="exact frozen asset set"):
            await freeze_scoring_run(
                session,
                scoring_run_id=created.run_id,
                analyses=analyses,
            )
        await session.rollback()
        run = await session.get(ScoringRun, created.run_id)
        assert run is not None
        assert run.state == ScoringRunState.RUNNING
        assert (
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(AssetRanking)
                    .where(AssetRanking.scoring_run_id == created.run_id)
                )
                or 0
            )
            == 0
        )
        states = set(
            (
                await session.scalars(
                    select(AssetScore.state).where(AssetScore.scoring_run_id == created.run_id)
                )
            ).all()
        )
        assert states == {AssetScoreState.PENDING}
