import hashlib
import hmac
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import Asset, GenerationJob, ReleaseVersion, WorkflowApproval
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import AssetKind, AssetState
from gen_automation.domain.generation_limits import MAX_OUTPUTS_PER_GENERATION_JOB
from gen_automation.domain.release_spec import (
    ArtifactSpecification,
    GenerationParameters,
    LoraSpecification,
    Sha256,
    WildcardName,
    WildcardVersionReference,
    WorkflowSpecification,
)
from gen_automation.domain.wildcards import MAX_WILDCARD_EXPANSIONS, MAX_WILDCARD_NESTING

_UNAVAILABLE_MESSAGE = "Generation details are unavailable for this image."
_LEGACY_PROMPT_FIELDS = (
    "prompt",
    "negative_prompt",
    "detailer_prompt",
    "detailer_negative_prompt",
)
_PROMPT_FIELDS = (
    "prompt",
    "character_a_prompt",
    "character_b_prompt",
    "negative_prompt",
    "detailer_prompt",
    "detailer_negative_prompt",
)


class GenerationDetailsNotFoundError(Exception):
    """The asset is not an available raw master associated with a generation job."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _SubjectSnapshot(_FrozenModel):
    name: str = Field(min_length=1, max_length=200)
    canonical_age: int = Field(ge=18, le=10_000)
    canonical_source_url: AnyHttpUrl


class _BatchSnapshot(_FrozenModel):
    index: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=100)
    image_offset: int = Field(ge=0)
    image_count: int = Field(ge=1, le=80_000)


class _WildcardSelection(_FrozenModel):
    field: Literal[
        "prompt",
        "character_a_prompt",
        "character_b_prompt",
        "negative_prompt",
        "detailer_prompt",
        "detailer_negative_prompt",
    ]
    occurrence: int = Field(ge=1, le=MAX_WILDCARD_EXPANSIONS)
    depth: int = Field(ge=1, le=MAX_WILDCARD_NESTING)
    name: WildcardName
    version_id: UUID
    version_no: int = Field(ge=1)
    entries_sha256: Sha256
    entry_index: int = Field(ge=0)
    entry_sha256: Sha256


class _PromptResolutionBase(_FrozenModel):
    seed: int = Field(ge=0, le=(2**63) - 1)
    source_prompt: str = Field(min_length=1, max_length=20_000)
    source_negative_prompt: str = Field(max_length=20_000)
    source_detailer_prompt: str = Field(max_length=20_000)
    source_detailer_negative_prompt: str = Field(max_length=20_000)
    source_prompt_sha256: Sha256
    source_negative_prompt_sha256: Sha256
    source_detailer_prompt_sha256: Sha256
    source_detailer_negative_prompt_sha256: Sha256
    resolved_prompt_sha256: Sha256
    resolved_negative_prompt_sha256: Sha256
    resolved_detailer_prompt_sha256: Sha256
    resolved_detailer_negative_prompt_sha256: Sha256
    wildcard_versions: list[WildcardVersionReference] = Field(max_length=64)
    selections: list[_WildcardSelection] = Field(max_length=MAX_WILDCARD_EXPANSIONS)

    @model_validator(mode="after")
    def validate_wildcard_evidence(self) -> "_PromptResolutionBase":
        references = {(item.name, item.version_id): item for item in self.wildcard_versions}
        for selection in self.selections:
            reference = references.get((selection.name, selection.version_id))
            if (
                reference is None
                or reference.version_no != selection.version_no
                or not hmac.compare_digest(reference.entries_sha256, selection.entries_sha256)
                or selection.entry_index >= reference.entry_count
            ):
                raise ValueError("wildcard selection does not match its frozen version")
        return self


class _PromptResolutionV1(_PromptResolutionBase):
    schema_version: Literal[1]

    @model_validator(mode="after")
    def validate_legacy_evidence(self) -> "_PromptResolutionV1":
        _validate_source_digests(self, _LEGACY_PROMPT_FIELDS)
        if any(selection.field not in _LEGACY_PROMPT_FIELDS for selection in self.selections):
            raise ValueError("legacy prompt resolution contains an unsupported field")
        return self


class _PromptResolutionV2(_PromptResolutionBase):
    schema_version: Literal[2]
    source_character_a_prompt: str = Field(max_length=20_000)
    source_character_b_prompt: str = Field(max_length=20_000)
    source_character_a_prompt_sha256: Sha256
    source_character_b_prompt_sha256: Sha256
    resolved_character_a_prompt_sha256: Sha256
    resolved_character_b_prompt_sha256: Sha256

    @model_validator(mode="after")
    def validate_evidence(self) -> "_PromptResolutionV2":
        _validate_source_digests(self, _PROMPT_FIELDS)
        return self


type _PromptResolution = _PromptResolutionV1 | _PromptResolutionV2


def _validate_source_digests(
    resolution: _PromptResolutionBase,
    field_names: tuple[str, ...],
) -> None:
    for field_name in field_names:
        source = getattr(resolution, f"source_{field_name}")
        expected = getattr(resolution, f"source_{field_name}_sha256")
        if not hmac.compare_digest(_text_sha256(source), expected):
            raise ValueError("prompt resolution source digest mismatch")


class _GenerationJobParametersV2(_FrozenModel):
    schema_version: Literal[2]
    release_version_id: UUID
    release_specification_sha256: Sha256
    approval_snapshot_sha256: Sha256
    ordinal: int = Field(ge=0)
    subjects: list[_SubjectSnapshot] = Field(min_length=1, max_length=20)
    checkpoint: ArtifactSpecification
    loras: list[LoraSpecification] = Field(max_length=8)
    workflow: WorkflowSpecification
    generation: GenerationParameters
    prompt_resolution: _PromptResolution
    output_generations: list[GenerationParameters] = Field(
        min_length=1,
        max_length=MAX_OUTPUTS_PER_GENERATION_JOB,
    )
    output_prompt_resolutions: list[_PromptResolution] = Field(
        min_length=1,
        max_length=MAX_OUTPUTS_PER_GENERATION_JOB,
    )
    batch: _BatchSnapshot | None = None

    @model_validator(mode="after")
    def validate_output_snapshots(self) -> "_GenerationJobParametersV2":
        output_count = len(self.output_generations)
        if (
            len(self.output_prompt_resolutions) != output_count
            or self.generation.outputs_per_job != output_count
            or self.prompt_resolution != self.output_prompt_resolutions[0]
        ):
            raise ValueError("generation output snapshots are inconsistent")

        base = self.generation.model_dump(mode="json")
        first = self.output_generations[0].model_dump(mode="json")
        if {**first, "outputs_per_job": output_count} != base:
            raise ValueError("first output snapshot does not match the job generation")
        varying = {
            "prompt",
            "character_a_prompt",
            "character_b_prompt",
            "negative_prompt",
            "detailer_prompt",
            "detailer_negative_prompt",
            "seed",
            "outputs_per_job",
        }
        static_base = {key: value for key, value in base.items() if key not in varying}
        for generation, resolution in zip(
            self.output_generations,
            self.output_prompt_resolutions,
            strict=True,
        ):
            snapshot = generation.model_dump(mode="json")
            if (
                generation.outputs_per_job != 1
                or {key: value for key, value in snapshot.items() if key not in varying}
                != static_base
            ):
                raise ValueError("generation output snapshots are inconsistent")
            if not _resolution_matches_generation(resolution, generation):
                raise ValueError("prompt resolution does not match its generated output")
        if len({item.seed for item in self.output_generations}) != output_count:
            raise ValueError("generation output seeds are not unique")
        if (
            self.batch is not None
            and self.batch.image_offset + output_count > self.batch.image_count
        ):
            raise ValueError("generation outputs exceed the frozen batch bounds")
        return self


def unavailable_generation_details() -> dict[str, object]:
    return {"available": False, "message": _UNAVAILABLE_MESSAGE}


async def load_generation_details(
    session: AsyncSession,
    *,
    asset_id: UUID,
) -> dict[str, object] | None:
    row = (
        await session.execute(
            select(
                Asset,
                GenerationJob,
                ReleaseVersion.id,
                ReleaseVersion.release_id,
                ReleaseVersion.specification_sha256,
            )
            .join(GenerationJob, GenerationJob.id == Asset.generation_job_id)
            .join(ReleaseVersion, ReleaseVersion.id == GenerationJob.release_version_id)
            .where(Asset.id == asset_id)
        )
    ).one_or_none()
    if row is None:
        raise GenerationDetailsNotFoundError("generation details unavailable")
    asset, job, release_version_id, release_id, release_specification_sha256 = row
    if (
        asset.kind != AssetKind.RAW_MASTER
        or asset.state != AssetState.AVAILABLE
        or asset.generation_job_id != job.id
        or asset.output_index is None
        or asset.release_id != release_id
        or job.release_version_id != release_version_id
    ):
        raise GenerationDetailsNotFoundError("generation details unavailable")

    try:
        if not hmac.compare_digest(canonical_sha256(job.parameters), job.parameters_sha256):
            return None
        parameters = _GenerationJobParametersV2.model_validate(job.parameters)
        if (
            parameters.release_version_id != job.release_version_id
            or not hmac.compare_digest(
                parameters.release_specification_sha256,
                release_specification_sha256,
            )
            or len(parameters.output_generations) != job.expected_output_count
            or asset.output_index < 0
            or asset.output_index >= job.expected_output_count
            or asset.width is None
            or asset.height is None
            or asset.image_format is None
            or asset.sha256 is None
            or not _is_sha256(asset.sha256)
        ):
            return None
        generation = parameters.output_generations[asset.output_index]
        resolution = parameters.output_prompt_resolutions[asset.output_index]
        workflow_node_classes = await session.scalar(
            select(WorkflowApproval.reviewed_node_classes)
            .where(WorkflowApproval.workflow_sha256 == parameters.workflow.sha256)
            .order_by(WorkflowApproval.approval_version.desc(), WorkflowApproval.id.desc())
            .limit(1)
        )
        reviewed_node_classes = (
            tuple(workflow_node_classes)
            if isinstance(workflow_node_classes, list)
            and all(isinstance(item, str) for item in workflow_node_classes)
            else None
        )
        return _response_payload(
            asset=asset,
            job=job,
            parameters=parameters,
            generation=generation,
            resolution=resolution,
            workflow_node_classes=reviewed_node_classes,
        )
    except (TypeError, ValueError, ValidationError):
        return None


def _response_payload(
    *,
    asset: Asset,
    job: GenerationJob,
    parameters: _GenerationJobParametersV2,
    generation: GenerationParameters,
    resolution: _PromptResolution,
    workflow_node_classes: tuple[str, ...] | None,
) -> dict[str, object]:
    assert asset.output_index is not None
    assert asset.width is not None
    assert asset.height is not None
    assert asset.image_format is not None
    assert asset.sha256 is not None
    return {
        "available": True,
        "asset": {
            "id": str(asset.id),
            "width": asset.width,
            "height": asset.height,
            "image_format": asset.image_format,
            "sha256": asset.sha256,
        },
        "job": {
            "id": str(job.id),
            "release_version_id": str(job.release_version_id),
            "ordinal": parameters.ordinal,
            "state": job.state.value,
            "expected_output_count": job.expected_output_count,
        },
        "provider": {"name": job.provider},
        "output": {"index": asset.output_index},
        "batch": parameters.batch.model_dump(mode="json") if parameters.batch else None,
        "subjects": [
            {"name": subject.name, "canonical_age": subject.canonical_age}
            for subject in parameters.subjects
        ],
        "composition": {"mode": generation.composition_mode},
        "prompts": _prompt_payload(generation, resolution),
        "sampling": {
            "seed": str(generation.seed),
            "width": generation.width,
            "height": generation.height,
            "steps": generation.steps,
            "cfg": generation.cfg,
            "sampler": generation.sampler,
            "scheduler": generation.scheduler,
            "clip_skip": generation.clip_skip,
        },
        "hires": {
            "enabled": (
                None
                if workflow_node_classes is None
                else "LatentUpscaleBy" in workflow_node_classes
            ),
            "scale": generation.hires_scale,
            "denoise": generation.hires_denoise,
            "upscale_method": generation.hires_upscale_method,
        },
        "detailer": {
            "enabled": (
                None if workflow_node_classes is None else "FaceDetailer" in workflow_node_classes
            ),
            "guide_size": generation.detailer_guide_size,
            "max_size": generation.detailer_max_size,
            "denoise": generation.detailer_denoise,
            "bbox_threshold": generation.detailer_bbox_threshold,
            "bbox_dilation": generation.detailer_bbox_dilation,
            "bbox_crop_factor": generation.detailer_bbox_crop_factor,
            "feather": generation.detailer_feather,
        },
        "checkpoint": {
            "name": parameters.checkpoint.name,
            "sha256": parameters.checkpoint.sha256,
        },
        "loras": [
            {"name": lora.name, "sha256": lora.sha256, "weight": lora.weight}
            for lora in parameters.loras
        ],
        "workflow": {
            "name": parameters.workflow.name,
            "version": parameters.workflow.version,
            "sha256": parameters.workflow.sha256,
        },
        "wildcards": {
            "schema_version": resolution.schema_version,
            "versions": [
                {
                    "name": item.name,
                    "library_id": str(item.library_id),
                    "version_id": str(item.version_id),
                    "version_no": item.version_no,
                    "entries_sha256": item.entries_sha256,
                    "entry_count": item.entry_count,
                }
                for item in resolution.wildcard_versions
            ],
            "selections": [item.model_dump(mode="json") for item in resolution.selections],
        },
        "integrity": {
            "asset_sha256": asset.sha256,
            "job_parameters_sha256": job.parameters_sha256,
            "release_specification_sha256": parameters.release_specification_sha256,
            "approval_snapshot_sha256": parameters.approval_snapshot_sha256,
        },
    }


def _prompt_payload(
    generation: GenerationParameters,
    resolution: _PromptResolution,
) -> dict[str, object]:
    positive = _prompt_pair(
        source=resolution.source_prompt,
        resolved=generation.prompt,
        source_sha256=resolution.source_prompt_sha256,
        resolved_sha256=resolution.resolved_prompt_sha256,
    )
    if isinstance(resolution, _PromptResolutionV2):
        character_a = _prompt_pair(
            source=resolution.source_character_a_prompt,
            resolved=generation.character_a_prompt,
            source_sha256=resolution.source_character_a_prompt_sha256,
            resolved_sha256=resolution.resolved_character_a_prompt_sha256,
        )
        character_b = _prompt_pair(
            source=resolution.source_character_b_prompt,
            resolved=generation.character_b_prompt,
            source_sha256=resolution.source_character_b_prompt_sha256,
            resolved_sha256=resolution.resolved_character_b_prompt_sha256,
        )
    else:
        empty_sha256 = _text_sha256("")
        character_a = _prompt_pair(
            source="",
            resolved=generation.character_a_prompt,
            source_sha256=empty_sha256,
            resolved_sha256=empty_sha256,
        )
        character_b = _prompt_pair(
            source="",
            resolved=generation.character_b_prompt,
            source_sha256=empty_sha256,
            resolved_sha256=empty_sha256,
        )
    negative = _prompt_pair(
        source=resolution.source_negative_prompt,
        resolved=generation.negative_prompt,
        source_sha256=resolution.source_negative_prompt_sha256,
        resolved_sha256=resolution.resolved_negative_prompt_sha256,
    )
    detailer_positive = _prompt_pair(
        source=resolution.source_detailer_prompt or resolution.source_prompt,
        resolved=generation.detailer_prompt or generation.prompt,
        source_sha256=(
            resolution.source_detailer_prompt_sha256
            if resolution.source_detailer_prompt
            else resolution.source_prompt_sha256
        ),
        resolved_sha256=(
            resolution.resolved_detailer_prompt_sha256
            if generation.detailer_prompt
            else resolution.resolved_prompt_sha256
        ),
        inherited=not generation.detailer_prompt,
    )
    detailer_negative = _prompt_pair(
        source=resolution.source_detailer_negative_prompt or resolution.source_negative_prompt,
        resolved=generation.detailer_negative_prompt or generation.negative_prompt,
        source_sha256=(
            resolution.source_detailer_negative_prompt_sha256
            if resolution.source_detailer_negative_prompt
            else resolution.source_negative_prompt_sha256
        ),
        resolved_sha256=(
            resolution.resolved_detailer_negative_prompt_sha256
            if generation.detailer_negative_prompt
            else resolution.resolved_negative_prompt_sha256
        ),
        inherited=not generation.detailer_negative_prompt,
    )
    return {
        "positive": positive,
        "character_a": character_a,
        "character_b": character_b,
        "negative": negative,
        "detailer_positive": detailer_positive,
        "detailer_negative": detailer_negative,
    }


def _prompt_pair(
    *,
    source: str,
    resolved: str,
    source_sha256: str,
    resolved_sha256: str,
    inherited: bool = False,
) -> dict[str, object]:
    return {
        "source": source,
        "resolved": resolved,
        "source_sha256": source_sha256,
        "resolved_sha256": resolved_sha256,
        "inherited": inherited,
    }


def _resolution_matches_generation(
    resolution: _PromptResolution,
    generation: GenerationParameters,
) -> bool:
    field_names: tuple[str, ...]
    if isinstance(resolution, _PromptResolutionV1):
        if generation.composition_mode != "single":
            return False
        field_names = _LEGACY_PROMPT_FIELDS
    else:
        field_names = _PROMPT_FIELDS
    return resolution.seed == generation.seed and all(
        hmac.compare_digest(
            getattr(resolution, f"resolved_{field_name}_sha256"),
            _text_sha256(getattr(generation, field_name)),
        )
        for field_name in field_names
    )


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
