import asyncio
import copy
import hashlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image
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
    ReleasePhase,
    ScoringRunState,
)
from gen_automation.quality import QualityConfig, QualityResult, UnsafeImageError, analyze_image
from gen_automation.services.quality import QualityConflictError, create_scoring_run
from gen_automation.services.quality_isolation import (
    QualityIsolationCrashError,
    QualityIsolationMemoryError,
    QualityIsolationPolicy,
    QualityIsolationProtocolError,
    QualityIsolationTimeoutError,
)
from gen_automation.services.quality_runtime import (
    QualityLeaseLostError,
    QualityRuntimeContractError,
    claim_quality_score,
    ensure_next_scoring_run,
    finalize_ready_scoring_run,
    process_claimed_quality_score,
    run_quality_cycle,
    stage_quality_analysis,
)
from gen_automation.storage.memory import MemoryObjectStore, StoredObject

_NOW = datetime(2026, 7, 28, 20, 0, tzinfo=UTC)
_POLICY = QualityIsolationPolicy()


def _png(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (64, 64), color).save(output, format="PNG")
    return output.getvalue()


@dataclass(frozen=True)
class RuntimeContext:
    database: Database
    store: MemoryObjectStore
    release_id: UUID
    release_version_id: UUID
    asset_ids: tuple[UUID, ...]
    payloads: tuple[bytes, ...]


@pytest.fixture
async def runtime_context(tmp_path: Path) -> AsyncIterator[RuntimeContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'quality-runtime.db').as_posix()}")
    await database.create_schema()
    store = MemoryObjectStore(bucket="quality-runtime")
    payloads = (
        _png((10, 50, 90)),
        _png((180, 90, 20)),
    )
    async with database.sessions() as session:
        project = Project(slug="quality-runtime", name="Quality runtime")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="runtime-release",
            title="Runtime release",
            phase=ReleasePhase.REVIEWING,
            current_version_no=1,
            desired_accepted_count=1,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={"version": 1},
            specification_sha256="1" * 64,
            created_by="test",
            created_at=_NOW,
        )
        session.add(version)
        await session.flush()
        job = GenerationJob(
            release_version_id=version.id,
            logical_key="2" * 64,
            parameters={"version": 1},
            parameters_sha256="3" * 64,
            state=GenerationState.SUCCEEDED,
            expected_output_count=len(payloads),
        )
        session.add(job)
        await session.flush()
        asset_ids: list[UUID] = []
        for index, payload in enumerate(payloads):
            key = f"masters/runtime-{index}.png"
            store.put_for_test(key, payload)
            stored = store.objects[key]
            asset = Asset(
                release_id=release.id,
                generation_job_id=job.id,
                output_index=index,
                kind=AssetKind.RAW_MASTER,
                state=AssetState.AVAILABLE,
                storage_backend=store.backend,
                storage_bucket=store.bucket,
                object_key=key,
                object_version_id=stored.version_id,
                sha256=hashlib.sha256(payload).hexdigest(),
                content_type="image/png",
                image_format="PNG",
                width=64,
                height=64,
                byte_size=len(payload),
                asset_metadata={},
                available_at=_NOW,
            )
            session.add(asset)
            await session.flush()
            asset_ids.append(asset.id)
        await session.commit()
        context = RuntimeContext(
            database=database,
            store=store,
            release_id=release.id,
            release_version_id=version.id,
            asset_ids=tuple(asset_ids),
            payloads=payloads,
        )
    try:
        yield context
    finally:
        await database.dispose()


async def _direct_analyzer(
    payload: bytes,
    config: QualityConfig,
    _policy: QualityIsolationPolicy,
) -> QualityResult:
    return analyze_image(payload, config=config)


