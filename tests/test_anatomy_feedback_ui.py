import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from gen_automation.api.routes import dashboard as dashboard_routes
from gen_automation.api.routes import review_tasks as review_routes
from gen_automation.app import create_app
from gen_automation.db.models import Asset, AssetScore, SemanticAssessment
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AdminRole,
    ReviewDecisionValue,
    ReviewTaskState,
    SemanticAssessmentState,
    SemanticEnforcementMode,
    SemanticFeedbackAgreement,
    SemanticGroundTruth,
    SemanticIssueCode,
    SemanticVerdict,
)
from gen_automation.semantic import SemanticIssue
from gen_automation.services.review import append_review_decision, transition_review_task
from gen_automation.services.semantic_anatomy import SemanticReviewAssessment
from gen_automation.services.semantic_feedback import SemanticAnatomyFeedbackResult
from tests.test_dashboard_review import (
    _FORM_HEADERS,
    SameOriginReviewStore,
    _create_task,
    _forms,
)
from tests.test_review_api import (
    ORIGIN,
    PASSWORD,
    ReviewApiContext,
    UserCredential,
    _seed_review_api,
    _settings,
    _totp_code,
)

_TEST_SEMANTIC_PROFILE = "a" * 64


def _login(client: TestClient, credential: UserCredential, *, csrf_cookie_name: str) -> str:
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
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(csrf_cookie_name)
    assert csrf is not None
    return csrf


async def _refresh_calibration(*_args: object, **_kwargs: object) -> None:
    return None


async def _feedback_target_exists(*_args: object, **_kwargs: object) -> bool:
    return True


async def _close_review_task(
    context: ReviewApiContext,
    *,
    task_id: UUID,
    target_state: ReviewTaskState,
) -> None:
    database = Database(context.settings.database_url)
    try:
        async with database.sessions() as session:
            expected_lock_version = 1
            if target_state == ReviewTaskState.COMPLETED:
                decision = await append_review_decision(
                    session,
                    review_task_id=task_id,
                    asset_id=context.asset_ids[0],
                    decision=ReviewDecisionValue.ACCEPT,
                    decided_by_user_id=context.users[AdminRole.OWNER].id,
                    expected_lock_version=expected_lock_version,
                    idempotency_key=f"feedback-complete-decision-{task_id}",
                    reason_code="manual_qc_pass",
                    note="Accepted before the anatomy result was labelled.",
                )
                expected_lock_version = decision.task_lock_version
            result = await transition_review_task(
                session,
                review_task_id=task_id,
                target_state=target_state,
                changed_by_user_id=context.users[AdminRole.OWNER].id,
                expected_lock_version=expected_lock_version,
                idempotency_key=f"feedback-transition-{target_state.value}-{task_id}",
            )
            assert result.state == target_state
    finally:
        await database.dispose()


async def _add_completed_assessment(
    context: ReviewApiContext,
) -> UUID:
    database = Database(context.settings.database_url)
    try:
        async with database.sessions() as session:
            score = await session.scalar(
                select(AssetScore).where(
                    AssetScore.scoring_run_id == context.scoring_run_id,
                    AssetScore.asset_id == context.asset_ids[0],
                )
            )
            asset = await session.get(Asset, context.asset_ids[0])
            assert score is not None
            assert asset is not None
            assert asset.content_type is not None
            completed_at = datetime.now(UTC)
            assessment = SemanticAssessment(
                scoring_run_id=score.scoring_run_id,
                asset_score_id=score.id,
                asset_id=score.asset_id,
                asset_storage_backend=score.asset_storage_backend,
                asset_storage_bucket=score.asset_storage_bucket,
                asset_object_key=score.asset_object_key,
                asset_object_version_id=score.asset_object_version_id,
                asset_sha256=score.asset_sha256,
                asset_content_type=asset.content_type,
                asset_byte_size=score.asset_byte_size,
                profile_sha256=_TEST_SEMANTIC_PROFILE,
                model_name="private/anatomy-vlm",
                model_revision="revision-completed-review",
                prompt_sha256="b" * 64,
                schema_sha256="c" * 64,
                state=SemanticAssessmentState.COMPLETED,
                attempts=1,
                max_attempts=1,
                available_at=completed_at,
                verdict=SemanticVerdict.PASS,
                confidence_micros=940_000,
                issues=[],
                response_sha256="d" * 64,
                created_at=completed_at,
                started_at=completed_at,
                completed_at=completed_at,
            )
            session.add(assessment)
            await session.commit()
            return assessment.id
    finally:
        await database.dispose()


