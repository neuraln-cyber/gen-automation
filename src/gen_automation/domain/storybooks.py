from __future__ import annotations

import re
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gen_automation.domain.canonical import canonical_sha256

STORYBOOK_REQUEST_SCHEMA: Final[Literal["storybook-request/v1"]] = "storybook-request/v1"
STORYBOOK_PLAN_SCHEMA: Final[Literal["storybook-plan/v1"]] = "storybook-plan/v1"
STORYBOOK_LAYOUT_SCHEMA: Final[Literal["storybook-layout/v1"]] = "storybook-layout/v1"
STORYBOOK_ASSESSMENT_SCHEMA: Final[Literal["storybook-content-assessment/v1"]] = (
    "storybook-content-assessment/v1"
)
MAX_STORYBOOK_PAGES = 24
MAX_STORYBOOK_CHARACTERS = 8
MAX_STORYBOOK_OVERLAYS_PER_PAGE = 8
MAX_STORYBOOK_TEXT_CHARACTERS = 320
NORMALIZED_SCALE = 1_000_000

_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_COLOR_PATTERN = re.compile(r"#[0-9A-F]{6}")


class StorybookInputMode(StrEnum):
    SELECTED_IMAGES = "selected_images"
    IDEA_ONLY = "idea_only"


class StorybookContentRating(StrEnum):
    SFW = "sfw"
    NSFW_ADULTS_ONLY = "nsfw_adults_only"


class StorybookContentAssessmentVerdict(StrEnum):
    APPROVED = "approved"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class StorybookOverlayKind(StrEnum):
    DIALOGUE = "dialogue"
    THOUGHT = "thought"
    NARRATION = "narration"
    SFX = "sfx"


class StorybookOverlayStyle(StrEnum):
    CLASSIC_LIGHT = "classic_light"
    CLASSIC_INVERSE = "classic_inverse"
    SOFT_CLOUD = "soft_cloud"
    ACCENT_FLOAT = "accent_float"
    THOUGHT_WHISPER = "thought_whisper"
    IMPACT_SFX = "impact_sfx"
    NARRATION = "narration"


class StorybookPlacementHint(StrEnum):
    AUTO = "auto"
    TOP_LEFT = "top_left"
    TOP_CENTER = "top_center"
    TOP_RIGHT = "top_right"
    MIDDLE_LEFT = "middle_left"
    MIDDLE_RIGHT = "middle_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_CENTER = "bottom_center"
    BOTTOM_RIGHT = "bottom_right"


