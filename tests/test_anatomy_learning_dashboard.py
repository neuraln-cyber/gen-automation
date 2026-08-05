import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

import gen_automation.api.routes.anatomy_learning_dashboard as learning_dashboard
from gen_automation.api.browser_anatomy_learning_forms import (
    anatomy_learning_form_token,
)
from gen_automation.api.security import require_release_reader
from gen_automation.db.models import AdminUser, SemanticLearningPolicy
from gen_automation.db.session import get_session
from gen_automation.domain.enums import AdminRole
from gen_automation.services.authentication import AuthenticatedPrincipal


def _principal(role: AdminRole) -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(
        session_id=uuid4(),
        user_id=uuid4(),
        username=f"{role.value}-test",
        display_name=f"{role.value.title()} Test",
        role=role,
        csrf_sha256="c" * 64,
        expires_at=now + timedelta(hours=1),
        idle_expires_at=now + timedelta(hours=1),
        reauthenticated_at=now,
        mfa_verified_at=now,
    )


def _seed_development_owner(client: TestClient) -> None:
    now = datetime.now(UTC)

    async def seed() -> None:
        database = client.app.state.database
        async with database.sessions() as session:
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

    asyncio.run(seed())


def _form_tokens(
    client: TestClient,
    *,
    action: str,
    parts: tuple[str, ...],
) -> tuple[str, str]:
    settings = client.app.state.settings
    csrf_token = anatomy_learning_form_token(
        settings,
        session_id=UUID(int=0),
        action="csrf",
        parts=(),
    )
    form_key = anatomy_learning_form_token(
        settings,
        session_id=UUID(int=0),
        action=action,
        parts=parts,
    )
    return csrf_token, form_key


def test_owner_can_open_anatomy_learning_without_starting_paid_work(
    client: TestClient,
) -> None:
    page = client.get("/dashboard/anatomy-learning")

    assert page.status_code == 200
    assert "Anatomy learning" in page.text
    assert "Meta-classifier training is free" in page.text
    assert "RunPod spending stays gated" in page.text
    assert "Viewing this page never starts a paid job" in page.text
    assert "No labeled anatomy profile yet" in page.text
    assert "/dashboard/anatomy-learning" in client.get("/dashboard").text
    assert "no-store" in page.headers["cache-control"]
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" in page.headers


def test_page_initializes_and_persists_the_owner_standing_policy(
    client: TestClient,
) -> None:
    _seed_development_owner(client)

    page = client.get("/dashboard/anatomy-learning")

    assert page.status_code == 200
    assert "Learning on" in page.text
    assert "Save learning settings" in page.text
    assert "never reserves a GPU or spends RunPod credit" in page.text

    async def load_policy() -> SemanticLearningPolicy | None:
        database = client.app.state.database
        async with database.sessions() as session:
            return await session.get(SemanticLearningPolicy, UUID(int=0))

    policy = asyncio.run(load_policy())
    assert policy is not None
    assert policy.auto_train_meta is True
    assert policy.auto_train_visual is True
    assert policy.lock_version == 1


def test_policy_form_is_csrf_protected_and_replay_idempotent(
    client: TestClient,
) -> None:
    _seed_development_owner(client)
    assert client.get("/dashboard/anatomy-learning").status_code == 200
    csrf_token, form_key = _form_tokens(client, action="policy", parts=("1",))
    form = {
        "csrf_token": csrf_token,
        "idempotency_key": form_key,
        "expected_lock_version": "1",
        "learning_enabled": "on",
        "auto_train_meta": "on",
        "auto_promote_validated": "on",
        "minimum_new_labels_for_retrain": "75",
        "max_visual_run_usd": "6.50",
    }

    invalid_csrf = client.post(
        "/dashboard/anatomy-learning/policy",
        data={**form, "csrf_token": "wrong-token"},
        follow_redirects=False,
    )
    first = client.post(
        "/dashboard/anatomy-learning/policy",
        data=form,
        follow_redirects=False,
    )
    replay = client.post(
        "/dashboard/anatomy-learning/policy",
        data=form,
        follow_redirects=False,
    )

    assert invalid_csrf.status_code == 403
    assert first.status_code == 303
    assert replay.status_code == 303
    assert first.headers["location"].endswith("notice=policy-saved")

    async def load_policy() -> SemanticLearningPolicy:
        database = client.app.state.database
        async with database.sessions() as session:
            policy = await session.scalar(select(SemanticLearningPolicy))
            assert policy is not None
            return policy

    policy = asyncio.run(load_policy())
    assert policy.lock_version == 2
    assert policy.minimum_new_labels_for_retrain == 75
    assert policy.max_visual_run_microusd == 6_500_000
    assert policy.auto_train_visual is False