async def _create_and_claim(
    context: RuntimeContext,
    *,
    worker_id: str = "quality:test",
    max_attempts: int = 3,
    now: datetime = _NOW,
):
    async with context.database.sessions() as session:
        await create_scoring_run(
            session,
            release_version_id=context.release_version_id,
            max_attempts=max_attempts,
            now=now,
        )
    async with context.database.sessions() as session:
        claim = await claim_quality_score(
            session,
            worker_id=worker_id,
            lease_seconds=120,
            now=now + timedelta(seconds=1),
        )
    assert claim is not None
    return claim


@pytest.mark.asyncio
async def test_cycle_stages_each_result_then_atomically_freezes_run(
    runtime_context: RuntimeContext,
) -> None:
    first = await run_quality_cycle(
        runtime_context.database.sessions,
        runtime_context.store,
        worker_id="quality:one",
        config=QualityConfig(),
        max_attempts=3,
        lease_seconds=120,
        retry_base_seconds=5,
        retry_max_seconds=30,
        policy=_POLICY,
        analyzer=_direct_analyzer,
    )
    assert first.created_run

    second = await run_quality_cycle(
        runtime_context.database.sessions,
        runtime_context.store,
        worker_id="quality:one",
        config=QualityConfig(),
        max_attempts=3,
        lease_seconds=120,
        retry_base_seconds=5,
        retry_max_seconds=30,
        policy=_POLICY,
        analyzer=_direct_analyzer,
    )
    assert second.processed_score
    assert not second.finalized_run
    async with runtime_context.database.sessions() as session:
        run = await session.scalar(select(ScoringRun))
        rankings = await session.scalar(select(func.count()).select_from(AssetRanking))
        staged = await session.scalar(
            select(func.count())
            .select_from(AssetScore)
            .where(
                AssetScore.state == AssetScoreState.PENDING,
                AssetScore.completed_at.is_not(None),
            )
        )
        assert run is not None and run.state == ScoringRunState.RUNNING
        assert rankings == 0
        assert staged == 1

    third = await run_quality_cycle(
        runtime_context.database.sessions,
        runtime_context.store,
        worker_id="quality:one",
        config=QualityConfig(),
        max_attempts=3,
        lease_seconds=120,
        retry_base_seconds=5,
        retry_max_seconds=30,
        policy=_POLICY,
        analyzer=_direct_analyzer,
    )
    assert third.processed_score
    assert third.finalized_run
    async with runtime_context.database.sessions() as session:
        run = await session.scalar(select(ScoringRun))
        rankings = await session.scalar(select(func.count()).select_from(AssetRanking))
        assert run is not None and run.state == ScoringRunState.COMPLETED
        assert rankings == 2


@pytest.mark.asyncio
async def test_completed_replay_is_noop_and_does_not_duplicate_run(
    runtime_context: RuntimeContext,
) -> None:
    for _ in range(3):
        await run_quality_cycle(
            runtime_context.database.sessions,
            runtime_context.store,
            worker_id="quality:replay",
            config=QualityConfig(),
            max_attempts=3,
            lease_seconds=120,
            retry_base_seconds=5,
            retry_max_seconds=30,
            policy=_POLICY,
            analyzer=_direct_analyzer,
        )
    replay = await run_quality_cycle(
        runtime_context.database.sessions,
        runtime_context.store,
        worker_id="quality:replay",
        config=QualityConfig(),
        max_attempts=3,
        lease_seconds=120,
        retry_base_seconds=5,
        retry_max_seconds=30,
        policy=_POLICY,
        analyzer=_direct_analyzer,
    )
    assert not replay.did_work
    async with runtime_context.database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ScoringRun)) == 1
        assert await session.scalar(select(func.count()).select_from(AssetRanking)) == 2


