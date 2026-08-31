import asyncio
import base64
import hashlib
import hmac
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher, Type
from fastapi.testclient import TestClient

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
    ScoringRunState,
)
from gen_automation.services.ranking_manifest import ranking_manifest_sha256
from gen_automation.storage.base import (
    ObjectMetadata,
    PresignedUpload,
)
from tests.test_dashboard_review import _FORM_HEADERS, _one_form

SESSION_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
TOTP_KEY = base64.urlsafe_b64encode(bytes(range(32, 64))).rstrip(b"=").decode("ascii")
DASHBOARD_PASSWORD = "dashboard test password"  # noqa: S105
NOW = datetime(2026, 7, 28, 15, 0, tzinfo=UTC)
RELEASE_ID = UUID("00000000-0000-4000-8000-000000000201")
ASSET_A_ID = UUID("00000000-0000-4000-8000-000000000211")
ASSET_B_ID = UUID("00000000-0000-4000-8000-000000000212")


@dataclass(frozen=True)
class SignCall:
    key: str
    version_id: str | None
    expires_in: int
    download_name: str | None


class RecordingObjectStore:
    backend = "s3"
    bucket = "private-ranked-assets"

    def __init__(self) -> None:
        self.calls: list[SignCall] = []

    async def ping(self) -> None:
        return None

    async def presign_upload(
        self,
        *,
        key: str,
        content_type: str,
        metadata: dict[str, str],
        expires_in: int,
        max_bytes: int,
    ) -> PresignedUpload:
        raise AssertionError("dashboard must not request upload access")

    async def presign_download(
        self,
        *,
        key: str,
        expires_in: int,
        download_name: str | None = None,
        version_id: str | None = None,
    ) -> str:
        call = SignCall(
            key=key,
            version_id=version_id,
            expires_in=expires_in,
            download_name=download_name,
        )
        self.calls.append(call)
        token = len(self.calls)
        kind = "attachment" if download_name else "view"
        return f"https://signed.example.test/{kind}/{token}?signature=private-{token}"

    async def head(self, key: str) -> ObjectMetadata | None:
        raise AssertionError("dashboard must use the frozen score snapshot")

    async def read_bytes(
        self,
        key: str,
        *,
        max_bytes: int,
        version_id: str | None = None,
        etag: str | None = None,
    ) -> bytes:
        raise AssertionError("dashboard must not proxy raw bytes")

    async def copy_if_absent(
        self,
        *,
        source_key: str,
        destination_key: str,
        content_type: str,
        metadata: dict[str, str],
        source_version_id: str | None = None,
        source_etag: str | None = None,
    ) -> ObjectMetadata:
        raise AssertionError("dashboard is read-only")

    async def delete(self, key: str, *, version_id: str | None = None) -> None:
        raise AssertionError("dashboard is read-only")

    async def close(self) -> None:
        return None


