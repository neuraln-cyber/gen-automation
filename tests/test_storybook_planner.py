from __future__ import annotations

from uuid import UUID, uuid4

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
    StorybookPlannerIdentity,
    StorybookProjectRequest,
    StorybookSelectedSource,
    StorybookStagePosition,
)
from gen_automation.semantic.storybooks import (
    STORYBOOK_PLANNER_PROMPT,
    StorybookPlannerDraft,
    StorybookPlannerPageDraft,
    StorybookPlannerRequest,
    build_storybook_planner_request,
    compile_storybook_plan,
    storybook_planner_prompt_sha256,
    storybook_planner_schema_sha256,
)

PROJECT_ID = uuid4()


def _planner() -> StorybookPlannerIdentity:
    return StorybookPlannerIdentity(
        model="pinned-story-planner",
        model_revision="0123456789abcdef",
        prompt_sha256=storybook_planner_prompt_sha256(),
        schema_sha256=storybook_planner_schema_sha256(),
    )


def _character(key: str = "a") -> StorybookCharacter:
    return StorybookCharacter(
        key=key,
        display_name=f"Approved adult {key.upper()}",
        subject_approval_id=uuid4(),
        approval_version=2,
        canonical_source_sha256=key * 64,
        canonical_age=28,
        adult_approval_evidence_sha256="a" * 64,
    )


def _source(selection_id: UUID, release_version_id: UUID, digest: str) -> StorybookSelectedSource:
    return StorybookSelectedSource(
        release_selection_id=selection_id,
        release_version_id=release_version_id,
        asset_id=uuid4(),
        source_storage_backend="s3",
        source_storage_bucket="private-story-source",
        source_object_key=f"masters/{digest}.png",
        source_object_version_id=f"version-{digest}",
        source_sha256=digest * 64,
        source_content_type="image/png",
        source_image_format="png",
        source_width=1024,
        source_height=1536,
        source_byte_size=2_000_000,
    )


def test_fixed_prompt_is_one_scene_per_page_and_allows_adults_only_copy() -> None:
    assert "never create panel grids" in STORYBOOK_PLANNER_PROMPT
    assert "nsfw_adults_only" in STORYBOOK_PLANNER_PROMPT
    assert "explicit fictional-adult sexual dialogue" in STORYBOOK_PLANNER_PROMPT
    assert "Never introduce minors" in STORYBOOK_PLANNER_PROMPT
    assert len(storybook_planner_prompt_sha256()) == 64
    assert len(storybook_planner_schema_sha256()) == 64