@pytest.mark.asyncio
async def test_exact_version_and_digest_are_checked_before_parser(
    runtime_context: RuntimeContext,
) -> None:
    claim = await _create_and_claim(runtime_context, max_attempts=1)
    runtime_context.store.put_for_test(claim.object_key, runtime_context.payloads[0])
    analyzer_called = False

    async def analyzer(
        _payload: bytes,
        _config: QualityConfig,
        _policy: QualityIsolationPolicy,
    ) -> QualityResult:
        nonlocal analyzer_called
        analyzer_called = True
        raise AssertionError("parser must not run")

    await process_claimed_quality_score(
        runtime_context.database.sessions,
        runtime_context.store,
        claim=claim,
        worker_id="quality:test",
        policy=_POLICY,
        retry_base_seconds=5,
        retry_max_seconds=30,
        analyzer=analyzer,
        now=_NOW + timedelta(seconds=2),
    )
    assert analyzer_called is False
    async with runtime_context.database.sessions() as session:
        score = await session.get(AssetScore, claim.score_id)
        assert score is not None
        assert score.state == AssetScoreState.PENDING
        assert score.completed_at is not None
        assert score.last_error_code == "asset_version_unavailable"
        assert claim.object_key not in (score.last_error_detail or "")

    claim_two = await _claim_other_score(runtime_context, worker_id="quality:digest")
    stored = runtime_context.store.objects[claim_two.object_key]
    mutated = b"x" * claim_two.byte_size
    runtime_context.store.objects[claim_two.object_key] = StoredObject(
        body=mutated,
        content_type=stored.content_type,
        metadata=stored.metadata,
        version_id=stored.version_id,
    )
    await process_claimed_quality_score(
        runtime_context.database.sessions,
        runtime_context.store,
        claim=claim_two,
        worker_id="quality:digest",
        policy=_POLICY,
        retry_base_seconds=5,
        retry_max_seconds=30,
        analyzer=analyzer,
        now=_NOW + timedelta(seconds=3),
    )
    async with runtime_context.database.sessions() as session:
        score = await session.get(AssetScore, claim_two.score_id)
        assert score is not None and score.last_error_code == "asset_sha256_mismatch"


async def _claim_other_score(context: RuntimeContext, *, worker_id: str):
    async with context.database.sessions() as session:
        claim = await claim_quality_score(
            session,
            worker_id=worker_id,
            lease_seconds=120,
            now=_NOW + timedelta(seconds=2),
        )
    assert claim is not None
    return claim


@pytest.mark.parametrize(
    ("factory", "expected_code"),
    [
        (
            lambda: QualityIsolationTimeoutError("secret timeout detail"),
            "analysis_timeout",
        ),
        (
            lambda: QualityIsolationMemoryError("secret memory detail"),
            "analysis_memory_limit",
        ),
        (
            lambda: QualityIsolationCrashError("secret crash detail"),
            "analysis_crash",
        ),
        (
            lambda: QualityIsolationProtocolError("secret protocol detail"),
            "analysis_protocol_error",
        ),
        (
            lambda: RuntimeError("secret unexpected detail"),
            "quality_processing_failed",
        ),
    ],
)
@pytest.mark.asyncio
async def test_isolation_failures_are_sanitized_and_terminal_at_attempt_limit(
    runtime_context: RuntimeContext,
    factory: Callable[[], Exception],
    expected_code: str,
) -> None:
    claim = await _create_and_claim(runtime_context, max_attempts=1)

    async def analyzer(
        _payload: bytes,
        _config: QualityConfig,
        _policy: QualityIsolationPolicy,
    ) -> QualityResult:
        raise factory()

    await process_claimed_quality_score(
        runtime_context.database.sessions,
        runtime_context.store,
        claim=claim,
        worker_id="quality:test",
        policy=_POLICY,
        retry_base_seconds=5,
        retry_max_seconds=30,
        analyzer=analyzer,
        now=_NOW + timedelta(seconds=2),
    )
    async with runtime_context.database.sessions() as session:
        score = await session.get(AssetScore, claim.score_id)
        assert score is not None
        assert score.state == AssetScoreState.PENDING
        assert score.completed_at is not None
        assert score.last_error_code == expected_code
        assert "secret" not in (score.last_error_detail or "")
        assert score.signal_detail["staged_analysis"] == {
            "kind": "corrupt",
            "error_code": expected_code,
        }


