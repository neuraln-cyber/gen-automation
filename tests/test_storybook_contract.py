from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from gen_automation.domain.storybooks import (
    StorybookCharacter,
    StorybookCharacterBlocking,
    StorybookContentAssessment,
    StorybookContentAssessmentVerdict,
    StorybookContentRating,
    StorybookInputMode,
    StorybookOverlay,
    StorybookOverlayKind,
    StorybookOverlayStyle,
    StorybookPagePlan,
    StorybookPlan,
    StorybookPlannerIdentity,
    StorybookProjectRequest,
    StorybookSelectedSource,
    StorybookStagePosition,
    require_storybook_content_approval,
)

PROJECT_ID = uuid4()


def _planner() -> StorybookPlannerIdentity:
    return StorybookPlannerIdentity(
        model="pinned-story-planner",
        model_revision="0123456789abcdef",
        prompt_sha256="0" * 64,
        schema_sha256="1" * 64,
    )


def _character(key: str = "a") -> StorybookCharacter:
    return StorybookCharacter(
        key=key,
        display_name=f"Character {key.upper()}",
        subject_approval_id=uuid4(),
        approval_version=3,
        canonical_source_sha256=key * 64,
        canonical_age=27,
        adult_approval_evidence_sha256="e" * 64,
    )


def _source(
    *,
    selection_id: UUID,
    release_version_id: UUID,
    digest: str,
) -> StorybookSelectedSource:
    return StorybookSelectedSource(
        release_selection_id=selection_id,
        release_version_id=release_version_id,
        asset_id=uuid4(),
        source_storage_backend="s3",
        source_storage_bucket="story-source",
        source_object_key=f"masters/{digest}.png",
        source_object_version_id=f"version-{digest}",
        source_sha256=digest * 64,
        source_content_type="image/png",
        source_image_format="png",
        source_width=1024,
        source_height=1536,
        source_byte_size=2_000_000,
    )


def test_selected_image_story_freezes_release_selections_and_source_versions() -> None:
    release_version_id = uuid4()
    selection_ids = (uuid4(), uuid4())
    sources = tuple(
        _source(
            selection_id=selection_id,
            release_version_id=release_version_id,
            digest=digest,
        )
        for selection_id, digest in zip(selection_ids, ("1", "2"), strict=True)
    )
    request = StorybookProjectRequest(
        project_id=PROJECT_ID,
        input_mode=StorybookInputMode.SELECTED_IMAGES,
        title="A quiet afternoon",
        general_idea="Two friends solve a harmless mystery and make up before sunset.",
        page_count=2,
        selected_release_version_id=release_version_id,
        selected_release_selection_ids=selection_ids,
    )
    plan = StorybookPlan(
        request=request,
        request_sha256=request.request_sha256,
        planner=_planner(),
        pages=(
            StorybookPagePlan(
                page_number=1,
                scene_summary="A misplaced notebook is discovered.",
                source=sources[0],
            ),
            StorybookPagePlan(
                page_number=2,
                scene_summary="The mystery is resolved at sunset.",
                source=sources[1],
            ),
        ),
    )

    assert [page.source for page in plan.pages] == list(sources)
    assert plan.pages[0].source is not None
    assert plan.pages[0].source.source_object_version_id == "version-1"
    assert len(plan.plan_sha256) == 64

    wrong_sources = ("8" * 64, "9" * 64)
    assessment = StorybookContentAssessment(
        plan_sha256=plan.plan_sha256,
        request_sha256=plan.request_sha256,
        content_rating=StorybookContentRating.SFW,
        source_sha256s=wrong_sources,
        profile_sha256="1" * 64,
        model="pinned-content-assessor",
        model_revision="0123456789abcdef",
        prompt_sha256="2" * 64,
        schema_sha256="3" * 64,
        verdict=StorybookContentAssessmentVerdict.APPROVED,
    )
    with pytest.raises(ValueError, match="frozen selections"):
        require_storybook_content_approval(
            plan=plan,
            assessment=assessment,
            source_sha256s=wrong_sources,
            expected_profile_sha256="1" * 64,
            expected_model="pinned-content-assessor",
            expected_model_revision="0123456789abcdef",
            expected_prompt_sha256="2" * 64,
            expected_schema_sha256="3" * 64,
        )


