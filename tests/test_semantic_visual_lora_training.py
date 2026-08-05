from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from gen_automation.domain.enums import (
    SemanticGroundTruth,
    SemanticIssueCode,
    SemanticVerdict,
)
from gen_automation.semantic.contract import assessment_profile_sha256
from gen_automation.services.semantic_learning_readiness import (
    SOURCE_EXPLICIT,
    SemanticLearningSample,
    summarize_semantic_learning_readiness,
)
from gen_automation.services.semantic_visual_lora_training import (
    ImmutableTrainingObject,
    RunPodVisualLoraTrainingPlan,
    VisualLoraDatasetIdentity,
    VisualLoraInputArtifacts,
    VisualLoraRunPodLimits,
    VisualLoraShadowOutput,
    VisualLoraSplitIdentity,
    VisualLoraStandingPolicyFacts,
    VisualLoraTrainerIdentity,
    VisualLoraTrainingIdentityError,
    VisualLoraTrainingNotReadyError,
    VisualLoraTrainingPolicyError,
    build_runpod_visual_lora_training_plan,
    evaluate_visual_lora_standing_policy_admission,
    evaluate_visual_lora_training_gate,
    maximum_visual_lora_cost_microusd,
    require_matching_visual_lora_replay,
)

_NOW = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)
_OWNER_ID = UUID(int=71_000)
_MODEL = "Qwen/Qwen3-VL-8B-Instruct"
_MODEL_REVISION = "60595ebc30ec8e3b1d3b9e65d4943ca011c0006a"
_PROFILE_SHA256 = assessment_profile_sha256(model=_MODEL, model_revision=_MODEL_REVISION)


def _sample(index: int) -> SemanticLearningSample:
    is_good = index < 1_500
    issues = (
        SemanticIssueCode.EXTRA_FINGER,
        SemanticIssueCode.EXTRA_TOE,
        SemanticIssueCode.EXTRA_LIMB,
        SemanticIssueCode.IMPOSSIBLE_JOINT,
    )
    return SemanticLearningSample(
        feedback_id=UUID(int=100_000 + index),
        assessment_id=UUID(int=200_000 + index),
        asset_id=UUID(int=300_000 + index),
        feedback_by_user_id=_OWNER_ID,
        profile_sha256=_PROFILE_SHA256,
        asset_sha256=f"{index + 1:064x}",
        ground_truth=(
            SemanticGroundTruth.ANATOMY_GOOD
            if is_good
            else SemanticGroundTruth.ANATOMY_DEFECT
        ),
        owner_issue_code=None if is_good else issues[index % len(issues)],
        source=SOURCE_EXPLICIT,
        verdict=SemanticVerdict.PASS if is_good else SemanticVerdict.SEVERE,
        confidence_micros=900_000,
        predicted_issues=(),
        release_id=UUID(int=400_000 + index % 5),
        generation_job_id=UUID(int=500_000 + index % 10),
        generated_at=_NOW + timedelta(days=index % 5),
        labeled_at=_NOW + timedelta(days=index % 5, minutes=index),
        completed_review=True,
    )


@pytest.fixture(scope="module")
def ready_inputs() -> dict[str, object]:
    report = summarize_semantic_learning_readiness(_sample(index) for index in range(2_000))
    profile = report.profiles[0]
    assert profile.lora.ready
    assert profile.meta_evaluation.ready
    holdout = profile.split.evaluation_holdout
    assert holdout is not None

    dataset = VisualLoraDatasetIdentity(
        owner_user_id=_OWNER_ID,
        semantic_profile_sha256=_PROFILE_SHA256,
        readiness_dataset_sha256=profile.dataset_sha256,
        label_manifest_sha256="b" * 64,
        assessment_model=_MODEL,
        assessment_model_revision=_MODEL_REVISION,
        binary_label_count=profile.binary_labeled_count,
        anatomy_good_count=profile.anatomy_good_count,
        anatomy_defect_count=profile.anatomy_defect_count,
        issue_coded_defect_count=profile.issue_coded_defect_count,
        explicit_label_count=profile.explicit_label_count,
    )
    split = VisualLoraSplitIdentity(
        dataset_label_manifest_sha256=dataset.label_manifest_sha256,
        split_manifest_sha256="c" * 64,
        group_key=holdout.group_key,
        cutoff_at=holdout.cutoff_at,
        training_count=holdout.training_count,
        training_good_count=holdout.training_good_count,
        training_defect_count=holdout.training_defect_count,
        holdout_count=holdout.holdout_count,
        holdout_good_count=holdout.holdout_good_count,
        holdout_defect_count=holdout.holdout_defect_count,
    )
    artifacts = VisualLoraInputArtifacts(
        dataset_archive=ImmutableTrainingObject(
            bucket="gen-automation-training",
            object_key="visual-lora/datasets/dataset-v1.tar",
            object_version_id="dataset-version-1",
            sha256="d" * 64,
            exact_size_bytes=500_000_000,
            content_type="application/x-tar",
        ),
        split_manifest=ImmutableTrainingObject(
            bucket="gen-automation-training",
            object_key="visual-lora/splits/split-v1.json",
            object_version_id="split-version-1",
            sha256=split.split_manifest_sha256,
            exact_size_bytes=200_000,
            content_type="application/json",
        ),
        recipe_manifest=ImmutableTrainingObject(
            bucket="gen-automation-training",
            object_key="visual-lora/recipes/recipe-v1.json",
            object_version_id="recipe-version-1",
            sha256="e" * 64,
            exact_size_bytes=8_000,
            content_type="application/json",
        ),
    )
    trainer = VisualLoraTrainerIdentity(
        trainer_contract_version="semantic-anatomy-visual-lora-trainer/v1",
        container_image="registry.example/gen-automation/visual-lora-trainer@sha256:"
        + "f" * 64,
        base_model=_MODEL,
        base_model_revision=_MODEL_REVISION,
    )
    runtime = 2 * 60 * 60
    hourly_cost = 1_500_000
    limits = VisualLoraRunPodLimits(
        gpu_type_ids=("NVIDIA A40", "NVIDIA RTX A6000"),
        max_runtime_seconds=runtime,
        max_hourly_cost_microusd=hourly_cost,
        max_cost_microusd=maximum_visual_lora_cost_microusd(
            max_hourly_cost_microusd=hourly_cost,
            max_runtime_seconds=runtime,
        ),
    )
    output = VisualLoraShadowOutput(
        bucket="gen-automation-training",
        object_key="visual-lora/challengers/qwen3-vl-owner-71000-v1.safetensors",
        max_size_bytes=1_000_000_000,
    )
    standing_policy = VisualLoraStandingPolicyFacts(
        owner_user_id=_OWNER_ID,
        learning_enabled=True,
        auto_train_visual=True,
        max_visual_run_microusd=10_000_000,
        lock_version=1,
    )
    return {
        "report": report,
        "dataset": dataset,
        "split": split,
        "artifacts": artifacts,
        "trainer": trainer,
        "limits": limits,
        "output": output,
        "standing_policy": standing_policy,
    }