def test_selected_image_draft_compiles_without_model_control_of_source_ids() -> None:
    release_version_id = uuid4()
    selection_ids = (uuid4(), uuid4())
    sources = (
        _source(selection_ids[0], release_version_id, "1"),
        _source(selection_ids[1], release_version_id, "2"),
    )
    request = StorybookProjectRequest(
        project_id=PROJECT_ID,
        input_mode=StorybookInputMode.SELECTED_IMAGES,
        general_idea="Turn these images into a short mystery.",
        page_count=2,
        selected_release_version_id=release_version_id,
        selected_release_selection_ids=selection_ids,
    )
    planner_request = build_storybook_planner_request(
        request=request,
        characters=(),
        adult_subject_gate_sha256=None,
        selected_sources=sources,
    )
    draft = StorybookPlannerDraft(
        planner_request_sha256=planner_request.planner_request_sha256,
        pages=(
            StorybookPlannerPageDraft(
                page_number=1,
                scene_summary="A clue is discovered.",
            ),
            StorybookPlannerPageDraft(
                page_number=2,
                scene_summary="The mystery is solved.",
            ),
        ),
    )

    plan = compile_storybook_plan(
        planner_request=planner_request,
        planner=_planner(),
        expected_model="pinned-story-planner",
        expected_model_revision="0123456789abcdef",
        selected_sources=sources,
        draft=draft,
    )

    assert [page.source for page in plan.pages] == list(sources)
    assert all(page.generation_prompt is None for page in plan.pages)

    forged_sources = (
        sources[0].model_copy(update={"source_object_version_id": "forged-version"}),
        sources[1],
    )
    with pytest.raises(ValueError, match="source context does not match"):
        compile_storybook_plan(
            planner_request=planner_request,
            planner=_planner(),
            expected_model="pinned-story-planner",
            expected_model_revision="0123456789abcdef",
            selected_sources=forged_sources,
            draft=draft,
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StorybookPlannerPageDraft.model_validate(
            {
                "page_number": 1,
                "scene_summary": "A clue is discovered.",
                "source_asset_id": str(uuid4()),
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StorybookPlannerDraft.model_validate(
            {
                "planner_request_sha256": planner_request.planner_request_sha256,
                "content_review_verdict": "nsfw_adults_only_approved",
                "pages": [{"page_number": 1, "scene_summary": "No self-approval."}],
            }
        )


def test_adults_only_idea_draft_preserves_explicit_text() -> None:
    character = _character()
    request = StorybookProjectRequest(
        project_id=PROJECT_ID,
        input_mode=StorybookInputMode.IDEA_ONLY,
        general_idea="An adults-only intimate story involving the approved fictional adult.",
        page_count=1,
        content_rating=StorybookContentRating.NSFW_ADULTS_ONLY,
        adult_content_attested=True,
        requested_subject_approval_ids=(character.subject_approval_id,),
    )
    planner_request = build_storybook_planner_request(
        request=request,
        characters=(character,),
        adult_subject_gate_sha256="f" * 64,
    )
    line = "Come closer."
    draft = StorybookPlannerDraft(
        planner_request_sha256=planner_request.planner_request_sha256,
        pages=(
            StorybookPlannerPageDraft(
                page_number=1,
                scene_summary="The approved fictional adult initiates an intimate moment.",
                generation_prompt="Explicit adults-only intimate scene with Character A.",
                character_blocking=(
                    StorybookCharacterBlocking(
                        character_key="a",
                        stage_position=StorybookStagePosition.CENTER,
                        pose_and_action="moving closer with an affectionate expression",
                    ),
                ),
                overlays=(
                    StorybookOverlay(
                        element_id="adult-line",
                        kind=StorybookOverlayKind.DIALOGUE,
                        text=line,
                        reading_order=1,
                        style=StorybookOverlayStyle.CLASSIC_INVERSE,
                        speaker_key="a",
                    ),
                ),
            ),
        ),
    )

    plan = compile_storybook_plan(
        planner_request=planner_request,
        planner=_planner(),
        expected_model="pinned-story-planner",
        expected_model_revision="0123456789abcdef",
        draft=draft,
    )

    assert planner_request.request.content_rating is StorybookContentRating.NSFW_ADULTS_ONLY
    assert plan.pages[0].overlays[0].text == line
    assert plan.pages[0].generation_prompt is not None


def test_compiler_binds_full_request_prompt_schema_and_selected_sources() -> None:
    request = StorybookProjectRequest(
        project_id=PROJECT_ID,
        input_mode=StorybookInputMode.IDEA_ONLY,
        general_idea="A quiet landscape story.",
        page_count=1,
    )
    planner_request = build_storybook_planner_request(
        request=request,
        characters=(),
        adult_subject_gate_sha256=None,
    )
    draft = StorybookPlannerDraft(
        planner_request_sha256="0" * 64,
        pages=(
            StorybookPlannerPageDraft(
                page_number=1,
                scene_summary="Sunlight crosses the valley.",
                generation_prompt="A wide animated valley at sunrise.",
            ),
        ),
    )
    with pytest.raises(ValueError, match="full planner request"):
        compile_storybook_plan(
            planner_request=planner_request,
            planner=_planner(),
            expected_model="pinned-story-planner",
            expected_model_revision="0123456789abcdef",
            draft=draft,
        )

    valid_draft = draft.model_copy(
        update={"planner_request_sha256": planner_request.planner_request_sha256}
    )
    untrusted_model = _planner().model_copy(update={"model": "untrusted-planner"})
    with pytest.raises(ValueError, match="model identity is not pinned"):
        compile_storybook_plan(
            planner_request=planner_request,
            planner=untrusted_model,
            expected_model="pinned-story-planner",
            expected_model_revision="0123456789abcdef",
            draft=valid_draft,
        )
    untrusted_revision = _planner().model_copy(update={"model_revision": "untrusted-revision"})
    with pytest.raises(ValueError, match="model identity is not pinned"):
        compile_storybook_plan(
            planner_request=planner_request,
            planner=untrusted_revision,
            expected_model="pinned-story-planner",
            expected_model_revision="0123456789abcdef",
            draft=valid_draft,
        )
    unpinned = _planner().model_copy(update={"prompt_sha256": "9" * 64})
    with pytest.raises(ValueError, match="prompt identity is not pinned"):
        compile_storybook_plan(
            planner_request=planner_request,
            planner=unpinned,
            expected_model="pinned-story-planner",
            expected_model_revision="0123456789abcdef",
            draft=valid_draft,
        )


def test_idea_only_planner_cast_must_match_requested_approval_ids() -> None:
    requested = _character("a")
    different = _character("b")
    request = StorybookProjectRequest(
        project_id=PROJECT_ID,
        input_mode=StorybookInputMode.IDEA_ONLY,
        general_idea="A story using the requested approved adult character.",
        page_count=1,
        requested_subject_approval_ids=(requested.subject_approval_id,),
    )
    with pytest.raises(ValueError, match="does not match the requested"):
        build_storybook_planner_request(
            request=request,
            characters=(different,),
            adult_subject_gate_sha256="f" * 64,
        )
    with pytest.raises(ValidationError, match="does not match the requested"):
        StorybookPlannerRequest(
            request=request,
            request_sha256=request.request_sha256,
            characters=(different,),
            adult_subject_gate_sha256="f" * 64,
        )


def test_planner_output_has_a_global_cost_and_token_budget() -> None:
    pages = tuple(
        StorybookPlannerPageDraft(
            page_number=index,
            scene_summary="s" * 1_500,
            generation_prompt="p" * 6_000,
        )
        for index in range(1, 13)
    )
    with pytest.raises(ValidationError, match="fixed response budget"):
        StorybookPlannerDraft(
            planner_request_sha256="f" * 64,
            pages=pages,
        )