class StorybookStagePosition(StrEnum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    FOREGROUND = "foreground"
    BACKGROUND = "background"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class StorybookNormalizedPoint(_StrictFrozenModel):
    x: int = Field(ge=0, le=NORMALIZED_SCALE)
    y: int = Field(ge=0, le=NORMALIZED_SCALE)


class StorybookNormalizedBox(_StrictFrozenModel):
    x: int = Field(ge=0, lt=NORMALIZED_SCALE)
    y: int = Field(ge=0, lt=NORMALIZED_SCALE)
    width: int = Field(gt=0, le=NORMALIZED_SCALE)
    height: int = Field(gt=0, le=NORMALIZED_SCALE)

    @model_validator(mode="after")
    def validate_bounds(self) -> StorybookNormalizedBox:
        if self.x + self.width > NORMALIZED_SCALE or self.y + self.height > NORMALIZED_SCALE:
            raise ValueError("storybook box exceeds the normalized canvas")
        return self


class StorybookProjectRequest(_StrictFrozenModel):
    schema_version: Literal["storybook-request/v1"] = STORYBOOK_REQUEST_SCHEMA
    project_id: UUID
    input_mode: StorybookInputMode
    title: str = Field(default="", max_length=160)
    general_idea: str = Field(min_length=1, max_length=4_000)
    page_count: int = Field(ge=1, le=MAX_STORYBOOK_PAGES)
    selected_release_version_id: UUID | None = None
    selected_release_selection_ids: tuple[UUID, ...] = Field(
        default=(), max_length=MAX_STORYBOOK_PAGES
    )
    requested_subject_approval_ids: tuple[UUID, ...] = Field(
        default=(), max_length=MAX_STORYBOOK_CHARACTERS
    )
    language: str = Field(default="en", pattern=r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
    content_rating: StorybookContentRating = StorybookContentRating.SFW
    adult_content_attested: bool = False
    candidates_per_page: Literal[1] = 1

    @model_validator(mode="after")
    def validate_input_mode(self) -> StorybookProjectRequest:
        if self.general_idea != self.general_idea.strip():
            raise ValueError("storybook idea must not have surrounding whitespace")
        if self.title != self.title.strip():
            raise ValueError("storybook title must not have surrounding whitespace")
        if len(set(self.selected_release_selection_ids)) != len(
            self.selected_release_selection_ids
        ):
            raise ValueError("storybook selected images must be unique")
        if len(set(self.requested_subject_approval_ids)) != len(
            self.requested_subject_approval_ids
        ):
            raise ValueError("storybook requested subjects must be unique")
        if self.input_mode is StorybookInputMode.SELECTED_IMAGES:
            if self.selected_release_version_id is None:
                raise ValueError("selected-image stories require a release version")
            if len(self.selected_release_selection_ids) != self.page_count:
                raise ValueError("selected-image stories require exactly one image per page")
            if self.requested_subject_approval_ids:
                raise ValueError("selected-image stories derive subjects from their release")
        elif self.selected_release_selection_ids or self.selected_release_version_id is not None:
            raise ValueError("idea-only stories cannot freeze selected images")
        if self.content_rating is StorybookContentRating.NSFW_ADULTS_ONLY:
            if not self.adult_content_attested:
                raise ValueError("adult Storybooks require an explicit adults-only attestation")
            if (
                self.input_mode is StorybookInputMode.IDEA_ONLY
                and not self.requested_subject_approval_ids
            ):
                raise ValueError("adult idea-only Storybooks require requested approved subjects")
        elif self.adult_content_attested:
            raise ValueError("SFW Storybooks cannot carry an adult-content attestation")
        return self

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class StorybookCharacter(_StrictFrozenModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    display_name: str = Field(min_length=1, max_length=120)
    subject_approval_id: UUID
    approval_version: int = Field(ge=1)
    canonical_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_age: int = Field(ge=18, le=10_000)
    adult_approval_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_aged_up_minor: Literal[False] = False
    continuity_description: str = Field(default="", max_length=1_000)

    @model_validator(mode="after")
    def validate_text(self) -> StorybookCharacter:
        if self.display_name != self.display_name.strip():
            raise ValueError("storybook character name must not have surrounding whitespace")
        if self.continuity_description != self.continuity_description.strip():
            raise ValueError(
                "storybook continuity description must not have surrounding whitespace"
            )
        return self


class StorybookSelectedSource(_StrictFrozenModel):
    release_selection_id: UUID
    release_version_id: UUID
    asset_id: UUID
    source_storage_backend: str = Field(min_length=1, max_length=50)
    source_storage_bucket: str = Field(min_length=1, max_length=255)
    source_object_key: str = Field(min_length=1, max_length=1_024)
    source_object_version_id: str = Field(min_length=1, max_length=1_024)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_content_type: str = Field(min_length=1, max_length=100)
    source_image_format: str = Field(min_length=1, max_length=20)
    source_width: int = Field(gt=0, le=32_768)
    source_height: int = Field(gt=0, le=32_768)
    source_byte_size: int = Field(gt=0, le=512 * 1024 * 1024)


class StorybookCharacterBlocking(_StrictFrozenModel):
    character_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    stage_position: StorybookStagePosition
    pose_and_action: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def validate_pose(self) -> StorybookCharacterBlocking:
        if self.pose_and_action != self.pose_and_action.strip():
            raise ValueError("storybook pose must not have surrounding whitespace")
        return self


class StorybookOverlay(_StrictFrozenModel):
    element_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    kind: StorybookOverlayKind
    text: str = Field(min_length=1, max_length=MAX_STORYBOOK_TEXT_CHARACTERS)
    reading_order: int = Field(ge=1, le=MAX_STORYBOOK_OVERLAYS_PER_PAGE)
    style: StorybookOverlayStyle
    speaker_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    placement_hint: StorybookPlacementHint = StorybookPlacementHint.AUTO
    accent_color: str = Field(default="#EF6BD2", pattern=r"^#[0-9A-F]{6}$")

    @model_validator(mode="after")
    def validate_semantics(self) -> StorybookOverlay:
        if self.text != self.text.strip():
            raise ValueError("storybook overlay text must not have surrounding whitespace")
        if self.kind in {StorybookOverlayKind.DIALOGUE, StorybookOverlayKind.THOUGHT}:
            if self.speaker_key is None:
                raise ValueError("dialogue and thought overlays require a speaker")
        elif self.speaker_key is not None:
            raise ValueError("narration and SFX overlays cannot declare a speaker")
        if (
            self.kind is StorybookOverlayKind.SFX
            and self.style is not StorybookOverlayStyle.IMPACT_SFX
        ):
            raise ValueError("SFX overlays require the impact SFX style")
        if (
            self.kind is StorybookOverlayKind.NARRATION
            and self.style is not StorybookOverlayStyle.NARRATION
        ):
            raise ValueError("narration overlays require the narration style")
        if self.kind in {StorybookOverlayKind.DIALOGUE, StorybookOverlayKind.THOUGHT} and (
            self.style in {StorybookOverlayStyle.IMPACT_SFX, StorybookOverlayStyle.NARRATION}
        ):
            raise ValueError("dialogue and thought overlays require a speech style")
        if _COLOR_PATTERN.fullmatch(self.accent_color) is None:
            raise ValueError("storybook accent color is invalid")
        return self


class StorybookPagePlan(_StrictFrozenModel):
    page_number: int = Field(ge=1, le=MAX_STORYBOOK_PAGES)
    scene_summary: str = Field(min_length=1, max_length=1_500)
    continuity_notes: str = Field(default="", max_length=1_500)
    source: StorybookSelectedSource | None = None
    generation_prompt: str | None = Field(default=None, min_length=1, max_length=6_000)
    character_blocking: tuple[StorybookCharacterBlocking, ...] = Field(
        default=(), max_length=MAX_STORYBOOK_CHARACTERS
    )
    overlays: tuple[StorybookOverlay, ...] = Field(
        default=(), max_length=MAX_STORYBOOK_OVERLAYS_PER_PAGE
    )

    @model_validator(mode="after")
    def validate_page(self) -> StorybookPagePlan:
        text_values = (self.scene_summary, self.continuity_notes, self.generation_prompt or "")
        if any(value != value.strip() for value in text_values):
            raise ValueError("storybook page text must not have surrounding whitespace")
        blocking_keys = [item.character_key for item in self.character_blocking]
        if len(set(blocking_keys)) != len(blocking_keys):
            raise ValueError("a character can appear only once in page blocking")
        element_ids = [item.element_id for item in self.overlays]
        reading_order = [item.reading_order for item in self.overlays]
        if len(set(element_ids)) != len(element_ids):
            raise ValueError("storybook overlay element IDs must be unique per page")
        if len(set(reading_order)) != len(reading_order):
            raise ValueError("storybook overlay reading order must be unique per page")
        if reading_order and sorted(reading_order) != list(range(1, len(reading_order) + 1)):
            raise ValueError("storybook overlay reading order must be contiguous")
        return self


class StorybookPlannerIdentity(_StrictFrozenModel):
    model: str = Field(min_length=1, max_length=200)
    model_revision: str = Field(min_length=7, max_length=200)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StorybookPlan(_StrictFrozenModel):
    schema_version: Literal["storybook-plan/v1"] = STORYBOOK_PLAN_SCHEMA
    request: StorybookProjectRequest
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    planner: StorybookPlannerIdentity
    characters: tuple[StorybookCharacter, ...] = Field(
        default=(), max_length=MAX_STORYBOOK_CHARACTERS
    )
    adult_subject_gate_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pages: tuple[StorybookPagePlan, ...] = Field(min_length=1, max_length=MAX_STORYBOOK_PAGES)

    @model_validator(mode="after")
    def validate_plan(self) -> StorybookPlan:
        if self.request_sha256 != self.request.request_sha256:
            raise ValueError("storybook plan request identity does not match")
        if len(self.pages) != self.request.page_count:
            raise ValueError("storybook plan must contain exactly the requested pages")
        if [page.page_number for page in self.pages] != list(range(1, len(self.pages) + 1)):
            raise ValueError("storybook pages must be ordered and contiguous")

        character_keys = [character.key for character in self.characters]
        if len(set(character_keys)) != len(character_keys):
            raise ValueError("storybook character keys must be unique")
        if (
            self.request.input_mode is StorybookInputMode.IDEA_ONLY
            and tuple(character.subject_approval_id for character in self.characters)
            != self.request.requested_subject_approval_ids
        ):
            raise ValueError("storybook cast does not match the requested approved subjects")
        known_characters = set(character_keys)
        for page in self.pages:
            page_characters = {item.character_key for item in page.character_blocking}
            overlay_speakers = {
                item.speaker_key for item in page.overlays if item.speaker_key is not None
            }
            if not page_characters.issubset(known_characters) or not overlay_speakers.issubset(
                known_characters
            ):
                raise ValueError("storybook page references an unknown character")

        if self.request.input_mode is StorybookInputMode.SELECTED_IMAGES:
            sources = tuple(page.source for page in self.pages)
            if any(source is None for source in sources):
                raise ValueError("selected-image story pages require frozen selected sources")
            selection_ids = tuple(
                source.release_selection_id for source in sources if source is not None
            )
            if selection_ids != self.request.selected_release_selection_ids:
                raise ValueError("selected-image story pages must preserve the chosen image order")
            if any(
                source.release_version_id != self.request.selected_release_version_id
                for source in sources
                if source is not None
            ):
                raise ValueError("selected-image story sources must share the requested release")
            if any(page.generation_prompt is not None for page in self.pages):
                raise ValueError("selected-image stories cannot contain generation prompts")
        elif any(page.source is not None or page.generation_prompt is None for page in self.pages):
            raise ValueError("idea-only story pages require generation prompts and no source image")
        if self.characters and self.adult_subject_gate_sha256 is None:
            raise ValueError("storybook cast requires a server-owned adult-subject gate")
        if self.request.content_rating is StorybookContentRating.NSFW_ADULTS_ONLY:
            if not self.characters or self.adult_subject_gate_sha256 is None:
                raise ValueError("adult Storybooks require current approved-adult cast evidence")
            used_characters: set[str] = set()
            for page in self.pages:
                page_characters = {item.character_key for item in page.character_blocking}
                if not page_characters:
                    raise ValueError("every adult Storybook page must identify its depicted cast")
                used_characters.update(page_characters)
            if used_characters != known_characters:
                raise ValueError("adult Storybook cast must be used by the planned pages")
        return self

    @property
    def plan_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class StorybookContentAssessment(_StrictFrozenModel):
    schema_version: Literal["storybook-content-assessment/v1"] = STORYBOOK_ASSESSMENT_SCHEMA
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_rating: StorybookContentRating
    source_sha256s: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_STORYBOOK_PAGES,
    )
    adult_subject_gate_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=200)
    model_revision: str = Field(min_length=7, max_length=200)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: StorybookContentAssessmentVerdict
    reason_codes: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_assessment(self) -> StorybookContentAssessment:
        if any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in self.source_sha256s
        ):
            raise ValueError("storybook assessment source digest is invalid")
        if self.content_rating is StorybookContentRating.NSFW_ADULTS_ONLY:
            if self.adult_subject_gate_sha256 is None:
                raise ValueError("adult content assessment requires the adult-subject gate")
        if len(set(self.reason_codes)) != len(self.reason_codes) or any(
            re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None for value in self.reason_codes
        ):
            raise ValueError("storybook assessment reason codes are invalid")
        return self

    @property
    def assessment_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    @property
    def has_approved_verdict(self) -> bool:
        return self.verdict is StorybookContentAssessmentVerdict.APPROVED