def _build(values: dict[str, object]) -> RunPodVisualLoraTrainingPlan:
    return build_runpod_visual_lora_training_plan(
        values["report"],  # type: ignore[arg-type]
        owner_user_id=_OWNER_ID,
        profile_sha256=_PROFILE_SHA256,
        dataset=values["dataset"],  # type: ignore[arg-type]
        split=values["split"],  # type: ignore[arg-type]
        artifacts=values["artifacts"],  # type: ignore[arg-type]
        trainer=values["trainer"],  # type: ignore[arg-type]
        recipe_sha256="e" * 64,
        limits=values["limits"],  # type: ignore[arg-type]
        output=values["output"],  # type: ignore[arg-type]
        standing_policy=values["standing_policy"],  # type: ignore[arg-type]
    )


def test_plan_is_deterministic_spend_disabled_and_shadow_only(
    ready_inputs: dict[str, object],
) -> None:
    first = _build(ready_inputs)
    second = _build(dict(reversed(tuple(ready_inputs.items()))))

    assert first == second
    assert first.mutates_runpod is False
    assert first.provider_spend_started is False
    assert first.provider_submission_available is False
    assert first.requires_persisted_standing_policy is True
    assert first.per_run_confirmation_required is False
    assert first.standing_policy_authorizes_bounded_training is True
    assert first.request.limits.workers_min == 0
    assert first.request.limits.workers_max == 1
    assert first.request.limits.gpu_count == 1
    assert first.request.limits.max_cost_microusd == 3_000_000
    assert first.request.dataset.issue_coded_defect_count == 500
    assert first.request.output.deployment_mode == "shadow"
    assert first.request.output.auto_promote is False
    assert first.request.output.may_change_review_decisions is False
    assert first.request.idempotency_key == (
        f"visual-lora-v1:{first.request.request_sha256}"
    )
    wire = first.model_dump_json()
    assert "https://" not in wire
    assert "api_key" not in wire.casefold()
    assert "secret" not in wire.casefold()


@pytest.mark.parametrize(
    ("policy_update", "blocker"),
    [
        ({"learning_enabled": False}, "learning is disabled"),
        ({"auto_train_visual": False}, "automatic visual training is disabled"),
        ({"max_visual_run_microusd": 2_999_999}, "cost cap is below"),
        ({"owner_user_id": UUID(int=99)}, "different owner"),
    ],
)
def test_standing_policy_admission_is_unattended_and_fail_closed(
    ready_inputs: dict[str, object],
    policy_update: dict[str, object],
    blocker: str,
) -> None:
    policy = ready_inputs["standing_policy"]
    assert isinstance(policy, VisualLoraStandingPolicyFacts)
    blocked = policy.model_copy(update=policy_update)
    admission = evaluate_visual_lora_standing_policy_admission(
        blocked,
        owner_user_id=_OWNER_ID,
        plan_max_cost_microusd=3_000_000,
    )
    assert not admission.admitted
    assert any(blocker in item for item in admission.blockers)

    with pytest.raises(VisualLoraTrainingPolicyError, match="standing policy"):
        _build({**ready_inputs, "standing_policy": blocked})


