import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from gen_automation.app import create_app
from gen_automation.db.models import (
    Asset,
    AssetRanking,
    AssetScore,
    GenerationJob,
    Project,
    Release,
    ReleaseVersion,
    ScoringRun,
    SemanticAssessment,
)
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    AssetKind,
    AssetScoreState,
    AssetState,
    GenerationState,
    RankingDisposition,
    ReleasePhase,
    ReviewDecisionValue,
    ScoringRunState,
    SemanticAssessmentState,
    SemanticEnforcementMode,
    SemanticIssueCode,
    SemanticVerdict,
)
from gen_automation.integrations.semantic_vlm import (
    SemanticVlmClient,
    SemanticVlmProtocolError,
    SemanticVlmUnavailableError,
)
from gen_automation.semantic import (
    SEMANTIC_SCHEMA_VERSION,
    SemanticAssessmentResult,
    SemanticIssue,
    assessment_profile_sha256,
    prompt_sha256,
    schema_sha256,
)
from gen_automation.services.review import (
    SEMANTIC_SEVERE_OVERRIDE_REASON_CODE,
    append_review_decision,
)
from gen_automation.services.semantic_anatomy import (
    SemanticAssessmentProfile,
    run_semantic_assessment_cycle,
)
from gen_automation.storage.memory import MemoryObjectStore
from tests.test_dashboard_review import (
    SameOriginReviewStore,
    _create_task,
)
from tests.test_review_api import (
    ORIGIN,
    _login,
    _seed_review_api,
    _settings,
)

_NOW = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
_MODEL = "Qwen/Qwen3-VL-8B-Instruct"
_REVISION = "0123456789abcdef"


def _response_from_request(
    request: httpx2.Request,
    *,
    issue_code: str = "extra_finger",
) -> httpx2.Response:
    body = json.loads(request.content)
    return httpx2.Response(
        200,
        headers={"content-type": "application/json"},
        json={
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "request_id": body["request_id"],
            "model": body["model"],
            "model_revision": body["model_revision"],
            "asset_sha256": body["image"]["sha256"],
            "assessment": {
                "verdict": "severe",
                "confidence": 0.96,
                "issues": [
                    {
                        "code": issue_code,
                        "confidence": 0.98,
                        "box": {
                            "x_min": 0.1,
                            "y_min": 0.2,
                            "x_max": 0.4,
                            "y_max": 0.7,
                        },
                    }
                ],
            },
        },
        request=request,
    )


@pytest.mark.asyncio
async def test_semantic_vlm_contract_is_strict_and_identity_bound() -> None:
    payload = b"bounded-image-payload"
    digest = hashlib.sha256(payload).hexdigest()
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _response_from_request(request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = SemanticVlmClient(
            http_client=http_client,
            endpoint_url="https://semantic.internal/v1/anatomy/assess",
            model=_MODEL,
            model_revision=_REVISION,
            timeout_seconds=30,
        )
        result = await client.assess(
            payload,
            content_type="image/png",
            asset_sha256=digest,
        )

    assert result.verdict == SemanticVerdict.SEVERE
    assert result.confidence_micros == 960_000
    assert result.issues[0].code == SemanticIssueCode.EXTRA_FINGER
    assert result.issues[0].box is not None
    request_body = json.loads(requests[0].content)
    assert request_body["task"]["prompt_sha256"] == prompt_sha256()
    assert request_body["task"]["schema_sha256"] == schema_sha256()
    assert requests[0].headers["idempotency-key"] == request_body["request_id"]

    def malformed_handler(request: httpx2.Request) -> httpx2.Response:
        return _response_from_request(request, issue_code="unbounded_provider_label")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(malformed_handler)) as http_client:
        client = SemanticVlmClient(
            http_client=http_client,
            endpoint_url="https://semantic.internal/v1/anatomy/assess",
            model=_MODEL,
            model_revision=_REVISION,
            timeout_seconds=30,
        )
        with pytest.raises(SemanticVlmProtocolError):
            await client.assess(
                payload,
                content_type="image/png",
                asset_sha256=digest,
            )


