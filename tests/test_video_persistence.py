from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from gen_automation.db.models import (
    AdminUser,
    Asset,
    AssetLineage,
    Project,
    Release,
    SaladDeployment,
    VideoGenerationAttempt,
    VideoGenerationJob,
    VideoGenerationOutput,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AdminRole,
    AssetKind,
    AssetState,
    DesiredDeploymentState,
    SaladDeploymentPurpose,
    SaladDeploymentState,
)
from gen_automation.domain.video import (
    VideoContentRating,
    VideoGenerationAttemptState,
    VideoGenerationState,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
SOURCE_SHA = "a" * 64
PROFILE_SHA = "b" * 64
TEST_PROFILE = "wan-i2v-economy"
REQUEST_SHA = "c" * 64
OUTPUT_SHA = "d" * 64


@dataclass(frozen=True)
class VideoPersistenceContext:
    database: Database
    release_id: UUID
    user_id: UUID
    source_asset_id: UUID
    deployment_id: UUID


def _deployment(
    version_no: int,
    purpose: SaladDeploymentPurpose,
    *,
    is_current: bool = True,
) -> SaladDeployment:
    return SaladDeployment(
        version_no=version_no,
        config_sha256=f"{version_no:x}" * 64,
        provider_configuration={},
        worker_image_digest="registry.example.test/video@sha256:" + ("f" * 64),
        organization_name="creator-org",
        project_name="animation",
        queue_name=f"animation-{version_no}",
        container_group_name=f"animation-worker-{version_no}",
        purpose=purpose,
        state=SaladDeploymentState.PLANNED,
        desired_state=DesiredDeploymentState.ACTIVE,
        is_current=is_current,
        min_replicas=0,
        max_replicas=1,
        desired_queue_length=1,
        max_hourly_cost_microusd=3_600_000,
    )


@pytest.fixture
async def video_persistence_context(
    tmp_path: Path,
) -> AsyncIterator[VideoPersistenceContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'video.db').as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        project = Project(slug="video-tests", name="Video Tests")
        owner = AdminUser(
            username_normalized="video-owner@example.test",
            display_name="Video Owner",
            password_hash="disabled-test-password-hash",  # noqa: S106
            role=AdminRole.OWNER,
            is_active=True,
            failed_login_count=0,
            password_changed_at=NOW,
            credential_version=1,
            lock_version=1,
        )
        session.add_all((project, owner))
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="animation",
            title="Animation",
            desired_accepted_count=1,
        )
        session.add(release)
        await session.flush()
        source = Asset(
            release_id=release.id,
            kind=AssetKind.DERIVATIVE,
            state=AssetState.AVAILABLE,
            storage_backend="s3",
            storage_bucket="private-assets",
            object_key="releases/animation/source.webp",
            object_version_id="source-v1",
            sha256=SOURCE_SHA,
            content_type="image/webp",
            image_format="WEBP",
            width=768,
            height=1_024,
            byte_size=120_000,
            asset_metadata={},
            available_at=NOW,
        )
        deployment = _deployment(1, SaladDeploymentPurpose.VIDEO)
        session.add_all((source, deployment))
        await session.commit()
        context = VideoPersistenceContext(
            database=database,
            release_id=release.id,
            user_id=owner.id,
            source_asset_id=source.id,
            deployment_id=deployment.id,
        )
    try:
        yield context
    finally:
        await database.dispose()


def _job(
    context: VideoPersistenceContext,
    *,
    request_sha256: str = REQUEST_SHA,
) -> VideoGenerationJob:
    return VideoGenerationJob(
        source_asset_id=context.source_asset_id,
        created_by_user_id=context.user_id,
        source_storage_backend="s3",
        source_storage_bucket="private-assets",
        source_object_key="releases/animation/source.webp",
        source_object_version_id="source-v1",
        source_sha256=SOURCE_SHA,
        source_content_type="image/webp",
        source_image_format="WEBP",
        source_width=768,
        source_height=1_024,
        source_byte_size=120_000,
        prompt="subtle natural movement",
        negative_prompt="camera cut",
        profile_key=TEST_PROFILE,
        profile_version="1",
        profile_sha256=PROFILE_SHA,
        seed=42,
        frame_count=73,
        fps=24,
        width=480,
        height=832,
        loop_mode="ping_pong",
        content_rating=VideoContentRating.EXPLICIT,
        source_rights_confirmed=True,
        lawful_use_confirmed=True,
        all_depicted_people_are_adults=True,
        consensual_adult_content_confirmed=True,
        no_real_person_sexual_content=True,
        request_sha256=request_sha256,
        estimated_cost_microusd=12_000,
        reserved_cost_microusd=15_000,
        cost_metadata={"pricing_profile": "economy-v1"},
    )