def test_missing_standing_policy_is_not_admitted() -> None:
    admission = evaluate_visual_lora_standing_policy_admission(
        None,
        owner_user_id=_OWNER_ID,
        plan_max_cost_microusd=3_000_000,
    )

    assert not admission.admitted
    assert admission.policy_lock_version is None
    assert admission.blockers == ("persisted standing learning policy is required",)


def test_gate_and_builder_fail_closed_before_readiness(
    ready_inputs: dict[str, object],
) -> None:
    report = summarize_semantic_learning_readiness((_sample(0),))
    gate = evaluate_visual_lora_training_gate(
        report,
        owner_user_id=_OWNER_ID,
        profile_sha256=_PROFILE_SHA256,
    )
    assert not gate.admitted
    assert gate.blockers

    values = {**ready_inputs, "report": report}
    with pytest.raises(VisualLoraTrainingNotReadyError) as caught:
        _build(values)
    assert caught.value.blockers == gate.blockers


def test_gate_rechecks_counts_instead_of_trusting_a_ready_flag(
    ready_inputs: dict[str, object],
) -> None:
    report = ready_inputs["report"]
    profile = report.profiles[0]  # type: ignore[union-attr]
    inconsistent = replace(
        report,  # type: ignore[arg-type]
        profiles=(replace(profile, binary_labeled_count=1),),
    )

    gate = evaluate_visual_lora_training_gate(
        inconsistent,
        owner_user_id=_OWNER_ID,
        profile_sha256=_PROFILE_SHA256,
    )

    assert not gate.admitted
    assert "readiness count is below 2000 binary labels" in gate.blockers


def test_builder_rejects_stale_dataset_and_split_identities(
    ready_inputs: dict[str, object],
) -> None:
    dataset = ready_inputs["dataset"]
    assert isinstance(dataset, VisualLoraDatasetIdentity)
    with pytest.raises(VisualLoraTrainingIdentityError, match="dataset identity"):
        _build(
            {
                **ready_inputs,
                "dataset": dataset.model_copy(update={"readiness_dataset_sha256": "0" * 64}),
            }
        )
    with pytest.raises(VisualLoraTrainingIdentityError, match="dataset identity"):
        _build(
            {
                **ready_inputs,
                "dataset": dataset.model_copy(update={"issue_coded_defect_count": 499}),
            }
        )

    split = ready_inputs["split"]
    assert isinstance(split, VisualLoraSplitIdentity)
    with pytest.raises(VisualLoraTrainingIdentityError, match="split identity"):
        _build(
            {
                **ready_inputs,
                "split": split.model_copy(
                    update={"cutoff_at": split.cutoff_at + timedelta(seconds=1)}
                ),
            }
        )


def test_builder_rejects_artifact_role_or_model_drift(
    ready_inputs: dict[str, object],
) -> None:
    artifacts = ready_inputs["artifacts"]
    assert isinstance(artifacts, VisualLoraInputArtifacts)
    with pytest.raises(VisualLoraTrainingIdentityError, match="split object"):
        _build(
            {
                **ready_inputs,
                "artifacts": artifacts.model_copy(
                    update={
                        "split_manifest": artifacts.split_manifest.model_copy(
                            update={"sha256": "9" * 64}
                        )
                    }
                ),
            }
        )

    trainer = ready_inputs["trainer"]
    assert isinstance(trainer, VisualLoraTrainerIdentity)
    with pytest.raises(VisualLoraTrainingIdentityError, match="base model"):
        _build(
            {
                **ready_inputs,
                "trainer": trainer.model_copy(update={"base_model_revision": "1" * 40}),
            }
        )


def test_execution_limits_and_trainer_must_be_pinned_and_self_consistent() -> None:
    with pytest.raises(ValidationError, match="pinned by SHA-256"):
        VisualLoraTrainerIdentity(
            trainer_contract_version="semantic-anatomy-visual-lora-trainer/v1",
            container_image="registry.example/" + "x" * 70 + ":latest",
            base_model=_MODEL,
            base_model_revision=_MODEL_REVISION,
        )

    with pytest.raises(ValidationError, match="total cost cap"):
        VisualLoraRunPodLimits(
            gpu_type_ids=("NVIDIA A40",),
            max_runtime_seconds=3_600,
            max_hourly_cost_microusd=1_000_000,
            max_cost_microusd=999_999,
        )


def test_request_integrity_and_idempotency_replay_are_fail_closed(
    ready_inputs: dict[str, object],
) -> None:
    first = _build(ready_inputs).request
    serialized = first.model_dump()
    serialized["request_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="request digest"):
        type(first).model_validate(serialized)

    assert require_matching_visual_lora_replay(first, first) is first
    output = ready_inputs["output"]
    assert isinstance(output, VisualLoraShadowOutput)
    second = _build(
        {
            **ready_inputs,
            "output": output.model_copy(
                update={
                    "object_key": "visual-lora/challengers/qwen3-vl-owner-71000-v2.safetensors"
                }
            ),
        }
    ).request
    with pytest.raises(VisualLoraTrainingIdentityError, match="different immutable request"):
        require_matching_visual_lora_replay(first, second)
