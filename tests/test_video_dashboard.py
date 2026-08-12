import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

import gen_automation.services.videos as video_service
from gen_automation.db.models import (
    AdminUser,
    Asset,
    GenerationJob,
    Project,
    Release,
    ReleaseVersion,
    VideoGenerationJob,
)
from gen_automation.domain.enums import (
    AdminRole,
    AssetKind,
    AssetState,
    GenerationState,
    ReleasePhase,
    ResourceHealth,
)
from gen_automation.domain.video import VideoContentRating, VideoGenerationState
from gen_automation.services.videos import (
    CreateVideoSubmission,
    VideoQualityProfile,
    VideoStudioInputError,
    create_video_submission,
    planning_estimate_microusd,
)
from gen_automation.storage.memory import MemoryObjectStore
from gen_automation.video_worker.models import DEFAULT_MAX_SOURCE_BYTES
from gen_automation.video_worker.profiles import (
    A14B_ADULT_VIDEO_PROFILE_REGISTRATION,
    A14B_VIDEO_PROFILE_REGISTRATION,
    HQ_VIDEO_PROFILE_REGISTRATION,
)

A14B_WORKER_IMAGE = (
    "ghcr.io/neuraln-cyber/gen-automation-a14b-registry/"
    "video-worker-a14b-private@sha256:" + "a" * 64
)
LEGACY_WORKER_IMAGE = "registry.example.test/video-worker@sha256:" + "b" * 64


