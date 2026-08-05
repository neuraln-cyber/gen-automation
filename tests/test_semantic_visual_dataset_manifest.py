from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from gen_automation.domain.enums import (
    SemanticGroundTruth,
    SemanticIssueCode,
    SemanticVerdict,
)
from gen_automation.services.semantic_learning_readiness import (
    SOURCE_EXPLICIT,
    SOURCE_INFERRED_REVIEW_ACCEPT,
    SemanticLearningSample,
)
from gen_automation.services.semantic_visual_dataset_manifest import (
    SemanticVisualAssetBinding,
    SemanticVisualAssetIdentity,
    SemanticVisualDatasetIdentityError,
    build_semantic_visual_dataset_manifest,
    semantic_visual_dataset_manifest_bytes,
)

_NOW = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)
_OWNER = UUID(int=90_000)
_PROFILE = "a" * 64


def _sample(
    index: int,
    *,
    release_number: int,
    truth: SemanticGroundTruth,
    source: str = SOURCE_EXPLICIT,
    sha256: str | None = None,
    issue_code: SemanticIssueCode | None = None,
) -> SemanticLearningSample:
    return SemanticLearningSample(
        feedback_id=UUID(int=100_000 + index),
        assessment_id=UUID(int=110_000 + index),
        asset_id=UUID(int=120_000 + index),
        feedback_by_user_id=_OWNER,
        profile_sha256=_PROFILE,
        asset_sha256=sha256 or f"{index + 1:064x}",
        ground_truth=truth,
        owner_issue_code=issue_code,
        source=source,
        verdict=(
            SemanticVerdict.SEVERE
            if truth == SemanticGroundTruth.ANATOMY_DEFECT
            else SemanticVerdict.PASS
        ),
        confidence_micros=900_000,
        predicted_issues=(),
        release_id=UUID(int=130_000 + release_number),
        generation_job_id=UUID(int=140_000 + release_number),
        generated_at=_NOW + timedelta(days=release_number),
        labeled_at=_NOW + timedelta(days=release_number, minutes=index),
        completed_review=True,
        checkpoint_cohort="1" * 64,
        lora_stack_cohort="2" * 64,
        workflow_cohort="3" * 64,
        style_cohort="4" * 64,
    )


def _binding(sample: SemanticLearningSample) -> SemanticVisualAssetBinding:
    return SemanticVisualAssetBinding(
        assessment_id=sample.assessment_id,
        asset_id=sample.asset_id,
        semantic_profile_sha256=sample.profile_sha256,
        asset=SemanticVisualAssetIdentity(
            bucket="visual-learning-test",
            object_key=f"masters/{sample.asset_id}.png",
            object_version_id=f"version-{sample.asset_id}",
            sha256=sample.asset_sha256,
            exact_size_bytes=10_000 + sample.asset_id.int,
            media_type="image/png",
        ),
    )


def _dataset() -> tuple[SemanticLearningSample, ...]:
    return tuple(
        _sample(
            release_number * 2 + offset,
            release_number=release_number,
            truth=truth,
            issue_code=(
                SemanticIssueCode.EXTRA_FINGER
                if truth == SemanticGroundTruth.ANATOMY_DEFECT
                else None
            ),
        )
        for release_number in range(3)
        for offset, truth in enumerate(
            (
                SemanticGroundTruth.ANATOMY_GOOD,
                SemanticGroundTruth.ANATOMY_DEFECT,
            )
        )
    )


def test_manifest_is_deterministic_and_freezes_whole_group_holdout() -> None:
    samples = _dataset()
    bindings = {sample.assessment_id: _binding(sample) for sample in reversed(samples)}

    first = build_semantic_visual_dataset_manifest(
        samples=reversed(samples),
        asset_bindings=bindings,
        owner_user_id=_OWNER,
        profile_sha256=_PROFILE,
    )
    second = build_semantic_visual_dataset_manifest(
        samples=samples,
        asset_bindings=dict(reversed(tuple(bindings.items()))),
        owner_user_id=_OWNER,
        profile_sha256=_PROFILE,
    )

    assert first == second
    assert first.group_key == "release_id"
    assert first.training_count + first.holdout_count == len(samples)
    training_groups = {
        entry.group_id for entry in first.entries if entry.membership == "train"
    }
    holdout_groups = {
        entry.group_id
        for entry in first.entries
        if entry.membership == "untouched_holdout"
    }
    assert training_groups.isdisjoint(holdout_groups)
    assert all(entry.asset.storage_backend == "s3" for entry in first.entries)
    payload = semantic_visual_dataset_manifest_bytes(first)
    assert b"prompt" not in payload.lower()
    assert b"credential" not in payload.lower()
    assert first.manifest_sha256 == second.manifest_sha256


def test_manifest_uses_readiness_strongest_evidence_dedupe() -> None:
    samples = list(_dataset())
    original = samples[0]
    replacement = _sample(
        50,
        release_number=0,
        truth=SemanticGroundTruth.ANATOMY_DEFECT,
        source=SOURCE_EXPLICIT,
        sha256=original.asset_sha256,
        issue_code=SemanticIssueCode.MALFORMED_HAND,
    )
    samples[0] = _sample(
        51,
        release_number=0,
        truth=SemanticGroundTruth.ANATOMY_GOOD,
        source=SOURCE_INFERRED_REVIEW_ACCEPT,
        sha256=original.asset_sha256,
    )
    samples.append(replacement)
    bindings = {sample.assessment_id: _binding(sample) for sample in samples}

    manifest = build_semantic_visual_dataset_manifest(
        samples=samples,
        asset_bindings=bindings,
        owner_user_id=_OWNER,
        profile_sha256=_PROFILE,
    )

    selected = [
        entry for entry in manifest.entries if entry.asset.sha256 == original.asset_sha256
    ]
    assert len(selected) == 1
    assert selected[0].feedback_id == replacement.feedback_id
    assert selected[0].binary_label == "anatomy_defect"
    assert selected[0].owner_issue_code == SemanticIssueCode.MALFORMED_HAND


def test_manifest_fails_closed_on_asset_identity_drift() -> None:
    samples = _dataset()
    bindings = {sample.assessment_id: _binding(sample) for sample in samples}
    first = samples[0]
    binding = bindings[first.assessment_id]
    bindings[first.assessment_id] = binding.model_copy(
        update={
            "asset": binding.asset.model_copy(update={"sha256": "f" * 64}),
        }
    )

    with pytest.raises(SemanticVisualDatasetIdentityError, match="content identity"):
        build_semantic_visual_dataset_manifest(
            samples=samples,
            asset_bindings=bindings,
            owner_user_id=_OWNER,
            profile_sha256=_PROFILE,
        )


@pytest.mark.parametrize("version_id", ["", "null", "https://signed.example/object"])
def test_asset_identity_requires_a_non_url_versioned_s3_object(version_id: str) -> None:
    with pytest.raises(ValidationError):
        SemanticVisualAssetIdentity(
            bucket="visual-learning-test",
            object_key="masters/image.png",
            object_version_id=version_id,
            sha256="b" * 64,
            exact_size_bytes=100,
            media_type="image/png",
        )
