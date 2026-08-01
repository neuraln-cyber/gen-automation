from gen_automation.db.models import Release, ScoringRun
from gen_automation.domain.enums import (
    GenerationAttemptState,
    GenerationState,
    ReleasePhase,
    ResourceHealth,
    ScoringRunState,
)
from gen_automation.services.new_sets import (
    GenerationProgressStage,
    GenerationScoringProgress,
    _generation_progress_stage,
)


def _release(
    *,
    phase: ReleasePhase = ReleasePhase.GENERATING,
    health: ResourceHealth = ResourceHealth.HEALTHY,
) -> Release:
    return Release(phase=phase, health=health)


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