def _hidden_value(page: str, name: str) -> str:
    match = re.search(rf'<input type="hidden" name="{name}" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def _enable_video(client: TestClient) -> None:
    settings = _application(client).state.settings
    settings.video_generation_enabled = True
    settings.salad_video_worker_image = A14B_WORKER_IMAGE


def _application(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def _seed_source(
    client: TestClient,
    *,
    width: int = 1200,
    height: int = 800,
    byte_size: int = 123_456,
) -> UUID:
    database = _application(client).state.database
    assert client.portal is not None
    asset_id = uuid4()

    async def seed() -> None:
        async with database.sessions() as session:
            now = datetime.now(UTC)
            actor = await session.get(AdminUser, UUID(int=0))
            if actor is None:
                actor = AdminUser(
                    id=UUID(int=0),
                    username_normalized="local-developer",
                    display_name="Local Developer",
                    password_hash="disabled-test-password-hash",  # noqa: S106
                    role=AdminRole.OWNER,
                    is_active=True,
                    failed_login_count=0,
                    password_changed_at=now,
                    lock_version=1,
                )
                session.add(actor)
            project = Project(
                id=uuid4(),
                slug=f"video-{asset_id.hex[:10]}",
                name="Video tests",
            )
            release = Release(
                id=uuid4(),
                project_id=project.id,
                slug=f"source-{asset_id.hex[:10]}",
                title="Private source set",
                phase=ReleasePhase.READY,
                health=ResourceHealth.HEALTHY,
                current_version_no=1,
                desired_accepted_count=1,
                lock_version=1,
            )
            version = ReleaseVersion(
                id=uuid4(),
                release_id=release.id,
                version_no=1,
                specification={},
                specification_sha256="a" * 64,
                created_by="test",
                created_at=now,
            )
            generation_job = GenerationJob(
                id=uuid4(),
                release_version_id=version.id,
                logical_key="source-job",
                parameters={},
                parameters_sha256="b" * 64,
                provider="salad",
                state=GenerationState.SUCCEEDED,
                priority=100,
                expected_output_count=1,
                attempt_count=1,
                max_attempts=3,
                lock_version=1,
            )
            source = Asset(
                id=asset_id,
                release_id=release.id,
                generation_job_id=generation_job.id,
                output_index=0,
                kind=AssetKind.RAW_MASTER,
                state=AssetState.AVAILABLE,
                storage_backend="memory",
                storage_bucket="private-test",
                object_key=f"masters/{asset_id}.png",
                object_version_id="version-1",
                sha256="c" * 64,
                content_type="image/png",
                image_format="png",
                width=width,
                height=height,
                byte_size=byte_size,
                asset_metadata={},
                available_at=now,
            )
            session.add_all([project, release, version, generation_job, source])
            await session.commit()

    client.portal.call(seed)
    return asset_id


def _form(
    page: str,
    *,
    asset_id: UUID,
    rating: str = "sfw",
    duration: str = "5",
    variants: str = "1",
    prompt: str = (
        "Locked camera. She shifts her weight, rolls her shoulders, and her hair follows."
    ),
) -> dict[str, str]:
    return {
        "csrf_token": _hidden_value(page, "csrf_token"),
        "submission_id": _hidden_value(page, "submission_id"),
        "idempotency_key": _hidden_value(page, "idempotency_key"),
        "source_asset_id": str(asset_id),
        "prompt": prompt,
        "content_rating": rating,
        "duration_seconds": duration,
        "variant_count": variants,
        "source_rights_confirmed": "on",
        "lawful_use_confirmed": "on",
    }


def _a14b_command(
    *,
    source_id: UUID,
    submission_id: UUID,
    rating: VideoContentRating = VideoContentRating.SFW,
) -> CreateVideoSubmission:
    is_adult = rating != VideoContentRating.SFW
    return CreateVideoSubmission(
        submission_id=submission_id,
        source_asset_id=source_id,
        prompt="Locked camera. She shifts her hips and shoulders while her hair follows.",
        content_rating=rating,
        duration_seconds=5,
        variant_count=1,
        source_rights_confirmed=True,
        lawful_use_confirmed=True,
        all_depicted_people_are_adults=is_adult,
        consensual_adult_content_confirmed=is_adult,
        no_real_person_sexual_content=is_adult,
        quality_profile=VideoQualityProfile.SMOOTHMIX_A14B_Q3,
    )


def test_animation_studio_disabled_is_a_clear_404_and_creates_nothing(
    client: TestClient,
) -> None:
    response = client.get("/dashboard/animations")
    post = client.post("/dashboard/animations", data={}, follow_redirects=False)

    assert response.status_code == 404
    assert "Animation Studio is not enabled" in response.text
    assert post.status_code == 404
    assert "Animation Studio is not enabled" in post.text
    assert 'href="/dashboard/animations"' not in client.get("/dashboard").text

    database = _application(client).state.database
    assert client.portal is not None

    async def count() -> int:
        async with database.sessions() as session:
            return int(await session.scalar(select(func.count(VideoGenerationJob.id))) or 0)

    assert client.portal.call(count) == 0


def test_animation_submit_freezes_source_and_replays_the_same_variant_group(
    client: TestClient,
) -> None:
    _enable_video(client)
    source_id = _seed_source(client)
    page = client.get("/dashboard/animations")

    assert page.status_code == 200
    assert "Animation Studio" in page.text
    assert "SmoothMix Wan 2.2 I2V-A14B Q3" in page.text
    assert "Required motion prompt" in page.text
    assert "Use natural-language action sentences." in page.text
    assert "gentle camera drift" not in page.text
    assert str(source_id) in page.text
    assert 'href="/dashboard/animations"' in page.text
    form = _form(page.text, asset_id=source_id)

    created = client.post(
        "/dashboard/animations",
        data=form,
        follow_redirects=False,
    )
    replay = client.post(
        "/dashboard/animations",
        data=form,
        follow_redirects=False,
    )

    assert created.status_code == 303
    assert replay.status_code == 303
    assert created.headers["location"] == replay.headers["location"]
    assert created.headers["location"].startswith("/dashboard/animations/")

    database = _application(client).state.database
    assert client.portal is not None

    async def jobs() -> list[VideoGenerationJob]:
        async with database.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(VideoGenerationJob).order_by(VideoGenerationJob.seed)
                    )
                ).all()
            )

    saved = client.portal.call(jobs)
    assert len(saved) == 1
    assert {job.created_by_user_id for job in saved} == {UUID(int=0)}
    assert {job.source_asset_id for job in saved} == {source_id}
    assert {job.source_object_key for job in saved} == {f"masters/{source_id}.png"}
    assert {job.source_object_version_id for job in saved} == {"version-1"}
    assert {job.source_sha256 for job in saved} == {"c" * 64}
    assert {job.source_width for job in saved} == {1200}
    assert {job.source_height for job in saved} == {800}
    assert {job.width for job in saved} == {1200}
    assert {job.height for job in saved} == {800}
    assert {job.frame_count for job in saved} == {81}
    assert {job.fps for job in saved} == {16}
    assert {job.loop_mode for job in saved} == {"forward"}
    assert {job.profile_key for job in saved} == {
        A14B_VIDEO_PROFILE_REGISTRATION.profile.profile_id
    }
    assert {job.state for job in saved} == {VideoGenerationState.QUEUED}
    assert {job.prompt for job in saved} == {
        "Locked camera. She shifts her weight, rolls her shoulders, and her hair follows."
    }
    assert {job.content_rating for job in saved} == {VideoContentRating.SFW}
    assert all(job.source_rights_confirmed and job.lawful_use_confirmed for job in saved)
    assert len({job.seed for job in saved}) == 1
    assert len({job.request_sha256 for job in saved}) == 1
    assert {job.estimated_cost_microusd for job in saved} == {1_750_000}
    assert {job.cost_metadata["variant_index"] for job in saved} == {1}
    assert {job.cost_metadata["variant_count"] for job in saved} == {1}

    status_page = client.get(created.headers["location"])
    assert status_page.status_code == 200
    assert "Creating your short loop" in status_page.text
    assert "Variant 1 of 1" in status_page.text
    assert "About 5s forward motion" in status_page.text
    assert 'data-video-status-refresh="8"' in status_page.text
    assert "Currently reserved" in status_page.text


