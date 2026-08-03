import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from gen_automation.api.routes import dashboard as dashboard_routes
from gen_automation.api.routes import review_tasks as review_routes
from gen_automation.app import create_app
from gen_automation.domain.enums import (
    AdminRole,
    SemanticAssessmentState,
    SemanticEnforcementMode,
    SemanticFeedbackAgreement,
    SemanticGroundTruth,
    SemanticIssueCode,
    SemanticVerdict,
)
from gen_automation.semantic import SemanticIssue
from gen_automation.services.semantic_anatomy import SemanticReviewAssessment
from gen_automation.services.semantic_feedback import SemanticAnatomyFeedbackResult
from tests.test_dashboard_review import _FORM_HEADERS, SameOriginReviewStore, _create_task
from tests.test_review_api import (
    ORIGIN,
    PASSWORD,
    UserCredential,
    _seed_review_api,
    _settings,
    _totp_code,
)


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


def test_owner_can_record_anatomy_feedback_through_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "anatomy-feedback-api.db")))
    task_id = asyncio.run(_create_task(context))
    assessment_id = uuid4()
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
        lambda _request: ("a" * 64, 900_000, SemanticEnforcementMode.SHADOW),
    )
    monkeypatch.setattr(
        review_routes,
        "semantic_feedback_target_belongs_to_review",
        _feedback_target_exists,
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
    template = (
        root / "src/gen_automation/templates/dashboard/review_task.html"
    ).read_text(encoding="utf-8")
    css = (root / "src/gen_automation/static/dashboard_ux.css").read_text(encoding="utf-8")

    assert 'data-semantic-mode="{{ semantic_mode }}"' in template
    assert "Predictions are visible for calibration only" in template
    assert "High-confidence severe flags can block" in template
    assert "data-anatomy-feedback-form" in template
    assert 'value="anatomy_good"' in template
    assert 'value="anatomy_defect"' in template
    assert 'value="unjudgeable"' in template
    assert "Good anatomy" in template
    assert "Flag incorrect" in template
    assert "Flag correct" in template
    assert "semantic-issue-chip" in template
    assert "Assessment details" in template
    assert "principal.role.value == \"owner\"" in template
    assert "@media (max-width: 680px)" in css
    assert ".anatomy-feedback-actions { grid-template-columns: 1fr; }" in css
    assert "min-height: 3.2rem" in css


def test_review_page_renders_prediction_details_saved_feedback_and_quick_actions(
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
    assert page.text.count("data-anatomy-feedback-form") == 1
    assert str(second_assessment_id) in page.text
    assert "Flag correct" in page.text
    assert "Flag incorrect" in page.text
