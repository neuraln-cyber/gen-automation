from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID

from fastapi.testclient import TestClient
from PIL import Image

from gen_automation.db.models import AdminUser
from gen_automation.domain.enums import AdminRole
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


def test_i2v_worker_endpoint_never_invents_progress(client: TestClient) -> None:
    response = client.get("/api/v1/i2v/worker")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status_available"] is False
    assert payload["deployment"] is None
    assert "not configured" in payload["message"]


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
