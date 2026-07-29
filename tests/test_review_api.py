import asyncio
import base64
import hashlib
import hmac
import json
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from argon2 import PasswordHasher, Type
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.api.routes import review_tasks as review_routes
from gen_automation.app import create_app
from gen_automation.auth.security import (
    PasswordManager,
    TotpSecretCipher,
    generate_totp_secret,
)
from gen_automation.config import Environment, Settings
from gen_automation.db.models import (
    AdminUser,
    Asset,
    AssetRanking,
    AssetScore,
    GenerationJob,
    Project,
    Release,
    ReleaseVersion,
    ReviewTask,
    ScoringRun,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AdminRole,
    AssetKind,
    AssetScoreState,
    AssetState,
    GenerationState,
    RankingDisposition,
    ReleasePhase,
    ReviewTaskState,
    ScoringRunState,
)
from gen_automation.services.ranking_manifest import ranking_manifest_sha256
from gen_automation.services.review import (
    ReviewTaskResult,
)
from gen_automation.services.review import (
    create_review_task as service_create_review_task,
)

ORIGIN = "http://testserver"
PASSWORD = "review API integration password"  # noqa: S105
SESSION_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
TOTP_KEY = base64.urlsafe_b64encode(bytes(range(32, 64))).rstrip(b"=").decode("ascii")
NOW = datetime(2026, 7, 28, 16, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class UserCredential:
    id: UUID
    username: str
    secret: str


@dataclass(frozen=True, slots=True)
class ReviewApiContext:
    settings: Settings
    scoring_run_id: UUID
    asset_ids: tuple[UUID, UUID]
    users: dict[AdminRole, UserCredential]


def _settings(database_path: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        public_base_url=ORIGIN,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        auto_create_schema=False,
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


async def _seed_review_api(settings: Settings) -> ReviewApiContext:
    database = Database(settings.database_url)
    try:
        await database.create_schema()
        now = datetime.now(UTC)
        credentials: dict[AdminRole, UserCredential] = {}
        async with database.sessions() as session:
            cipher = TotpSecretCipher({"key-1": TOTP_KEY}, active_key_id="key-1")
            users: list[AdminUser] = []
            for role in AdminRole:
                secret = generate_totp_secret()
                username = f"{role.value}@example.test"
                user = AdminUser(
                    username_normalized=username,
                    display_name=f"Review {role.value.title()}",
                    password_hash=_password_manager().hash(PASSWORD),
                    role=role,
                    is_active=True,
                    failed_login_count=0,
                    password_changed_at=now,
                    credential_version=1,
                    lock_version=1,
                )
                session.add(user)
                await session.flush()
                user.totp_secret_ciphertext = cipher.encrypt(
                    secret,
                    subject=f"admin-user:{user.id}",
                )
                user.totp_confirmed_at = now
                credentials[role] = UserCredential(
                    id=user.id,
                    username=username,
                    secret=secret,
                )
                users.append(user)

            project = Project(slug="review-api", name="Review API")
            session.add(project)
            await session.flush()
            release = Release(
                project_id=project.id,
                slug="review-release",
                title="Review release",
                phase=ReleasePhase.REVIEWING,
                current_version_no=1,
                desired_accepted_count=1,
                lock_version=1,
            )
            session.add(release)
            await session.flush()
            version = ReleaseVersion(
                release_id=release.id,
                version_no=1,
                specification={"schema_version": 1},
                specification_sha256="a" * 64,
                created_by="test",
                created_at=NOW,
            )
            session.add(version)
            await session.flush()
            job = GenerationJob(
                release_version_id=version.id,
                logical_key="b" * 64,
                parameters={"batch": 2},
                parameters_sha256="c" * 64,
                state=GenerationState.SUCCEEDED,
                expected_output_count=2,
                attempt_count=1,
                max_attempts=3,
                lock_version=1,
            )
            session.add(job)
            await session.flush()
            asset_ids = (
                UUID("20000000-0000-4000-8000-000000000001"),
                UUID("20000000-0000-4000-8000-000000000002"),
            )
            assets = [
                _raw_asset(
                    asset_id=asset_id,
                    release_id=release.id,
                    job_id=job.id,
                    output_index=index,
                )
                for index, asset_id in enumerate(asset_ids)
            ]
            session.add_all(assets)
            await session.flush()
            scoring_run = ScoringRun(
                release_version_id=version.id,
                configuration={"quality": "review-api-v1"},
                config_sha256="d" * 64,
                input_manifest_sha256="e" * 64,
                scorer_version="review-api-scorer-v1",
                pillow_version="12.0.0",
                state=ScoringRunState.RUNNING,
                asset_count=2,
                max_attempts=3,
                created_at=NOW,
                started_at=NOW,
                completed_at=None,
            )
            session.add(scoring_run)
            await session.flush()
            manifest_rows: list[tuple[AssetRanking, AssetScore]] = []
            for rank, asset in enumerate(assets, start=1):
                aggregate = 1_000_000 - rank
                score = AssetScore(
                    scoring_run_id=scoring_run.id,
                    asset_id=asset.id,
                    asset_storage_backend=asset.storage_backend,
                    asset_storage_bucket=asset.storage_bucket,
                    asset_sha256=asset.sha256,
                    asset_object_key=asset.object_key,
                    asset_object_version_id=asset.object_version_id,
                    asset_byte_size=asset.byte_size,
                    asset_image_format=asset.image_format,
                    asset_width=asset.width,
                    asset_height=asset.height,
                    state=AssetScoreState.FLAGGED_CORRUPT,
                    attempts=1,
                    max_attempts=3,
                    available_at=NOW,
                    aggregate_score_micros=aggregate,
                    signal_detail={"classification": "fixture"},
                    scorer_version=scoring_run.scorer_version,
                    pillow_version=scoring_run.pillow_version,
                    config_sha256=scoring_run.config_sha256,
                    completed_at=NOW + timedelta(minutes=1),
                    created_at=NOW,
                )
                session.add(score)
                await session.flush()
                ranking = AssetRanking(
                    scoring_run_id=scoring_run.id,
                    asset_score_id=score.id,
                    asset_id=asset.id,
                    rank=rank,
                    aggregate_score_micros=aggregate,
                    disposition=RankingDisposition.FLAGGED_REVIEW,
                    explanation={"rank": rank},
                    is_duplicate_representative=False,
                    scorer_version=scoring_run.scorer_version,
                    pillow_version=scoring_run.pillow_version,
                    config_sha256=scoring_run.config_sha256,
                    frozen_at=NOW + timedelta(minutes=1),
                )
                session.add(ranking)
                manifest_rows.append((ranking, score))
            await session.flush()
            scoring_run.ranking_manifest_sha256 = ranking_manifest_sha256(
                scoring_run,
                manifest_rows,
            )
            scoring_run.state = ScoringRunState.COMPLETED
            scoring_run.completed_at = NOW + timedelta(minutes=1)
            await session.commit()
            return ReviewApiContext(
                settings=settings,
                scoring_run_id=scoring_run.id,
                asset_ids=asset_ids,
                users=credentials,
            )
    finally:
        await database.dispose()


def _raw_asset(
    *,
    asset_id: UUID,
    release_id: UUID,
    job_id: UUID,
    output_index: int,
) -> Asset:
    return Asset(
        id=asset_id,
        release_id=release_id,
        generation_job_id=job_id,
        output_index=output_index,
        kind=AssetKind.RAW_MASTER,
        state=AssetState.AVAILABLE,
        storage_backend="s3",
        storage_bucket="private-review-api-bucket",
        object_key=f"raw/private-secret-{output_index}.png",
        object_version_id=f"private-version-{output_index}",
        sha256=f"{output_index + 1:064x}",
        content_type="image/png",
        image_format="PNG",
        width=1024,
        height=1024,
        byte_size=2_048 + output_index,
        asset_metadata={"private": True},
        available_at=NOW - timedelta(days=1),
    )


def _totp_code(secret: str) -> str:
    counter = int(datetime.now(UTC).timestamp()) // 30
    digest = hmac.new(
        base64.b32decode(secret),
        struct.pack(">Q", counter),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 15
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def _login(
    client: TestClient,
    settings: Settings,
    credential: UserCredential,
) -> str:
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": credential.username,
            "password": PASSWORD,
            "totp_code": _totp_code(credential.secret),
        },
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    csrf = client.cookies.get(settings.auth_csrf_cookie_name)
    assert csrf is not None
    return csrf


def _mutation_headers(csrf: str, key: str) -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": csrf,
        "Idempotency-Key": key,
    }


async def _review_task_rows(settings: Settings) -> tuple[int, UUID | None]:
    database = Database(settings.database_url)
    try:
        async with database.sessions() as session:
            count = int(await session.scalar(select(func.count()).select_from(ReviewTask)) or 0)
            actor = await session.scalar(select(ReviewTask.created_by_user_id))
            return count, actor
    finally:
        await database.dispose()


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        (AdminRole.OWNER, True),
        (AdminRole.REVIEWER, True),
        (AdminRole.ADMIN, False),
        (AdminRole.PUBLISHER, False),
    ],
)
def test_review_role_matrix_denies_before_service_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: AdminRole,
    allowed: bool,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / f"roles-{role.value}.db")))
    app = create_app(context.settings)
    actor_calls: list[UUID] = []

    async def tracked_create(
        session: AsyncSession,
        *,
        scoring_run_id: UUID,
        created_by_user_id: UUID,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> ReviewTaskResult:
        actor_calls.append(created_by_user_id)
        return await service_create_review_task(
            session,
            scoring_run_id=scoring_run_id,
            created_by_user_id=created_by_user_id,
            idempotency_key=idempotency_key,
            now=now,
        )

    monkeypatch.setattr(review_routes, "create_review_task", tracked_create)
    with TestClient(
        app,
        base_url=ORIGIN,
        client=("192.0.2.80", 50000),
    ) as client:
        csrf = _login(client, context.settings, context.users[role])
        response = client.post(
            "/api/v1/review-tasks",
            json={"scoring_run_id": str(context.scoring_run_id)},
            headers=_mutation_headers(csrf, f"role-create-{role.value}"),
        )
        read_unknown = client.get(f"/api/v1/review-tasks/{uuid4()}")

    count, stored_actor = asyncio.run(_review_task_rows(context.settings))
    if allowed:
        assert response.status_code == 201
        assert actor_calls == [context.users[role].id]
        assert count == 1
        assert stored_actor == context.users[role].id
        assert read_unknown.status_code == 404
    else:
        assert response.status_code == 403
        assert response.json() == {"detail": "permission denied"}
        assert actor_calls == []
        assert count == 0
        assert stored_actor is None
        assert read_unknown.status_code == 403


def test_review_mutations_require_origin_csrf_and_replay_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "workflow.db")))
    app = create_app(context.settings)
    service_calls = 0

    async def tracked_create(
        session: AsyncSession,
        *,
        scoring_run_id: UUID,
        created_by_user_id: UUID,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> ReviewTaskResult:
        nonlocal service_calls
        service_calls += 1
        return await service_create_review_task(
            session,
            scoring_run_id=scoring_run_id,
            created_by_user_id=created_by_user_id,
            idempotency_key=idempotency_key,
            now=now,
        )

    monkeypatch.setattr(review_routes, "create_review_task", tracked_create)
    credential = context.users[AdminRole.REVIEWER]
    with TestClient(
        app,
        base_url=ORIGIN,
        client=("192.0.2.81", 50000),
    ) as client:
        csrf = _login(client, context.settings, credential)
        command = {"scoring_run_id": str(context.scoring_run_id)}
        missing_origin = client.post(
            "/api/v1/review-tasks",
            json=command,
            headers={
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "missing-origin",
            },
        )
        missing_csrf = client.post(
            "/api/v1/review-tasks",
            json=command,
            headers={
                "Origin": ORIGIN,
                "Idempotency-Key": "missing-csrf",
            },
        )
        wrong_csrf = client.post(
            "/api/v1/review-tasks",
            json=command,
            headers=_mutation_headers("wrong-csrf-token", "wrong-csrf"),
        )
        missing_create_key = client.post(
            "/api/v1/review-tasks",
            json=command,
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        created = client.post(
            "/api/v1/review-tasks",
            json=command,
            headers=_mutation_headers(csrf, "create-review-task"),
        )
        replay = client.post(
            "/api/v1/review-tasks",
            json=command,
            headers=_mutation_headers(csrf, "create-review-task"),
        )

        assert missing_origin.status_code == 403
        assert missing_csrf.status_code == 403
        assert wrong_csrf.status_code == 403
        assert missing_create_key.status_code == 422
        assert service_calls == 2
        assert created.status_code == 201
        assert created.headers["idempotency-replayed"] == "false"
        assert replay.status_code == 200
        assert replay.headers["idempotency-replayed"] == "true"
        created_body = created.json()
        replay_body = replay.json()
        assert set(created_body) == {
            "task_id",
            "release_version_id",
            "scoring_run_id",
            "desired_accepted_count",
            "ranked_asset_count",
            "state",
            "lock_version",
            "replayed",
        }
        assert created_body["replayed"] is False
        assert replay_body == {**created_body, "replayed": True}
        task_id = created_body["task_id"]

        extra_actor = client.post(
            f"/api/v1/review-tasks/{task_id}/decisions",
            json={
                "asset_id": str(context.asset_ids[0]),
                "decision": "accept",
                "expected_lock_version": 1,
                "decided_by_user_id": str(context.users[AdminRole.OWNER].id),
            },
            headers=_mutation_headers(csrf, "extra-actor-field"),
        )
        missing_version = client.post(
            f"/api/v1/review-tasks/{task_id}/decisions",
            json={
                "asset_id": str(context.asset_ids[0]),
                "decision": "accept",
            },
            headers=_mutation_headers(csrf, "missing-lock-version"),
        )
        missing_decision_key = client.post(
            f"/api/v1/review-tasks/{task_id}/decisions",
            json={
                "asset_id": str(context.asset_ids[0]),
                "decision": "accept",
                "expected_lock_version": 1,
            },
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        invalid_reason = client.post(
            f"/api/v1/review-tasks/{task_id}/decisions",
            json={
                "asset_id": str(context.asset_ids[0]),
                "decision": "accept",
                "expected_lock_version": 1,
                "reason_code": "NOT A MACHINE CODE",
            },
            headers=_mutation_headers(csrf, "invalid-reason"),
        )
        decision_command = {
            "asset_id": str(context.asset_ids[0]),
            "decision": "accept",
            "expected_lock_version": 1,
            "reason_code": "manual_qc_pass",
            "note": "Accepted after full-resolution review.",
        }
        decision = client.post(
            f"/api/v1/review-tasks/{task_id}/decisions",
            json=decision_command,
            headers=_mutation_headers(csrf, "accept-first-asset"),
        )
        decision_replay = client.post(
            f"/api/v1/review-tasks/{task_id}/decisions",
            json=decision_command,
            headers=_mutation_headers(csrf, "accept-first-asset"),
        )
        stale = client.post(
            f"/api/v1/review-tasks/{task_id}/decisions",
            json={
                "asset_id": str(context.asset_ids[1]),
                "decision": "reject",
                "expected_lock_version": 1,
                "reason_code": "manual_reject",
            },
            headers=_mutation_headers(csrf, "stale-second-asset"),
        )
        summary = client.get(f"/api/v1/review-tasks/{task_id}")
        completed = client.post(
            f"/api/v1/review-tasks/{task_id}:complete",
            json={"expected_lock_version": 2},
            headers=_mutation_headers(csrf, "complete-review-task"),
        )
        missing_transition_key = client.post(
            f"/api/v1/review-tasks/{task_id}:cancel",
            json={"expected_lock_version": 3},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        completed_replay = client.post(
            f"/api/v1/review-tasks/{task_id}:complete",
            json={"expected_lock_version": 2},
            headers=_mutation_headers(csrf, "complete-review-task"),
        )

    assert extra_actor.status_code == 422
    assert missing_version.status_code == 422
    assert missing_decision_key.status_code == 422
    assert missing_transition_key.status_code == 422
    assert invalid_reason.status_code == 422
    assert invalid_reason.json() == {"detail": "review request is invalid"}
    assert decision.status_code == 201
    assert decision_replay.status_code == 200
    decision_body = decision.json()
    assert set(decision_body) == {
        "decision_id",
        "task_id",
        "asset_id",
        "revision",
        "decision",
        "reason_code",
        "note",
        "decided_by_user_id",
        "supersedes_decision_id",
        "task_lock_version",
        "replayed",
    }
    assert decision_body["decided_by_user_id"] == str(credential.id)
    assert decision_body["task_lock_version"] == 2
    assert decision_replay.json() == {**decision_body, "replayed": True}
    assert stale.status_code == 409
    assert stale.json() == {"detail": "review request conflicts with current state"}

    assert summary.status_code == 200
    summary_body = summary.json()
    assert set(summary_body) == {
        "task_id",
        "state",
        "lock_version",
        "desired_accepted_count",
        "ranked_asset_count",
        "accepted_count",
        "rejected_count",
        "held_count",
        "undecided_count",
        "x_selected_count",
        "assets",
        "semantic_gate",
    }
    assert summary_body["semantic_gate"] == {
        "enabled": False,
        "ranked_asset_count": 2,
        "terminal_count": 0,
        "pending_count": 0,
        "unavailable_count": 0,
        "severe_count": 0,
        "severe_override_count": 0,
        "severe_blocked_count": 0,
        "completion_ready": True,
    }
    assert summary_body["accepted_count"] == 1
    assert summary_body["undecided_count"] == 1
    assert summary_body["lock_version"] == 2
    assert [asset["rank"] for asset in summary_body["assets"]] == [1, 2]
    safe_asset_fields = {
        "asset_id",
        "rank",
        "decision_id",
        "revision",
        "decision",
        "reason_code",
        "note",
        "decided_by_user_id",
        "decided_at",
        "selected_for_x",
        "semantic_severe_override_attested",
    }
    assert all(set(asset) == safe_asset_fields for asset in summary_body["assets"])
    serialized = json.dumps(
        {
            "created": created_body,
            "decision": decision_body,
            "summary": summary_body,
        }
    )
    assert "object_key" not in serialized
    assert "storage_bucket" not in serialized
    assert "object_version" not in serialized
    assert "private-secret" not in serialized
    assert "private-review-api-bucket" not in serialized

    assert completed.status_code == 200
    assert completed.json() == {
        "task_id": task_id,
        "state": ReviewTaskState.COMPLETED.value,
        "lock_version": 3,
        "accepted_count": 1,
        "replayed": False,
    }
    assert completed_replay.status_code == 200
    assert completed_replay.json() == {
        **completed.json(),
        "replayed": True,
    }
    assert completed_replay.headers["idempotency-replayed"] == "true"


def test_review_cancel_and_typed_not_found_errors(
    tmp_path: Path,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "cancel.db")))
    app = create_app(context.settings)
    with TestClient(
        app,
        base_url=ORIGIN,
        client=("192.0.2.82", 50000),
    ) as client:
        csrf = _login(
            client,
            context.settings,
            context.users[AdminRole.REVIEWER],
        )
        missing_run = client.post(
            "/api/v1/review-tasks",
            json={"scoring_run_id": str(uuid4())},
            headers=_mutation_headers(csrf, "missing-scoring-run"),
        )
        created = client.post(
            "/api/v1/review-tasks",
            json={"scoring_run_id": str(context.scoring_run_id)},
            headers=_mutation_headers(csrf, "create-cancel-task"),
        )
        task_id = created.json()["task_id"]
        cancelled = client.post(
            f"/api/v1/review-tasks/{task_id}:cancel",
            json={"expected_lock_version": 1},
            headers=_mutation_headers(csrf, "cancel-review-task"),
        )
        replay = client.post(
            f"/api/v1/review-tasks/{task_id}:cancel",
            json={"expected_lock_version": 1},
            headers=_mutation_headers(csrf, "cancel-review-task"),
        )
        after_cancel = client.post(
            f"/api/v1/review-tasks/{task_id}:complete",
            json={"expected_lock_version": 2},
            headers=_mutation_headers(csrf, "complete-cancelled-task"),
        )

    assert missing_run.status_code == 404
    assert missing_run.json() == {"detail": "review resource was not found"}
    assert created.status_code == 201
    assert cancelled.status_code == 200
    assert cancelled.json() == {
        "task_id": task_id,
        "state": ReviewTaskState.CANCELLED.value,
        "lock_version": 2,
        "accepted_count": 0,
        "replayed": False,
    }
    assert replay.status_code == 200
    assert replay.json() == {**cancelled.json(), "replayed": True}
    assert after_cancel.status_code == 409
