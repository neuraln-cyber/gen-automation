import asyncio
import base64
from datetime import UTC, datetime
from pathlib import Path

from argon2 import PasswordHasher, Type
from fastapi.testclient import TestClient

from gen_automation.app import create_app
from gen_automation.auth.security import PasswordManager
from gen_automation.config import Environment, Settings
from gen_automation.db.models import AdminUser
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole

ORIGIN = "http://testserver"
PASSWORD = "compliance API integration password"  # noqa: S105
SESSION_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
TOTP_KEY = base64.urlsafe_b64encode(bytes(range(32, 64))).rstrip(b"=").decode("ascii")


def _settings(database_path: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        public_base_url=ORIGIN,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        auto_create_schema=False,
        auth_enabled=True,
        auth_require_totp=False,
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


async def _seed_users(settings: Settings) -> None:
    database = Database(settings.database_url)
    try:
        await database.create_schema()
        now = datetime.now(UTC)
        password_hash = _password_manager().hash(PASSWORD)
        async with database.sessions() as session:
            session.add_all(
                [
                    AdminUser(
                        username_normalized=f"{role.value}@example.test",
                        display_name=role.value.title(),
                        password_hash=password_hash,
                        role=role,
                        is_active=True,
                        failed_login_count=0,
                        password_changed_at=now,
                        credential_version=1,
                        lock_version=1,
                    )
                    for role in AdminRole
                ]
            )
            await session.commit()
    finally:
        await database.dispose()


def _login(client: TestClient, role: AdminRole) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": f"{role.value}@example.test",
            "password": PASSWORD,
        },
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200
    csrf = client.cookies.get("gen_csrf")
    assert csrf is not None
    return csrf


def _subject_payload() -> dict[str, object]:
    return {
        "slug": "fictional-adult",
        "display_name": "Fictional Adult",
        "canonical_source_url": "https://canon.example.test/adult",
        "canonical_age": 25,
        "clearly_adult": True,
        "is_fictional": True,
        "is_aged_up_minor": False,
        "distribution_rights_approved": True,
        "adult_derivative_rights_approved": True,
        "evidence": {
            "summary": "Age and commercial adult derivative rights reviewed.",
            "source_urls": ["https://rights.example.test/record"],
            "document_sha256s": ["a" * 64],
            "internal_reference": "LEGAL-2026-001",
        },
    }


def test_compliance_api_requires_role_origin_csrf_and_is_idempotent(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "compliance-api.db")
    asyncio.run(_seed_users(settings))
    path = "/api/v1/compliance/subjects"

    with TestClient(
        create_app(settings),
        base_url=ORIGIN,
        client=("192.0.2.44", 50000),
    ) as client:
        unauthenticated = client.post(
            path,
            json=_subject_payload(),
            headers={"Origin": ORIGIN, "Idempotency-Key": "unauthenticated"},
        )
        assert unauthenticated.status_code == 401

        reviewer_csrf = _login(client, AdminRole.REVIEWER)
        forbidden = client.post(
            path,
            json=_subject_payload(),
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": reviewer_csrf,
                "Idempotency-Key": "reviewer-forbidden",
            },
        )
        assert forbidden.status_code == 403

        client.cookies.clear()
        admin_csrf = _login(client, AdminRole.ADMIN)
        missing_csrf = client.post(
            path,
            json=_subject_payload(),
            headers={"Origin": ORIGIN, "Idempotency-Key": "missing-csrf"},
        )
        wrong_origin = client.post(
            path,
            json=_subject_payload(),
            headers={
                "Origin": "https://attacker.example",
                "Sec-Fetch-Site": "cross-site",
                "X-CSRF-Token": admin_csrf,
                "Idempotency-Key": "wrong-origin",
            },
        )
        assert missing_csrf.status_code == 403
        assert wrong_origin.status_code == 403

        headers = {
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": admin_csrf,
            "Idempotency-Key": "approve-subject-v1",
        }
        created = client.post(path, json=_subject_payload(), headers=headers)
        replay = client.post(path, json=_subject_payload(), headers=headers)
        assert created.status_code == 201
        assert replay.status_code == 201
        assert created.json()["approval_id"] == replay.json()["approval_id"]
        assert created.headers["idempotency-replayed"] == "false"
        assert replay.headers["idempotency-replayed"] == "true"

        current = client.get("/api/v1/compliance/subject")
        assert current.status_code == 200
        assert [row["approval_id"] for row in current.json()] == [created.json()["approval_id"]]

        rejected_payload = _subject_payload()
        rejected_payload["is_aged_up_minor"] = True
        rejected = client.post(
            path,
            json=rejected_payload,
            headers={
                **headers,
                "Idempotency-Key": "reject-aged-up-minor",
            },
        )
        assert rejected.status_code == 422

        revoked = client.post(
            f"{path}/{created.json()['approval_id']}:revoke",
            json={
                "expected_approval_version": 1,
                "reason_code": "rights_expired",
                "note": "Renewal pending.",
            },
            headers={
                **headers,
                "Idempotency-Key": "revoke-subject-v1",
            },
        )
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoked"
        assert revoked.json()["is_current"] is False
