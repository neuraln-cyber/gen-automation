from uuid import UUID

import pytest
from pydantic import ValidationError

from gen_automation.domain.video import (
    VideoComplianceAttestations,
    VideoContentRating,
    VideoCostSummary,
    VideoGenerationParameters,
    VideoGenerationRequest,
    VideoSourceSnapshot,
)
from gen_automation.video_worker.profiles import (
    A14B_ADULT_VIDEO_PROFILE,
    A14B_ADULT_VIDEO_PROFILE_REGISTRATION,
    A14B_VIDEO_PROFILE,
    A14B_VIDEO_PROFILE_REGISTRATION,
    HQ_VIDEO_PROFILE,
    HQ_VIDEO_PROFILE_REGISTRATION,
    PINNED_VIDEO_PROFILE,
    PINNED_VIDEO_PROFILE_SHA256,
)

SHA = "a" * 64
PROFILE_SHA = PINNED_VIDEO_PROFILE_SHA256
TEST_PROFILE = PINNED_VIDEO_PROFILE.profile_id
ASSET_ID = UUID("019fa795-8862-7142-a182-1746d6b0694e")


def _source(*, width: int = 768, height: int = 1_024) -> VideoSourceSnapshot:
    return VideoSourceSnapshot(
        asset_id=ASSET_ID,
        storage_backend="s3",
        storage_bucket="private-assets",
        object_key="releases/example/source.webp",
        object_version_id="source-version-1",
        sha256=SHA,
        content_type="image/webp",
        image_format="WEBP",
        width=width,
        height=height,
        byte_size=120_000,
    )


def _parameters(*, frame_count: int = 73, fps: int = 24) -> VideoGenerationParameters:
    return VideoGenerationParameters(
        prompt="subtle natural movement",
        negative_prompt="camera cut",
        profile_key=TEST_PROFILE,
        profile_version=PINNED_VIDEO_PROFILE.adapter_revision,
        profile_sha256=PROFILE_SHA,
        seed=42,
        frame_count=frame_count,
        fps=fps,
        width=480,
        height=832,
    )


def _compliance(*, adult: bool) -> VideoComplianceAttestations:
    return VideoComplianceAttestations(
        source_rights_confirmed=True,
        lawful_use_confirmed=True,
        all_depicted_people_are_adults=adult,
        consensual_adult_content_confirmed=adult,
        no_real_person_sexual_content=adult,
    )


def test_video_request_freezes_source_profile_parameters_and_attestations() -> None:
    request = VideoGenerationRequest(
        source=_source(),
        parameters=_parameters(),
        content_rating=VideoContentRating.EXPLICIT,
        compliance=_compliance(adult=True),
    )
    duplicate = VideoGenerationRequest(
        source=_source(),
        parameters=_parameters(),
        content_rating=VideoContentRating.EXPLICIT,
        compliance=_compliance(adult=True),
    )

    assert request.request_sha256 == duplicate.request_sha256
    assert len(request.request_sha256) == 64
    assert request.source.object_version_id == "source-version-1"
    assert request.parameters.seed == 42
    assert request.parameters.loop_mode == "ping_pong"
    assert request.compliance.policy_version == "video-compliance/v1"


def test_adult_video_requires_per_submission_adult_attestations() -> None:
    with pytest.raises(ValidationError, match="all-adults attestation"):
        VideoGenerationRequest(
            source=_source(),
            parameters=_parameters(),
            content_rating=VideoContentRating.NSFW,
            compliance=_compliance(adult=False),
        )

    sfw_request = VideoGenerationRequest(
        source=_source(),
        parameters=_parameters(),
        content_rating=VideoContentRating.SFW,
        compliance=_compliance(adult=False),
    )
    assert sfw_request.content_rating is VideoContentRating.SFW


def test_every_video_requires_source_rights_and_lawful_use_attestations() -> None:
    compliance = VideoComplianceAttestations(
        source_rights_confirmed=False,
        lawful_use_confirmed=True,
    )

    with pytest.raises(ValidationError, match="source-rights attestation"):
        VideoGenerationRequest(
            source=_source(),
            parameters=_parameters(),
            compliance=compliance,
        )