@pytest.mark.asyncio
async def test_corrupt_parser_outcome_is_staged_without_retry(
    runtime_context: RuntimeContext,
) -> None:
    claim = await _create_and_claim(runtime_context, max_attempts=3)

    async def corrupt(
        _payload: bytes,
        _config: QualityConfig,
        _policy: QualityIsolationPolicy,
    ) -> QualityResult:
        raise UnsafeImageError("untrusted parser details")

    await process_claimed_quality_score(
        runtime_context.database.sessions,
        runtime_context.store,
        claim=claim,
        worker_id="quality:test",
        policy=_POLICY,
        retry_base_seconds=5,
        retry_max_seconds=30,
        analyzer=corrupt,
        now=_NOW + timedelta(seconds=2),
    )
    async with runtime_context.database.sessions() as session:
        score = await session.get(AssetScore, claim.score_id)
        assert score is not None
        assert score.attempts == 1
        assert score.last_error_code == "corrupt_image"
        assert score.completed_at is not None


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_after_restart(
    runtime_context: RuntimeContext,
) -> None:
    first = await _create_and_claim(runtime_context, now=_NOW)
    async with runtime_context.database.sessions() as session:
        second = await claim_quality_score(
            session,
            worker_id="quality:restart",
            lease_seconds=120,
            now=first.lease_expires_at + timedelta(seconds=1),
        )
    assert second is not None
    assert second.score_id == first.score_id
    assert second.attempt == 2


@pytest.mark.asyncio
async def test_final_expired_lease_is_staged_fail_closed(
    runtime_context: RuntimeContext,
) -> None:
    first = await _create_and_claim(runtime_context, max_attempts=1, now=_NOW)

    async with runtime_context.database.sessions() as session:
        recovered = await claim_quality_score(
            session,
            worker_id="quality:restart",
            lease_seconds=120,
            now=first.lease_expires_at + timedelta(seconds=1),
        )
    assert recovered is None

    async with runtime_context.database.sessions() as session:
        score = await session.get(AssetScore, first.score_id)
        rankings = await session.scalar(select(func.count()).select_from(AssetRanking))
        assert score is not None
        assert score.state == AssetScoreState.PENDING
        assert score.completed_at is not None
        assert score.completed_at.replace(tzinfo=UTC) == (
            first.lease_expires_at + timedelta(seconds=1)
        )
        assert score.last_error_code == "analysis_lease_expired"
        assert score.signal_detail["staged_analysis"] == {
            "kind": "corrupt",
            "error_code": "analysis_lease_expired",
        }
        assert rankings == 0


@pytest.mark.asyncio
async def test_tampered_staged_metrics_cannot_freeze_partial_rankings(
    runtime_context: RuntimeContext,
) -> None:
    first = await _create_and_claim(runtime_context, now=_NOW)
    first_payload = runtime_context.payloads[runtime_context.asset_ids.index(first.asset_id)]
    async with runtime_context.database.sessions() as session:
        await stage_quality_analysis(
            session,
            claim=first,
            worker_id="quality:test",
            analysis=analyze_image(first_payload),
            now=_NOW + timedelta(seconds=2),
        )

    second = await _claim_other_score(runtime_context, worker_id="quality:test")
    second_payload = runtime_context.payloads[runtime_context.asset_ids.index(second.asset_id)]
    async with runtime_context.database.sessions() as session:
        await stage_quality_analysis(
            session,
            claim=second,
            worker_id="quality:test",
            analysis=analyze_image(second_payload),
            now=_NOW + timedelta(seconds=3),
        )

    async with runtime_context.database.sessions() as session:
        score = await session.get(AssetScore, first.score_id)
        assert score is not None
        tampered = copy.deepcopy(score.signal_detail)
        tampered["staged_analysis"]["result"]["metrics"]["sharpness_micros"] += 1
        score.signal_detail = tampered
        await session.commit()

    async with runtime_context.database.sessions() as session:
        with pytest.raises(QualityRuntimeContractError):
            await finalize_ready_scoring_run(
                session,
                now=_NOW + timedelta(seconds=4),
            )

    async with runtime_context.database.sessions() as session:
        run = await session.scalar(select(ScoringRun))
        rankings = await session.scalar(select(func.count()).select_from(AssetRanking))
        assert run is not None
        assert run.state == ScoringRunState.RUNNING
        assert rankings == 0


