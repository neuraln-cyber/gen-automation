import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

from argon2 import PasswordHasher, Type
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from gen_automation.app import create_app
from gen_automation.auth.security import PasswordManager
from gen_automation.config import Environment, Settings
from gen_automation.db.models import AdminSession, AdminUser, LoraImportJob
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole
from gen_automation.integrations.civitai.client import CivitaiClient
from gen_automation.integrations.civitai.models import (
    CivitaiLoraVersionChoice,
    CivitaiSourceRef,
)
from gen_automation.storage.memory import MemoryObjectStore
from gen_automation.storage.model_artifacts import (
    QUARANTINE_CONTENT_TYPE,
    ModelArtifactStore,
)

_AUTH_ORIGIN = "http://testserver"
_AUTH_PASSWORD = "a long unique LoRA manager test password"  # noqa: S105
_AUTH_SESSION_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
_AUTH_TOTP_KEY = base64.urlsafe_b64encode(bytes(range(32, 64))).rstrip(b"=").decode("ascii")


def _auth_settings(database_path: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        public_base_url=_AUTH_ORIGIN,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        auth_enabled=True,
        auth_require_totp=False,
        auth_recent_auth_seconds=60,
        session_secret=_AUTH_SESSION_KEY,
        auth_totp_active_key_id="key-1",
        auth_totp_encryption_keys={"key-1": _AUTH_TOTP_KEY},
    )


async def _prepare_authenticated_owner(settings: Settings) -> None:
    database = Database(settings.database_url)
    try:
        await database.create_schema()
        password_manager = PasswordManager(
            PasswordHasher(
                time_cost=1,
                memory_cost=8192,
                parallelism=1,
                hash_len=16,
                salt_len=16,
                type=Type.ID,
            )
        )
        now = datetime.now(UTC)
        async with database.sessions() as session:
            session.add(
                AdminUser(
                    username_normalized="owner@example.test",
                    display_name="Owner",
                    password_hash=password_manager.hash(_AUTH_PASSWORD),
                    role=AdminRole.OWNER,
                    is_active=True,
                    failed_login_count=0,
                    password_changed_at=now,
                    credential_version=1,
                    lock_version=1,
                )
            )
            await session.commit()
    finally:
        await database.dispose()


