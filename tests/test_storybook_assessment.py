from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from gen_automation.domain.storybooks import (
    StorybookCharacter,
    StorybookCharacterBlocking,
    StorybookContentRating,
    StorybookInputMode,
    StorybookOverlay,
    StorybookOverlayKind,
    StorybookOverlayStyle,
    StorybookPagePlan,
    StorybookPlan,
    StorybookPlannerIdentity,
    StorybookProjectRequest,
    StorybookStagePosition,
    require_storybook_content_approval,
)
from gen_automation.semantic.storybook_assessment import (
    STORYBOOK_ASSESSMENT_PROMPT,
    StorybookAssessmentProviderVerdict,
    StorybookContentAssessmentDraft,
    build_storybook_content_assessment_request,
    compile_storybook_content_assessment,
    storybook_assessment_profile_sha256,
)

PROJECT_ID = uuid4()


def _adult_plan() -> StorybookPlan:
    character = StorybookCharacter(
        key="a",
        display_name="Approved fictional adult",
        subject_approval_id=uuid4(),
        approval_version=4,
        canonical_source_sha256="c" * 64,
        canonical_age=29,
        adult_approval_evidence_sha256="e" * 64,
    )
    request = StorybookProjectRequest(
        project_id=PROJECT_ID,
        input_mode=StorybookInputMode.IDEA_ONLY,
        general_idea="An explicit adults-only encounter between approved fictional adults.",
        page_count=1,
        content_rating=StorybookContentRating.NSFW_ADULTS_ONLY,
        adult_content_attested=True,
        requested_subject_approval_ids=(character.subject_approval_id,),
    )
    return StorybookPlan(
        request=request,
        request_sha256=request.request_sha256,
        planner=StorybookPlannerIdentity(
            model="planner",
            model_revision="0123456789abcdef",
            prompt_sha256="1" * 64,
            schema_sha256="2" * 64,
        ),
        characters=(character,),
        adult_subject_gate_sha256="f" * 64,
        pages=(
            StorybookPagePlan(
                page_number=1,
                scene_summary="An explicit fictional-adult scene.",
                continuity_notes="The approved adult remains the only depicted person.",
                generation_prompt="Explicit adults-only intimate scene with Character A.",
                character_blocking=(
                    StorybookCharacterBlocking(
                        character_key="a",
                        stage_position=StorybookStagePosition.CENTER,
                        pose_and_action="leaning closer in an intimate pose",
                    ),
                ),
                overlays=(
                    StorybookOverlay(
                        element_id="line-a",
                        kind=StorybookOverlayKind.DIALOGUE,
                        text="Come closer.",
                        reading_order=1,
                        style=StorybookOverlayStyle.CLASSIC_INVERSE,
                        speaker_key="a",
                    ),
                ),
            ),
        ),
    )


def test_adult_profile_allows_explicit_adult_material_but_rejects_minor_content() -> None:
    assert "explicit fictional-adult sexual imagery" in STORYBOOK_ASSESSMENT_PROMPT
    assert "Do not downgrade explicit" in STORYBOOK_ASSESSMENT_PROMPT
    assert "adult material" in STORYBOOK_ASSESSMENT_PROMPT
    assert "Reject minors" in STORYBOOK_ASSESSMENT_PROMPT
    assert storybook_assessment_profile_sha256(
        StorybookContentRating.SFW
    ) != storybook_assessment_profile_sha256(StorybookContentRating.NSFW_ADULTS_ONLY)


def test_assessment_manifest_contains_every_adult_text_surface_and_compiles() -> None:
    plan = _adult_plan()
    sources = ("9" * 64,)
    request = build_storybook_content_assessment_request(
        plan=plan,
        source_sha256s=sources,
    )
    page = request.pages[0]
    assert request.general_idea == plan.request.general_idea
    assert page.scene_summary == plan.pages[0].scene_summary
    assert page.continuity_notes == plan.pages[0].continuity_notes
    assert page.generation_prompt == plan.pages[0].generation_prompt
    assert page.blocking[0].pose_and_action == "leaning closer in an intimate pose"
    assert page.overlays[0].text == "Come closer."

    draft = StorybookContentAssessmentDraft(
        assessment_request_sha256=request.assessment_request_sha256,
        verdict=StorybookAssessmentProviderVerdict.APPROVED,
    )
    assessment = compile_storybook_content_assessment(
        request=request,
        draft=draft,
        model="pinned-content-assessor",
        model_revision="0123456789abcdef",
    )
    require_storybook_content_approval(
        plan=plan,
        assessment=assessment,
        source_sha256s=sources,
        expected_profile_sha256=assessment.profile_sha256,
        expected_model="pinned-content-assessor",
        expected_model_revision="0123456789abcdef",
        expected_prompt_sha256=assessment.prompt_sha256,
        expected_schema_sha256=assessment.schema_sha256,
    )


def test_assessor_cannot_choose_plan_source_or_profile_identities() -> None:
    request = build_storybook_content_assessment_request(
        plan=_adult_plan(),
        source_sha256s=("8" * 64,),
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StorybookContentAssessmentDraft.model_validate(
            {
                "assessment_request_sha256": request.assessment_request_sha256,
                "verdict": "approved",
                "plan_sha256": "0" * 64,
            }
        )
    with pytest.raises(ValueError, match="does not match"):
        compile_storybook_content_assessment(
            request=request,
            draft=StorybookContentAssessmentDraft(
                assessment_request_sha256="0" * 64,
                verdict=StorybookAssessmentProviderVerdict.APPROVED,
            ),
            model="pinned-content-assessor",
            model_revision="0123456789abcdef",
        )


def test_adult_assessment_requires_server_owned_cast_gate() -> None:
    plan = _adult_plan()
    with pytest.raises(ValidationError, match="approved-adult cast gate"):
        build_storybook_content_assessment_request(
            plan=plan.model_copy(update={"adult_subject_gate_sha256": None}),
            source_sha256s=("7" * 64,),
        )