@pytest.mark.parametrize("prompt", ["", "   "])
def test_animation_form_rejects_blank_a14b_prompt_without_creating_a_job(
    client: TestClient,
    prompt: str,
) -> None:
    _enable_video(client)
    source_id = _seed_source(client)
    page = client.get("/dashboard/animations")

    rejected = client.post(
        "/dashboard/animations",
        data=_form(page.text, asset_id=source_id, prompt=prompt),
    )

    assert rejected.status_code == 422
    assert "Motion prompt is required for SmoothMix A14B animation." in rejected.text
    database = _application(client).state.database
    assert client.portal is not None

    async def count_jobs() -> int:
        async with database.sessions() as session:
            return int(await session.scalar(select(func.count(VideoGenerationJob.id))) or 0)

    assert client.portal.call(count_jobs) == 0


@pytest.mark.parametrize("prompt", ["", "   "])
def test_video_service_rejects_blank_a14b_prompt_without_creating_a_job(
    client: TestClient,
    prompt: str,
) -> None:
    _enable_video(client)
    source_id = _seed_source(client)
    database = _application(client).state.database
    assert client.portal is not None

    async def submit() -> int:
        async with database.sessions() as session:
            with pytest.raises(VideoStudioInputError, match="motion prompt is required"):
                await create_video_submission(
                    session,
                    command=CreateVideoSubmission(
                        submission_id=uuid4(),
                        source_asset_id=source_id,
                        prompt=prompt,
                        content_rating=VideoContentRating.SFW,
                        duration_seconds=5,
                        variant_count=1,
                        source_rights_confirmed=True,
                        lawful_use_confirmed=True,
                        quality_profile=VideoQualityProfile.SMOOTHMIX_A14B_Q3,
                    ),
                    actor_user_id=UUID(int=0),
                    max_hourly_cost_usd=Decimal("0.35"),
                    runtime_worker_image_digest=A14B_WORKER_IMAGE,
                )
            return int(await session.scalar(select(func.count(VideoGenerationJob.id))) or 0)

    assert client.portal.call(submit) == 0