def test_video_parameters_enforce_economical_native_profiles() -> None:
    with pytest.raises(ValidationError):
        _parameters(frame_count=122)

    with pytest.raises(ValidationError):
        _parameters(fps=16)

    with pytest.raises(ValidationError, match="selected profile"):
        VideoGenerationParameters(
            prompt="",
            negative_prompt="",
            profile_key=PINNED_VIDEO_PROFILE.profile_id,
            profile_version=PINNED_VIDEO_PROFILE.adapter_revision,
            profile_sha256=PROFILE_SHA,
            seed=0,
            frame_count=73,
            fps=24,
            width=832,
            height=832,
        )


def test_hq_video_parameters_require_exact_shape_and_short_timing() -> None:
    parameters = VideoGenerationParameters(
        profile_key=HQ_VIDEO_PROFILE.profile_id,
        profile_version=HQ_VIDEO_PROFILE.adapter_revision,
        profile_sha256=HQ_VIDEO_PROFILE_REGISTRATION.job_contract_sha256,
        seed=42,
        frame_count=73,
        fps=24,
        width=1152,
        height=1472,
    )

    assert (parameters.width, parameters.height) == (1152, 1472)

    with pytest.raises(ValidationError, match="selected profile"):
        VideoGenerationParameters(
            **parameters.model_dump(mode="python", exclude={"frame_count"}),
            frame_count=121,
        )


def test_a14b_request_preserves_source_size_and_routes_content_rating() -> None:
    source = _source(width=701, height=1_101)
    base_parameters = VideoGenerationParameters(
        profile_key=A14B_VIDEO_PROFILE.profile_id,
        profile_version=A14B_VIDEO_PROFILE.adapter_revision,
        profile_sha256=A14B_VIDEO_PROFILE_REGISTRATION.job_contract_sha256,
        seed=42,
        frame_count=81,
        fps=16,
        width=702,
        height=1_102,
        loop_mode="forward",
    )
    request = VideoGenerationRequest(
        source=source,
        parameters=base_parameters,
        content_rating=VideoContentRating.SFW,
        compliance=_compliance(adult=False),
    )

    assert (request.parameters.width, request.parameters.height) == (702, 1_102)
    assert request.parameters.loop_mode == "forward"

    with pytest.raises(ValidationError, match="requires SFW content"):
        VideoGenerationRequest(
            source=source,
            parameters=base_parameters,
            content_rating=VideoContentRating.EXPLICIT,
            compliance=_compliance(adult=True),
        )

    adult_parameters = VideoGenerationParameters(
        **base_parameters.model_dump(
            mode="python",
            exclude={"profile_key", "profile_version", "profile_sha256"},
        ),
        profile_key=A14B_ADULT_VIDEO_PROFILE.profile_id,
        profile_version=A14B_ADULT_VIDEO_PROFILE.adapter_revision,
        profile_sha256=A14B_ADULT_VIDEO_PROFILE_REGISTRATION.job_contract_sha256,
    )
    adult_request = VideoGenerationRequest(
        source=source,
        parameters=adult_parameters,
        content_rating=VideoContentRating.EXPLICIT,
        compliance=_compliance(adult=True),
    )
    assert adult_request.parameters.profile_key == A14B_ADULT_VIDEO_PROFILE.profile_id

    with pytest.raises(ValidationError, match="preserve the source image"):
        VideoGenerationRequest(
            source=source,
            parameters=VideoGenerationParameters(
                **base_parameters.model_dump(
                    mode="python",
                    exclude={"width", "height"},
                ),
                width=700,
                height=1_100,
            ),
            content_rating=VideoContentRating.SFW,
            compliance=_compliance(adult=False),
        )


def test_video_cost_summary_rejects_negative_amounts() -> None:
    assert VideoCostSummary(actual_cost_microusd=1_500).actual_cost_microusd == 1_500
    with pytest.raises(ValidationError):
        VideoCostSummary(actual_cost_microusd=-1)
