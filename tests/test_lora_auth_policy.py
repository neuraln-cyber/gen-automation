import asyncio
import base64
import hashlib
import hmac
import json
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from argon2 import PasswordHasher, Type
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from gen_automation.app import create_app
from gen_automation.auth.security import (
    PasswordManager,
    TotpSecretCipher,
    generate_totp_secret,
)
from gen_automation.config import Environment, Settings
from gen_automation.db.models import AdminSession, AdminUser
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole
from gen_automation.integrations.civitai import CivitaiClient
from gen_automation.integrations.civitai.models import (
    CivitaiFileScan,
    CivitaiLicenseTerms,
    CivitaiModelType,
    CivitaiResolvedLora,
    CivitaiSourceRef,
)
from gen_automation.storage.memory import MemoryObjectStore
from gen_automation.storage.model_artifacts import (
    QUARANTINE_CONTENT_TYPE,
    ModelArtifactStore,
)

SESSION_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
TOTP_KEY = base64.urlsafe_b64encode(bytes(range(32, 64))).rstrip(b"=").decode("ascii")
PASSWORD = "a long unique LoRA policy test password"  # noqa: S105
ORIGIN = "http://testserver"


def _settings(database_path: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        public_base_url=ORIGIN,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        auth_enabled=True,
        auth_require_totp=True,
        session_secret=SESSION_KEY,
        auth_totp_active_key_id="key-1",
        auth_totp_encryption_keys={"key-1": TOTP_KEY},
    )


def _password_manager() -> PasswordManager:
    return PasswordManager(
        PasswordHasher(
            time_cost=1,
            memory_cost=8192,
            parallelism=1,
            hash_len=16,
            salt_len=16,
            type=Type.ID,
        )
    )