def test_a14b_admission_requires_exact_private_worker_image_before_cap_use(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_video(client)
    source_id = _seed_source(client)
    application = _application(client)
    application.state.settings.salad_video_worker_image = LEGACY_WORKER_IMAGE
    unavailable = client.get("/dashboard/animations")
    assert unavailable.status_code == 503
    assert "A14B runtime is not ready" in unavailable.text
    database = application.state.database
    assert client.portal is not None
    cap_calls = 0

    async def observe_cap(*_args: object, **_kwargs: object) -> None:
        nonlocal cap_calls
        cap_calls += 1

    monkeypatch.setattr(video_service, "_validate_a14b_cumulative_budget", observe_cap)

    async def reject_legacy_then_admit_private() -> tuple[int, UUID]:
        async with database.sessions() as session:
            with pytest.raises(VideoStudioInputError, match="configured video worker image"):
                await create_video_submission(
                    session,
                    command=_a14b_command(source_id=source_id, submission_id=uuid4()),
                    actor_user_id=UUID(int=0),
                    max_hourly_cost_usd=Decimal("0.35"),
                    runtime_worker_image_digest=LEGACY_WORKER_IMAGE,
                )
            assert cap_calls == 0
            created = await create_video_submission(
                session,
                command=_a14b_command(source_id=source_id, submission_id=uuid4()),
                actor_user_id=UUID(int=0),
                max_hourly_cost_usd=Decimal("0.35"),
                runtime_worker_image_digest=A14B_WORKER_IMAGE,
            )
            await session.commit()
            count = int(await session.scalar(select(func.count(VideoGenerationJob.id))) or 0)
            return count, created.jobs[0].id

    count, job_id = client.portal.call(reject_legacy_then_admit_private)
    assert count == 1
    assert cap_calls == 1
    application.state.settings.salad_video_worker_image = LEGACY_WORKER_IMAGE
    assert client.get(f"/dashboard/animations/{job_id}").status_code == 200
    application.state.settings.salad_video_worker_image = A14B_WORKER_IMAGE
    assert client.get("/dashboard/animations").status_code == 200


def test_a14b_cumulative_spend_rejects_sequential_overage_without_an_extra_job(
    client: TestClient,
) -> None:
    _enable_video(client)
    source_id = _seed_source(client)
    database = _application(client).state.database
    assert client.portal is not None

    async def submit_to_ceiling() -> tuple[int, int]:
        async with database.sessions() as session:
            for _ in range(4):
                await create_video_submission(
                    session,
                    command=_a14b_command(source_id=source_id, submission_id=uuid4()),
                    actor_user_id=UUID(int=0),
                    max_hourly_cost_usd=Decimal("0.35"),
                    runtime_worker_image_digest=A14B_WORKER_IMAGE,
                )
                await session.commit()
            first = (
                await session.scalars(
                    select(VideoGenerationJob).order_by(VideoGenerationJob.created_at)
                )
            ).first()
            assert first is not None
            first.state = VideoGenerationState.CANCELLED
            first.completed_at = datetime.now(UTC)
            await session.commit()

            with pytest.raises(VideoStudioInputError, match="cumulative estimate exceeds"):
                await create_video_submission(
                    session,
                    command=_a14b_command(source_id=source_id, submission_id=uuid4()),
                    actor_user_id=UUID(int=0),
                    max_hourly_cost_usd=Decimal("0.35"),
                    runtime_worker_image_digest=A14B_WORKER_IMAGE,
                )
            count = int(await session.scalar(select(func.count(VideoGenerationJob.id))) or 0)
            total = int(
                await session.scalar(select(func.sum(VideoGenerationJob.estimated_cost_microusd)))
                or 0
            )
            return count, total

    assert client.portal.call(submit_to_ceiling) == (4, 7_000_000)


def test_a14b_base_and_adult_profiles_share_one_cumulative_spend_ceiling(
    client: TestClient,
) -> None:
    _enable_video(client)
    source_id = _seed_source(client)
    database = _application(client).state.database
    assert client.portal is not None

    async def submit_mixed_profiles() -> tuple[int, int]:
        async with database.sessions() as session:
            for rating in (
                VideoContentRating.SFW,
                VideoContentRating.EXPLICIT,
                VideoContentRating.SFW,
                VideoContentRating.EXPLICIT,
            ):
                await create_video_submission(
                    session,
                    command=_a14b_command(
                        source_id=source_id,
                        submission_id=uuid4(),
                        rating=rating,
                    ),
                    actor_user_id=UUID(int=0),
                    max_hourly_cost_usd=Decimal("0.35"),
                    runtime_worker_image_digest=A14B_WORKER_IMAGE,
                )
                await session.commit()

            with pytest.raises(VideoStudioInputError, match="cumulative estimate exceeds"):
                await create_video_submission(
                    session,
                    command=_a14b_command(
                        source_id=source_id,
                        submission_id=uuid4(),
                        rating=VideoContentRating.EXPLICIT,
                    ),
                    actor_user_id=UUID(int=0),
                    max_hourly_cost_usd=Decimal("0.35"),
                    runtime_worker_image_digest=A14B_WORKER_IMAGE,
                )
            base_count = int(
                await session.scalar(
                    select(func.count(VideoGenerationJob.id)).where(
                        VideoGenerationJob.profile_key
                        == A14B_VIDEO_PROFILE_REGISTRATION.profile.profile_id
                    )
                )
                or 0
            )
            adult_count = int(
                await session.scalar(
                    select(func.count(VideoGenerationJob.id)).where(
                        VideoGenerationJob.profile_key
                        == A14B_ADULT_VIDEO_PROFILE_REGISTRATION.profile.profile_id
                    )
                )
                or 0
            )
            return base_count, adult_count

    assert client.portal.call(submit_mixed_profiles) == (2, 2)


def test_exact_a14b_replay_at_the_cumulative_ceiling_does_not_double_count(
    client: TestClient,
) -> None:
    _enable_video(client)
    source_id = _seed_source(client)
    database = _application(client).state.database
    assert client.portal is not None

    async def submit_and_replay() -> tuple[int, int, bool]:
        async with database.sessions() as session:
            replay_command = _a14b_command(source_id=source_id, submission_id=uuid4())
            first = await create_video_submission(
                session,
                command=replay_command,
                actor_user_id=UUID(int=0),
                max_hourly_cost_usd=Decimal("0.35"),
                runtime_worker_image_digest=A14B_WORKER_IMAGE,
            )
            await session.commit()
            for _ in range(3):
                await create_video_submission(
                    session,
                    command=_a14b_command(source_id=source_id, submission_id=uuid4()),
                    actor_user_id=UUID(int=0),
                    max_hourly_cost_usd=Decimal("0.35"),
                    runtime_worker_image_digest=A14B_WORKER_IMAGE,
                )
                await session.commit()

            replay = await create_video_submission(
                session,
                command=replay_command,
                actor_user_id=UUID(int=0),
                max_hourly_cost_usd=Decimal("0.35"),
                runtime_worker_image_digest=A14B_WORKER_IMAGE,
            )
            count = int(await session.scalar(select(func.count(VideoGenerationJob.id))) or 0)
            total = int(
                await session.scalar(select(func.sum(VideoGenerationJob.estimated_cost_microusd)))
                or 0
            )
            return count, total, replay.jobs[0].id == first.jobs[0].id

    assert client.portal.call(submit_and_replay) == (4, 7_000_000, True)


def test_hidden_hq_canary_uses_native_shape_one_attempt_and_stable_seed(
    client: TestClient,
) -> None:
    _enable_video(client)
    source_id = _seed_source(client, width=1144, height=1480)
    database = _application(client).state.database
    assert client.portal is not None

    async def submit(submission_id: UUID) -> VideoGenerationJob:
        async with database.sessions() as session:
            async with session.begin():
                await create_video_submission(
                    session,
                    command=CreateVideoSubmission(
                        submission_id=submission_id,
                        source_asset_id=source_id,
                        prompt="locked camera, one blink and one slow breath",
                        content_rating=VideoContentRating.SFW,
                        duration_seconds=3,
                        variant_count=1,
                        source_rights_confirmed=True,
                        lawful_use_confirmed=True,
                        quality_profile=VideoQualityProfile.HQ_NATIVE,
                    ),
                    actor_user_id=UUID(int=0),
                    max_hourly_cost_usd=Decimal("0.35"),
                    runtime_worker_image_digest=LEGACY_WORKER_IMAGE,
                )
            return (
                await session.scalars(
                    select(VideoGenerationJob).order_by(VideoGenerationJob.created_at.desc())
                )
            ).first()

    first = client.portal.call(submit, uuid4())
    second = client.portal.call(submit, uuid4())

    assert first is not None and second is not None
    assert first.profile_key == HQ_VIDEO_PROFILE_REGISTRATION.profile.profile_id
    assert first.profile_sha256 == HQ_VIDEO_PROFILE_REGISTRATION.job_contract_sha256
    assert (first.width, first.height, first.frame_count, first.fps) == (1152, 1472, 73, 24)
    assert first.max_attempts == 1
    assert first.estimated_cost_microusd == 297_306
    assert first.cost_metadata["estimated_runtime_seconds"] == 3058
    assert first.cost_metadata["native_pixel_count"] == 1152 * 1472
    assert first.cost_metadata["experiment_seed_basis"] == "source-sha256-and-prompt/v1"
    assert first.seed == second.seed

    with pytest.raises(VideoStudioInputError, match="one 3-second canary"):
        planning_estimate_microusd(
            max_hourly_cost_usd=Decimal("0.35"),
            duration_seconds=5,
            variant_count=1,
            quality_profile=VideoQualityProfile.HQ_NATIVE,
        )


def test_animation_preview_is_private_and_conditionally_revalidated(
    client: TestClient,
) -> None:
    _enable_video(client)
    store = MemoryObjectStore(bucket="private-test")
    _application(client).state.object_store = store
    output = BytesIO()
    Image.new("RGB", (96, 64), (84, 42, 160)).save(output, format="PNG")
    source = output.getvalue()
    source_id = _seed_source(
        client,
        width=96,
        height=64,
        byte_size=len(source),
    )
    key = f"masters/{source_id}.png"
    store.put_for_test(key, source, content_type="image/png")
    stored = store.objects[key]
    source_sha256 = hashlib.sha256(source).hexdigest()
    database = _application(client).state.database
    assert client.portal is not None

    async def bind_exact_source() -> None:
        async with database.sessions() as session:
            asset = await session.get(Asset, source_id)
            assert asset is not None
            asset.storage_backend = store.backend
            asset.storage_bucket = store.bucket
            asset.object_version_id = stored.version_id
            asset.sha256 = source_sha256
            await session.commit()

    client.portal.call(bind_exact_source)
    preview_url = f"/dashboard/animations/assets/{source_id}/preview/{source_sha256[:16]}.jpg"

    preview = client.get(preview_url)

    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("image/jpeg")
    assert preview.headers["cache-control"] == "private, no-cache, must-revalidate"
    assert preview.headers["vary"].lower() == "cookie"
    cached = client.get(preview_url, headers={"If-None-Match": preview.headers["etag"]})
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["cache-control"] == "private, no-cache, must-revalidate"


def test_animation_studio_filters_and_rejects_sources_above_worker_limits(
    client: TestClient,
) -> None:
    _enable_video(client)
    valid_source_id = _seed_source(client)
    source_id = _seed_source(client, byte_size=DEFAULT_MAX_SOURCE_BYTES + 1)

    page = client.get("/dashboard/animations")
    assert page.status_code == 200
    assert str(valid_source_id) in page.text
    assert str(source_id) not in page.text

    form = _form(page.text, asset_id=source_id)
    rejected = client.post("/dashboard/animations", data=form)
    assert rejected.status_code == 409
    assert "The source image or saved form changed" in rejected.text


def test_animation_status_can_cancel_the_queued_a14b_job(
    client: TestClient,
) -> None:
    _enable_video(client)
    source_id = _seed_source(client)
    page = client.get("/dashboard/animations")
    created = client.post(
        "/dashboard/animations",
        data=_form(page.text, asset_id=source_id),
        follow_redirects=False,
    )
    assert created.status_code == 303
    status_url = created.headers["location"]
    status_page = client.get(status_url)
    assert status_page.status_code == 200
    assert "Cancel this run" in status_page.text
    assert "Conservative usage estimate" in status_page.text
    assert "Actual so far" not in status_page.text

    cancelled = client.post(
        f"{status_url}/cancel",
        data={"csrf_token": _hidden_value(status_page.text, "csrf_token")},
        follow_redirects=False,
    )
    assert cancelled.status_code == 303
    assert cancelled.headers["location"] == status_url

    database = _application(client).state.database
    assert client.portal is not None

    async def states() -> list[VideoGenerationState]:
        async with database.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(VideoGenerationJob.state).order_by(VideoGenerationJob.seed)
                    )
                ).all()
            )

    assert client.portal.call(states) == [VideoGenerationState.CANCELLED]
    terminal_page = client.get(status_url)
    assert terminal_page.status_code == 200
    assert "Cancel this run" not in terminal_page.text


