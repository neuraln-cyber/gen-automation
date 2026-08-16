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
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"i2v_lora_profile_enabled": True}
    )
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
                "runpod_authorization": "written_permission",
                "loras": [
                    {
                        "catalog_id": "wan-general-nsfw-v0.08a",
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
    assert jobs[0]["settings_snapshot"]["runpod_authorization"] == "written_permission"

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


def test_i2v_reviewed_lora_catalog_is_closed_and_profile_gated(client: TestClient) -> None:
    disabled = client.get("/api/v1/i2v/loras")

    assert disabled.status_code == 200
    payload = disabled.json()
    assert payload["profile_enabled"] is False
    assert payload["maximum_selections"] == 3
    assert len(payload["loras"]) == 5
    assert all(item["available"] is False for item in payload["loras"])
    assert ".safetensors" not in disabled.text

    client.app.state.settings = client.app.state.settings.model_copy(
        update={"i2v_lora_profile_enabled": True}
    )
    enabled = client.get("/api/v1/i2v/loras").json()
    assert enabled["maximum_selections"] == 3
    assert "Select up to 3" in enabled["message"]
    by_id = {item["catalog_id"]: item for item in enabled["loras"]}
    assert all(item["available"] is True for item in enabled["loras"])
    assert by_id["wan-general-nsfw-v0.08a"]["automatic_trigger_words"] == ["nsfwsks"]
    assert by_id["dr34ml4y-aio-nsfw-wan22-v2"]["automatic_trigger_words"] == []
    assert len(by_id["dr34ml4y-aio-nsfw-wan22-v2"]["trigger_words"]) == 5
    assert by_id["smoothmix-xxx-animations-wan22"]["trigger_words"] == []
    assert by_id["bouncing-boobs-wan22"]["recommended_initial_strength"] == 1.0
    assert by_id["m4crom4sti4-natural-breasts-k3nk"]["recommended_initial_strength"] == 0.5
    assert by_id["dr34ml4y-aio-nsfw-wan22-v2"]["recommended_initial_strength"] == 0.7
    assert by_id["smoothmix-xxx-animations-wan22"]["recommended_initial_strength"] == 1.0
    assert by_id["m4crom4sti4-natural-breasts-k3nk"]["commercial_use"] == []
    assert by_id["bouncing-boobs-wan22"]["creator_name"] == "ai_build_art"
    assert by_id["bouncing-boobs-wan22"]["canonical_source_url"] == (
        "https://civitai.com/models/1343431"
    )
    assert by_id["bouncing-boobs-wan22"]["canonical_version_urls"] == [
        "https://civitai.com/models/1343431?modelVersionId=2191217",
        "https://civitai.com/models/1343431?modelVersionId=2191270",
    ]


def test_i2v_lora_gate_rejects_direct_and_preset_derived_selections(
    client: TestClient,
) -> None:
    _seed_development_owner(client)
    store = MemoryObjectStore(bucket="i2v-lora-gate-tests")
    client.app.state.object_store = store
    completed = _complete_uploaded_input(client, store)
    headers = {"X-CSRF-Token": "development"}

    rejected = client.post(
        "/api/v1/i2v/jobs",
        json={
            "input_id": completed["input_id"],
            "positive_prompt": "controlled motion",
            "settings": {"loras": [{"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3}]},
        },
        headers=headers,
    )
    assert rejected.status_code == 409

    arbitrary = client.post(
        "/api/v1/i2v/presets",
        json={
            "name": "unsafe filenames",
            "settings": {
                "loras": [
                    {"high": "arbitrary-high.safetensors", "low": "arbitrary-low.safetensors"}
                ]
            },
        },
        headers=headers,
    )
    assert arbitrary.status_code == 422

    client.app.state.settings = client.app.state.settings.model_copy(
        update={"i2v_lora_profile_enabled": True}
    )
    preset = client.post(
        "/api/v1/i2v/presets",
        json={
            "name": "LoRA rollout preset",
            "positive_prompt": "controlled motion",
            "settings": {"loras": [{"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3}]},
        },
        headers=headers,
    ).json()
    lora_job = client.post(
        "/api/v1/i2v/jobs",
        json={"input_id": completed["input_id"], "preset_id": preset["preset_id"]},
        headers=headers,
    ).json()["jobs"][0]
    assert lora_job["settings_snapshot"]["loras"] == [
        {"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3}
    ]
    updated_preset = client.put(
        f"/api/v1/i2v/presets/{preset['preset_id']}",
        json={
            "name": preset["name"],
            "positive_prompt": preset["positive_prompt"],
            "expected_lock_version": preset["lock_version"],
            "settings": {"loras": [{"catalog_id": "bouncing-boobs-wan22", "strength": 0.6}]},
        },
        headers=headers,
    ).json()
    assert updated_preset["settings"]["loras"] == [
        {"catalog_id": "bouncing-boobs-wan22", "strength": 0.6}
    ]
    frozen_job = next(
        item
        for item in client.get("/api/v1/i2v/jobs").json()
        if item["job_id"] == lora_job["job_id"]
    )
    assert frozen_job["settings_snapshot"]["loras"] == [
        {"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3}
    ]
    client.post(f"/api/v1/i2v/jobs/{lora_job['job_id']}:cancel", headers=headers)
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"i2v_lora_profile_enabled": False}
    )

    retry = client.post(f"/api/v1/i2v/jobs/{lora_job['job_id']}:retry", headers=headers)
    assert retry.status_code == 409

    stale = client.post(
        "/api/v1/i2v/jobs",
        json={"input_id": completed["input_id"], "preset_id": preset["preset_id"]},
        headers=headers,
    )
    assert stale.status_code == 409
    baseline_override = client.post(
        "/api/v1/i2v/jobs",
        json={
            "input_id": completed["input_id"],
            "preset_id": preset["preset_id"],
            "settings": {"loras": []},
        },
        headers=headers,
    )
    assert baseline_override.status_code == 201
    assert baseline_override.json()["jobs"][0]["settings_snapshot"]["loras"] == []


def test_i2v_dream_lora_rejects_mutually_exclusive_terms_before_queue_write(
    client: TestClient,
) -> None:
    _seed_development_owner(client)
    store = MemoryObjectStore(bucket="i2v-dream-prompt-tests")
    client.app.state.object_store = store
    completed = _complete_uploaded_input(client, store)
    headers = {"X-CSRF-Token": "development"}
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"i2v_lora_profile_enabled": True}
    )
    dream_settings = {"loras": [{"catalog_id": "dr34ml4y-aio-nsfw-wan22-v2", "strength": 0.7}]}

    direct = client.post(
        "/api/v1/i2v/jobs",
        json={
            "input_id": completed["input_id"],
            "positive_prompt": "m15510n4ry then bl0wj0b",
            "settings": dream_settings,
        },
        headers=headers,
    )
    assert direct.status_code == 422
    assert "mutually exclusive" in direct.json()["detail"]
    assert client.get("/api/v1/i2v/jobs").json() == []

    preset_rejected = client.post(
        "/api/v1/i2v/presets",
        json={
            "name": "conflicting Dream preset",
            "positive_prompt": "c0wg1rl and d0gg1e",
            "settings": dream_settings,
        },
        headers=headers,
    )
    assert preset_rejected.status_code == 422

    preset = client.post(
        "/api/v1/i2v/presets",
        json={
            "name": "historical Dream preset",
            "positive_prompt": "m15510n4ry",
            "settings": dream_settings,
        },
        headers=headers,
    ).json()

    async def make_historical_prompt_invalid() -> None:
        from gen_automation.db.models import I2VPreset

        async with client.app.state.database.sessions() as session:
            record = await session.get(I2VPreset, UUID(preset["preset_id"]))
            assert record is not None
            record.positive_prompt = "m15510n4ry and bl0wj0b"
            await session.commit()

    assert client.portal is not None
    client.portal.call(make_historical_prompt_invalid)
    derived = client.post(
        "/api/v1/i2v/jobs",
        json={"input_id": completed["input_id"], "preset_id": preset["preset_id"]},
        headers=headers,
    )
    assert derived.status_code == 422
    assert client.get("/api/v1/i2v/jobs").json() == []


def test_i2v_enqueue_rejects_legacy_preset_lora_shape_with_public_gate_enabled(
    client: TestClient,
) -> None:
    _seed_development_owner(client)
    store = MemoryObjectStore(bucket="i2v-legacy-preset-tests")
    client.app.state.object_store = store
    completed = _complete_uploaded_input(client, store)
    headers = {"X-CSRF-Token": "development"}
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"i2v_lora_profile_enabled": True}
    )
    preset = client.post(
        "/api/v1/i2v/presets",
        json={"name": "temporary baseline", "positive_prompt": "controlled motion"},
        headers=headers,
    ).json()

    async def install_legacy_shape() -> None:
        from gen_automation.db.models import I2VPreset

        async with client.app.state.database.sessions() as session:
            record = await session.get(I2VPreset, UUID(preset["preset_id"]))
            assert record is not None
            record.settings = {
                "loras": [
                    {
                        "high": "arbitrary-high.safetensors",
                        "low": "arbitrary-low.safetensors",
                        "strength": 1.0,
                    }
                ]
            }
            await session.commit()

    assert client.portal is not None
    client.portal.call(install_legacy_shape)
    rejected = client.post(
        "/api/v1/i2v/jobs",
        json={"input_id": completed["input_id"], "preset_id": preset["preset_id"]},
        headers=headers,
    )
    assert rejected.status_code == 422
    assert "contain only catalog_id and strength" in rejected.json()["detail"]
    assert client.get("/api/v1/i2v/jobs").json() == []

    baseline = client.post(
        "/api/v1/i2v/jobs",
        json={
            "input_id": completed["input_id"],
            "preset_id": preset["preset_id"],
            "settings": {"loras": []},
        },
        headers=headers,
    )
    assert baseline.status_code == 201


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


def test_i2v_rollout_freeze_blocks_reorder_and_retry_but_allows_cancel(
    client: TestClient,
) -> None:
    _seed_development_owner(client)
    store = MemoryObjectStore(bucket="i2v-rollout-freeze-tests")
    client.app.state.object_store = store
    completed = _complete_uploaded_input(client, store)
    headers = {"X-CSRF-Token": "development"}
    created = client.post(
        "/api/v1/i2v/jobs",
        json={
            "input_id": completed["input_id"],
            "positive_prompt": "one controlled motion",
            "batch_count": 3,
        },
        headers=headers,
    ).json()["jobs"]
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"i2v_hires_profile_enabled": False}
    )

    cancelled = client.post(
        f"/api/v1/i2v/jobs/{created[2]['job_id']}:cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    frozen_snapshot = client.get("/api/v1/i2v/jobs").json()

    reorder = client.patch(
        "/api/v1/i2v/queue",
        json={
            "job_id": created[1]["job_id"],
            "before_job_id": created[0]["job_id"],
        },
        headers=headers,
    )
    retry = client.post(
        f"/api/v1/i2v/jobs/{created[2]['job_id']}:retry",
        headers=headers,
    )

    assert reorder.status_code == 409
    assert retry.status_code == 409
    assert "queue writes are paused" in reorder.json()["detail"]
    assert "queue writes are paused" in retry.json()["detail"]
    assert client.get("/api/v1/i2v/jobs").json() == frozen_snapshot


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
