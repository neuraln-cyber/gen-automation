import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr

from gen_automation.db.models import AdminUser, LoraImportJob
from gen_automation.domain.enums import AdminRole
from gen_automation.integrations.civitai.client import CivitaiClient
from gen_automation.integrations.civitai.models import (
    CivitaiLicenseTerms,
    CivitaiLoraVersionChoice,
    CivitaiLoraVersionListing,
    CivitaiSourceRef,
)
from gen_automation.storage.memory import MemoryObjectStore
from gen_automation.storage.model_artifacts import (
    QUARANTINE_CONTENT_TYPE,
    ModelArtifactStore,
)


def _safetensors_bytes() -> bytes:
    data = b"\x00\x00\x00\x00"
    header = json.dumps(
        {
            "__metadata__": {"format": "pt"},
            "lora_A.weight": {
                "dtype": "F32",
                "shape": [1],
                "data_offsets": [0, len(data)],
            },
        },
        separators=(",", ":"),
    ).encode()
    return len(header).to_bytes(8, "little") + header + data


class _NoncommercialVersionListingClient(CivitaiClient):
    def __init__(self) -> None:
        pass

    async def list_lora_versions(
        self,
        source: str | CivitaiSourceRef,
        *,
        allow_commercial_use_override: bool = False,
    ) -> CivitaiLoraVersionListing:
        assert isinstance(source, CivitaiSourceRef)
        assert source.model_id == 123
        assert allow_commercial_use_override is True
        return CivitaiLoraVersionListing(
            versions=(
                CivitaiLoraVersionChoice(
                    version_id=456,
                    name="Reviewed version",
                    base_model="Illustrious",
                    target_filename="reviewed.safetensors",
                    declared_size_bytes=1024,
                    sha256="b" * 64,
                ),
            ),
            license_terms=CivitaiLicenseTerms(
                allow_no_credit=False,
                commercial_use=("RentCivit",),
                allow_derivatives=True,
                allow_different_license=False,
            ),
        )


def test_lora_routes_fail_closed_when_feature_is_disabled(client: TestClient) -> None:
    response = client.get("/api/v1/loras")
    assert response.status_code == 503
    assert response.json()["detail"] == "LoRA management is not enabled"


