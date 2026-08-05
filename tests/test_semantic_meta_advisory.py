from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import gen_automation.services.semantic_meta_advisory as advisory_service
from gen_automation.domain.enums import (
    SemanticAssessmentState,
    SemanticIssueCode,
    SemanticVerdict,
)
from gen_automation.semantic import SemanticIssue
from gen_automation.services.semantic_anatomy import SemanticReviewAssessment
from gen_automation.services.semantic_learning_readiness import SEMANTIC_META_FEATURE_NAMES
from gen_automation.services.semantic_meta_advisory import load_semantic_meta_advisories
from gen_automation.services.semantic_meta_classifier import (
    SemanticMetaClassifierError,
    SemanticMetaModel,
    SemanticMetaTrainingParameters,
    SemanticMetaTriage,
)


def _model(*, owner_user_id: UUID, profile_sha256: str) -> SemanticMetaModel:
    weights = [0.0] * len(SEMANTIC_META_FEATURE_NAMES)
    weights[2] = 2.0  # The pinned third feature is ``verdict_severe``.
    return SemanticMetaModel(
        feature_schema_version="semantic-anatomy-meta-features/v1",
        feature_names=SEMANTIC_META_FEATURE_NAMES,
        owner_user_id=str(owner_user_id),
        profile_sha256=profile_sha256,
        training_dataset_sha256="b" * 64,
        split_manifest_sha256="c" * 64,
        fit_sample_sha256="d" * 64,
        fit_sample_count=200,
        parameters=SemanticMetaTrainingParameters(),
        feature_means=(0.0,) * len(SEMANTIC_META_FEATURE_NAMES),
        feature_scales=(1.0,) * len(SEMANTIC_META_FEATURE_NAMES),
        weights=tuple(weights),
        intercept=-1.0,
        keep_threshold_micros=300_000,
        reject_threshold_micros=700_000,
    )


def _assessment(
    asset_id: UUID,
    *,
    state: SemanticAssessmentState,
    verdict: SemanticVerdict | None,
) -> SemanticReviewAssessment:
    return SemanticReviewAssessment(
        assessment_id=uuid4(),
        asset_id=asset_id,
        state=state,
        verdict=verdict,
        confidence_micros=900_000 if verdict is not None else None,
        issues=(
            (
                SemanticIssue(
                    code=SemanticIssueCode.EXTRA_FINGER,
                    confidence_micros=850_000,
                ),
            )
            if verdict == SemanticVerdict.SEVERE
            else ()
        ),
        model_name="private/anatomy-vlm",
        model_revision="revision-1",
        completed_at=datetime.now(UTC) if state == SemanticAssessmentState.COMPLETED else None,
        error_code=None,
    )


def test_promoted_model_scores_only_completed_same_scope_assessments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_user_id = uuid4()
    profile_sha256 = "a" * 64
    model = _model(owner_user_id=owner_user_id, profile_sha256=profile_sha256)
    keep_asset = uuid4()
    reject_asset = uuid4()
    pending_asset = uuid4()

    async def load_model(
        _session: AsyncSession,
        *,
        owner_user_id: UUID,
        profile_sha256: str,
    ) -> SemanticMetaModel:
        assert str(owner_user_id) == model.owner_user_id
        assert profile_sha256 == model.profile_sha256
        return model

    # Keep the test independent of persistence while exercising the exact live
    # feature extraction and scoring path.
    monkeypatch.setattr(advisory_service, "load_effective_semantic_meta_model", load_model)
    assessments = {
        keep_asset: _assessment(
            keep_asset,
            state=SemanticAssessmentState.COMPLETED,
            verdict=SemanticVerdict.PASS,
        ),
        reject_asset: _assessment(
            reject_asset,
            state=SemanticAssessmentState.COMPLETED,
            verdict=SemanticVerdict.SEVERE,
        ),
        pending_asset: _assessment(
            pending_asset,
            state=SemanticAssessmentState.PENDING,
            verdict=None,
        ),
    }

    result = asyncio.run(
        load_semantic_meta_advisories(
            cast(AsyncSession, object()),
            owner_user_id=owner_user_id,
            profile_sha256=profile_sha256,
            assessments=assessments,
        )
    )

    assert set(result) == {keep_asset, reject_asset}
    assert result[keep_asset].triage == SemanticMetaTriage.KEEP
    assert result[keep_asset].probability_micros == 268_941
    assert result[reject_asset].triage == SemanticMetaTriage.REJECT
    assert result[reject_asset].probability_micros == 731_059
    assert result[reject_asset].label == "likely reject"
    assert result[reject_asset].probability_percent == "73.1%"


def test_missing_promoted_model_has_no_advisory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def load_model(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(advisory_service, "load_effective_semantic_meta_model", load_model)
    result = asyncio.run(
        load_semantic_meta_advisories(
            cast(AsyncSession, object()),
            owner_user_id=uuid4(),
            profile_sha256="a" * 64,
            assessments={},
        )
    )
    assert result == {}


def test_cross_owner_promoted_model_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_owner = uuid4()
    model = _model(owner_user_id=uuid4(), profile_sha256="a" * 64)

    async def load_model(*_args: object, **_kwargs: object) -> SemanticMetaModel:
        return model

    monkeypatch.setattr(advisory_service, "load_effective_semantic_meta_model", load_model)
    with pytest.raises(SemanticMetaClassifierError, match="another owner"):
        asyncio.run(
            load_semantic_meta_advisories(
                cast(AsyncSession, object()),
                owner_user_id=requested_owner,
                profile_sha256="a" * 64,
                assessments={},
            )
        )


def test_cross_profile_promoted_model_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_user_id = uuid4()
    model = _model(owner_user_id=owner_user_id, profile_sha256="b" * 64)

    async def load_model(*_args: object, **_kwargs: object) -> SemanticMetaModel:
        return model

    monkeypatch.setattr(advisory_service, "load_effective_semantic_meta_model", load_model)
    with pytest.raises(SemanticMetaClassifierError, match="another profile"):
        asyncio.run(
            load_semantic_meta_advisories(
                cast(AsyncSession, object()),
                owner_user_id=owner_user_id,
                profile_sha256="a" * 64,
                assessments={},
            )
        )
