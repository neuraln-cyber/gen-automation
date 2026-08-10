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

SHA = "a" * 64
PROFILE_SHA = "b" * 64
TEST_PROFILE = "wan-i2v-economy"
ASSET_ID = UUID("019fa795-8862-7142-a182-1746d6b0694e")


def _source() -> VideoSourceSnapshot:
    return VideoSourceSnapshot(
        asset_id=ASSET_ID,
        storage_backend="s3",
        storage_bucket="private-assets",
        object_key="releases/example/source.webp",
        object_version_id="source-version-1",
        sha256=SHA,
        content_type="image/webp",
        image_format="WEBP",
        width=768,
        height=1_024,
        byte_size=120_000,
    )


def _parameters(*, frame_count: int = 73, fps: int = 24) -> VideoGenerationParameters:
    return VideoGenerationParameters(
        prompt="subtle natural movement",
        negative_prompt="camera cut",
        profile_key=TEST_PROFILE,
        profile_version="1",
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

    with pytest.raises(ValidationError, match="832x480 or 480x832"):
        VideoGenerationParameters(
            prompt="",
            negative_prompt="",
            profile_key="economy",
            profile_version="1",
            profile_sha256=PROFILE_SHA,
            seed=0,
            frame_count=73,
            fps=24,
            width=832,
            height=832,
        )


def test_video_cost_summary_rejects_negative_amounts() -> None:
    assert VideoCostSummary(actual_cost_microusd=1_500).actual_cost_microusd == 1_500
    with pytest.raises(ValidationError):
        VideoCostSummary(actual_cost_microusd=-1)