def test_story_input_modes_cannot_mix_release_selections_and_requested_subjects() -> None:
    with pytest.raises(ValidationError, match="exactly one image per page"):
        StorybookProjectRequest(
            project_id=PROJECT_ID,
            input_mode=StorybookInputMode.SELECTED_IMAGES,
            general_idea="A safe story.",
            page_count=2,
            selected_release_version_id=uuid4(),
            selected_release_selection_ids=(uuid4(),),
        )
    with pytest.raises(ValidationError, match="cannot freeze selected images"):
        StorybookProjectRequest(
            project_id=PROJECT_ID,
            input_mode=StorybookInputMode.IDEA_ONLY,
            general_idea="A safe story.",
            page_count=1,
            selected_release_version_id=uuid4(),
        )
    with pytest.raises(ValidationError, match="derive subjects"):
        StorybookProjectRequest(
            project_id=PROJECT_ID,
            input_mode=StorybookInputMode.SELECTED_IMAGES,
            general_idea="A safe story.",
            page_count=1,
            selected_release_version_id=uuid4(),
            selected_release_selection_ids=(uuid4(),),
            requested_subject_approval_ids=(uuid4(),),
        )


def test_idea_only_plan_requires_a_generation_intent_for_every_page() -> None:
    request = StorybookProjectRequest(
        project_id=PROJECT_ID,
        input_mode=StorybookInputMode.IDEA_ONLY,
        general_idea="A cheerful day trip.",
        page_count=1,
    )
    with pytest.raises(ValidationError, match="require generation prompts"):
        StorybookPlan(
            request=request,
            request_sha256=request.request_sha256,
            planner=_planner(),
            pages=(
                StorybookPagePlan(
                    page_number=1,
                    scene_summary="The cast arrives at the station.",
                ),
            ),
        )


def test_idea_only_plan_can_choose_character_positions_for_each_scene() -> None:
    character = _character()
    request = StorybookProjectRequest(
        project_id=PROJECT_ID,
        input_mode=StorybookInputMode.IDEA_ONLY,
        general_idea="An adult friend returns a lost library book before closing time.",
        page_count=1,
        requested_subject_approval_ids=(character.subject_approval_id,),
    )
    plan = StorybookPlan(
        request=request,
        request_sha256=request.request_sha256,
        planner=_planner(),
        characters=(character,),
        adult_subject_gate_sha256="f" * 64,
        pages=(
            StorybookPagePlan(
                page_number=1,
                scene_summary="The approved character approaches the library desk.",
                generation_prompt="Animated story scene at an adult community library.",
                character_blocking=(
                    StorybookCharacterBlocking(
                        character_key="a",
                        stage_position=StorybookStagePosition.FOREGROUND,
                        pose_and_action="holding the book and walking toward the desk",
                    ),
                ),
            ),
        ),
    )

    assert plan.pages[0].source is None
    assert [item.character_key for item in plan.pages[0].character_blocking] == ["a"]


def test_adults_only_storybooks_require_attestation_subjects_and_used_cast() -> None:
    with pytest.raises(ValidationError, match="explicit adults-only attestation"):
        StorybookProjectRequest(
            project_id=PROJECT_ID,
            input_mode=StorybookInputMode.IDEA_ONLY,
            general_idea="An adults-only fictional story.",
            page_count=1,
            content_rating=StorybookContentRating.NSFW_ADULTS_ONLY,
        )
    with pytest.raises(ValidationError, match="requested approved subjects"):
        StorybookProjectRequest(
            project_id=PROJECT_ID,
            input_mode=StorybookInputMode.IDEA_ONLY,
            general_idea="An adults-only fictional story.",
            page_count=1,
            content_rating=StorybookContentRating.NSFW_ADULTS_ONLY,
            adult_content_attested=True,
        )

    first = _character("a")
    unused = _character("b")
    request = StorybookProjectRequest(
        project_id=PROJECT_ID,
        input_mode=StorybookInputMode.IDEA_ONLY,
        general_idea="An adults-only fictional story involving approved adult characters.",
        page_count=1,
        content_rating=StorybookContentRating.NSFW_ADULTS_ONLY,
        adult_content_attested=True,
        requested_subject_approval_ids=(
            first.subject_approval_id,
            unused.subject_approval_id,
        ),
    )
    with pytest.raises(ValidationError, match="cast must be used"):
        StorybookPlan(
            request=request,
            request_sha256=request.request_sha256,
            planner=_planner(),
            characters=(first, unused),
            adult_subject_gate_sha256="f" * 64,
            pages=(
                StorybookPagePlan(
                    page_number=1,
                    scene_summary="An explicit fictional-adult scene.",
                    generation_prompt="Explicit adults-only scene using only Character A.",
                    character_blocking=(
                        StorybookCharacterBlocking(
                            character_key="a",
                            stage_position=StorybookStagePosition.CENTER,
                            pose_and_action="posing at the center of the scene",
                        ),
                    ),
                ),
            ),
        )