def _totp_code(secret: str, unix_time: int) -> str:
    counter = unix_time // 30
    digest = hmac.new(
        base64.b32decode(secret),
        struct.pack(">Q", counter),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 15
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


async def _prepare_owner(settings: Settings) -> str:
    database = Database(settings.database_url)
    try:
        await database.create_schema()
        secret = generate_totp_secret()
        now = datetime.now(UTC)
        async with database.sessions() as session:
            owner = AdminUser(
                username_normalized="owner@example.test",
                display_name="Owner",
                password_hash=_password_manager().hash(PASSWORD),
                role=AdminRole.OWNER,
                is_active=True,
                failed_login_count=0,
                password_changed_at=now,
                credential_version=1,
                lock_version=1,
            )
            session.add(owner)
            await session.flush()
            owner.totp_secret_ciphertext = TotpSecretCipher(
                {"key-1": TOTP_KEY},
                active_key_id="key-1",
            ).encrypt(secret, subject=f"admin-user:{owner.id}")
            owner.totp_confirmed_at = now
            await session.commit()
        return secret
    finally:
        await database.dispose()


def _login(client: TestClient, owner_secret: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": "owner@example.test",
            "password": PASSWORD,
            "totp_code": _totp_code(owner_secret, int(datetime.now(UTC).timestamp())),
        },
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200
    csrf = client.cookies.get("gen_csrf")
    assert csrf is not None
    return csrf


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


class _ResolvedCivitaiClient(CivitaiClient):
    def __init__(self) -> None:
        # This typed test double deliberately avoids constructing a network client.
        pass

    async def resolve_lora(
        self,
        source: str | CivitaiSourceRef,
        *,
        version_id: int | None = None,
    ) -> CivitaiResolvedLora:
        assert isinstance(source, CivitaiSourceRef)
        assert source.model_id == 196908
        assert (version_id or source.version_id) == 2372164
        return CivitaiResolvedLora(
            model_id=196908,
            version_id=2372164,
            file_id=7654321,
            model_type=CivitaiModelType.LORA,
            model_name="Disgusted Face",
            version_name="Test version",
            target_filename="disgusted-face.safetensors",
            canonical_source_url=("https://civitai.com/models/196908?modelVersionId=2372164"),
            creator="Goofy AI",
            base_model="Illustrious",
            trained_words=("disgusted face",),
            declared_size_bytes=1024,
            sha256="b" * 64,
            scan=CivitaiFileScan(pickle_result="Success", virus_result="Success"),
            license_terms=CivitaiLicenseTerms(
                allow_no_credit=True,
                commercial_use=("Image",),
                allow_derivatives=True,
                allow_different_license=True,
            ),
            nsfw=False,
            nsfw_level=0,
            _download_url="https://civitai.com/api/download/models/2372164",
        )


def test_lora_onboarding_accepts_stale_recent_auth_but_keeps_csrf_and_retire_gate(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "lora-auth-policy.db")
    owner_secret = asyncio.run(_prepare_owner(settings))
    bucket = "test-lora-auth-policy-bucket"
    memory = MemoryObjectStore(bucket=bucket)

    with TestClient(
        create_app(settings),
        base_url=ORIGIN,
        client=("192.0.2.31", 50000),
    ) as client:
        client.app.state.settings.lora_manager_enabled = True
        client.app.state.settings.salad_worker_artifact_bucket = SecretStr(bucket)
        client.app.state.model_artifact_store = ModelArtifactStore(memory)
        client.app.state.civitai_client = _ResolvedCivitaiClient()
        csrf = _login(client, owner_secret)

        async def make_session_stale() -> None:
            async with client.app.state.database.sessions() as session:
                browser_session = await session.scalar(select(AdminSession))
                assert browser_session is not None
                old = datetime.now(UTC) - timedelta(hours=2)
                browser_session.created_at = old
                browser_session.reauthenticated_at = old
                await session.commit()

        assert client.portal is not None
        client.portal.call(make_session_stale)
        body = _safetensors_bytes()
        command = {
            "display_name": "Stale session upload",
            "canonical_source_url": "https://models.example.test/stale-session-upload",
            "license_url": "https://models.example.test/stale-session-upload/license",
            "commercial_use_attested": True,
            "adult_use_attested": True,
            "target_filename": "stale-session-upload.safetensors",
            "expected_sha256": hashlib.sha256(body).hexdigest(),
            "expected_byte_size": len(body),
            "expected_metadata": {},
            "trigger_words": ["stale session style"],
        }
        path = "/api/v1/loras/imports/manual"
        no_csrf = client.post(
            path,
            json=command,
            headers={"Origin": ORIGIN, "Idempotency-Key": "stale-no-csrf"},
        )
        assert no_csrf.status_code == 403
        wrong_origin = client.post(
            path,
            json=command,
            headers={
                "Origin": "https://attacker.example",
                "Sec-Fetch-Site": "cross-site",
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "stale-wrong-origin",
            },
        )
        assert wrong_origin.status_code == 403

        mutation_headers = {
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "stale-manual-create",
        }
        created = client.post(path, json=command, headers=mutation_headers)
        assert created.status_code == 201
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
            headers={
                **mutation_headers,
                "Idempotency-Key": "stale-manual-complete",
            },
        )
        assert completed.status_code == 200
        assert completed.json()["import"]["status"] == "queued"

        resolved = client.post(
            "/api/v1/loras/civitai:resolve",
            json={
                "url": (
                    "https://civitai.red/models/196908/"
                    "disgusted-face-illustriousponysd15-or-goofy-ai"
                    "?modelVersionId=2372164"
                )
            },
            headers=mutation_headers,
        )
        assert resolved.status_code == 200
        assert resolved.json()["canonical_source_url"] == (
            "https://civitai.com/models/196908?modelVersionId=2372164"
        )

        civitai_created = client.post(
            "/api/v1/loras/imports/civitai",
            json={
                "display_name": "Stale session Civitai import",
                "canonical_source_url": "https://civitai.com/models/196908",
                "license_url": "https://civitai.com/models/196908",
                "commercial_use_attested": True,
                "adult_use_attested": True,
                "target_filename": "disgusted-face.safetensors",
                "expected_sha256": "b" * 64,
                "expected_byte_size": 1024,
                "expected_metadata": {},
                "trigger_words": ["disgusted face"],
                "civitai_model_id": 196908,
                "civitai_version_id": 2372164,
                "civitai_file_id": 7654321,
            },
            headers={
                **mutation_headers,
                "Idempotency-Key": "stale-civitai-create",
            },
        )
        assert civitai_created.status_code == 201
        civitai_import = civitai_created.json()["import"]
        cancelled = client.post(
            f"/api/v1/loras/imports/{civitai_import['id']}:cancel",
            json={"expected_lock_version": civitai_import["lock_version"]},
            headers={
                **mutation_headers,
                "Idempotency-Key": "stale-civitai-cancel",
            },
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["import"]["status"] == "cancelled"

        destructive = client.post(
            f"/api/v1/loras/{uuid4()}:retire",
            json={"purge_requested": False},
            headers={
                **mutation_headers,
                "Idempotency-Key": "stale-retire",
            },
        )
        assert destructive.status_code == 401
        assert destructive.json()["detail"] == "recent authentication required"