def test_lora_dashboard_csp_allows_the_manual_upload_bucket(client: TestClient) -> None:
    bucket = "test-lora-browser-upload-bucket"
    client.app.state.settings.lora_manager_enabled = True
    client.app.state.settings.salad_worker_artifact_bucket = SecretStr(bucket)
    client.app.state.settings.salad_worker_artifact_region = SecretStr("eu-central-1")
    database = client.app.state.database
    assert client.portal is not None

    async def seed_development_owner() -> None:
        async with database.sessions() as session:
            if await session.get(AdminUser, UUID(int=0)) is None:
                session.add(
                    AdminUser(
                        id=UUID(int=0),
                        username_normalized="local-developer",
                        display_name="Local Developer",
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

    client.portal.call(seed_development_owner)

    response = client.get("/dashboard/loras")

    assert response.status_code == 200
    connect_sources = (
        response.headers["content-security-policy"]
        .split("connect-src ", maxsplit=1)[1]
        .split(";", maxsplit=1)[0]
        .split()
    )
    assert f"https://{bucket}.s3.eu-central-1.amazonaws.com" in connect_sources

    non_lora_response = client.get("/dashboard")
    non_lora_connect_sources = (
        non_lora_response.headers["content-security-policy"]
        .split("connect-src ", maxsplit=1)[1]
        .split(";", maxsplit=1)[0]
        .split()
    )
    assert f"https://{bucket}.s3.eu-central-1.amazonaws.com" not in non_lora_connect_sources


def test_manual_route_creates_idempotent_grant_and_freezes_exact_upload(
    client: TestClient,
) -> None:
    bucket = "test-lora-route-bucket"
    memory = MemoryObjectStore(bucket=bucket)
    client.app.state.settings.lora_manager_enabled = True
    client.app.state.settings.salad_worker_artifact_bucket = SecretStr(bucket)
    client.app.state.model_artifact_store = ModelArtifactStore(memory)
    client.app.state.civitai_client = cast(CivitaiClient, object())
    database = client.app.state.database
    assert client.portal is not None

    async def seed_development_owner() -> None:
        async with database.sessions() as session:
            if await session.get(AdminUser, UUID(int=0)) is None:
                session.add(
                    AdminUser(
                        id=UUID(int=0),
                        username_normalized="local-developer",
                        display_name="Local Developer",
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

    client.portal.call(seed_development_owner)
    body = _safetensors_bytes()
    command = {
        "display_name": "Route upload",
        "canonical_source_url": "https://models.example.test/route-upload",
        "license_url": "https://models.example.test/route-upload/license",
        "commercial_use_attested": True,
        "adult_use_attested": True,
        "target_filename": "route-upload.safetensors",
        "expected_sha256": hashlib.sha256(body).hexdigest(),
        "expected_byte_size": len(body),
        "expected_metadata": {},
        "trigger_words": ["route style"],
    }
    headers = {"Idempotency-Key": "route-manual-create"}
    created = client.post("/api/v1/loras/imports/manual", json=command, headers=headers)
    replay = client.post("/api/v1/loras/imports/manual", json=command, headers=headers)
    assert created.status_code == 201
    assert replay.status_code == 201
    assert created.headers["idempotency-replayed"] == "false"
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json()["import"]["id"] == created.json()["import"]["id"]
    assert created.json()["upload"]["method"] == "POST"

    job_id = UUID(created.json()["import"]["id"])
    staging_key = f"onboarding/loras/{job_id}/source.safetensors"
    memory.put_for_test(
        staging_key,
        body,
        content_type=QUARANTINE_CONTENT_TYPE,
        metadata={"upload-id": str(job_id)},
    )

    async def uploaded_identity() -> tuple[str, str]:
        metadata = await memory.head(staging_key)
        assert metadata is not None
        assert metadata.version_id is not None
        assert metadata.etag is not None
        return metadata.version_id, metadata.etag

    version_id, etag = client.portal.call(uploaded_identity)
    completed = client.post(
        f"/api/v1/loras/imports/{job_id}:complete",
        json={
            "object_version_id": version_id,
            "object_etag": etag,
            "byte_size": len(body),
        },
        headers={"Idempotency-Key": "route-manual-complete"},
    )
    assert completed.status_code == 200
    assert completed.json()["import"]["status"] == "queued"
    library = client.get("/api/v1/loras")
    assert library.status_code == 200
    assert len(library.json()["imports"]) == 1


def test_civitai_route_rejects_an_invalid_url_as_input(client: TestClient) -> None:
    client.app.state.settings.lora_manager_enabled = True
    client.app.state.civitai_client = cast(CivitaiClient, object())
    response = client.post(
        "/api/v1/loras/civitai:resolve",
        json={"url": "https://attacker.example.test/models/1"},
    )
    assert response.status_code == 422


def test_model_only_civitai_override_reports_provider_terms_truthfully(
    client: TestClient,
) -> None:
    client.app.state.settings.lora_manager_enabled = True
    client.app.state.civitai_client = _NoncommercialVersionListingClient()

    response = client.post(
        "/api/v1/loras/civitai:resolve",
        json={
            "url": "https://civitai.com/models/123",
            "commercial_use_override_attested": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["commercial_image_allowed"] is False
    assert response.json()["commercial_use_override_applied"] is True
    assert response.json()["provider_commercial_use"] == ["RentCivit"]
    assert response.json()["versions"][0]["version_id"] == 456


def test_civitai_create_derives_provenance_and_replays_without_provider(
    client: TestClient,
) -> None:
    client.app.state.settings.lora_manager_enabled = True
    database = client.app.state.database
    assert client.portal is not None

    async def seed_owner() -> None:
        async with database.sessions() as session:
            if await session.get(AdminUser, UUID(int=0)) is None:
                session.add(
                    AdminUser(
                        id=UUID(int=0),
                        username_normalized="local-developer",
                        display_name="Local Developer",
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

    client.portal.call(seed_owner)
    command = {
        "display_name": "Resolved Civitai LoRA",
        "canonical_source_url": "https://www.civitai.com/models/999",
        "license_url": "https://attacker.example.test/unrelated-license",
        "commercial_use_attested": True,
        "adult_use_attested": True,
        "target_filename": "resolved-lora.safetensors",
        "expected_sha256": "a" * 64,
        "expected_byte_size": 1_024,
        "expected_metadata": {},
        "trigger_words": ["resolved style"],
        "civitai_model_id": 123,
        "civitai_version_id": 456,
        "civitai_file_id": 789,
    }
    headers = {"Idempotency-Key": "route-civitai-create"}
    created = client.post("/api/v1/loras/imports/civitai", json=command, headers=headers)
    assert created.status_code == 201
    assert created.headers["idempotency-replayed"] == "false"

    # A lost response must remain replayable even while provider metadata is
    # unavailable; the background import owns the re-resolution step.
    client.app.state.civitai_client = None
    replay = client.post("/api/v1/loras/imports/civitai", json=command, headers=headers)
    assert replay.status_code == 201
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json()["import"]["id"] == created.json()["import"]["id"]

    async def persisted_sources() -> tuple[str, str]:
        async with database.sessions() as session:
            job = await session.get(LoraImportJob, UUID(created.json()["import"]["id"]))
            assert job is not None
            return job.canonical_source_url, job.license_url

    assert client.portal.call(persisted_sources) == (
        "https://civitai.com/models/123?modelVersionId=456",
        "https://civitai.com/models/123?modelVersionId=456",
    )