def _feedback_result(
    *,
    assessment_id: UUID,
    user_id: UUID,
    agreement: SemanticFeedbackAgreement = SemanticFeedbackAgreement.CORRECT,
    ground_truth: SemanticGroundTruth = SemanticGroundTruth.ANATOMY_DEFECT,
) -> SemanticAnatomyFeedbackResult:
    return SemanticAnatomyFeedbackResult(
        feedback_id=uuid4(),
        assessment_id=assessment_id,
        asset_id=uuid4(),
        user_id=user_id,
        agreement=agreement,
        ground_truth=ground_truth,
        issue_code=None,
        note=None,
        created_at=datetime.now(UTC),
        created=True,
    )


def test_owner_can_record_anatomy_feedback_for_completed_review_through_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "anatomy-feedback-api.db")))
    task_id = asyncio.run(_create_task(context))
    assessment_id = asyncio.run(_add_completed_assessment(context))
    asyncio.run(
        _close_review_task(
            context,
            task_id=task_id,
            target_state=ReviewTaskState.COMPLETED,
        )
    )
    captured: dict[str, object] = {}

    async def record_feedback(_session: object, **kwargs: object) -> SemanticAnatomyFeedbackResult:
        captured.update(kwargs)
        return _feedback_result(
            assessment_id=UUID(str(kwargs["assessment_id"])),
            user_id=UUID(str(kwargs["user_id"])),
        )

    monkeypatch.setattr(
        review_routes,
        "_semantic_gate_configuration",
        lambda _request: (_TEST_SEMANTIC_PROFILE, 900_000, SemanticEnforcementMode.SHADOW),
    )
    monkeypatch.setattr(
        review_routes,
        "record_semantic_anatomy_feedback",
        record_feedback,
    )
    monkeypatch.setattr(
        review_routes,
        "refresh_semantic_calibration_artifact",
        _refresh_calibration,
    )

    app = create_app(context.settings)
    with TestClient(app, base_url=ORIGIN, client=("192.0.2.111", 50000)) as client:
        csrf = _login(
            client,
            context.users[AdminRole.OWNER],
            csrf_cookie_name=context.settings.auth_csrf_cookie_name,
        )
        response = client.post(
            f"/api/v1/review-tasks/{task_id}/anatomy-feedback",
            json={
                "assessment_id": str(assessment_id),
                "ground_truth": "anatomy_defect",
                "issue_code": None,
                "note": None,
            },
            headers={**_FORM_HEADERS, "X-CSRF-Token": csrf},
        )

    assert response.status_code == 201
    assert response.json()["agreement"] == "correct"
    assert response.json()["ground_truth"] == "anatomy_defect"
    assert captured == {
        "assessment_id": assessment_id,
        "user_id": context.users[AdminRole.OWNER].id,
        "ground_truth": SemanticGroundTruth.ANATOMY_DEFECT,
        "issue_code": None,
        "note": None,
    }


@pytest.mark.parametrize(
    ("terminal_state", "expected_form_count"),
    (
        (ReviewTaskState.COMPLETED, 1),
        (ReviewTaskState.CANCELLED, 0),
    ),
)
def test_owner_feedback_controls_are_available_after_completion_but_not_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_state: ReviewTaskState,
    expected_form_count: int,
) -> None:
    context = asyncio.run(
        _seed_review_api(_settings(tmp_path / f"anatomy-feedback-{terminal_state.value}.db"))
    )
    task_id = asyncio.run(_create_task(context))
    assessment_id = asyncio.run(_add_completed_assessment(context))
    asyncio.run(
        _close_review_task(
            context,
            task_id=task_id,
            target_state=terminal_state,
        )
    )
    monkeypatch.setattr(
        dashboard_routes,
        "_configured_semantic_profile_sha256",
        lambda _settings: _TEST_SEMANTIC_PROFILE,
    )

    action = f"/dashboard/review-tasks/{task_id}/anatomy-feedback"
    app = create_app(context.settings)
    with TestClient(app, base_url=ORIGIN, client=("192.0.2.115", 50000)) as client:
        app.state.object_store = SameOriginReviewStore()
        _login(
            client,
            context.users[AdminRole.OWNER],
            csrf_cookie_name=context.settings.auth_csrf_cookie_name,
        )
        page = client.get(f"/dashboard/review-tasks/{task_id}")

    assert page.status_code == 200
    forms = _forms(page.text, action)
    assert len(forms) == expected_form_count
    assert "data-anatomy-training-control" not in page.text
    if terminal_state == ReviewTaskState.COMPLETED:
        assert forms[0].fields["assessment_id"] == str(assessment_id)
        assert forms[0].fields["csrf_token"]
        assert len(forms[0].fields["csrf_token"]) > 10
        assert "Good anatomy" in page.text
        assert "Defect" in page.text
        assert "Unsure" in page.text
    else:
        assert "This task is cancelled and is now read-only" in page.text