def test_content_assessment_is_independent_and_bound_to_plan_text_and_sources() -> None:
    character = _character()
    request = StorybookProjectRequest(
        project_id=PROJECT_ID,
        input_mode=StorybookInputMode.IDEA_ONLY,
        general_idea="An adults-only fictional scene.",
        page_count=1,
        content_rating=StorybookContentRating.NSFW_ADULTS_ONLY,
        adult_content_attested=True,
        requested_subject_approval_ids=(character.subject_approval_id,),
    )
    page = StorybookPagePlan(
        page_number=1,
        scene_summary="An intimate moment between approved fictional adults.",
        generation_prompt="Explicit adults-only scene with Character A.",
        character_blocking=(
            StorybookCharacterBlocking(
                character_key="a",
                stage_position=StorybookStagePosition.CENTER,
                pose_and_action="leaning closer",
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
    )
    plan = StorybookPlan(
        request=request,
        request_sha256=request.request_sha256,
        planner=_planner(),
        characters=(character,),
        adult_subject_gate_sha256="f" * 64,
        pages=(page,),
    )
    source_sha256s = ("9" * 64,)
    assessment = StorybookContentAssessment(
        plan_sha256=plan.plan_sha256,
        request_sha256=plan.request_sha256,
        content_rating=plan.request.content_rating,
        source_sha256s=source_sha256s,
        adult_subject_gate_sha256=plan.adult_subject_gate_sha256,
        profile_sha256="1" * 64,
        model="pinned-content-assessor",
        model_revision="0123456789abcdef",
        prompt_sha256="2" * 64,
        schema_sha256="3" * 64,
        verdict=StorybookContentAssessmentVerdict.APPROVED,
    )
    require_storybook_content_approval(
        plan=plan,
        assessment=assessment,
        source_sha256s=source_sha256s,
        expected_profile_sha256="1" * 64,
        expected_model="pinned-content-assessor",
        expected_model_revision="0123456789abcdef",
        expected_prompt_sha256="2" * 64,
        expected_schema_sha256="3" * 64,
    )

    edited_page = page.model_copy(
        update={
            "overlays": (page.overlays[0].model_copy(update={"text": "Different adult dialogue."}),)
        }
    )
    edited_plan = plan.model_copy(update={"pages": (edited_page,)})
    with pytest.raises(ValueError, match="does not match the plan"):
        require_storybook_content_approval(
            plan=edited_plan,
            assessment=assessment,
            source_sha256s=source_sha256s,
            expected_profile_sha256="1" * 64,
            expected_model="pinned-content-assessor",
            expected_model_revision="0123456789abcdef",
            expected_prompt_sha256="2" * 64,
            expected_schema_sha256="3" * 64,
        )


def test_overlay_semantics_are_bounded_and_speaker_aware() -> None:
    with pytest.raises(ValidationError, match="require a speaker"):
        StorybookOverlay(
            element_id="dialogue",
            kind=StorybookOverlayKind.DIALOGUE,
            text="Hello!",
            reading_order=1,
            style=StorybookOverlayStyle.CLASSIC_LIGHT,
        )
    with pytest.raises(ValidationError, match="impact SFX style"):
        StorybookOverlay(
            element_id="sound",
            kind=StorybookOverlayKind.SFX,
            text="WHOOSH",
            reading_order=1,
            style=StorybookOverlayStyle.ACCENT_FLOAT,
        )