def _settings(database_path: Path, *, auth_enabled: bool = False) -> Settings:
    return Settings(
        environment=Environment.TEST,
        public_base_url="http://testserver",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        auto_create_schema=True,
        auth_enabled=auth_enabled,
        auth_require_totp=auth_enabled,
        auth_development_bypass_enabled=not auth_enabled,
        session_secret=SESSION_KEY,
        auth_totp_active_key_id="key-1" if auth_enabled else None,
        auth_totp_encryption_keys={"key-1": TOTP_KEY} if auth_enabled else {},
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


async def _prepare_user(
    settings: Settings,
    *,
    role: AdminRole,
    username: str,
) -> str:
    database = Database(settings.database_url)
    try:
        await database.create_schema()
        secret = generate_totp_secret()
        now = datetime.now(UTC)
        async with database.sessions() as session:
            user = AdminUser(
                username_normalized=username,
                display_name=f"Dashboard {role.value.title()}",
                password_hash=_password_manager().hash(DASHBOARD_PASSWORD),
                role=role,
                is_active=True,
                failed_login_count=0,
                password_changed_at=now,
                credential_version=1,
                lock_version=1,
            )
            session.add(user)
            await session.flush()
            cipher = TotpSecretCipher({"key-1": TOTP_KEY}, active_key_id="key-1")
            user.totp_secret_ciphertext = cipher.encrypt(
                secret,
                subject=f"admin-user:{user.id}",
            )
            user.totp_confirmed_at = now
            await session.commit()
        return secret
    finally:
        await database.dispose()


async def _prepare_owner(settings: Settings) -> str:
    return await _prepare_user(
        settings,
        role=AdminRole.OWNER,
        username="owner@example.test",
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


def _login(client: TestClient, *, username: str, secret: str) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": username,
            "password": DASHBOARD_PASSWORD,
            "totp_code": _totp_code(
                secret,
                int(datetime.now(UTC).timestamp()),
            ),
        },
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200


async def _seed_release(
    settings: Settings,
    *,
    completed_run: bool = True,
    complete_ranking: bool = True,
) -> None:
    database = Database(settings.database_url)
    try:
        await database.create_schema()
        async with database.sessions() as session:
            project = Project(
                id=UUID("00000000-0000-4000-8000-000000000200"),
                slug="art-project",
                name="Art project",
            )
            release = Release(
                id=RELEASE_ID,
                project_id=project.id,
                slug="night-set",
                title="Night set",
                phase=ReleasePhase.REVIEWING,
                current_version_no=1,
                desired_accepted_count=2,
            )
            version = ReleaseVersion(
                id=UUID("00000000-0000-4000-8000-000000000202"),
                release_id=release.id,
                version_no=1,
                specification={"subject": "fictional adult"},
                specification_sha256="1" * 64,
                created_by="test",
                created_at=NOW,
            )
            session.add_all([project, release, version])
            await session.flush()
            job = GenerationJob(
                id=UUID("00000000-0000-4000-8000-000000000203"),
                release_version_id=version.id,
                logical_key="2" * 64,
                parameters={"batch": 1},
                parameters_sha256="3" * 64,
                state=GenerationState.SUCCEEDED,
                expected_output_count=2,
            )
            session.add(job)
            await session.flush()
            assets = (
                _asset(
                    asset_id=ASSET_A_ID,
                    release_id=release.id,
                    job_id=job.id,
                    output_index=0,
                    current_key="current/mutated-a.png",
                    current_version="current-version-a",
                ),
                _asset(
                    asset_id=ASSET_B_ID,
                    release_id=release.id,
                    job_id=job.id,
                    output_index=1,
                    current_key="current/mutated-b.png",
                    current_version="current-version-b",
                ),
            )
            session.add_all(assets)
            if completed_run:
                run = ScoringRun(
                    id=UUID("00000000-0000-4000-8000-000000000220"),
                    release_version_id=version.id,
                    configuration={"version": 1},
                    config_sha256="4" * 64,
                    input_manifest_sha256="5" * 64,
                    scorer_version="quality-v1",
                    pillow_version="11.3.0",
                    state=ScoringRunState.RUNNING,
                    asset_count=2,
                    max_attempts=1,
                    created_at=NOW,
                    started_at=NOW,
                    completed_at=None,
                )
                session.add(run)
                await session.flush()
                score_a = _score(
                    run_id=run.id,
                    asset_id=ASSET_A_ID,
                    score_id=UUID("00000000-0000-4000-8000-000000000221"),
                    frozen_key="frozen/private-master-a.png",
                    frozen_version="exact-version-a-secret",
                    aggregate_score=700_000,
                )
                score_b = _score(
                    run_id=run.id,
                    asset_id=ASSET_B_ID,
                    score_id=UUID("00000000-0000-4000-8000-000000000222"),
                    frozen_key="frozen/private-master-b.png",
                    frozen_version="exact-version-b-secret",
                    aggregate_score=900_000,
                )
                session.add_all([score_a, score_b])
                await session.flush()
                rankings = [
                    _ranking(
                        run_id=run.id,
                        score=score_a,
                        rank=2,
                        aggregate_score=700_000,
                    ),
                    _ranking(
                        run_id=run.id,
                        score=score_b,
                        rank=1,
                        aggregate_score=900_000,
                    ),
                ]
                selected_rankings = rankings if complete_ranking else rankings[:1]
                session.add_all(selected_rankings)
                await session.flush()
                if complete_ranking:
                    run.ranking_manifest_sha256 = ranking_manifest_sha256(
                        run,
                        [
                            (rankings[0], score_a),
                            (rankings[1], score_b),
                        ],
                    )
                    run.state = ScoringRunState.COMPLETED
                    run.completed_at = NOW
            await session.commit()
    finally:
        await database.dispose()


def _asset(
    *,
    asset_id: UUID,
    release_id: UUID,
    job_id: UUID,
    output_index: int,
    current_key: str,
    current_version: str,
) -> Asset:
    return Asset(
        id=asset_id,
        release_id=release_id,
        generation_job_id=job_id,
        output_index=output_index,
        kind=AssetKind.RAW_MASTER,
        state=AssetState.AVAILABLE,
        storage_backend="s3",
        storage_bucket=RecordingObjectStore.bucket,
        object_key=current_key,
        object_version_id=current_version,
        sha256=f"{output_index + 6}" * 64,
        content_type="image/png",
        image_format="PNG",
        width=1024,
        height=1536,
        byte_size=2_000_000 + output_index,
        asset_metadata={},
        available_at=NOW,
    )


def _score(
    *,
    run_id: UUID,
    asset_id: UUID,
    score_id: UUID,
    frozen_key: str,
    frozen_version: str,
    aggregate_score: int,
) -> AssetScore:
    return AssetScore(
        id=score_id,
        scoring_run_id=run_id,
        asset_id=asset_id,
        asset_storage_backend="s3",
        asset_storage_bucket=RecordingObjectStore.bucket,
        asset_sha256="a" * 64 if asset_id == ASSET_A_ID else "b" * 64,
        asset_object_key=frozen_key,
        asset_object_version_id=frozen_version,
        asset_byte_size=2_000_000,
        asset_image_format="PNG",
        asset_width=1024,
        asset_height=1536,
        state=AssetScoreState.SCORED,
        attempts=1,
        max_attempts=1,
        available_at=NOW,
        luminance_mean_micros=500_000,
        luminance_std_micros=200_000,
        dynamic_range_micros=800_000,
        entropy_bits_micros=6_000_000,
        entropy_normalized_micros=750_000,
        sharpness_micros=700_000,
        dhash_hex="0123456789abcdef",
        aggregate_score_micros=aggregate_score,
        score_breakdown={"total_micros": aggregate_score},
        signal_detail={"classification": "scored", "requires_review": False},
        scorer_version="quality-v1",
        pillow_version="11.3.0",
        config_sha256="4" * 64,
        created_at=NOW,
        started_at=NOW,
        completed_at=NOW,
    )


def _ranking(
    *,
    run_id: UUID,
    score: AssetScore,
    rank: int,
    aggregate_score: int,
) -> AssetRanking:
    return AssetRanking(
        scoring_run_id=run_id,
        asset_score_id=score.id,
        asset_id=score.asset_id,
        rank=rank,
        aggregate_score_micros=aggregate_score,
        disposition=RankingDisposition.REVIEW_CANDIDATE,
        explanation={
            "quality_state": "scored",
            "signal": {"classification": "scored"},
            "untrusted_detail": "<script>hidden-object-secret</script>",
            "duplicate": None,
        },
        is_duplicate_representative=False,
        scorer_version="quality-v1",
        pillow_version="11.3.0",
        config_sha256="4" * 64,
        frozen_at=NOW,
    )


def test_dashboard_requires_authentication_before_loading_private_state(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "auth-required.db", auth_enabled=True)
    asyncio.run(_prepare_owner(settings))
    asyncio.run(_seed_release(settings))
    store = RecordingObjectStore()
    app = create_app(settings)

    with TestClient(
        app,
        base_url="http://testserver",
        client=("192.0.2.55", 50000),
    ) as client:
        app.state.object_store = store
        response = client.get(
            f"/dashboard/releases/{RELEASE_ID}",
            follow_redirects=False,
        )
        login = client.get(f"/dashboard/releases/{RELEASE_ID}")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert response.headers["cache-control"] == "no-store"
    assert login.status_code == 200
    assert login.url.path == "/login"
    assert [redirect.status_code for redirect in login.history] == [303]
    assert "Operator sign in" in login.text
    assert store.calls == []
    assert "frozen/private-master" not in response.text
    assert "exact-version" not in response.text
    assert "private-" not in response.text


@pytest.mark.parametrize(
    ("role", "may_view_raw_masters"),
    [
        (AdminRole.OWNER, True),
        (AdminRole.REVIEWER, True),
        (AdminRole.ADMIN, False),
        (AdminRole.PUBLISHER, False),
    ],
)
def test_raw_master_access_is_least_privilege_without_eager_signing(
    tmp_path: Path,
    role: AdminRole,
    may_view_raw_masters: bool,
) -> None:
    settings = _settings(tmp_path / f"role-{role.value}.db", auth_enabled=True)
    owner_secret = asyncio.run(_prepare_owner(settings))
    username = "owner@example.test"
    secret = owner_secret
    if role != AdminRole.OWNER:
        username = f"{role.value}@example.test"
        secret = asyncio.run(
            _prepare_user(
                settings,
                role=role,
                username=username,
            )
        )
    asyncio.run(_seed_release(settings))
    store = RecordingObjectStore()
    app = create_app(settings)
    with TestClient(
        app,
        base_url="http://testserver",
        client=("192.0.2.55", 50000),
    ) as client:
        app.state.object_store = store
        _login(client, username=username, secret=secret)
        index = client.get("/dashboard")
        detail = client.get(f"/dashboard/releases/{RELEASE_ID}")
        if may_view_raw_masters:
            assert store.calls == []
            bootstrap_form = _one_form(
                detail.text,
                f"/dashboard/releases/{RELEASE_ID}/review-tasks",
            )
            created = client.post(
                bootstrap_form.action,
                data=bootstrap_form.fields,
                headers=_FORM_HEADERS,
                follow_redirects=False,
            )
            canonical = client.get(f"/dashboard/releases/{RELEASE_ID}")

    assert index.status_code == 200
    assert "2 ranked masters" in index.text
    assert "no-store" in detail.headers["cache-control"]
    if may_view_raw_masters:
        assert detail.status_code == 200
        assert "data-review-bootstrap" in detail.text
        assert created.status_code == 303
        assert created.headers["location"] == f"/dashboard/releases/{RELEASE_ID}"
        assert canonical.status_code == 200
        assert "data-review-workspace" in canonical.text
        assert store.calls == []
        assert f"/dashboard/assets/{ASSET_A_ID}/view" in canonical.text
        assert f"/dashboard/assets/{ASSET_A_ID}/download" in canonical.text
        assert (
            f"/dashboard/assets/{ASSET_A_ID}/previews/dashboard-preview-v1/{'a' * 16}.jpg"
        ) in canonical.text
        assert "Download exact raw master" in canonical.text
    else:
        assert detail.status_code == 403
        assert detail.json() == {"detail": "permission denied"}
        assert store.calls == []
        assert "frozen/private-master" not in detail.text
        assert "exact-version" not in detail.text


def test_ranked_dashboard_orders_assets_and_uses_stable_private_routes(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "ranked.db", auth_enabled=True)
    owner_secret = asyncio.run(_prepare_owner(settings))
    asyncio.run(_seed_release(settings))
    store = RecordingObjectStore()
    app = create_app(settings)

    with TestClient(
        app,
        base_url="http://testserver",
        client=("192.0.2.56", 50000),
    ) as client:
        app.state.object_store = store
        _login(client, username="owner@example.test", secret=owner_secret)
        index = client.get("/dashboard")
        bootstrap = client.get(f"/dashboard/releases/{RELEASE_ID}")
        bootstrap_form = _one_form(
            bootstrap.text,
            f"/dashboard/releases/{RELEASE_ID}/review-tasks",
        )
        created = client.post(
            bootstrap_form.action,
            data=bootstrap_form.fields,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        detail = client.get(f"/dashboard/releases/{RELEASE_ID}")

    assert index.status_code == 200
    assert "2 ranked masters" in index.text
    assert f"/dashboard/releases/{RELEASE_ID}" in index.text
    assert bootstrap.status_code == 200
    assert "data-review-bootstrap" in bootstrap.text
    assert "data-review-workspace" not in bootstrap.text
    assert "Start ranked review" not in bootstrap.text
    assert "Open ranked review" not in bootstrap.text
    assert created.status_code == 303
    assert created.headers["location"] == f"/dashboard/releases/{RELEASE_ID}"
    assert detail.status_code == 200
    assert "data-review-workspace" in detail.text
    assert detail.text.index("#1") < detail.text.index("#2")
    assert detail.text.index(str(ASSET_B_ID)) < detail.text.index(str(ASSET_A_ID))
    assert "90.0%" in detail.text
    assert "70.0%" in detail.text
    assert "1024" in detail.text
    assert "1536" in detail.text
    assert "Download exact raw master" in detail.text
    assert "Quality state: scored" in detail.text
    assert 'data-asset-sort="generation"' in detail.text
    assert 'data-batch-image-number="2"' in detail.text
    assert 'data-batch-image-number="1"' in detail.text
    assert "Batch 1 &middot; image 2" in detail.text
    assert '<script src="/static/dashboard.js" defer></script>' in detail.text
    assert "hidden-object-secret" not in detail.text
    assert "frozen/private-master" not in detail.text
    assert "current/mutated" not in detail.text
    assert "exact-version" not in detail.text
    assert "default-src &#39;none&#39;" not in detail.text
    assert "no-store" in detail.headers["cache-control"]
    assert "script-src 'self'" in detail.headers["content-security-policy"]
    assert "img-src 'self' https: blob: http:" in detail.headers["content-security-policy"]

    assert store.calls == []
    for asset_id, digest_character in (
        (ASSET_A_ID, "a"),
        (ASSET_B_ID, "b"),
    ):
        assert f"/dashboard/assets/{asset_id}/view" in detail.text
        assert f"/dashboard/assets/{asset_id}/download" in detail.text
        assert (
            f"/dashboard/assets/{asset_id}/previews/dashboard-preview-v1/"
            f"{digest_character * 16}.jpg"
        ) in detail.text


def test_missing_or_incomplete_ranking_fails_clearly_without_signing(
    tmp_path: Path,
) -> None:
    missing_settings = _settings(tmp_path / "missing.db")
    asyncio.run(_seed_release(missing_settings, completed_run=False))
    missing_store = RecordingObjectStore()
    missing_app = create_app(missing_settings)
    with TestClient(missing_app, base_url="http://testserver") as client:
        missing_app.state.object_store = missing_store
        missing = client.get(f"/dashboard/releases/{RELEASE_ID}")

    assert missing.status_code == 409
    assert "Ranking not ready" in missing.text
    assert "no completed ranking" in missing.text
    assert missing_store.calls == []

    incomplete_settings = _settings(tmp_path / "incomplete.db")
    asyncio.run(_seed_release(incomplete_settings, complete_ranking=False))
    incomplete_store = RecordingObjectStore()
    incomplete_app = create_app(incomplete_settings)
    with TestClient(incomplete_app, base_url="http://testserver") as client:
        incomplete_app.state.object_store = incomplete_store
        incomplete = client.get(f"/dashboard/releases/{RELEASE_ID}")

    assert incomplete.status_code == 409
    assert "Ranking not ready" in incomplete.text
    assert "no completed ranking" in incomplete.text
    assert incomplete_store.calls == []
