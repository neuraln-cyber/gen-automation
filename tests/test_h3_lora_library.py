"""File-management checks only: synthetic bytes in memory, no GPU or real uploads."""

import hashlib
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from gen_automation.db.models import (
    AdminUser,
    I2VInput,
    I2VJob,
    LoraImportJob,
    ManagedLoraArtifact,
    ModelArtifactApproval,
)
from gen_automation.domain.enums import AdminRole, ModelArtifactFamily
from gen_automation.gpu_worker.artifacts import (
    ArtifactKind,
    ModelArtifactSpec,
    create_artifact_manifest,
)
from gen_automation.integrations.civitai.client import CivitaiClient
from gen_automation.services.lora_runtime import LoraRuntime
from gen_automation.services.managed_artifact_manifest import (
    ManagedArtifactManifestError,
    build_effective_artifact_manifest,
)
from gen_automation.storage.memory import MemoryObjectStore
from gen_automation.storage.model_artifacts import ModelArtifactStore


def _setup(client: TestClient) -> tuple[MemoryObjectStore, LoraRuntime]:
    settings = client.app.state.settings
    settings.lora_manager_enabled = True
    settings.salad_worker_artifact_bucket = SecretStr("h3-test-models")
    settings.salad_worker_artifact_region = SecretStr("eu-central-1")
    memory = MemoryObjectStore(bucket="h3-test-models")
    store = ModelArtifactStore(memory)
    client.app.state.model_artifact_store = store
    database = client.app.state.database

    async def seed() -> None:
        async with database.sessions() as session:
            session.add(
                AdminUser(
                    id=UUID(int=0),
                    username_normalized="local-developer",
                    display_name="Owner",
                    password_hash="disabled-test-password-hash",  # noqa: S106
                    role=AdminRole.OWNER,
                    is_active=True,
                    failed_login_count=0,
                    password_changed_at=datetime.now(UTC),
                    credential_version=1,
                    lock_version=1,
                )
            )
            await session.commit()

    assert client.portal is not None
    client.portal.call(seed)
    return memory, LoraRuntime(
        settings=settings,
        sessions=database.sessions,
        store=store,
        civitai=cast(CivitaiClient, object()),
        worker_id="h3-library-test",
    )


def _body() -> bytes:
    header = b'{"lora_A.weight":{"dtype":"F32","shape":[1],"data_offsets":[0,4]}}'
    return len(header).to_bytes(8, "little") + header + b"\0\0\0\0"


def _create(client: TestClient, *, family: str = "minimax_h3", key: str = "upload") -> str:
    body = _body()
    result = client.post(
        "/api/v1/loras/imports/manual",
        headers={"Idempotency-Key": key},
        json={
            "display_name": "H3 test file",
            "model_family": family,
            "canonical_source_url": "https://example.test/model",
            "license_url": "https://example.test/license",
            "target_filename": "h3-test.safetensors",
            "expected_byte_size": len(body),
            "expected_sha256": hashlib.sha256(body).hexdigest(),
            "commercial_use_attested": True,
            "adult_use_attested": True,
        },
    )
    assert result.status_code == 201, result.text
    assert result.json()["import"]["model_family"] == family
    return str(result.json()["import"]["id"])


def _capture(client: TestClient, memory: MemoryObjectStore, identifier: str) -> None:
    memory.put_for_test(
        f"onboarding/loras/{identifier}/source.safetensors",
        _body(),
        content_type="application/octet-stream",
        metadata={
            "artifact-kind": "managed-lora",
            "format": "safetensors",
            "upload-id": identifier,
        },
    )
    captured = client.post(
        f"/api/v1/loras/imports/{identifier}:capture",
        headers={"Idempotency-Key": f"capture-{identifier}"},
    )
    assert captured.status_code == 200, captured.text


def test_h3_dashboard_separate_library_and_manual_upload(client: TestClient) -> None:
    _setup(client)
    page = client.get("/dashboard/loras?library=h3")
    assert page.status_code == 200
    assert "H3 LoRA manager" in page.text
    assert 'data-list-url="/api/v1/loras?library=h3"' in page.text
    assert 'data-return-url="/dashboard/loras?library=h3#lora-imports"' in page.text
    assert '<option value="minimax_h3">' in page.text
    assert '<option value="anima">' not in page.text
    assert "data-lora-civitai-import-form" not in page.text
    assert "file verification does not certify H3 model compatibility" in page.text
    assert (
        "https://h3-test-models.s3.eu-central-1.amazonaws.com"
        in page.headers["content-security-policy"]
    )
    image_page = client.get("/dashboard/loras")
    assert '<option value="anima">' in image_page.text
    assert '<option value="minimax_h3">' not in image_page.text
    client.app.state.settings.i2v_profile = "minimax_h3"
    assert 'href="/dashboard/loras?library=h3"' in client.get("/dashboard/animations").text


def test_h3_imports_do_not_leak_into_image_library(client: TestClient) -> None:
    _setup(client)
    identifier = _create(client)
    assert _create(client) == identifier  # Same logical upload is idempotent.
    _create(client, family="anima", key="image-upload")
    h3 = client.get("/api/v1/loras?library=h3").json()
    images = client.get("/api/v1/loras").json()
    assert [item["id"] for item in h3["imports"]] == [identifier]
    assert [item["model_family"] for item in images["imports"]] == ["anima"]
    assert client.get("/api/v1/loras?library=bogus").status_code == 422