def test_adult_animation_requires_fresh_explicit_attestations(
    client: TestClient,
) -> None:
    _enable_video(client)
    source_id = _seed_source(client)
    page = client.get("/dashboard/animations")
    missing = _form(page.text, asset_id=source_id, rating="explicit")

    rejected = client.post("/dashboard/animations", data=missing)
    assert rejected.status_code == 422
    assert "adult content requires an all-adults attestation" in rejected.text.lower()
    assert (
        re.search(
            r'<input[^>]+name="all_depicted_people_are_adults"[^>]+checked',
            rejected.text,
            re.DOTALL,
        )
        is None
    )

    accepted_form = dict(missing)
    accepted_form.update(
        {
            "all_depicted_people_are_adults": "on",
            "consensual_adult_content_confirmed": "on",
            "no_real_person_sexual_content": "on",
        }
    )
    accepted = client.post(
        "/dashboard/animations",
        data=accepted_form,
        follow_redirects=False,
    )
    assert accepted.status_code == 303

    database = _application(client).state.database
    assert client.portal is not None

    async def saved() -> list[VideoGenerationJob]:
        async with database.sessions() as session:
            return list((await session.scalars(select(VideoGenerationJob))).all())

    jobs = client.portal.call(saved)
    assert len(jobs) == 1
    assert jobs[0].content_rating == VideoContentRating.EXPLICIT
    assert jobs[0].profile_key == A14B_ADULT_VIDEO_PROFILE_REGISTRATION.profile.profile_id
    assert jobs[0].all_depicted_people_are_adults is True
    assert jobs[0].consensual_adult_content_confirmed is True
    assert jobs[0].no_real_person_sexual_content is True