def require_storybook_content_approval(
    *,
    plan: StorybookPlan,
    assessment: StorybookContentAssessment,
    source_sha256s: tuple[str, ...],
    expected_profile_sha256: str,
    expected_model: str,
    expected_model_revision: str,
    expected_prompt_sha256: str,
    expected_schema_sha256: str,
) -> None:
    """Check immutable assessment binding after authoritative DB revalidation."""

    if assessment.verdict is not StorybookContentAssessmentVerdict.APPROVED:
        raise ValueError("storybook content assessment is not approved")
    if assessment.plan_sha256 != plan.plan_sha256:
        raise ValueError("storybook content assessment does not match the plan")
    if assessment.request_sha256 != plan.request_sha256:
        raise ValueError("storybook content assessment does not match the request")
    if assessment.content_rating is not plan.request.content_rating:
        raise ValueError("storybook content assessment does not match the rating")
    if assessment.adult_subject_gate_sha256 != plan.adult_subject_gate_sha256:
        raise ValueError("storybook content assessment does not match the subject gate")
    if assessment.source_sha256s != source_sha256s:
        raise ValueError("storybook content assessment does not match the page sources")
    if len(source_sha256s) != len(plan.pages):
        raise ValueError("storybook content assessment requires one source per page")
    if plan.request.input_mode is StorybookInputMode.SELECTED_IMAGES:
        frozen_source_sha256s = tuple(
            page.source.source_sha256 for page in plan.pages if page.source is not None
        )
        if frozen_source_sha256s != source_sha256s:
            raise ValueError("storybook assessment sources do not match frozen selections")
    expected_identity = (
        expected_profile_sha256,
        expected_model,
        expected_model_revision,
        expected_prompt_sha256,
        expected_schema_sha256,
    )
    if (
        assessment.profile_sha256,
        assessment.model,
        assessment.model_revision,
        assessment.prompt_sha256,
        assessment.schema_sha256,
    ) != expected_identity:
        raise ValueError("storybook content assessment identity is not pinned")


class StorybookOverlayPlacement(_StrictFrozenModel):
    element_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    box: StorybookNormalizedBox
    text_lines: tuple[str, ...] = Field(min_length=1, max_length=32)
    font_size_micros: int = Field(ge=20_000, le=120_000)
    rotation_millidegrees: int = Field(ge=-15_000, le=15_000)
    tail_target: StorybookNormalizedPoint | None = None
    manual_review_required: bool = False


class StorybookPageLayout(_StrictFrozenModel):
    schema_version: Literal["storybook-layout/v1"] = STORYBOOK_LAYOUT_SCHEMA
    page_number: int = Field(ge=1, le=MAX_STORYBOOK_PAGES)
    placements: tuple[StorybookOverlayPlacement, ...] = Field(
        max_length=MAX_STORYBOOK_OVERLAYS_PER_PAGE
    )
    manual_review_required: bool = False

    @property
    def layout_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


def is_storybook_key(value: str) -> bool:
    return _KEY_PATTERN.fullmatch(value) is not None
