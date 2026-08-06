from datetime import UTC, datetime, timedelta

import pytest

from gen_automation.db.models import Release, SaladDeployment, ScoringRun
from gen_automation.domain.enums import (
    GenerationAttemptState,
    GenerationState,
    ReleasePhase,
    ResourceHealth,
    SaladDeploymentState,
    ScoringRunState,
)
from gen_automation.services.new_sets import (
    GenerationProgressError,
    GenerationProgressStage,
    GenerationProgressStageView,
    GenerationScoringProgress,
    _generation_progress_stage,
    _overlay_provider_preparation,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _release(
    *,
    phase: ReleasePhase = ReleasePhase.GENERATING,
    health: ResourceHealth = ResourceHealth.HEALTHY,
) -> Release:
    return Release(phase=phase, health=health)


def _stage(*, key: GenerationProgressStage, step: int) -> GenerationProgressStageView:
    return GenerationProgressStageView(
        key=key,
        step=step,
        step_count=5,
        label="Generic status",
        detail="Generic detail.",
    )


def _deployment(
    *,
    state: SaladDeploymentState = SaladDeploymentState.PROVISIONING,
    provider_status: str = ("queue=1;group=pending;pending=1;phase=image_pull;progress=21"),
    last_error_code: str = "provider_image_preparation_pending",
    last_observed_at: datetime = NOW,
    is_current: bool = True,
) -> SaladDeployment:
    return SaladDeployment(
        version_no=1,
        config_sha256="a" * 64,
        provider_configuration={},
        worker_image_digest=f"registry.example/worker@sha256:{'b' * 64}",
        organization_name="organization",
        project_name="project",
        queue_name="queue",
        container_group_name="group",
        state=state,
        is_current=is_current,
        max_hourly_cost_microusd=1_000_000,
        provider_status=provider_status,
        last_observed_at=last_observed_at,
        last_error_code=last_error_code,
    )


def test_generation_progress_distinguishes_gpu_startup_from_active_generation() -> None:
    startup, startup_error = _generation_progress_stage(
        release=_release(),
        state_counts={GenerationState.RUNNING: 1},
        attempt_counts={GenerationAttemptState.SUBMITTED: 1},
        generated_outputs=0,
        expected_outputs=4,
        scoring_run=None,
        scoring_progress=None,
        ranking_count=0,
        ready_for_review=False,
        failed_jobs=0,
    )
    generating, generating_error = _generation_progress_stage(
        release=_release(),
        state_counts={GenerationState.RUNNING: 1},
        attempt_counts={GenerationAttemptState.RUNNING: 1},
        generated_outputs=1,
        expected_outputs=4,
        scoring_run=None,
        scoring_progress=None,
        ranking_count=0,
        ready_for_review=False,
        failed_jobs=0,
    )

    assert startup.key == GenerationProgressStage.GPU_STARTING
    assert startup.step == 2
    assert startup_error is None
    assert generating.key == GenerationProgressStage.GENERATING
    assert generating.step == 3
    assert "1 of 4" in generating.detail
    assert generating_error is None


def test_generation_progress_tracks_scoring_then_opens_review() -> None:
    running = ScoringRun(state=ScoringRunState.RUNNING, asset_count=4)
    scoring, scoring_error = _generation_progress_stage(
        release=_release(phase=ReleasePhase.REVIEWING),
        state_counts={GenerationState.SUCCEEDED: 1},
        attempt_counts={},
        generated_outputs=4,
        expected_outputs=4,
        scoring_run=running,
        scoring_progress=GenerationScoringProgress(completed=2, total=4, percent=50.0),
        ranking_count=0,
        ready_for_review=False,
        failed_jobs=0,
    )
    completed = ScoringRun(state=ScoringRunState.COMPLETED, asset_count=4)
    review, review_error = _generation_progress_stage(
        release=_release(phase=ReleasePhase.REVIEWING),
        state_counts={GenerationState.SUCCEEDED: 1},
        attempt_counts={},
        generated_outputs=4,
        expected_outputs=4,
        scoring_run=completed,
        scoring_progress=GenerationScoringProgress(completed=4, total=4, percent=100.0),
        ranking_count=4,
        ready_for_review=True,
        failed_jobs=0,
    )

    assert scoring.key == GenerationProgressStage.SCORING
    assert scoring.step == 4
    assert "2 of 4" in scoring.detail
    assert scoring_error is None
    assert review.key == GenerationProgressStage.REVIEW
    assert review.step == 5
    assert review_error is None


def test_generation_progress_fails_closed_for_terminal_jobs_and_incomplete_ranking() -> None:
    generation_error_stage, generation_error = _generation_progress_stage(
        release=_release(health=ResourceHealth.BLOCKED),
        state_counts={GenerationState.DEAD_LETTER: 1},
        attempt_counts={},
        generated_outputs=2,
        expected_outputs=4,
        scoring_run=None,
        scoring_progress=None,
        ranking_count=0,
        ready_for_review=False,
        failed_jobs=1,
    )
    completed = ScoringRun(state=ScoringRunState.COMPLETED, asset_count=4)
    ranking_error_stage, ranking_error = _generation_progress_stage(
        release=_release(phase=ReleasePhase.REVIEWING),
        state_counts={GenerationState.SUCCEEDED: 1},
        attempt_counts={},
        generated_outputs=4,
        expected_outputs=4,
        scoring_run=completed,
        scoring_progress=GenerationScoringProgress(completed=4, total=4, percent=100.0),
        ranking_count=3,
        ready_for_review=False,
        failed_jobs=0,
    )

    assert generation_error_stage.key == GenerationProgressStage.ERROR
    assert generation_error is not None
    assert generation_error.code == "generation_failed"
    assert generation_error.retryable is False
    assert ranking_error_stage.key == GenerationProgressStage.ERROR
    assert ranking_error is not None
    assert ranking_error.code == "ranking_incomplete"


@pytest.mark.parametrize(
    ("stage_key", "step"),
    (
        (GenerationProgressStage.QUEUED, 1),
        (GenerationProgressStage.GPU_STARTING, 2),
    ),
)
def test_worker_image_preparation_overlays_only_starting_progress(
    stage_key: GenerationProgressStage,
    step: int,
) -> None:
    stage, error = _overlay_provider_preparation(
        _stage(key=stage_key, step=step),
        None,
        deployment=_deployment(),
        now=NOW,
    )

    assert stage.key == stage_key
    assert stage.step == step
    assert stage.label == "Preparing worker image (21%)"
    assert "start automatically" in stage.detail
    assert error is None


def test_worker_image_preparation_accepts_sanitized_unknown_group_status() -> None:
    stage, error = _overlay_provider_preparation(
        _stage(key=GenerationProgressStage.QUEUED, step=1),
        None,
        deployment=_deployment(
            provider_status=("queue=1;group=unknown;pending=1;phase=image_pull;progress=21")
        ),
        now=NOW,
    )

    assert stage.label == "Preparing worker image (21%)"
    assert error is None


def test_worker_image_preparation_stall_is_visible_retryable_and_keeps_polling() -> None:
    stage, error = _overlay_provider_preparation(
        _stage(key=GenerationProgressStage.QUEUED, step=1),
        None,
        deployment=_deployment(
            state=SaladDeploymentState.DEGRADED,
            last_error_code="provider_image_preparation_stalled",
        ),
        now=NOW,
    )

    assert stage.key == GenerationProgressStage.ERROR
    assert stage.step == 1
    assert stage.label == "Worker image preparation stalled"
    assert "at least 30 minutes" in stage.detail
    assert error == GenerationProgressError(
        code="provider_image_preparation_stalled",
        message=(
            "Worker image preparation has not advanced for at least 30 minutes. "
            "You can stop this run safely; no generated images will be discarded."
        ),
        retryable=True,
    )


@pytest.mark.parametrize(
    "deployment",
    (
        _deployment(
            provider_status=(
                "queue=1;group=pending;pending=1;phase=image_pull;progress=21;raw=unsafe"
            )
        ),
        _deployment(last_observed_at=NOW - timedelta(minutes=6)),
        _deployment(state=SaladDeploymentState.ACTIVE),
        _deployment(last_error_code="provider_start_pending"),
        _deployment(is_current=False),
    ),
    ids=("malformed", "stale", "other-state", "other-error", "not-current"),
)
def test_worker_image_preparation_falls_back_for_untrusted_or_irrelevant_state(
    deployment: SaladDeployment,
) -> None:
    original = _stage(key=GenerationProgressStage.QUEUED, step=1)
    stage, error = _overlay_provider_preparation(
        original,
        None,
        deployment=deployment,
        now=NOW,
    )

    assert stage is original
    assert error is None


def test_worker_image_preparation_never_masks_active_generation() -> None:
    original = _stage(key=GenerationProgressStage.GENERATING, step=3)
    existing_error = GenerationProgressError(
        code="existing",
        message="Existing generation status.",
        retryable=False,
    )

    stage, error = _overlay_provider_preparation(
        original,
        existing_error,
        deployment=_deployment(
            state=SaladDeploymentState.DEGRADED,
            last_error_code="provider_image_preparation_stalled",
        ),
        now=NOW,
    )

    assert stage is original
    assert error is existing_error