def test_animation_form_rejects_payload_changes_and_caller_storage_fields(
    client: TestClient,
) -> None:
    _enable_video(client)
    source_id = _seed_source(client, width=700, height=1100)
    page = client.get("/dashboard/animations")
    original = _form(
        page.text,
        asset_id=source_id,
        duration="5",
        prompt="slow portrait camera movement",
    )
    created = client.post(
        "/dashboard/animations",
        data=original,
        follow_redirects=False,
    )
    assert created.status_code == 303

    changed = dict(original)
    changed["prompt"] = "a different prompt under the same form key"
    conflict = client.post("/dashboard/animations", data=changed)
    assert conflict.status_code == 409

    caller_storage = dict(original)
    caller_storage["source_object_key"] = "attacker/chosen-object.png"
    rejected = client.post("/dashboard/animations", data=caller_storage)
    assert rejected.status_code == 400

    invalid_key = dict(original)
    invalid_key["idempotency_key"] = "video-studio-" + "0" * 64
    csrf_rejected = client.post("/dashboard/animations", data=invalid_key)
    assert csrf_rejected.status_code == 400

    database = _application(client).state.database
    assert client.portal is not None

    async def jobs() -> list[VideoGenerationJob]:
        async with database.sessions() as session:
            return list((await session.scalars(select(VideoGenerationJob))).all())

    saved = client.portal.call(jobs)
    assert len(saved) == 1
    assert saved[0].source_object_key == f"masters/{source_id}.png"
    assert saved[0].width == 700
    assert saved[0].height == 1100
    assert saved[0].frame_count == 81
    assert saved[0].fps == 16
    assert saved[0].loop_mode == "forward"
    assert saved[0].estimated_cost_microusd == 1_750_000
    assert saved[0].cost_metadata["requested_duration_seconds"] == 5


def test_animation_status_template_uses_a_private_loop_player() -> None:
    template = Path("src/gen_automation/templates/dashboard/video_status.html").read_text(
        encoding="utf-8"
    )

    assert "{% if job.output_url %}" in template
    assert 'src="{{ job.output_url }}"' in template
    assert "controls" in template
    assert "loop" in template
    assert "muted" in template
    assert "playsinline" in template
    assert 'preload="metadata"' in template