@pytest.mark.asyncio
async def test_lost_lease_cannot_stage_result(runtime_context: RuntimeContext) -> None:
    claim = await _create_and_claim(runtime_context)
    with pytest.raises(QualityLeaseLostError, match="not active"):
        async with runtime_context.database.sessions() as session:
            await stage_quality_analysis(
                session,
                claim=claim,
                worker_id="quality:test",
                analysis=analyze_image(runtime_context.payloads[0]),
                now=claim.lease_expires_at + timedelta(seconds=1),
            )


@pytest.mark.asyncio
async def test_two_controllers_do_not_claim_the_same_score(
    runtime_context: RuntimeContext,
) -> None:
    async with runtime_context.database.sessions() as session:
        await create_scoring_run(
            session,
            release_version_id=runtime_context.release_version_id,
            now=_NOW,
        )

    async def claim(worker_id: str):
        async with runtime_context.database.sessions() as session:
            return await claim_quality_score(
                session,
                worker_id=worker_id,
                lease_seconds=120,
                now=_NOW + timedelta(seconds=1),
            )

    first, second = await asyncio.gather(
        claim("quality:race-a"),
        claim("quality:race-b"),
    )
    claimed = [item for item in (first, second) if item is not None]
    assert claimed
    assert len({item.score_id for item in claimed}) == len(claimed)


@pytest.mark.asyncio
async def test_only_current_reviewing_version_can_be_scored(
    runtime_context: RuntimeContext,
) -> None:
    async with runtime_context.database.sessions() as session:
        release = await session.get(Release, runtime_context.release_id)
        assert release is not None
        release.phase = ReleasePhase.APPROVED
        await session.commit()
    async with runtime_context.database.sessions() as session:
        with pytest.raises(QualityConflictError, match="current release version in review"):
            await create_scoring_run(
                session,
                release_version_id=runtime_context.release_version_id,
                now=_NOW,
            )
    async with runtime_context.database.sessions() as session:
        assert not await ensure_next_scoring_run(session, now=_NOW)
    async with runtime_context.database.sessions() as session:
        release = await session.get(Release, runtime_context.release_id)
        assert release is not None
        release.phase = ReleasePhase.REVIEWING
        release.current_version_no = 2
        await session.commit()
    async with runtime_context.database.sessions() as session:
        with pytest.raises(QualityConflictError, match="current release version in review"):
            await create_scoring_run(
                session,
                release_version_id=runtime_context.release_version_id,
                now=_NOW,
            )


@pytest.mark.asyncio
async def test_finalizer_refuses_partial_staging(runtime_context: RuntimeContext) -> None:
    claim = await _create_and_claim(runtime_context)
    await process_claimed_quality_score(
        runtime_context.database.sessions,
        runtime_context.store,
        claim=claim,
        worker_id="quality:test",
        policy=_POLICY,
        retry_base_seconds=5,
        retry_max_seconds=30,
        analyzer=_direct_analyzer,
        now=_NOW + timedelta(seconds=2),
    )
    async with runtime_context.database.sessions() as session:
        assert await finalize_ready_scoring_run(session, now=_NOW) is None
    async with runtime_context.database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AssetRanking)) == 0