def test_ready_manual_training_request_is_cpu_only_and_idempotently_signed(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_development_owner(client)
    assert client.get("/dashboard/anatomy-learning").status_code == 200
    profile_sha256 = "a" * 64
    dataset_sha256 = "b" * 64
    csrf_token, form_key = _form_tokens(
        client,
        action="train-meta",
        parts=(profile_sha256, dataset_sha256),
    )
    calls: list[tuple[UUID, str]] = []

    async def request_override(*_args: object, **kwargs: object) -> object:
        calls.append((kwargs["owner_user_id"], kwargs["profile_sha256"]))
        return SimpleNamespace(dataset_sha256=dataset_sha256)

    monkeypatch.setattr(
        learning_dashboard,
        "request_meta_training_run",
        request_override,
    )
    form = {
        "csrf_token": csrf_token,
        "idempotency_key": form_key,
        "profile_sha256": profile_sha256,
        "dataset_sha256": dataset_sha256,
    }

    invalid_key = client.post(
        "/dashboard/anatomy-learning/train",
        data={**form, "idempotency_key": "wrong-key"},
        follow_redirects=False,
    )
    queued = client.post(
        "/dashboard/anatomy-learning/train",
        data=form,
        follow_redirects=False,
    )
    repeated = client.post(
        "/dashboard/anatomy-learning/train",
        data=form,
        follow_redirects=False,
    )

    assert invalid_key.status_code == 400
    assert queued.status_code == 303
    assert repeated.status_code == 303
    assert "cpu-training-queued" in queued.headers["location"]
    assert calls == [
        (UUID(int=0), profile_sha256),
        (UUID(int=0), profile_sha256),
    ]


def test_anatomy_learning_is_owner_only(client: TestClient) -> None:
    reviewer = _principal(AdminRole.REVIEWER)
    application = cast(FastAPI, client.app)

    async def reviewer_reader() -> AuthenticatedPrincipal:
        return reviewer

    application.dependency_overrides[require_release_reader] = reviewer_reader
    try:
        page = client.get("/dashboard/anatomy-learning")
    finally:
        application.dependency_overrides.pop(require_release_reader, None)

    assert page.status_code == 403
    assert "Anatomy learning is owner-only" in page.text
    assert "personalized training data" in page.text


def test_learning_page_renders_exact_readiness_and_latest_metrics(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_sha256 = "a" * 64
    now = datetime.now(UTC)
    owner = _principal(AdminRole.OWNER)
    ready_phase = SimpleNamespace(
        ready=True,
        blockers=(),
        operational_target="Run in shadow mode before promotion.",
    )
    collecting_phase = SimpleNamespace(
        ready=False,
        blockers=("need 500 binary labels (have 340)", "need 5 generation batches"),
        operational_target="Keep collecting labels from completed reviews.",
    )
    holdout = SimpleNamespace(
        holdout_count=70,
        holdout_good_count=45,
        holdout_defect_count=25,
    )
    split = SimpleNamespace(
        release_set_count=4,
        completed_review_set_count=3,
        generation_batch_count=4,
        generated_utc_date_count=3,
        recommended_group_key="release_set",
        set_group_split_eligible=True,
        batch_group_split_eligible=True,
        temporal_split_eligible=True,
        evaluation_holdout=holdout,
    )
    cohorts = SimpleNamespace(
        checkpoint_count=2,
        lora_stack_count=6,
        workflow_count=2,
        style_stack_count=5,
        missing_style_metadata_count=7,
    )

    def named(name: str, count: int) -> SimpleNamespace:
        return SimpleNamespace(name=name, count=count)

    profile = SimpleNamespace(
        profile_sha256=profile_sha256,
        dataset_sha256="b" * 64,
        binary_labeled_count=340,
        unique_content_count=342,
        duplicate_content_count=2,
        excluded_conflicting_content_count=1,
        anatomy_good_count=220,
        anatomy_defect_count=120,
        explicit_label_count=180,
        issue_coded_defect_count=77,
        unjudgeable_count=2,
        source_counts=(
            named("explicit", 180),
            named("inferred_review_accept", 140),
            named("inferred_anatomy_reject", 22),
        ),
        owner_issue_counts=(
            named("extra_finger", 31),
            named("extra_limb", 18),
        ),
        owner_issue_family_counts=(
            named("hand", 41),
            named("limb_or_duplicate", 20),
        ),
        split=split,
        cohorts=cohorts,
        calibration=ready_phase,
        meta_classifier=collecting_phase,
        meta_evaluation=ready_phase,
        lora=collecting_phase,
    )
    readiness = SimpleNamespace(profiles=(profile,))
    policy = SimpleNamespace(
        learning_enabled=True,
        auto_train_meta=True,
        auto_train_visual=False,
        auto_promote_validated=True,
        minimum_new_labels_for_retrain=50,
        max_visual_run_microusd=7_500_000,
        lock_version=1,
    )
    run = SimpleNamespace(
        profile_sha256=profile_sha256,
        kind=SimpleNamespace(value="meta_classifier"),
        state=SimpleNamespace(value="succeeded"),
        created_at=now,
        attempts=1,
        max_attempts=3,
        provider=None,
        dataset_sha256="b" * 64,
        artifact_sha256="d" * 64,
        last_error_code=None,
        evaluation_report={"false_reject_count": 0, "reject_precision_micros": 975_000},
    )
    promotion = SimpleNamespace(
        profile_sha256=profile_sha256,
        kind=SimpleNamespace(value="meta_classifier"),
        decision=SimpleNamespace(value="promoted"),
        reason="Untouched holdout safety gates passed.",
        keep_threshold_micros=180_000,
        reject_threshold_micros=890_000,
        created_at=now,
    )

    class _ScalarRows:
        def __init__(self, rows: tuple[object, ...]) -> None:
            self._rows = rows

        def all(self) -> tuple[object, ...]:
            return self._rows

    class _Session:
        def __init__(self) -> None:
            self._results = [_ScalarRows((run,)), _ScalarRows((promotion,))]

        async def get(self, model: object, identity: object) -> object:
            assert model is SemanticLearningPolicy
            assert identity == owner.user_id
            return policy

        async def scalars(self, _statement: object) -> _ScalarRows:
            return self._results.pop(0)

    session = _Session()
    application = cast(FastAPI, client.app)

    async def owner_reader() -> AuthenticatedPrincipal:
        return owner

    async def session_override() -> object:
        yield session

    async def readiness_override(*_args: object, **_kwargs: object) -> object:
        assert _kwargs["owner_user_id"] == owner.user_id
        return readiness

    async def policy_override(*_args: object, **_kwargs: object) -> object:
        assert _kwargs["owner_user_id"] == owner.user_id
        return policy

    monkeypatch.setattr(
        learning_dashboard,
        "build_semantic_learning_readiness_report",
        readiness_override,
    )
    monkeypatch.setattr(
        learning_dashboard,
        "_initialize_owner_policy",
        policy_override,
    )
    application.dependency_overrides[require_release_reader] = owner_reader
    application.dependency_overrides[get_session] = session_override
    try:
        page = client.get("/dashboard/anatomy-learning")
    finally:
        application.dependency_overrides.pop(require_release_reader, None)
        application.dependency_overrides.pop(get_session, None)

    assert page.status_code == 200
    assert "340 learning labels" in page.text
    assert "need 500 binary labels (have 340)" in page.text
    assert "need 5 generation batches" in page.text
    assert "inferred review accept" in page.text
    assert "extra finger" in page.text
    assert "Issue-coded defects" in page.text
    assert "Generation cohorts" in page.text
    assert "Attempt 1 / 3" in page.text
    assert "false_reject_count" in page.text
    assert "Untouched holdout safety gates passed" in page.text
    assert "$7.50" in page.text
    assert "Queue free CPU challenger now" not in page.text
    assert "provider submission remains unavailable" in page.text