def test_h3_upload_verify_delete_exact_version_without_worker(client: TestClient) -> None:
    memory, runtime = _setup(client)
    identifier = _create(client)
    _capture(client, memory, identifier)
    assert client.portal is not None
    assert client.portal.call(runtime.import_once)
    # No image-model manifest or provider client is configured. Registration
    # must succeed without installing H3 files in the image worker.
    assert client.portal.call(runtime.lifecycle_once)
    entries = client.get("/api/v1/loras?library=h3").json()["entries"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["model_family"] == "minimax_h3"
    assert entry["status"] == "active"
    assert client.get("/api/v1/loras").json()["entries"] == []

    async def identity() -> tuple[str, str]:
        async with client.app.state.database.sessions() as session:
            artifact = await session.get(ManagedLoraArtifact, UUID(entry["id"]))
            assert artifact is not None
            approval = await session.get(ModelArtifactApproval, artifact.approval_id)
            assert approval is not None
            assert approval.model_family == ModelArtifactFamily.MINIMAX_H3
            return artifact.object_key, artifact.object_version_id

    object_key, version = client.portal.call(identity)

    async def image_manifest_excludes_h3() -> None:
        baseline = create_artifact_manifest(
            (
                ModelArtifactSpec(
                    logical_name="image-model",
                    kind=ArtifactKind.CHECKPOINT,
                    source_object_id="models/image.safetensors",
                    source_object_version_id="v1",
                    sha256="f" * 64,
                    exact_size_bytes=100,
                    max_size_bytes=100,
                    target_filename="image.safetensors",
                ),
            )
        )
        async with client.app.state.database.sessions() as session:
            manifest = await build_effective_artifact_manifest(
                session,
                baseline=baseline,
                expected_bucket=memory.bucket,
            )
            assert len(manifest.manifest.artifacts) == 1
            with pytest.raises(ManagedArtifactManifestError, match="required LoRA"):
                await build_effective_artifact_manifest(
                    session,
                    baseline=baseline,
                    expected_bucket=memory.bucket,
                    required_lora_sha256s=[entry["sha256"]],
                )

    client.portal.call(image_manifest_excludes_h3)
    response = client.post(
        f"/api/v1/loras/{entry['id']}:retire",
        headers={"Idempotency-Key": "h3-delete"},
        json={"expected_lock_version": entry["lock_version"], "purge_requested": True},
    )
    assert response.status_code == 200, response.text
    assert client.portal.call(runtime.lifecycle_once)
    assert client.portal.call(runtime.lifecycle_once)
    assert client.get("/api/v1/loras?library=h3").json()["entries"][0]["status"] == "purged"

    async def absent() -> bool:
        return await memory.head(object_key, version_id=version) is None

    assert client.portal.call(absent)


def test_cross_family_duplicate_cannot_relabel_a_file(client: TestClient) -> None:
    memory, runtime = _setup(client)
    first = _create(client)
    _capture(client, memory, first)
    assert client.portal is not None
    assert client.portal.call(runtime.import_once)
    second = _create(client, family="anima", key="wrong-family")
    _capture(client, memory, second)
    assert client.portal.call(runtime.import_once)

    async def inspect() -> None:
        async with client.app.state.database.sessions() as session:
            job = await session.get(LoraImportJob, UUID(second))
            assert job is not None
            assert job.state == "failed"
            approvals = (await session.scalars(select(ModelArtifactApproval))).all()
            assert len(approvals) == 1
            assert approvals[0].model_family == ModelArtifactFamily.MINIMAX_H3

    client.portal.call(inspect)


def test_h3_library_fails_closed_when_disabled(client: TestClient) -> None:
    assert client.get("/api/v1/loras?library=h3").status_code == 503
    assert client.get("/dashboard/loras?library=h3").status_code == 503


def test_h3_delete_waits_for_video_reference_and_can_be_restored(client: TestClient) -> None:
    memory, runtime = _setup(client)
    identifier = _create(client)
    _capture(client, memory, identifier)
    assert client.portal is not None
    assert client.portal.call(runtime.import_once)
    assert client.portal.call(runtime.lifecycle_once)
    entry = client.get("/api/v1/loras?library=h3").json()["entries"][0]

    async def seed_reference() -> None:
        async with client.app.state.database.sessions() as session:
            source = I2VInput(
                created_by_user_id=UUID(int=0),
                source="upload",
                display_name="Local fixture",
                storage_backend="s3",
                storage_bucket="fixture",
                object_key="fixture.png",
                sha256="b" * 64,
                content_type="image/png",
                width=32,
                height=32,
                byte_size=100,
            )
            session.add(source)
            await session.flush()
            session.add(
                I2VJob(
                    created_by_user_id=UUID(int=0),
                    input_id=source.id,
                    positive_prompt="",
                    negative_prompt="",
                    input_snapshot={},
                    preset_snapshot={},
                    settings_snapshot={"h3_loras": [{"sha256": entry["sha256"]}]},
                    request_sha256="c" * 64,
                    state="queued",
                    queue_position=1,
                )
            )
            await session.commit()

    client.portal.call(seed_reference)
    deleted = client.post(
        f"/api/v1/loras/{entry['id']}:retire",
        headers={"Idempotency-Key": "h3-delete-pending"},
        json={"purge_requested": True},
    )
    assert deleted.status_code == 200, deleted.text
    assert not client.portal.call(runtime.lifecycle_once)
    pending = client.get("/api/v1/loras?library=h3").json()["entries"][0]
    assert pending["status"] == "retiring"
    assert pending["size_bytes"] > 0
    restored = client.post(
        f"/api/v1/loras/{entry['id']}:restore",
        headers={"Idempotency-Key": "h3-restore-pending"},
        json={},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["entry"]["status"] == "active"
