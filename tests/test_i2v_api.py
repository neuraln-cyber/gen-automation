import hashlib
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from PIL import Image

from gen_automation.db.models import (
    AdminUser,
    Asset,
    GenerationJob,
    Project,
    Release,
    ReleaseVersion,
)
from gen_automation.domain.enums import AdminRole, AssetKind, AssetState
from gen_automation.storage.memory import MemoryObjectStore


def _png_bytes() -> bytes:
    target = BytesIO()
    Image.new("RGB", (48, 64), (36, 72, 110)).save(target, format="PNG")
    return target.getvalue()


def _seed_development_owner(client: TestClient) -> None:
    async def seed() -> None:
        async with client.app.state.database.sessions() as session:
            if await session.get(AdminUser, UUID(int=0)) is not None:
                return
            now = datetime.now(UTC)
            session.add(
                AdminUser(
                    id=UUID(int=0),
                    username_normalized="local-developer",
                    display_name="Local Developer",
                    password_hash="unused-development-password-hash",  # noqa: S106
                    role=AdminRole.OWNER,
                    is_active=True,
                    failed_login_count=0,
                    password_changed_at=now,
                    credential_version=1,
                    lock_version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

    assert client.portal is not None
    client.portal.call(seed)


def _complete_uploaded_input(client: TestClient, store: MemoryObjectStore) -> dict[str, object]:
    intent_response = client.post(
        "/api/v1/i2v/inputs/uploads",
        json={"display_name": "source.png", "content_type": "image/png"},
        headers={"X-CSRF-Token": "development"},
    )
    assert intent_response.status_code == 201
    intent = intent_response.json()
    assert intent["method"] == "POST"
    assert intent["fields"]["content-length-range"].startswith("1,")

    fields = intent["fields"]
    metadata = {
        name.removeprefix("x-amz-meta-"): value
        for name, value in fields.items()
        if name.startswith("x-amz-meta-")
    }
    store.put_for_test(
        fields["key"],
        _png_bytes(),
        content_type="image/png",
        metadata=metadata,
    )
    complete_response = client.post(
        f"/api/v1/i2v/inputs/uploads/{intent['upload_id']}:complete",
        json={"display_name": "source.png"},
        headers={"X-CSRF-Token": "development"},
    )
    assert complete_response.status_code == 201, complete_response.text
    completed = complete_response.json()
    assert completed["source"] == "upload"
    assert completed["width"] == 48
    assert completed["height"] == 64
    assert fields["key"] not in store.objects
    assert completed["object_key"] in store.objects
    return completed


async def _seed_generated_asset(client: TestClient, store: MemoryObjectStore) -> UUID:
    asset_id = uuid4()
    now = datetime.now(UTC)
    source = BytesIO()
    Image.new("RGB", (1144, 1480), (65, 90, 125)).save(source, format="PNG")
    source_bytes = source.getvalue()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    source_key = f"raw/{asset_id}.png"
    store.put_for_test(source_key, source_bytes, content_type="image/png")

    async with client.app.state.database.sessions() as session:
        project = Project(slug=f"i2v-source-{asset_id.hex}", name="Older I2V Source")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug=f"i2v-source-{asset_id.hex}",
            title="Older I2V Source",
            current_version_no=1,
            desired_accepted_count=1,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={"schema_version": 1},
            specification_sha256=hashlib.sha256(asset_id.bytes).hexdigest(),
            created_by="test",
            created_at=now,
        )
        session.add(version)
        await session.flush()
        job = GenerationJob(
            release_version_id=version.id,
            logical_key=hashlib.sha256(b"logical" + asset_id.bytes).hexdigest(),
            parameters={"ordinal": 0},
            parameters_sha256=hashlib.sha256(b"parameters" + asset_id.bytes).hexdigest(),
            provider="salad",
            expected_output_count=1,
        )
        session.add(job)
        await session.flush()
        session.add(
            Asset(
                id=asset_id,
                release_id=release.id,
                generation_job_id=job.id,
                output_index=0,
                kind=AssetKind.RAW_MASTER,
                state=AssetState.AVAILABLE,
                storage_backend=store.backend,
                storage_bucket=store.bucket,
                object_key=source_key,
                object_version_id=store.objects[source_key].version_id,
                sha256=source_sha256,
                content_type="image/png",
                image_format="PNG",
                width=1144,
                height=1480,
                byte_size=len(source_bytes),
                asset_metadata={},
                available_at=now,
            )
        )
        await session.commit()
    return asset_id


def test_i2v_upload_presets_and_queue_are_one_focused_flow(client: TestClient) -> None:
    _seed_development_owner(client)
    store = MemoryObjectStore(bucket="i2v-tests")
    client.app.state.object_store = store
    completed = _complete_uploaded_input(client, store)

    preset_response = client.post(
        "/api/v1/i2v/presets",
        json={
            "name": "Smooth five seconds",
            "description": "Stable FastFidelity baseline",
            "positive_prompt": "one smooth motion, stable camera",
            "negative_prompt": "flicker, jitter, background drift",
            "settings": {"duration_seconds": 5, "fps": 16, "steps": 4},
        },
        headers={"X-CSRF-Token": "development"},
    )
    assert preset_response.status_code == 201
    preset = preset_response.json()

    queued_response = client.post(
        "/api/v1/i2v/jobs",
        json={
            "input_id": completed["input_id"],
            "preset_id": preset["preset_id"],
            "positive_prompt": "operator-authored adult motion direction",
            "negative_prompt": "camera shake",
            "settings": {
                "duration_seconds": 5,
                "fps": 16,
                "loras": [
                    {
                        "high": "motion-high.safetensors",
                        "low": "motion-low.safetensors",
                        "strength": 0.3,
                    }
                ],
            },
            "batch_count": 2,
        },
        headers={"X-CSRF-Token": "development"},
    )
    assert queued_response.status_code == 201
    jobs = queued_response.json()["jobs"]
    assert [item["queue_position"] for item in jobs] == [1, 2]
    assert all(
        item["positive_prompt"] == "operator-authored adult motion direction" for item in jobs
    )
    assert jobs[0]["settings_snapshot"]["loras"][0]["strength"] == 0.3

    move_response = client.patch(
        "/api/v1/i2v/queue",
        json={"job_id": jobs[1]["job_id"], "before_job_id": jobs[0]["job_id"]},
        headers={"X-CSRF-Token": "development"},
    )
    assert move_response.status_code == 200
    assert [item["job_id"] for item in move_response.json()] == [
        jobs[1]["job_id"],
        jobs[0]["job_id"],
    ]

    cancel_response = client.post(
        f"/api/v1/i2v/jobs/{jobs[1]['job_id']}:cancel",
        headers={"X-CSRF-Token": "development"},
    )
    assert cancel_response.status_code == 200
    assert cancel_response.json()["state"] == "cancelled"
    retry_response = client.post(
        f"/api/v1/i2v/jobs/{jobs[1]['job_id']}:retry",
        headers={"X-CSRF-Token": "development"},
    )
    assert retry_response.status_code == 200
    assert retry_response.json()["state"] == "queued"


def test_i2v_generated_asset_registration_returns_source_dimensions(
    client: TestClient,
) -> None:
    _seed_development_owner(client)
    store = MemoryObjectStore(bucket="i2v-asset-id-tests")
    client.app.state.object_store = store
    assert client.portal is not None
    asset_id = client.portal.call(_seed_generated_asset, client, store)

    response = client.post(
        "/api/v1/i2v/inputs/from-asset",
        json={"asset_id": str(asset_id)},
        headers={"X-CSRF-Token": "development"},
    )

    assert response.status_code == 201, response.text
    registered = response.json()
    assert registered["source"] == "generation"
    assert registered["asset_id"] == str(asset_id)
    assert registered["width"] == 1144
    assert registered["height"] == 1480
    assert registered["display_name"] == f"Generated image {str(asset_id)[:8]}"
    assert registered["object_key"] in store.objects


def test_i2v_worker_endpoint_never_invents_progress(client: TestClient) -> None:
    response = client.get("/api/v1/i2v/worker")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status_available"] is False
    assert payload["deployment"] is None
    assert "not configured" in payload["message"]


def test_i2v_queue_is_paused_until_matching_hires_worker_is_enabled(
    client: TestClient,
) -> None:
    _seed_development_owner(client)
    store = MemoryObjectStore(bucket="i2v-tests")
    client.app.state.object_store = store
    completed = _complete_uploaded_input(client, store)
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"i2v_hires_profile_enabled": False}
    )

    response = client.post(
        "/api/v1/i2v/jobs",
        json={
            "input_id": completed["input_id"],
            "positive_prompt": "one controlled motion",
            "settings": {"width": 768, "height": 992},
            "batch_count": 1,
        },
        headers={"X-CSRF-Token": "development"},
    )

    assert response.status_code == 409
    assert "coordinated worker rollout" in response.json()["detail"]
    assert client.get("/api/v1/i2v/jobs").json() == []


def test_i2v_upload_completion_is_bound_to_issuing_operator_metadata(
    client: TestClient,
) -> None:
    store = MemoryObjectStore(bucket="i2v-tests")
    client.app.state.object_store = store
    intent = client.post(
        "/api/v1/i2v/inputs/uploads",
        json={"display_name": "source.png", "content_type": "image/png"},
        headers={"X-CSRF-Token": "development"},
    ).json()
    fields = intent["fields"]
    metadata = {
        name.removeprefix("x-amz-meta-"): value
        for name, value in fields.items()
        if name.startswith("x-amz-meta-")
    }
    metadata["actor-user-id"] = "00000000-0000-0000-0000-000000000001"
    store.put_for_test(fields["key"], _png_bytes(), content_type="image/png", metadata=metadata)

    response = client.post(
        f"/api/v1/i2v/inputs/uploads/{intent['upload_id']}:complete",
        json={"display_name": "source.png"},
        headers={"X-CSRF-Token": "development"},
    )
    assert response.status_code == 409
    assert "identity" in response.json()["detail"]