def test_dashboard_feedback_is_owner_only_and_ignores_issue_for_good_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "anatomy-feedback-web.db")))
    task_id = asyncio.run(_create_task(context))
    assessment_id = uuid4()
    captured: dict[str, object] = {}

    async def record_feedback(_session: object, **kwargs: object) -> SemanticAnatomyFeedbackResult:
        captured.update(kwargs)
        return _feedback_result(
            assessment_id=UUID(str(kwargs["assessment_id"])),
            user_id=UUID(str(kwargs["user_id"])),
            agreement=SemanticFeedbackAgreement.INCORRECT,
            ground_truth=SemanticGroundTruth.ANATOMY_GOOD,
        )

    monkeypatch.setattr(
        dashboard_routes,
        "_configured_semantic_profile_sha256",
        lambda _settings: "a" * 64,
    )
    monkeypatch.setattr(
        dashboard_routes,
        "semantic_feedback_target_belongs_to_review",
        _feedback_target_exists,
    )
    monkeypatch.setattr(
        dashboard_routes,
        "record_semantic_anatomy_feedback",
        record_feedback,
    )
    monkeypatch.setattr(
        dashboard_routes,
        "refresh_semantic_calibration_artifact",
        _refresh_calibration,
    )
    action = f"/dashboard/review-tasks/{task_id}/anatomy-feedback"
    app = create_app(context.settings)
    with TestClient(app, base_url=ORIGIN, client=("192.0.2.112", 50000)) as client:
        app.state.object_store = SameOriginReviewStore()
        csrf = _login(
            client,
            context.users[AdminRole.REVIEWER],
            csrf_cookie_name=context.settings.auth_csrf_cookie_name,
        )
        denied = client.post(
            action,
            data={
                "csrf_token": csrf,
                "assessment_id": str(assessment_id),
                "ground_truth": "anatomy_good",
                "issue_code": "malformed_hand",
                "note": "Looks normal.",
            },
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        csrf = _login(
            client,
            context.users[AdminRole.OWNER],
            csrf_cookie_name=context.settings.auth_csrf_cookie_name,
        )
        saved = client.post(
            action,
            data={
                "csrf_token": csrf,
                "assessment_id": str(assessment_id),
                "ground_truth": "anatomy_good",
                "issue_code": "malformed_hand",
                "note": "Looks normal.",
            },
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )

    assert denied.status_code == 403
    assert saved.status_code == 303
    assert saved.headers["location"] == f"/dashboard/review-tasks/{task_id}"
    assert captured["ground_truth"] == SemanticGroundTruth.ANATOMY_GOOD
    assert captured["issue_code"] is None


def test_anatomy_feedback_template_is_quick_responsive_and_explicit() -> None:
    root = Path(__file__).parents[1]
    template = (root / "src/gen_automation/templates/dashboard/review_task.html").read_text(
        encoding="utf-8"
    )
    css = (root / "src/gen_automation/static/dashboard_ux.css").read_text(encoding="utf-8")

    assert 'data-semantic-mode="{{ semantic_mode }}"' in template
    assert "Predictions are visible for calibration only" in template
    assert "High-confidence severe flags can block" in template
    assert "data-anatomy-learning-status" in template
    assert "Anatomy learning" in template
    assert "Collecting" in template
    assert "Calibrating" in template
    assert "Improved" in template
    assert "Stable" in template
    assert "Regressed" in template
    assert "prior policy retained" in template.lower()
    assert "How learning is measured" in template
    assert "Plain rejection only removes an image from the final set" in template
    assert "never labels" in template
    assert "it as an anatomy defect" in template
    assert "Validation F1" in template
    assert "False-positive rate" in template
    assert "Applied threshold" in template
    assert "Latest candidate" in template
    assert "candidate_threshold_micros" in template
    assert "out-of-fold images" in template
    assert 'principal.role.value == "owner"' in template
    assert "data-anatomy-feedback-form" in template
    assert 'summary.state.value == "completed" and principal.role.value == "owner"' in template
    assert "data-anatomy-training-control" in template
    assert "data-anatomy-training-toggle" in template
    assert "data-anatomy-training-issue" in template
    assert "Use rejection for anatomy training" in template
    assert "Only enable this when anatomy is the reason the image is rejected" in template
    assert 'value="anatomy_good"' in template
    assert 'value="anatomy_defect"' in template
    assert 'value="unjudgeable"' in template
    assert "Good anatomy" in template
    assert "Flag incorrect" in template
    assert "Flag correct" in template
    assert "semantic-issue-chip" in template
    assert "Assessment details" in template
    assert 'principal.role.value == "owner"' in template
    assert "@media (max-width: 680px)" in css
    assert ".anatomy-learning-progress { grid-template-columns: 1fr;" in css
    assert ".anatomy-learning-validation { grid-template-columns: 1fr; }" in css
    assert ".anatomy-feedback-actions { grid-template-columns: 1fr; }" in css
    assert "min-height: 3.2rem" in css


def test_open_review_page_renders_prediction_and_integrated_anatomy_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "anatomy-feedback-page.db")))
    task_id = asyncio.run(_create_task(context))
    first_assessment_id = uuid4()
    second_assessment_id = uuid4()
    completed_at = datetime.now(UTC)
    assessments = {
        context.asset_ids[0]: SemanticReviewAssessment(
            assessment_id=first_assessment_id,
            asset_id=context.asset_ids[0],
            state=SemanticAssessmentState.COMPLETED,
            verdict=SemanticVerdict.PASS,
            confidence_micros=940_000,
            issues=(),
            model_name="private/anatomy-vlm",
            model_revision="revision-123",
            completed_at=completed_at,
            error_code=None,
        ),
        context.asset_ids[1]: SemanticReviewAssessment(
            assessment_id=second_assessment_id,
            asset_id=context.asset_ids[1],
            state=SemanticAssessmentState.COMPLETED,
            verdict=SemanticVerdict.SEVERE,
            confidence_micros=970_000,
            issues=(
                SemanticIssue(
                    code=SemanticIssueCode.MALFORMED_HAND,
                    confidence_micros=960_000,
                ),
            ),
            model_name="private/anatomy-vlm",
            model_revision="revision-123",
            completed_at=completed_at,
            error_code=None,
        ),
    }

    async def load_assessments(*_args: object, **_kwargs: object) -> object:
        return assessments

    async def load_feedback(*_args: object, **_kwargs: object) -> object:
        saved = _feedback_result(
            assessment_id=first_assessment_id,
            user_id=context.users[AdminRole.OWNER].id,
            ground_truth=SemanticGroundTruth.ANATOMY_GOOD,
        )
        return {first_assessment_id: saved}

    async def load_calibration(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(
            effective_threshold_micros=900_000,
            minimum_per_class=20,
            learning_status="collecting",
            minimum_samples=100,
            sample_count=12,
            anatomy_good_count=11,
            anatomy_defect_count=1,
            unjudgeable_count=2,
            explicit_label_count=8,
            inferred_label_count=6,
            validation_sample_count=4,
            validation_f1_micros=500_000,
            validation_f1_delta_micros=None,
            validation_false_positive_rate_micros=None,
            previous_validation_f1_micros=None,
            candidate_threshold_micros=850_000,
            active_policy_changed=False,
            version=3,
            created_at=completed_at,
        )

    monkeypatch.setattr(
        dashboard_routes,
        "_configured_semantic_profile_sha256",
        lambda _settings: "a" * 64,
    )
    monkeypatch.setattr(dashboard_routes, "_semantic_mode", lambda _settings: "shadow")
    monkeypatch.setattr(
        dashboard_routes,
        "load_semantic_review_assessments",
        load_assessments,
    )
    monkeypatch.setattr(
        dashboard_routes,
        "load_semantic_anatomy_feedback",
        load_feedback,
    )
    monkeypatch.setattr(
        dashboard_routes,
        "load_latest_semantic_calibration_artifact",
        load_calibration,
    )

    app = create_app(context.settings)
    with TestClient(app, base_url=ORIGIN, client=("192.0.2.113", 50000)) as client:
        app.state.object_store = SameOriginReviewStore()
        _login(
            client,
            context.users[AdminRole.OWNER],
            csrf_cookie_name=context.settings.auth_csrf_cookie_name,
        )
        page = client.get(f"/dashboard/review-tasks/{task_id}")

    assert page.status_code == 200
    assert 'data-semantic-mode="shadow"' in page.text
    assert "private/anatomy-vlm" in page.text
    assert "revision-123" in page.text
    assert "malformed hand" in page.text
    assert "96.0%" in page.text
    assert "Saved:" in page.text
    assert "data-anatomy-feedback-form" not in page.text
    assert page.text.count("data-review-decision-form") == len(context.asset_ids)
    assert page.text.count("data-anatomy-training-control") == len(context.asset_ids)
    assert page.text.count("data-anatomy-training-toggle") == len(context.asset_ids)
    assert page.text.count("data-anatomy-training-issue") == len(context.asset_ids)
    assert "Use rejection for anatomy training" in page.text
    assert "Plain rejection only removes an image from the final set" in page.text
    assert str(second_assessment_id) in page.text
    assert 'data-anatomy-learning-status="collecting"' in page.text
    assert "12 / 100 useful labels" in page.text
    assert "19 more defect" in page.text
    assert "Not available" in page.text
    assert "From review choices" in page.text
    assert "Applied threshold" in page.text
    assert "90.0%" in page.text
    assert "Latest candidate" in page.text


@pytest.mark.parametrize(
    "mode",
    (
        SemanticEnforcementMode.SHADOW,
        SemanticEnforcementMode.ASSIST,
        SemanticEnforcementMode.ENFORCE,
    ),
)
def test_review_page_distinguishes_missing_and_active_anatomy_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: SemanticEnforcementMode,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / f"anatomy-status-{mode.value}.db")))
    task_id = asyncio.run(_create_task(context))
    visible_assessments: dict[UUID, SemanticReviewAssessment] = {}

    async def load_assessments(*_args: object, **_kwargs: object) -> object:
        return visible_assessments

    async def load_feedback(*_args: object, **_kwargs: object) -> object:
        return {}

    monkeypatch.setattr(
        dashboard_routes,
        "_configured_semantic_profile_sha256",
        lambda _settings: "a" * 64,
    )
    monkeypatch.setattr(
        dashboard_routes,
        "_semantic_mode",
        lambda _settings: mode.value,
    )
    monkeypatch.setattr(
        dashboard_routes,
        "load_semantic_review_assessments",
        load_assessments,
    )
    monkeypatch.setattr(
        dashboard_routes,
        "load_semantic_anatomy_feedback",
        load_feedback,
    )

    settings = context.settings.model_copy(update={"semantic_anatomy_mode": mode})
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN, client=("192.0.2.114", 50000)) as client:
        app.state.object_store = SameOriginReviewStore()
        _login(
            client,
            context.users[AdminRole.OWNER],
            csrf_cookie_name=settings.auth_csrf_cookie_name,
        )

        missing = client.get(f"/dashboard/review-tasks/{task_id}")
        assert missing.status_code == 200
        assert "Anatomy check:" in missing.text
        assert "not scheduled" in missing.text
        assert "not yet scheduled for the current anatomy model" in missing.text
        assert "queued or not yet created" not in missing.text
        assert "data-anatomy-feedback-form" not in missing.text
        if mode == SemanticEnforcementMode.ENFORCE:
            assert (
                "Review completion is blocked until this check completes or becomes unavailable"
                in missing.text
            )
            assert "This check does not block review completion" not in missing.text
        else:
            assert (
                f"This check does not block review completion in {mode.value} mode" in missing.text
            )
            assert "Review completion is blocked until this check completes" not in missing.text

        state_copy = {
            SemanticAssessmentState.PENDING: (
                "queued",
                "queued and waiting for an anatomy worker",
            ),
            SemanticAssessmentState.PROCESSING: (
                "processing",
                "currently processing",
            ),
            SemanticAssessmentState.RETRY_WAIT: (
                "waiting to retry",
                "waiting for an automatic retry after a temporary failure",
            ),
        }
        for state, (heading, detail) in state_copy.items():
            visible_assessments.clear()
            visible_assessments.update(
                {
                    asset_id: SemanticReviewAssessment(
                        assessment_id=uuid4(),
                        asset_id=asset_id,
                        state=state,
                        verdict=None,
                        confidence_micros=None,
                        issues=(),
                        model_name="private/anatomy-vlm",
                        model_revision="revision-123",
                        completed_at=None,
                        error_code=None,
                    )
                    for asset_id in context.asset_ids
                }
            )
            page = client.get(f"/dashboard/review-tasks/{task_id}")

            assert page.status_code == 200
            assert heading in page.text
            assert detail in page.text
            assert "data-anatomy-feedback-form" not in page.text
            assert "pending; decisions remain available but completion is blocked" not in page.text

            if mode == SemanticEnforcementMode.ENFORCE:
                assert (
                    "Review completion is blocked until this check completes or becomes unavailable"
                ) in page.text
                assert "This check does not block review completion" not in page.text
            else:
                assert (
                    f"This check does not block review completion in {mode.value} mode" in page.text
                )
                assert "Review completion is blocked until this check completes" not in page.text