@dataclass(frozen=True, slots=True)
class SemanticRuntimeContext:
    database: Database
    store: MemoryObjectStore
    asset_id: UUID


@pytest.fixture
async def semantic_runtime_context(tmp_path: Path) -> AsyncIterator[SemanticRuntimeContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'semantic.db').as_posix()}")
    await database.create_schema()
    store = MemoryObjectStore(bucket="semantic-runtime")
    payload = b"exact-semantic-master"
    object_key = "masters/exact.png"
    store.put_for_test(object_key, payload)
    stored = store.objects[object_key]
    async with database.sessions() as session:
        project = Project(slug="semantic", name="Semantic")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="semantic-release",
            title="Semantic release",
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
            expected_output_count=1,
        )
        session.add(job)
        await session.flush()
        asset = Asset(
            release_id=release.id,
            generation_job_id=job.id,
            output_index=0,
            kind=AssetKind.RAW_MASTER,
            state=AssetState.AVAILABLE,
            storage_backend=store.backend,
            storage_bucket=store.bucket,
            object_key=object_key,
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
        run = ScoringRun(
            release_version_id=version.id,
            configuration={"quality": "fixture"},
            config_sha256="4" * 64,
            input_manifest_sha256="5" * 64,
            ranking_manifest_sha256=None,
            scorer_version="fixture",
            pillow_version="12.0.0",
            state=ScoringRunState.RUNNING,
            asset_count=1,
            max_attempts=3,
            created_at=_NOW,
            started_at=_NOW,
            completed_at=None,
        )
        session.add(run)
        await session.flush()
        score = AssetScore(
            scoring_run_id=run.id,
            asset_id=asset.id,
            asset_storage_backend=asset.storage_backend,
            asset_storage_bucket=asset.storage_bucket,
            asset_sha256=asset.sha256,
            asset_object_key=asset.object_key,
            asset_object_version_id=asset.object_version_id,
            asset_byte_size=asset.byte_size,
            asset_image_format=asset.image_format,
            asset_width=asset.width,
            asset_height=asset.height,
            state=AssetScoreState.FLAGGED_CORRUPT,
            attempts=1,
            max_attempts=3,
            available_at=_NOW,
            aggregate_score_micros=500_000,
            signal_detail={"classification": "fixture"},
            scorer_version=run.scorer_version,
            pillow_version=run.pillow_version,
            config_sha256=run.config_sha256,
            completed_at=_NOW + timedelta(seconds=1),
            created_at=_NOW,
        )
        session.add(score)
        await session.flush()
        session.add(
            AssetRanking(
                scoring_run_id=run.id,
                asset_score_id=score.id,
                asset_id=asset.id,
                rank=1,
                aggregate_score_micros=500_000,
                disposition=RankingDisposition.FLAGGED_REVIEW,
                explanation={"fixture": True},
                is_duplicate_representative=False,
                scorer_version=run.scorer_version,
                pillow_version=run.pillow_version,
                config_sha256=run.config_sha256,
                frozen_at=_NOW + timedelta(seconds=1),
            )
        )
        await session.flush()
        run.ranking_manifest_sha256 = "6" * 64
        run.state = ScoringRunState.COMPLETED
        run.completed_at = _NOW + timedelta(seconds=1)
        await session.commit()
        context = SemanticRuntimeContext(database=database, store=store, asset_id=asset.id)
    try:
        yield context
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_unavailable_semantic_service_retries_then_keeps_master_for_review(
    semantic_runtime_context: SemanticRuntimeContext,
) -> None:
    calls = 0

    async def unavailable(
        _payload: bytes,
        _content_type: str,
        _sha256: str,
    ) -> SemanticAssessmentResult:
        nonlocal calls
        calls += 1
        raise SemanticVlmUnavailableError("scale-to-zero service unavailable")

    profile = SemanticAssessmentProfile(model_name=_MODEL, model_revision=_REVISION)
    first = await run_semantic_assessment_cycle(
        semantic_runtime_context.database.sessions,
        semantic_runtime_context.store,
        worker_id="semantic:test",
        profile=profile,
        analyzer=unavailable,
        max_assessments_per_profile=1,
        asset_allowlist=(),
        max_attempts=2,
        lease_seconds=120,
        retry_base_seconds=5,
        retry_max_seconds=30,
        now=_NOW + timedelta(minutes=1),
    )
    assert first.created_assessment
    assert first.processed_assessment
    async with semantic_runtime_context.database.sessions() as session:
        assessment = await session.scalar(select(SemanticAssessment))
        assert assessment is not None
        assert assessment.state == SemanticAssessmentState.RETRY_WAIT
        assert assessment.verdict is None
        assert assessment.issues is None

    second = await run_semantic_assessment_cycle(
        semantic_runtime_context.database.sessions,
        semantic_runtime_context.store,
        worker_id="semantic:test",
        profile=profile,
        analyzer=unavailable,
        max_assessments_per_profile=0,
        asset_allowlist=(),
        max_attempts=2,
        lease_seconds=120,
        retry_base_seconds=5,
        retry_max_seconds=30,
        now=_NOW + timedelta(minutes=1, seconds=6),
    )
    assert second.processed_assessment
    assert calls == 2
    async with semantic_runtime_context.database.sessions() as session:
        assessment = await session.scalar(select(SemanticAssessment))
        asset = await session.get(Asset, semantic_runtime_context.asset_id)
        assert assessment is not None
        assert assessment.state == SemanticAssessmentState.UNAVAILABLE
        assert assessment.verdict is None
        assert assessment.issues is None
        assert assessment.last_error_code == "semantic_service_unavailable"
        assert asset is not None
        assert asset.state == AssetState.AVAILABLE
        assert semantic_runtime_context.store.objects


async def _add_ranked_asset(
    context: SemanticRuntimeContext,
    *,
    output_index: int,
    rank: int,
) -> UUID:
    payload = f"exact-semantic-master-{output_index}".encode()
    object_key = f"masters/exact-{output_index}.png"
    context.store.put_for_test(object_key, payload)
    stored = context.store.objects[object_key]
    async with context.database.sessions() as session:
        original = await session.get(Asset, context.asset_id)
        assert original is not None
        assert original.generation_job_id is not None
        job = await session.get(GenerationJob, original.generation_job_id)
        assert job is not None
        asset = Asset(
            release_id=original.release_id,
            generation_job_id=job.id,
            output_index=output_index,
            kind=AssetKind.RAW_MASTER,
            state=AssetState.AVAILABLE,
            storage_backend=context.store.backend,
            storage_bucket=context.store.bucket,
            object_key=object_key,
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
        run = ScoringRun(
            release_version_id=job.release_version_id,
            configuration={"quality": f"fixture-{output_index}"},
            config_sha256="7" * 64,
            input_manifest_sha256="8" * 64,
            ranking_manifest_sha256=None,
            scorer_version="fixture",
            pillow_version="12.0.0",
            state=ScoringRunState.RUNNING,
            asset_count=1,
            max_attempts=3,
            created_at=_NOW + timedelta(seconds=1),
            started_at=_NOW + timedelta(seconds=1),
            completed_at=None,
        )
        session.add(run)
        await session.flush()
        score = AssetScore(
            scoring_run_id=run.id,
            asset_id=asset.id,
            asset_storage_backend=asset.storage_backend,
            asset_storage_bucket=asset.storage_bucket,
            asset_sha256=asset.sha256,
            asset_object_key=asset.object_key,
            asset_object_version_id=asset.object_version_id,
            asset_byte_size=asset.byte_size,
            asset_image_format=asset.image_format,
            asset_width=asset.width,
            asset_height=asset.height,
            state=AssetScoreState.FLAGGED_CORRUPT,
            attempts=1,
            max_attempts=3,
            available_at=_NOW,
            aggregate_score_micros=400_000,
            signal_detail={"classification": "fixture"},
            scorer_version=run.scorer_version,
            pillow_version=run.pillow_version,
            config_sha256=run.config_sha256,
            completed_at=_NOW + timedelta(seconds=1),
            created_at=_NOW,
        )
        session.add(score)
        await session.flush()
        session.add(
            AssetRanking(
                scoring_run_id=run.id,
                asset_score_id=score.id,
                asset_id=asset.id,
                rank=rank,
                aggregate_score_micros=400_000,
                disposition=RankingDisposition.FLAGGED_REVIEW,
                explanation={"fixture": True},
                is_duplicate_representative=False,
                scorer_version=run.scorer_version,
                pillow_version=run.pillow_version,
                config_sha256=run.config_sha256,
                frozen_at=_NOW + timedelta(seconds=1),
            )
        )
        await session.flush()
        run.ranking_manifest_sha256 = "9" * 64
        run.state = ScoringRunState.COMPLETED
        run.completed_at = _NOW + timedelta(seconds=2)
        await session.commit()
        return asset.id


async def _passing_assessment(
    _payload: bytes,
    _content_type: str,
    _sha256: str,
) -> SemanticAssessmentResult:
    return SemanticAssessmentResult(
        verdict=SemanticVerdict.PASS,
        confidence_micros=990_000,
        issues=(),
    )


@pytest.mark.asyncio
async def test_zero_assessment_limit_schedules_no_new_rows(
    semantic_runtime_context: SemanticRuntimeContext,
) -> None:
    result = await run_semantic_assessment_cycle(
        semantic_runtime_context.database.sessions,
        semantic_runtime_context.store,
        worker_id="semantic:zero-limit",
        profile=SemanticAssessmentProfile(model_name=_MODEL, model_revision=_REVISION),
        analyzer=_passing_assessment,
        max_assessments_per_profile=0,
        asset_allowlist=(),
        max_attempts=2,
        lease_seconds=120,
        retry_base_seconds=5,
        retry_max_seconds=30,
        now=_NOW + timedelta(minutes=1),
    )

    assert not result.did_work
    async with semantic_runtime_context.database.sessions() as session:
        assert list((await session.scalars(select(SemanticAssessment))).all()) == []


@pytest.mark.asyncio
async def test_profile_assessment_limit_counts_all_existing_rows(
    semantic_runtime_context: SemanticRuntimeContext,
) -> None:
    await _add_ranked_asset(semantic_runtime_context, output_index=1, rank=1)
    profile = SemanticAssessmentProfile(model_name=_MODEL, model_revision=_REVISION)
    first = await run_semantic_assessment_cycle(
        semantic_runtime_context.database.sessions,
        semantic_runtime_context.store,
        worker_id="semantic:hard-limit",
        profile=profile,
        analyzer=_passing_assessment,
        max_assessments_per_profile=1,
        asset_allowlist=(),
        max_attempts=2,
        lease_seconds=120,
        retry_base_seconds=5,
        retry_max_seconds=30,
        now=_NOW + timedelta(minutes=1),
    )
    second = await run_semantic_assessment_cycle(
        semantic_runtime_context.database.sessions,
        semantic_runtime_context.store,
        worker_id="semantic:hard-limit",
        profile=profile,
        analyzer=_passing_assessment,
        max_assessments_per_profile=1,
        asset_allowlist=(),
        max_attempts=2,
        lease_seconds=120,
        retry_base_seconds=5,
        retry_max_seconds=30,
        now=_NOW + timedelta(minutes=2),
    )

    assert first.created_assessment
    assert first.processed_assessment
    assert not second.did_work
    async with semantic_runtime_context.database.sessions() as session:
        assessments = list((await session.scalars(select(SemanticAssessment))).all())
        assert len(assessments) == 1
        assert assessments[0].state == SemanticAssessmentState.COMPLETED


@pytest.mark.asyncio
async def test_asset_allowlist_restricts_new_assessment_selection(
    semantic_runtime_context: SemanticRuntimeContext,
) -> None:
    allowed_asset_id = await _add_ranked_asset(
        semantic_runtime_context,
        output_index=1,
        rank=1,
    )
    result = await run_semantic_assessment_cycle(
        semantic_runtime_context.database.sessions,
        semantic_runtime_context.store,
        worker_id="semantic:allowlist",
        profile=SemanticAssessmentProfile(model_name=_MODEL, model_revision=_REVISION),
        analyzer=_passing_assessment,
        max_assessments_per_profile=1,
        asset_allowlist=(allowed_asset_id,),
        max_attempts=2,
        lease_seconds=120,
        retry_base_seconds=5,
        retry_max_seconds=30,
        now=_NOW + timedelta(minutes=1),
    )

    assert result.created_assessment
    assert result.processed_assessment
    async with semantic_runtime_context.database.sessions() as session:
        assessment = await session.scalar(select(SemanticAssessment))
        assert assessment is not None
        assert assessment.asset_id == allowed_asset_id


async def _seed_dashboard_assessments_and_override(
    database_url: str,
    *,
    scoring_run_id: UUID,
    asset_ids: tuple[UUID, UUID],
    task_id: UUID,
    reviewer_id: UUID,
) -> None:
    database = Database(database_url)
    try:
        async with database.sessions() as session:
            scores = {
                score.asset_id: score
                for score in (
                    await session.scalars(
                        select(AssetScore).where(AssetScore.scoring_run_id == scoring_run_id)
                    )
                ).all()
            }
            assets = {
                asset.id: asset
                for asset in (
                    await session.scalars(select(Asset).where(Asset.id.in_(asset_ids)))
                ).all()
            }
            profile = assessment_profile_sha256(
                model=_MODEL,
                model_revision=_REVISION,
            )
            results = (
                SemanticAssessmentResult(
                    verdict=SemanticVerdict.SEVERE,
                    confidence_micros=960_000,
                    issues=(
                        SemanticIssue(
                            code=SemanticIssueCode.EXTRA_LIMB,
                            confidence_micros=980_000,
                        ),
                    ),
                ),
                SemanticAssessmentResult(
                    verdict=SemanticVerdict.PASS,
                    confidence_micros=990_000,
                    issues=(),
                ),
            )
            for index, asset_id in enumerate(asset_ids):
                score = scores[asset_id]
                asset = assets[asset_id]
                wire = results[index].to_wire()
                session.add(
                    SemanticAssessment(
                        scoring_run_id=scoring_run_id,
                        asset_score_id=score.id,
                        asset_id=asset_id,
                        asset_storage_backend=score.asset_storage_backend,
                        asset_storage_bucket=score.asset_storage_bucket,
                        asset_object_key=score.asset_object_key,
                        asset_object_version_id=score.asset_object_version_id,
                        asset_sha256=score.asset_sha256,
                        asset_content_type=asset.content_type,
                        asset_byte_size=score.asset_byte_size,
                        profile_sha256=profile,
                        model_name=_MODEL,
                        model_revision=_REVISION,
                        prompt_sha256=prompt_sha256(),
                        schema_sha256=schema_sha256(),
                        state=SemanticAssessmentState.COMPLETED,
                        attempts=1,
                        max_attempts=3,
                        available_at=_NOW,
                        verdict=results[index].verdict,
                        confidence_micros=results[index].confidence_micros,
                        issues=wire["issues"],
                        response_sha256=canonical_sha256(wire),
                        created_at=_NOW,
                        started_at=_NOW,
                        completed_at=_NOW + timedelta(seconds=1),
                    )
                )
            await session.commit()
        async with database.sessions() as session:
            await append_review_decision(
                session,
                review_task_id=task_id,
                asset_id=asset_ids[0],
                decision=ReviewDecisionValue.ACCEPT,
                decided_by_user_id=reviewer_id,
                expected_lock_version=1,
                idempotency_key="semantic-manual-override",
                reason_code="manual_semantic_override",
                now=_NOW + timedelta(minutes=2),
            )
    finally:
        await database.dispose()


def test_high_confidence_severe_bucket_is_last_visible_and_overridable(
    tmp_path: Path,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "semantic-dashboard.db")))
    enforced_settings = context.settings.model_copy(
        update={
            "semantic_anatomy_enabled": True,
            "semantic_anatomy_mode": SemanticEnforcementMode.ENFORCE,
            "semantic_anatomy_endpoint_url": "http://semantic.internal/v1/anatomy/assess",
            "semantic_anatomy_model": _MODEL,
            "semantic_anatomy_model_revision": _REVISION,
        }
    )
    task_id = asyncio.run(_create_task(context))
    asyncio.run(
        _seed_dashboard_assessments_and_override(
            context.settings.database_url,
            scoring_run_id=context.scoring_run_id,
            asset_ids=context.asset_ids,
            task_id=task_id,
            reviewer_id=context.users[AdminRole.REVIEWER].id,
        )
    )
    app = create_app(enforced_settings)
    action = f"/dashboard/review-tasks/{task_id}"
    with TestClient(
        app,
        base_url=ORIGIN,
        client=("192.0.2.99", 50000),
    ) as client:
        app.state.object_store = SameOriginReviewStore()
        _login(client, enforced_settings, context.users[AdminRole.REVIEWER])
        page = client.get(action)

    assert page.status_code == 200
    normal_position = page.text.index(str(context.asset_ids[1]))
    bucket_position = page.text.index("AI excluded</h2>")
    severe_position = page.text.index(str(context.asset_ids[0]))
    assert normal_position < bucket_position < severe_position
    assert "Manual decision recorded: accept." in page.text
    assert "they are not deleted" in page.text.lower()
    severe_card = page.text[severe_position:]
    assert 'name="asset_id"' in severe_card
    assert str(context.asset_ids[0]) in severe_card


