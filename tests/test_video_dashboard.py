import hashlib
import re
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

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
from gen_automation.storage.memory import MemoryObjectStore
from gen_automation.video_worker.models import DEFAULT_MAX_SOURCE_BYTES


def _hidden_value(page: str, name: str) -> str:
    match = re.search(rf'<input type="hidden" name="{name}" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def _enable_video(client: TestClient) -> None:
    _application(client).state.settings.video_generation_enabled = True


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
    duration: str = "3",
    variants: str = "1",
    prompt: str = "gentle breathing and subtle camera drift",
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
    assert "Wan 2.2 TI2V 5B" in page.text
    assert str(source_id) in page.text
    assert 'href="/dashboard/animations"' in page.text
    form = _form(page.text, asset_id=source_id, variants="2")

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
    assert len(saved) == 2
    assert {job.created_by_user_id for job in saved} == {UUID(int=0)}
    assert {job.source_asset_id for job in saved} == {source_id}
    assert {job.source_object_key for job in saved} == {f"masters/{source_id}.png"}
    assert {job.source_object_version_id for job in saved} == {"version-1"}
    assert {job.source_sha256 for job in saved} == {"c" * 64}
    assert {job.source_width for job in saved} == {1200}
    assert {job.source_height for job in saved} == {800}
    assert {job.width for job in saved} == {832}
    assert {job.height for job in saved} == {480}
    assert {job.frame_count for job in saved} == {73}
    assert {job.fps for job in saved} == {24}
    assert {job.loop_mode for job in saved} == {"ping_pong"}
    assert {job.state for job in saved} == {VideoGenerationState.QUEUED}
    assert {job.content_rating for job in saved} == {VideoContentRating.SFW}
    assert all(job.source_rights_confirmed and job.lawful_use_confirmed for job in saved)
    assert len({job.seed for job in saved}) == 2
    assert len({job.request_sha256 for job in saved}) == 2
    assert {job.estimated_cost_microusd for job in saved} == {35_000}
    assert {job.cost_metadata["variant_index"] for job in saved} == {1, 2}
    assert {job.cost_metadata["variant_count"] for job in saved} == {2}

    status_page = client.get(created.headers["location"])
    assert status_page.status_code == 200
    assert "Creating your short loop" in status_page.text
    assert "Variant 1 of 2" in status_page.text
    assert "Variant 2 of 2" in status_page.text
    assert 'data-video-status-refresh="8"' in status_page.text
    assert "Currently reserved" in status_page.text


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


def test_animation_status_can_cancel_every_queued_variant(
    client: TestClient,
) -> None:
    _enable_video(client)
    source_id = _seed_source(client)
    page = client.get("/dashboard/animations")
    created = client.post(
        "/dashboard/animations",
        data=_form(page.text, asset_id=source_id, variants="3"),
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

    assert client.portal.call(states) == [VideoGenerationState.CANCELLED] * 3
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
    assert saved[0].width == 480
    assert saved[0].height == 832
    assert saved[0].frame_count == 121
    assert saved[0].estimated_cost_microusd == 58_334
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