async def test_video_job_attempt_output_and_lineage_persist_atomically(
    video_persistence_context: VideoPersistenceContext,
) -> None:
    context = video_persistence_context
    async with context.database.sessions() as session:
        job = _job(context)
        session.add(job)
        await session.flush()
        attempt = VideoGenerationAttempt(
            video_generation_job_id=job.id,
            salad_deployment_id=context.deployment_id,
            attempt_no=1,
            provider="salad",
            provider_external_id="video-provider-job-1",
            submission_key="video-submission-1",
            request_sha256=REQUEST_SHA,
            state=VideoGenerationAttemptState.SUCCEEDED,
            worker_image_digest="registry.example.test/video@sha256:" + ("f" * 64),
            request_metadata={"profile": "economy"},
            response_metadata={"provider_state": "completed"},
            completed_at=NOW,
            actual_cost_microusd=11_500,
            billed_duration_ms=92_000,
        )
        output_asset = Asset(
            release_id=context.release_id,
            kind=AssetKind.DERIVATIVE,
            state=AssetState.AVAILABLE,
            storage_backend="s3",
            storage_bucket="private-assets",
            object_key="video/jobs/output.mp4",
            object_version_id="output-v1",
            sha256=OUTPUT_SHA,
            content_type="video/mp4",
            image_format="MP4",
            width=480,
            height=832,
            byte_size=2_400_000,
            asset_metadata={"media_kind": "video"},
            available_at=NOW,
        )
        session.add_all((attempt, output_asset))
        await session.flush()
        lineage = AssetLineage(
            parent_asset_id=context.source_asset_id,
            child_asset_id=output_asset.id,
            relation="animated_from",
            recipe_version="wan-i2v-economy/1",
            created_at=NOW,
        )
        session.add(lineage)
        await session.flush()
        output = VideoGenerationOutput(
            video_generation_job_id=job.id,
            successful_attempt_id=attempt.id,
            source_asset_id=context.source_asset_id,
            asset_id=output_asset.id,
            asset_lineage_id=lineage.id,
            storage_backend="s3",
            storage_bucket="private-assets",
            object_key="video/jobs/output.mp4",
            object_version_id="output-v1",
            sha256=OUTPUT_SHA,
            content_type="video/mp4",
            video_format="MP4",
            width=480,
            height=832,
            byte_size=2_400_000,
            frame_count=144,
            fps=24,
            duration_ms=6_000,
            created_at=NOW,
        )
        job.state = VideoGenerationState.SUCCEEDED
        job.completed_at = NOW
        job.actual_cost_microusd = 11_500
        job.billed_duration_ms = 92_000
        session.add(output)
        await session.commit()

        persisted = await session.scalar(
            select(VideoGenerationJob).where(VideoGenerationJob.id == job.id)
        )
        persisted_output = await session.scalar(
            select(VideoGenerationOutput).where(
                VideoGenerationOutput.video_generation_job_id == job.id
            )
        )
        assert persisted is not None
        assert persisted.source_object_version_id == "source-v1"
        assert persisted.actual_cost_microusd == 11_500
        assert persisted_output is not None
        assert persisted_output.source_asset_id == context.source_asset_id
        assert persisted_output.asset_lineage_id == lineage.id


async def test_database_rejects_missing_explicit_content_attestations(
    video_persistence_context: VideoPersistenceContext,
) -> None:
    context = video_persistence_context
    async with context.database.sessions() as session:
        job = _job(context, request_sha256="e" * 64)
        job.all_depicted_people_are_adults = False
        session.add(job)

        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_one_current_salad_deployment_is_allowed_per_purpose(
    video_persistence_context: VideoPersistenceContext,
) -> None:
    context = video_persistence_context
    async with context.database.sessions() as session:
        image_deployment = _deployment(2, SaladDeploymentPurpose.IMAGE)
        session.add(image_deployment)
        await session.commit()

        second_video_deployment = _deployment(3, SaladDeploymentPurpose.VIDEO)
        session.add(second_video_deployment)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