def test_configured_semantic_gate_blocks_api_and_guides_owner_override(
    tmp_path: Path,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "semantic-gate.db")))
    task_id = asyncio.run(_create_task(context))
    asyncio.run(
        _seed_dashboard_assessments_and_override(
            context.settings.database_url,
            scoring_run_id=context.scoring_run_id,
            asset_ids=context.asset_ids,
            task_id=task_id,
            reviewer_id=context.users[AdminRole.REVIEWER].id,
        )
    )
    enabled_settings = context.settings.model_copy(
        update={
            "semantic_anatomy_enabled": True,
            "semantic_anatomy_mode": SemanticEnforcementMode.ENFORCE,
            "semantic_anatomy_endpoint_url": "http://semantic.internal/v1/anatomy/assess",
            "semantic_anatomy_model": _MODEL,
            "semantic_anatomy_model_revision": _REVISION,
        }
    )
    app = create_app(enabled_settings)
    detail_action = f"/dashboard/review-tasks/{task_id}"
    api_action = f"/api/v1/review-tasks/{task_id}"
    with TestClient(
        app,
        base_url=ORIGIN,
        client=("192.0.2.100", 50000),
    ) as client:
        app.state.object_store = SameOriginReviewStore()
        csrf = _login(client, enabled_settings, context.users[AdminRole.OWNER])
        page = client.get(detail_action)
        summary = client.get(api_action)
        blocked = client.post(
            f"{api_action}:complete",
            json={"expected_lock_version": 2},
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "semantic-blocked-completion",
            },
        )
        override = client.post(
            f"{api_action}/decisions",
            json={
                "asset_id": str(context.asset_ids[0]),
                "decision": "accept",
                "expected_lock_version": 2,
                "reason_code": SEMANTIC_SEVERE_OVERRIDE_REASON_CODE,
                "note": "Owner inspected the anatomy at full resolution.",
            },
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "semantic-owner-override",
            },
        )
        overridden_page = client.get(detail_action)
        completed = client.post(
            f"{api_action}:complete",
            json={"expected_lock_version": 3},
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "semantic-complete-after-override",
            },
        )

    assert page.status_code == 200
    assert "Anatomy checks terminal" in page.text
    assert "Severe final-set blocks" in page.text
    assert "Completion is blocked because an accepted high-confidence severe image" in page.text
    assert f'value="{SEMANTIC_SEVERE_OVERRIDE_REASON_CODE}"' in page.text
    assert summary.status_code == 200
    assert summary.json()["semantic_gate"] == {
        "enabled": True,
        "mode": "enforce",
        "ranked_asset_count": 2,
        "terminal_count": 2,
        "pending_count": 0,
        "unavailable_count": 0,
        "severe_count": 1,
        "severe_override_count": 0,
        "severe_blocked_count": 1,
        "completion_ready": False,
    }
    assert blocked.status_code == 409
    assert override.status_code == 201
    assert "Owner override recorded" in overridden_page.text
    assert completed.status_code == 200