class _VersionListingCivitaiClient(CivitaiClient):
    def __init__(self) -> None:
        pass

    async def list_lora_versions(
        self,
        source: str | CivitaiSourceRef,
    ) -> tuple[CivitaiLoraVersionChoice, ...]:
        del source
        return (
            CivitaiLoraVersionChoice(
                version_id=2372164,
                name="Illustrious",
                base_model="Illustrious",
                target_filename="disgusted-face.safetensors",
                declared_size_bytes=1024,
                sha256="a" * 64,
            ),
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


def test_lora_routes_fail_closed_when_feature_is_disabled(client: TestClient) -> None:
    response = client.get("/api/v1/loras")
    assert response.status_code == 503
    assert response.json()["detail"] == "LoRA management is not enabled"


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


def test_lora_onboarding_accepts_stale_session_with_csrf_and_keeps_retirement_recent(
    tmp_path: Path,
) -> None:
    settings = _auth_settings(tmp_path / "lora-onboarding-auth.db")
    asyncio.run(_prepare_authenticated_owner(settings))
    bucket = "authenticated-lora-route-bucket"
    memory = MemoryObjectStore(bucket=bucket)
    body = _safetensors_bytes()

    with TestClient(
        create_app(settings),
        base_url=_AUTH_ORIGIN,
        client=("192.0.2.50", 50000),
    ) as client:
        client.app.state.settings.lora_manager_enabled = True
        client.app.state.settings.salad_worker_artifact_bucket = SecretStr(bucket)
        client.app.state.model_artifact_store = ModelArtifactStore(memory)
        client.app.state.civitai_client = _VersionListingCivitaiClient()
        login = client.post(
            "/api/v1/auth/login",
            json={
                "username": "owner@example.test",
                "password": _AUTH_PASSWORD,
            },
            headers={"Origin": _AUTH_ORIGIN, "Sec-Fetch-Site": "same-origin"},
        )
        assert login.status_code == 200
        csrf = client.cookies.get(settings.auth_csrf_cookie_name)
        assert csrf is not None
        assert client.portal is not None

        async def make_session_stale() -> None:
            async with client.app.state.database.sessions() as session:
                browser_session = await session.scalar(select(AdminSession))
                assert browser_session is not None
                old = datetime.now(UTC) - timedelta(minutes=2)
                browser_session.created_at = old
                browser_session.reauthenticated_at = old
                await session.commit()

        client.portal.call(make_session_stale)
        manual_command = {
            "display_name": "Stale-session upload",
            "canonical_source_url": "https://models.example.test/stale-upload",
            "license_url": "https://models.example.test/stale-upload/license",
            "commercial_use_attested": True,
            "adult_use_attested": True,
            "target_filename": "stale-upload.safetensors",
            "expected_sha256": hashlib.sha256(body).hexdigest(),
            "expected_byte_size": len(body),
            "expected_metadata": {},
            "trigger_words": ["stale session style"],
        }
        same_origin = {
            "Origin": _AUTH_ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": csrf,
        }
        missing_csrf = client.post(
            "/api/v1/loras/imports/manual",
            json=manual_command,
            headers={
                "Origin": _AUTH_ORIGIN,
                "Sec-Fetch-Site": "same-origin",
                "Idempotency-Key": "manual-missing-csrf",
            },
        )
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["detail"] == "CSRF validation failed"
        wrong_origin = client.post(
            "/api/v1/loras/imports/manual",
            json=manual_command,
            headers={
                **same_origin,
                "Origin": "https://attacker.example.test",
                "Sec-Fetch-Site": "cross-site",
                "Idempotency-Key": "manual-wrong-origin",
            },
        )
        assert wrong_origin.status_code == 403
        assert wrong_origin.json()["detail"] == "request origin is not allowed"

        created = client.post(
            "/api/v1/loras/imports/manual",
            json=manual_command,
            headers={**same_origin, "Idempotency-Key": "stale-manual-create"},
        )
        assert created.status_code == 201
        assert created.json()["import"]["status"] == "awaiting_upload"
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
            headers={**same_origin, "Idempotency-Key": "stale-manual-complete"},
        )
        assert completed.status_code == 200
        assert completed.json()["import"]["status"] == "queued"

        resolved = client.post(
            "/api/v1/loras/civitai:resolve",
            json={"url": "https://civitai.com/models/196908/disgusted-face"},
            headers=same_origin,
        )
        assert resolved.status_code == 200
        assert resolved.json()["versions"][0]["version_id"] == 2372164
        civitai_command = {
            "display_name": "Stale-session Civitai import",
            "canonical_source_url": "https://civitai.com/models/196908",
            "license_url": "https://civitai.com/models/196908",
            "commercial_use_attested": True,
            "adult_use_attested": True,
            "target_filename": "disgusted-face.safetensors",
            "expected_sha256": "a" * 64,
            "expected_byte_size": 1024,
            "expected_metadata": {},
            "trigger_words": ["disgusted face"],
            "civitai_model_id": 196908,
            "civitai_version_id": 2372164,
            "civitai_file_id": 1,
        }
        civitai_created = client.post(
            "/api/v1/loras/imports/civitai",
            json=civitai_command,
            headers={**same_origin, "Idempotency-Key": "stale-civitai-create"},
        )
        assert civitai_created.status_code == 201
        assert civitai_created.json()["import"]["status"] == "queued"
        civitai_job_id = civitai_created.json()["import"]["id"]
        cancelled = client.post(
            f"/api/v1/loras/imports/{civitai_job_id}:cancel",
            json={},
            headers={**same_origin, "Idempotency-Key": "stale-civitai-cancel"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["import"]["status"] == "cancelled"

        retirement = client.post(
            f"/api/v1/loras/{UUID(int=1)}:retire",
            json={},
            headers={**same_origin, "Idempotency-Key": "stale-retire"},
        )
        assert retirement.status_code == 401
        assert retirement.json()["detail"] == "recent authentication required"


def test_civitai_route_rejects_an_invalid_url_as_input(client: TestClient) -> None:
    client.app.state.settings.lora_manager_enabled = True
    client.app.state.civitai_client = cast(CivitaiClient, object())
    response = client.post(
        "/api/v1/loras/civitai:resolve",
        json={"url": "https://attacker.example.test/models/1"},
    )
    assert response.status_code == 422


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
