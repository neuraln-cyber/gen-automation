import hashlib
import hmac
import json
import math
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.controlled_duo import (
    DuoCompositionPreset,
    DuoIsolationMode,
    DuoQualityMode,
    WorkflowCapability,
    require_controlled_duo_capabilities,
    require_controlled_trio_capabilities,
)
from gen_automation.domain.deliverability import (
    DeliverabilityError,
    require_comfy_workflow_deliverability,
)
from gen_automation.domain.generation_limits import (
    MAX_INLINE_OUTPUTS_PER_SIGNED_GENERATION_JOB,
    MAX_PROMPT_TEXT_BYTES_PER_GENERATION_JOB,
    MAX_SIGNED_PROMPT_BUDGET_BYTES_PER_GENERATION_JOB,
    referenced_worker_prompt_budget_bytes,
    signed_worker_prompt_budget_bytes,
    utf8_prompt_bytes,
)
from gen_automation.domain.release_spec import (
    ArtifactSpecification,
    GenerationParameters,
    LoraSpecification,
    WorkflowSpecification,
)
from gen_automation.domain.signing import SigningMaterialError, validate_private_key
from gen_automation.gpu_worker.artifacts import (
    ArtifactKind,
    ArtifactManifest,
    ModelArtifactSpec,
)
from gen_automation.gpu_worker.models import (
    KEY_ID_PATTERN,
    MAX_HARD_REFERENCED_PAYLOAD_BYTES,
    GenerateEnvelope,
    GeneratePayload,
    GeneratePayloadReference,
    ReferencedGenerateEnvelope,
    UploadGrant,
)
from gen_automation.gpu_worker.security import calculate_signature
from gen_automation.services.assets import create_raw_master_upload_intents
from gen_automation.services.controlled_trio import (
    CONTROLLED_TRIO_MARKER_NODE_CLASS,
    ControlledTrioContractError,
    controlled_trio_bindings,
    prepare_controlled_trio_template,
)
from gen_automation.services.salad import SaladJobInputContext
from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectConflictError,
    ObjectStore,
    ObjectStoreError,
)

MAX_WORKFLOW_BYTES = 192 * 1024
MAX_RENDERED_WORKFLOW_BYTES = 512 * 1024
MAX_ENVELOPE_BYTES = 256 * 1024
MAX_SERIALIZED_UPLOAD_GRANT_BYTES = 12 * 1024
MAX_SIGNED_ENVELOPE_FIXED_OVERHEAD_BYTES = 16 * 1024
REFERENCED_PAYLOAD_CONTENT_TYPE = "application/vnd.gen-automation.generate-payload+json"
REFERENCED_PAYLOAD_PREFIX = "staging/worker-requests"
MAX_JSON_DEPTH = 64
MAX_JSON_ITEMS = 50_000
MIN_POST_ACCEPTANCE_UPLOAD_SECONDS = 3600
MAX_RUNTIME_LORAS = 8
LORA_CHAIN_NODE_CLASS = "GenAutomationLoraChain"
CONTROLLED_DUO_MARKER_NODE_CLASS = "GenAutomationControlledDuoV2"
MULTI_PROMPT_SHARED_NODE_CLASSES = frozenset(
    {
        "CheckpointLoaderSimple",
        "UNETLoader",
        "CLIPLoader",
        "VAELoader",
        CONTROLLED_DUO_MARKER_NODE_CLASS,
        CONTROLLED_TRIO_MARKER_NODE_CLASS,
        LORA_CHAIN_NODE_CLASS,
        "CLIPSetLastLayer",
        "UltralyticsDetectorProvider",
    }
)
type UploadContentType = Literal["image/png", "image/jpeg", "image/webp"]


class WorkerInputError(Exception):
    """A redacted failure safe for the provider orchestration layer."""


@dataclass(frozen=True)
class _ResolvedJobParameters:
    worker_request_budget_version: Literal[1, 2]
    checkpoint: ArtifactSpecification
    loras: tuple[LoraSpecification, ...]
    workflow: WorkflowSpecification
    generation: GenerationParameters
    output_generations: tuple[GenerationParameters, ...] = ()

    def bindings(
        self,
        runtime: "_RuntimeArtifactBindings",
        *,
        generation: GenerationParameters | None = None,
    ) -> dict[str, object]:
        checkpoint = self.checkpoint.model_dump(mode="json")
        checkpoint["runtime_filename"] = runtime.checkpoint_filename
        loras: list[dict[str, object]] = []
        for lora, filename in zip(self.loras, runtime.lora_filenames, strict=True):
            value = lora.model_dump(mode="json")
            value["runtime_filename"] = filename
            loras.append(value)
        selected_generation = generation or self.generation
        generation_binding = selected_generation.model_dump(mode="json")
        generation_binding["clip_stop_at_layer"] = -selected_generation.clip_skip
        generation_binding["detailer_prompt"] = (
            selected_generation.detailer_prompt or selected_generation.prompt
        )
        generation_binding["detailer_negative_prompt"] = (
            selected_generation.detailer_negative_prompt or selected_generation.negative_prompt
        )
        bindings: dict[str, object] = {
            "checkpoint": checkpoint,
            "loras": loras,
            "workflow": self.workflow.model_dump(mode="json"),
            "generation": generation_binding,
        }
        if runtime.primary_kind == ArtifactKind.DIFFUSION_MODEL:
            bindings["diffusion_model"] = {
                "runtime_filename": runtime.checkpoint_filename,
            }
        if runtime.text_encoder_filename is not None:
            bindings["text_encoder"] = {
                "runtime_filename": runtime.text_encoder_filename,
            }
        if runtime.vae_filename is not None:
            bindings["vae"] = {
                "runtime_filename": runtime.vae_filename,
            }
        controlled_duo = _controlled_duo_bindings(selected_generation)
        if controlled_duo is not None:
            bindings["controlled_duo"] = controlled_duo
        try:
            controlled_trio = controlled_trio_bindings(selected_generation)
        except ControlledTrioContractError as error:
            raise WorkerInputError(str(error)) from error
        if controlled_trio is not None:
            bindings["controlled_trio"] = controlled_trio
        if runtime.detector_filename is not None:
            bindings["detector"] = {
                "runtime_filename": runtime.detector_filename,
                "comfy_name": f"bbox/{runtime.detector_filename}",
            }
        return bindings

    def output_bindings(
        self,
        runtime: "_RuntimeArtifactBindings",
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            self.bindings(runtime, generation=generation) for generation in self.output_generations
        )


@dataclass(frozen=True)
class _RuntimeArtifactBindings:
    primary_kind: ArtifactKind
    checkpoint_filename: str
    lora_filenames: tuple[str, ...]
    detector_filename: str | None
    text_encoder_filename: str | None
    vae_filename: str | None


@dataclass(frozen=True, slots=True)
class _DuoMaskRegion:
    x: int
    y: int
    width: int
    height: int
    feather_left: int
    feather_top: int
    feather_right: int
    feather_bottom: int

    def as_binding(self) -> dict[str, int]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "feather_left": self.feather_left,
            "feather_top": self.feather_top,
            "feather_right": self.feather_right,
            "feather_bottom": self.feather_bottom,
        }


_DUO_PRESET_REGION_RATIOS: dict[
    DuoCompositionPreset,
    tuple[tuple[float, float, float, float], tuple[float, float, float, float]],
] = {
    DuoCompositionPreset.FLEXIBLE: (
        (0.02, 0.03, 0.46, 0.94),
        (0.52, 0.03, 0.46, 0.94),
    ),
    DuoCompositionPreset.CLOSE_PORTRAIT: (
        (0.04, 0.08, 0.44, 0.84),
        (0.52, 0.08, 0.44, 0.84),
    ),
    DuoCompositionPreset.OVERHEAD: (
        (0.07, 0.12, 0.40, 0.72),
        (0.54, 0.07, 0.39, 0.76),
    ),
    DuoCompositionPreset.LOW_ANGLE: (
        (0.03, 0.22, 0.45, 0.75),
        (0.52, 0.11, 0.45, 0.86),
    ),
    DuoCompositionPreset.DIAGONAL_DEPTH: (
        (0.02, 0.30, 0.52, 0.67),
        (0.58, 0.05, 0.39, 0.52),
    ),
    DuoCompositionPreset.BACK_TO_BACK: (
        (0.05, 0.09, 0.43, 0.84),
        (0.52, 0.09, 0.43, 0.84),
    ),
    DuoCompositionPreset.FULL_BODY: (
        (0.08, 0.03, 0.38, 0.94),
        (0.54, 0.03, 0.38, 0.94),
    ),
}
_DUO_POSITIVE_INVARIANT = "exactly two clearly adult characters, both visible, no other people"
_DUO_NEGATIVE_INVARIANT = (
    "third person, extra person, background person, duplicate character, cloned face, "
    "merged bodies, fused faces, shared face, mixed identity, swapped clothing"
)
_DUO_LOCAL_NEGATIVE_INVARIANT = (
    "extra face inside this region, duplicate subject inside this region, "
    "the other character's hair traits, the other character's outfit, identity crossover"
)


def _join_prompt_parts(*parts: str) -> str:
    return ", ".join(part.strip(" ,\n\t") for part in parts if part.strip(" ,\n\t"))


def _aligned_duo_region(
    *,
    canvas_width: int,
    canvas_height: int,
    ratios: tuple[float, float, float, float],
    feather: int,
) -> _DuoMaskRegion:
    x_ratio, y_ratio, width_ratio, height_ratio = ratios
    x = min(canvas_width - 64, max(0, round((canvas_width * x_ratio) / 8.0) * 8))
    y = min(canvas_height - 64, max(0, round((canvas_height * y_ratio) / 8.0) * 8))
    width = min(
        canvas_width - x,
        max(64, round((canvas_width * width_ratio) / 8.0) * 8),
    )
    height = min(
        canvas_height - y,
        max(64, round((canvas_height * height_ratio) / 8.0) * 8),
    )
    return _DuoMaskRegion(
        x=x,
        y=y,
        width=width,
        height=height,
        feather_left=min(feather, width // 4),
        feather_top=min(feather, height // 4),
        feather_right=min(feather, width // 4),
        feather_bottom=min(feather, height // 4),
    )


def _controlled_duo_bindings(
    generation: GenerationParameters,
) -> dict[str, object] | None:
    if generation.duo_contract_version != 2:
        return None
    preset = generation.composition_preset_id
    if generation.composition_mode != "duo" or not isinstance(preset, DuoCompositionPreset):
        raise WorkerInputError("Controlled Duo generation parameters are invalid")

    feather = min(32, max(8, generation.width // 64))
    character_a_ratios, character_b_ratios = _DUO_PRESET_REGION_RATIOS[preset]
    character_a = _aligned_duo_region(
        canvas_width=generation.width,
        canvas_height=generation.height,
        ratios=character_a_ratios,
        feather=feather,
    )
    character_b = _aligned_duo_region(
        canvas_width=generation.width,
        canvas_height=generation.height,
        ratios=character_b_ratios,
        feather=feather,
    )
    horizontal_overlap = max(character_a.x, character_b.x) < min(
        character_a.x + character_a.width,
        character_b.x + character_b.width,
    )
    vertical_overlap = max(character_a.y, character_b.y) < min(
        character_a.y + character_a.height,
        character_b.y + character_b.height,
    )
    if horizontal_overlap and vertical_overlap:
        raise WorkerInputError("Controlled Duo preset masks must be disjoint")
    shared_positive = _join_prompt_parts(
        _DUO_POSITIVE_INVARIANT,
        generation.prompt,
        generation.interaction_prompt,
        generation.camera_prompt,
    )
    shared_negative = _join_prompt_parts(
        generation.negative_prompt,
        _DUO_NEGATIVE_INVARIANT,
    )
    character_a_local_positive = _join_prompt_parts(
        "left-side subject only, character A only",
        generation.character_a_prompt,
        generation.character_a_pose_prompt,
    )
    character_b_local_positive = _join_prompt_parts(
        "right-side subject only, character B only",
        generation.character_b_prompt,
        generation.character_b_pose_prompt,
    )
    character_a_local_negative = _join_prompt_parts(
        generation.character_a_negative_prompt,
        _DUO_LOCAL_NEGATIVE_INVARIANT,
    )
    character_b_local_negative = _join_prompt_parts(
        generation.character_b_negative_prompt,
        _DUO_LOCAL_NEGATIVE_INVARIANT,
    )
    refinement_fraction = 0.25 if generation.duo_quality_mode == DuoQualityMode.DRAFT else 0.50
    refinement_floor = 6 if generation.duo_quality_mode == DuoQualityMode.DRAFT else 10
    base_steps = (
        min(generation.steps, max(8, math.ceil(generation.steps * 0.60)))
        if generation.duo_quality_mode == DuoQualityMode.DRAFT
        else generation.steps
    )
    refinement_steps = min(
        generation.steps,
        max(
            refinement_floor,
            math.ceil(generation.steps * refinement_fraction),
        ),
    )
    refinement_denoise = 0.30 if generation.duo_quality_mode == DuoQualityMode.DRAFT else 0.42
    return {
        "contract_version": 2,
        "composition_preset_id": preset.value,
        "isolation_mode": generation.duo_isolation_mode.value,
        "quality_mode": generation.duo_quality_mode.value,
        "shared_positive_prompt": shared_positive,
        "shared_negative_prompt": shared_negative,
        "character_a_local_positive_prompt": character_a_local_positive,
        "character_b_local_positive_prompt": character_b_local_positive,
        "character_a_local_negative_prompt": character_a_local_negative,
        "character_b_local_negative_prompt": character_b_local_negative,
        "character_a_refinement_positive_prompt": _join_prompt_parts(
            "one subject only in this masked region, preserve the existing pose and framing",
            generation.prompt,
            generation.camera_prompt,
            character_a_local_positive,
        ),
        "character_b_refinement_positive_prompt": _join_prompt_parts(
            "one subject only in this masked region, preserve the existing pose and framing",
            generation.prompt,
            generation.camera_prompt,
            character_b_local_positive,
        ),
        "character_a_refinement_negative_prompt": _join_prompt_parts(
            shared_negative,
            character_a_local_negative,
        ),
        "character_b_refinement_negative_prompt": _join_prompt_parts(
            shared_negative,
            character_b_local_negative,
        ),
        "character_a": character_a.as_binding(),
        "character_b": character_b.as_binding(),
        "base_steps": base_steps,
        "refinement_steps": refinement_steps,
        "refinement_denoise": refinement_denoise,
    }


def _resolve_manifest_artifact(
    manifest: ArtifactManifest,
    specification: ArtifactSpecification,
    *,
    kind: ArtifactKind,
) -> ModelArtifactSpec:
    matches = tuple(
        artifact
        for artifact in manifest.artifacts
        if artifact.kind == kind and artifact.sha256 == specification.sha256
    )
    if len(matches) != 1:
        raise WorkerInputError("generation artifacts do not match the worker manifest")
    artifact = matches[0]
    if (
        artifact.source_object_id is not None
        and artifact.source_object_id != specification.storage_key
    ):
        raise WorkerInputError("generation artifacts do not match the worker manifest")
    return artifact


def _resolve_runtime_artifacts(
    resolved: _ResolvedJobParameters,
    manifest: ArtifactManifest,
) -> _RuntimeArtifactBindings:
    if len(resolved.loras) > MAX_RUNTIME_LORAS:
        raise WorkerInputError("generation supports at most eight LoRAs")
    if len({lora.sha256 for lora in resolved.loras}) != len(resolved.loras):
        raise WorkerInputError("generation artifacts do not match the worker manifest")

    primaries = tuple(
        artifact
        for artifact in manifest.artifacts
        if artifact.kind in {ArtifactKind.CHECKPOINT, ArtifactKind.DIFFUSION_MODEL}
        and artifact.sha256 == resolved.checkpoint.sha256
    )
    if len(primaries) != 1:
        raise WorkerInputError("generation artifacts do not match the worker manifest")
    checkpoint = primaries[0]
    if (
        checkpoint.source_object_id is not None
        and checkpoint.source_object_id != resolved.checkpoint.storage_key
    ):
        raise WorkerInputError("generation artifacts do not match the worker manifest")
    loras = tuple(
        _resolve_manifest_artifact(manifest, lora, kind=ArtifactKind.LORA)
        for lora in resolved.loras
    )
    detectors = tuple(
        artifact for artifact in manifest.artifacts if artifact.kind == ArtifactKind.DETECTOR
    )
    if len(detectors) > 1:
        raise WorkerInputError("generation supports at most one face detector")
    text_encoders = tuple(
        artifact for artifact in manifest.artifacts if artifact.kind == ArtifactKind.TEXT_ENCODER
    )
    vaes = tuple(artifact for artifact in manifest.artifacts if artifact.kind == ArtifactKind.VAE)
    if checkpoint.kind == ArtifactKind.CHECKPOINT:
        if text_encoders or vaes:
            raise WorkerInputError("generation artifacts do not match the worker manifest")
    elif len(text_encoders) != 1 or len(vaes) != 1:
        raise WorkerInputError("Anima runtime support artifacts are unavailable")
    return _RuntimeArtifactBindings(
        primary_kind=checkpoint.kind,
        checkpoint_filename=checkpoint.target_filename,
        lora_filenames=tuple(lora.target_filename for lora in loras),
        detector_filename=detectors[0].target_filename if detectors else None,
        text_encoder_filename=(text_encoders[0].target_filename if text_encoders else None),
        vae_filename=vaes[0].target_filename if vaes else None,
    )


def _require_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise WorkerInputError("generation parameters are invalid")
    return value


def _resolve_job_parameters(context: SaladJobInputContext) -> _ResolvedJobParameters:
    parameters = _require_mapping(context.parameters)
    if canonical_sha256(dict(parameters)) != context.parameters_sha256:
        raise WorkerInputError("generation parameter integrity check failed")
    schema_version = parameters.get("schema_version")
    if schema_version not in {1, 2}:
        raise WorkerInputError("generation parameter schema is unsupported")
    if parameters.get("release_version_id") != str(context.release_version_id):
        raise WorkerInputError("generation parameter identity check failed")
    worker_request_budget_version = parameters.get("worker_request_budget_version", 1)
    if worker_request_budget_version not in {1, 2}:
        raise WorkerInputError("generation parameter schema is unsupported")

    try:
        checkpoint = ArtifactSpecification.model_validate(parameters.get("checkpoint"))
        loras_raw = parameters.get("loras")
        if not isinstance(loras_raw, list):
            raise WorkerInputError("generation parameters are invalid")
        loras = tuple(LoraSpecification.model_validate(item) for item in loras_raw)
        workflow = WorkflowSpecification.model_validate(parameters.get("workflow"))
        generation = GenerationParameters.model_validate(parameters.get("generation"))
        output_generations: tuple[GenerationParameters, ...] = ()
        if schema_version == 2:
            raw_output_generations = parameters.get("output_generations")
            if (
                not isinstance(raw_output_generations, list)
                or len(raw_output_generations) != context.expected_output_count
            ):
                raise WorkerInputError("generation output parameters are invalid")
            output_generations = tuple(
                GenerationParameters.model_validate(item) for item in raw_output_generations
            )
    except ValidationError:
        raise WorkerInputError("generation parameters are invalid") from None
    if generation.duo_contract_version == 2:
        try:
            require_controlled_duo_capabilities(
                frozenset(workflow.capabilities),
                isolation_mode=generation.duo_isolation_mode,
                quality_mode=generation.duo_quality_mode,
            )
        except ValueError:
            raise WorkerInputError("Controlled Duo workflow capability is invalid") from None
    elif generation.duo_contract_version == 3:
        try:
            require_controlled_trio_capabilities(
                frozenset(workflow.capabilities),
                isolation_mode=generation.duo_isolation_mode,
                quality_mode=generation.duo_quality_mode,
            )
        except ValueError:
            raise WorkerInputError("Controlled Trio workflow capability is invalid") from None
    if generation.outputs_per_job != context.expected_output_count:
        raise WorkerInputError("generation output count is inconsistent")
    if output_generations:
        if any(item.outputs_per_job != 1 for item in output_generations):
            raise WorkerInputError("generation output parameters are invalid")
        base = generation.model_dump(mode="json")
        first = output_generations[0].model_dump(mode="json")
        if {**first, "outputs_per_job": generation.outputs_per_job} != base:
            raise WorkerInputError("generation output parameters are inconsistent")
        varying_fields = {
            "prompt",
            "character_a_prompt",
            "character_b_prompt",
            "character_a_pose_prompt",
            "character_b_pose_prompt",
            "character_c_prompt",
            "character_c_pose_prompt",
            "character_a_negative_prompt",
            "character_b_negative_prompt",
            "character_c_negative_prompt",
            "interaction_prompt",
            "camera_prompt",
            "negative_prompt",
            "detailer_prompt",
            "detailer_negative_prompt",
            "seed",
            "outputs_per_job",
        }
        static_base = {key: value for key, value in base.items() if key not in varying_fields}
        if any(
            {
                key: value
                for key, value in item.model_dump(mode="json").items()
                if key not in varying_fields
            }
            != static_base
            for item in output_generations
        ):
            raise WorkerInputError("generation output parameters are inconsistent")
        if len({item.seed for item in output_generations}) != len(output_generations):
            raise WorkerInputError("generation output seeds are inconsistent")
        output_prompt_values = tuple(
            value
            for item in output_generations
            for value in (
                item.prompt,
                item.character_a_prompt,
                item.character_b_prompt,
                item.character_a_pose_prompt,
                item.character_b_pose_prompt,
                item.character_c_prompt,
                item.character_c_pose_prompt,
                item.character_a_negative_prompt,
                item.character_b_negative_prompt,
                item.character_c_negative_prompt,
                item.interaction_prompt,
                item.camera_prompt,
                item.negative_prompt,
                item.detailer_prompt,
                item.detailer_negative_prompt,
            )
        )
        prompt_bytes = utf8_prompt_bytes(output_prompt_values)
        referenced = (
            worker_request_budget_version >= 2
            and context.expected_output_count > MAX_INLINE_OUTPUTS_PER_SIGNED_GENERATION_JOB
        )
        budgeted_prompt_bytes = (
            referenced_worker_prompt_budget_bytes(output_prompt_values)
            if referenced
            else (
                signed_worker_prompt_budget_bytes(output_prompt_values)
                if worker_request_budget_version >= 2
                else prompt_bytes
            )
        )
        prompt_budget_limit = (
            MAX_PROMPT_TEXT_BYTES_PER_GENERATION_JOB
            if referenced or worker_request_budget_version < 2
            else MAX_SIGNED_PROMPT_BUDGET_BYTES_PER_GENERATION_JOB
        )
        if budgeted_prompt_bytes > prompt_budget_limit:
            raise WorkerInputError("generation prompt text exceeds the worker request budget")
    return _ResolvedJobParameters(
        worker_request_budget_version=worker_request_budget_version,
        checkpoint=checkpoint,
        loras=loras,
        workflow=workflow,
        generation=generation,
        output_generations=output_generations,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerInputError("workflow template is invalid")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise WorkerInputError("workflow template is invalid")


def _validate_json_shape(value: object, *, depth: int = 0, counter: list[int]) -> None:
    counter[0] += 1
    if counter[0] > MAX_JSON_ITEMS or depth > MAX_JSON_DEPTH:
        raise WorkerInputError("workflow template is invalid")
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkerInputError("workflow template is invalid")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_shape(item, depth=depth + 1, counter=counter)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 512:
                raise WorkerInputError("workflow template is invalid")
            _validate_json_shape(item, depth=depth + 1, counter=counter)
        return
    raise WorkerInputError("workflow template is invalid")


def _parse_workflow_template(raw: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise WorkerInputError("workflow template is invalid") from None
    _validate_json_shape(parsed, counter=[0])
    if not isinstance(parsed, dict) or not parsed:
        raise WorkerInputError("workflow template is invalid")
    return parsed


def _is_binding(value: object, path: str) -> bool:
    return value == {"$gen": path}


def _require_template_node(
    template: Mapping[str, object],
    node_id: object,
    node_class: str,
) -> Mapping[str, object]:
    if not isinstance(node_id, str) or not node_id:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    node = template.get(node_id)
    if not isinstance(node, Mapping) or node.get("class_type") != node_class:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    inputs = node.get("inputs")
    if not isinstance(inputs, Mapping):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    return inputs


def _require_template_link(value: object, node_id: str) -> None:
    if value != [node_id, 0]:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")


def _validate_controlled_duo_mask_evidence(
    template: Mapping[str, object],
    *,
    mask_node_id: str,
    character: Literal["character_a", "character_b"],
) -> str:
    composite = _require_template_node(template, mask_node_id, "MaskComposite")
    if (
        set(composite) != {"destination", "source", "x", "y", "operation"}
        or composite.get("operation") != "add"
    ):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    destination = composite.get("destination")
    source = composite.get("source")
    if (
        not isinstance(destination, list)
        or len(destination) != 2
        or not isinstance(destination[0], str)
        or destination[1] != 0
        or not isinstance(source, list)
        or len(source) != 2
        or not isinstance(source[0], str)
        or source[1] != 0
    ):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    full_mask = _require_template_node(template, destination[0], "SolidMask")
    feather = _require_template_node(template, source[0], "FeatherMask")
    region_link = feather.get("mask")
    if (
        not isinstance(region_link, list)
        or len(region_link) != 2
        or not isinstance(region_link[0], str)
        or region_link[1] != 0
    ):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    region_mask = _require_template_node(template, region_link[0], "SolidMask")
    if (
        set(full_mask) != {"value", "width", "height"}
        or set(region_mask) != {"value", "width", "height"}
        or set(feather) != {"mask", "left", "top", "right", "bottom"}
        or full_mask.get("value") != 0.0
        or not _is_binding(full_mask.get("width"), "generation.width")
        or not _is_binding(full_mask.get("height"), "generation.height")
        or region_mask.get("value") != 1.0
        or not _is_binding(
            region_mask.get("width"),
            f"controlled_duo.{character}.width",
        )
        or not _is_binding(
            region_mask.get("height"),
            f"controlled_duo.{character}.height",
        )
        or not _is_binding(composite.get("x"), f"controlled_duo.{character}.x")
        or not _is_binding(composite.get("y"), f"controlled_duo.{character}.y")
    ):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    for edge in ("left", "top", "right", "bottom"):
        if not _is_binding(
            feather.get(edge),
            f"controlled_duo.{character}.feather_{edge}",
        ):
            raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    return destination[0]


def _validate_controlled_duo_prompt_evidence(
    template: Mapping[str, object],
    *,
    isolation_mode: DuoIsolationMode,
) -> dict[str, str]:
    clip_nodes = [
        node_id
        for node_id, raw_node in template.items()
        if isinstance(raw_node, Mapping) and raw_node.get("class_type") == "CLIPSetLastLayer"
    ]
    if len(clip_nodes) != 1:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    prompt_nodes: dict[str, str] = {}
    for node_id, raw_node in template.items():
        if not isinstance(raw_node, Mapping) or raw_node.get("class_type") != "CLIPTextEncode":
            continue
        inputs = raw_node.get("inputs")
        if not isinstance(inputs, Mapping):
            raise WorkerInputError("Controlled Duo workflow evidence is invalid")
        text = inputs.get("text")
        if (
            not isinstance(text, Mapping)
            or set(text) != {"$gen"}
            or set(inputs) != {"clip", "text"}
            or not isinstance(text["$gen"], str)
            or text["$gen"] in prompt_nodes
            or inputs.get("clip") != [clip_nodes[0], 0]
        ):
            raise WorkerInputError("Controlled Duo workflow evidence is invalid")
        prompt_nodes[text["$gen"]] = node_id
    expected_paths = {
        "controlled_duo.shared_positive_prompt",
        "controlled_duo.shared_negative_prompt",
    }
    expected_paths.update(
        {
            "controlled_duo.character_a_local_positive_prompt",
            "controlled_duo.character_b_local_positive_prompt",
            "controlled_duo.character_a_local_negative_prompt",
            "controlled_duo.character_b_local_negative_prompt",
        }
    )
    if isolation_mode == DuoIsolationMode.STRICT:
        expected_paths.update(
            {
                "controlled_duo.character_a_refinement_positive_prompt",
                "controlled_duo.character_b_refinement_positive_prompt",
                "controlled_duo.character_a_refinement_negative_prompt",
                "controlled_duo.character_b_refinement_negative_prompt",
            }
        )
    if set(prompt_nodes) != expected_paths:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    return prompt_nodes


def _find_conditioning_combine(
    template: Mapping[str, object],
    *,
    conditioning_1: str,
    conditioning_2: str,
) -> str:
    matches = []
    for node_id, raw_node in template.items():
        if not isinstance(raw_node, Mapping) or raw_node.get("class_type") != "ConditioningCombine":
            continue
        inputs = raw_node.get("inputs")
        if isinstance(inputs, Mapping) and inputs == {
            "conditioning_1": [conditioning_1, 0],
            "conditioning_2": [conditioning_2, 0],
        }:
            matches.append(node_id)
    if len(matches) != 1:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    return matches[0]


def _only_controlled_duo_node(
    template: Mapping[str, object],
    node_class: str,
) -> tuple[str, Mapping[str, object]]:
    matches = [
        (node_id, raw_node)
        for node_id, raw_node in template.items()
        if isinstance(raw_node, Mapping) and raw_node.get("class_type") == node_class
    ]
    if len(matches) != 1:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    node_id, raw_node = matches[0]
    inputs = raw_node.get("inputs")
    if not isinstance(inputs, Mapping):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    return node_id, inputs


def _validate_controlled_duo_backbone_evidence(
    template: Mapping[str, object],
) -> tuple[str, str, str]:
    checkpoint_node_id, checkpoint = _only_controlled_duo_node(
        template,
        "CheckpointLoaderSimple",
    )
    lora_chain_node_id, lora_chain = _only_controlled_duo_node(
        template,
        LORA_CHAIN_NODE_CLASS,
    )
    _clip_node_id, clip = _only_controlled_duo_node(template, "CLIPSetLastLayer")
    latent_node_id, latent = _only_controlled_duo_node(template, "EmptyLatentImage")
    if (
        checkpoint != {"ckpt_name": {"$gen": "checkpoint.runtime_filename"}}
        or lora_chain
        != {
            "model": [checkpoint_node_id, 0],
            "clip": [checkpoint_node_id, 1],
        }
        or clip
        != {
            "clip": [lora_chain_node_id, 1],
            "stop_at_clip_layer": {"$gen": "generation.clip_stop_at_layer"},
        }
        or latent
        != {
            "batch_size": {"$gen": "generation.outputs_per_job"},
            "height": {"$gen": "generation.height"},
            "width": {"$gen": "generation.width"},
        }
    ):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    return checkpoint_node_id, lora_chain_node_id, latent_node_id


def _validate_controlled_duo_sampler_common_inputs(
    sampler: Mapping[str, object],
    *,
    lora_chain_node_id: str,
) -> None:
    if (
        set(sampler)
        != {
            "cfg",
            "denoise",
            "latent_image",
            "model",
            "negative",
            "positive",
            "sampler_name",
            "scheduler",
            "seed",
            "steps",
        }
        or sampler.get("cfg") != {"$gen": "generation.cfg"}
        or sampler.get("model") != [lora_chain_node_id, 0]
        or sampler.get("sampler_name") != {"$gen": "generation.sampler"}
        or sampler.get("scheduler") != {"$gen": "generation.scheduler"}
        or sampler.get("seed") != {"$gen": "generation.seed"}
    ):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")


def _validate_controlled_duo_sampling_evidence(
    template: Mapping[str, object],
    *,
    marker_inputs: Mapping[str, object],
    isolation_mode: DuoIsolationMode,
    character_a_mask_node_id: str,
    character_b_mask_node_id: str,
    prompt_node_ids: Mapping[str, str],
    lora_chain_node_id: str,
    base_latent_node_id: str,
) -> None:
    base_sampler_id = marker_inputs.get("base_sampler_node_id")
    final_sampler_id = marker_inputs.get("final_sampler_node_id")
    base_sampler = _require_template_node(template, base_sampler_id, "KSampler")
    _validate_controlled_duo_sampler_common_inputs(
        base_sampler,
        lora_chain_node_id=lora_chain_node_id,
    )
    if isolation_mode == DuoIsolationMode.BALANCED and final_sampler_id != base_sampler_id:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    set_masks = [
        node
        for node in template.values()
        if isinstance(node, Mapping) and node.get("class_type") == "ConditioningSetMask"
    ]
    if len(set_masks) != 4:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    expected_masked_prompts = {
        "controlled_duo.character_a_local_positive_prompt": character_a_mask_node_id,
        "controlled_duo.character_b_local_positive_prompt": character_b_mask_node_id,
        "controlled_duo.character_a_local_negative_prompt": character_a_mask_node_id,
        "controlled_duo.character_b_local_negative_prompt": character_b_mask_node_id,
    }
    masked_prompt_nodes: dict[str, str] = {}
    for prompt_path, expected_mask_id in expected_masked_prompts.items():
        encode_node_id = prompt_node_ids[prompt_path]
        matches = []
        for node_id, node in template.items():
            if not isinstance(node, Mapping) or node.get("class_type") != "ConditioningSetMask":
                continue
            inputs = node.get("inputs")
            if isinstance(inputs, Mapping) and inputs == {
                "conditioning": [encode_node_id, 0],
                "mask": [expected_mask_id, 0],
                "strength": 1.0,
                "set_cond_area": "default",
            }:
                matches.append(node_id)
        if len(matches) != 1:
            raise WorkerInputError("Controlled Duo workflow evidence is invalid")
        masked_prompt_nodes[prompt_path] = matches[0]
    if len(set(masked_prompt_nodes.values())) != 4:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    positive_a = _find_conditioning_combine(
        template,
        conditioning_1=prompt_node_ids["controlled_duo.shared_positive_prompt"],
        conditioning_2=masked_prompt_nodes["controlled_duo.character_a_local_positive_prompt"],
    )
    positive_final = _find_conditioning_combine(
        template,
        conditioning_1=positive_a,
        conditioning_2=masked_prompt_nodes["controlled_duo.character_b_local_positive_prompt"],
    )
    negative_a = _find_conditioning_combine(
        template,
        conditioning_1=prompt_node_ids["controlled_duo.shared_negative_prompt"],
        conditioning_2=masked_prompt_nodes["controlled_duo.character_a_local_negative_prompt"],
    )
    negative_final = _find_conditioning_combine(
        template,
        conditioning_1=negative_a,
        conditioning_2=masked_prompt_nodes["controlled_duo.character_b_local_negative_prompt"],
    )
    if (
        base_sampler.get("positive") != [positive_final, 0]
        or base_sampler.get("negative") != [negative_final, 0]
        or base_sampler.get("latent_image") != [base_latent_node_id, 0]
        or base_sampler.get("denoise") != 1.0
        or not _is_binding(base_sampler.get("steps"), "controlled_duo.base_steps")
    ):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    if isolation_mode == DuoIsolationMode.BALANCED:
        return

    character_a_noise_mask_id = marker_inputs.get("character_a_noise_mask_node_id")
    character_a_sampler_id = marker_inputs.get("character_a_sampler_node_id")
    character_b_noise_mask_id = marker_inputs.get("character_b_noise_mask_node_id")
    character_b_sampler_id = marker_inputs.get("character_b_sampler_node_id")
    character_a_noise = _require_template_node(
        template,
        character_a_noise_mask_id,
        "SetLatentNoiseMask",
    )
    character_a_sampler = _require_template_node(
        template,
        character_a_sampler_id,
        "KSampler",
    )
    character_b_noise = _require_template_node(
        template,
        character_b_noise_mask_id,
        "SetLatentNoiseMask",
    )
    character_b_sampler = _require_template_node(
        template,
        character_b_sampler_id,
        "KSampler",
    )
    _validate_controlled_duo_sampler_common_inputs(
        character_a_sampler,
        lora_chain_node_id=lora_chain_node_id,
    )
    _validate_controlled_duo_sampler_common_inputs(
        character_b_sampler,
        lora_chain_node_id=lora_chain_node_id,
    )
    assert isinstance(base_sampler_id, str)
    assert isinstance(character_a_noise_mask_id, str)
    assert isinstance(character_a_sampler_id, str)
    assert isinstance(character_b_noise_mask_id, str)
    assert isinstance(character_b_sampler_id, str)
    _require_template_link(character_a_noise.get("samples"), base_sampler_id)
    _require_template_link(character_a_noise.get("mask"), character_a_mask_node_id)
    _require_template_link(
        character_a_sampler.get("latent_image"),
        character_a_noise_mask_id,
    )
    _require_template_link(character_b_noise.get("samples"), character_a_sampler_id)
    _require_template_link(character_b_noise.get("mask"), character_b_mask_node_id)
    _require_template_link(
        character_b_sampler.get("latent_image"),
        character_b_noise_mask_id,
    )
    if set(character_a_noise) != {"samples", "mask"} or set(character_b_noise) != {
        "samples",
        "mask",
    }:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    if final_sampler_id != character_b_sampler_id:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    for sampler in (character_a_sampler, character_b_sampler):
        if not _is_binding(
            sampler.get("steps"), "controlled_duo.refinement_steps"
        ) or not _is_binding(
            sampler.get("denoise"),
            "controlled_duo.refinement_denoise",
        ):
            raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    if (
        character_a_sampler.get("positive")
        != [prompt_node_ids["controlled_duo.character_a_refinement_positive_prompt"], 0]
        or character_a_sampler.get("negative")
        != [prompt_node_ids["controlled_duo.character_a_refinement_negative_prompt"], 0]
        or character_b_sampler.get("positive")
        != [prompt_node_ids["controlled_duo.character_b_refinement_positive_prompt"], 0]
        or character_b_sampler.get("negative")
        != [prompt_node_ids["controlled_duo.character_b_refinement_negative_prompt"], 0]
    ):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")


def _prepare_controlled_duo_template(
    template: dict[str, object],
    *,
    specification: WorkflowSpecification,
    generation: GenerationParameters,
) -> dict[str, object]:
    marker_nodes = [
        (node_id, raw_node)
        for node_id, raw_node in template.items()
        if isinstance(raw_node, Mapping)
        and raw_node.get("class_type") == CONTROLLED_DUO_MARKER_NODE_CLASS
    ]
    capabilities = frozenset(specification.capabilities)
    declares_controlled_duo = WorkflowCapability.CONTROLLED_DUO_V2 in capabilities
    if generation.duo_contract_version != 2:
        if marker_nodes or declares_controlled_duo:
            raise WorkerInputError("Controlled Duo workflow capability is invalid")
        return template
    if len(marker_nodes) != 1 or not declares_controlled_duo:
        raise WorkerInputError("Controlled Duo workflow capability is invalid")

    marker_node_id, marker = marker_nodes[0]
    marker_inputs = marker.get("inputs")
    if not isinstance(marker_inputs, Mapping):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    isolation_value = marker_inputs.get("isolation_mode")
    if not isinstance(isolation_value, str):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    try:
        isolation_mode = DuoIsolationMode(isolation_value)
    except ValueError:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid") from None
    expected_keys = {
        "contract_version",
        "isolation_mode",
        "mask_topology",
        "character_a_mask_node_id",
        "character_b_mask_node_id",
        "base_sampler_node_id",
        "final_sampler_node_id",
    }
    if isolation_mode == DuoIsolationMode.STRICT:
        expected_keys.update(
            {
                "character_a_noise_mask_node_id",
                "character_a_sampler_node_id",
                "character_b_noise_mask_node_id",
                "character_b_sampler_node_id",
            }
        )
    if (
        set(marker_inputs) != expected_keys
        or marker_inputs.get("contract_version") != 2
        or marker_inputs.get("mask_topology") != "disjoint_preset_rectangles_v1"
        or generation.duo_isolation_mode != isolation_mode
    ):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    declares_strict = WorkflowCapability.DUO_STRICT_ISOLATION in capabilities
    if declares_strict != (isolation_mode == DuoIsolationMode.STRICT):
        raise WorkerInputError("Controlled Duo workflow capability is invalid")
    if WorkflowCapability.DUO_HIGH_QUALITY in capabilities:
        raise WorkerInputError("Controlled Duo high-quality workflow evidence is unavailable")

    class_counts: Counter[object] = Counter(
        raw_node.get("class_type")
        for raw_node in template.values()
        if isinstance(raw_node, Mapping)
    )
    expected_sampler_count = 1 if isolation_mode == DuoIsolationMode.BALANCED else 3
    expected_noise_mask_count = 0 if isolation_mode == DuoIsolationMode.BALANCED else 2
    expected_prompt_count = 6 if isolation_mode == DuoIsolationMode.BALANCED else 10
    expected_class_counts: Counter[object] = Counter(
        {
            "CheckpointLoaderSimple": 1,
            LORA_CHAIN_NODE_CLASS: 1,
            "CLIPSetLastLayer": 1,
            "SolidMask": 3,
            "FeatherMask": 2,
            "MaskComposite": 2,
            "CLIPTextEncode": expected_prompt_count,
            "ConditioningSetMask": 4,
            "ConditioningCombine": 4,
            "EmptyLatentImage": 1,
            "KSampler": expected_sampler_count,
            "SetLatentNoiseMask": expected_noise_mask_count,
            "VAEDecode": 1,
            "SaveImage": 1,
            CONTROLLED_DUO_MARKER_NODE_CLASS: 1,
        }
    )
    if class_counts != expected_class_counts:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")

    checkpoint_node_id, lora_chain_node_id, base_latent_node_id = (
        _validate_controlled_duo_backbone_evidence(template)
    )

    character_a_mask_node_id = marker_inputs.get("character_a_mask_node_id")
    character_b_mask_node_id = marker_inputs.get("character_b_mask_node_id")
    if not isinstance(character_a_mask_node_id, str) or not isinstance(
        character_b_mask_node_id,
        str,
    ):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    character_a_full_mask = _validate_controlled_duo_mask_evidence(
        template,
        mask_node_id=character_a_mask_node_id,
        character="character_a",
    )
    character_b_full_mask = _validate_controlled_duo_mask_evidence(
        template,
        mask_node_id=character_b_mask_node_id,
        character="character_b",
    )
    if character_a_full_mask != character_b_full_mask:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    prompt_node_ids = _validate_controlled_duo_prompt_evidence(
        template,
        isolation_mode=isolation_mode,
    )
    _validate_controlled_duo_sampling_evidence(
        template,
        marker_inputs=marker_inputs,
        isolation_mode=isolation_mode,
        character_a_mask_node_id=character_a_mask_node_id,
        character_b_mask_node_id=character_b_mask_node_id,
        prompt_node_ids=prompt_node_ids,
        lora_chain_node_id=lora_chain_node_id,
        base_latent_node_id=base_latent_node_id,
    )
    final_sampler_id = marker_inputs["final_sampler_node_id"]
    decode_inputs = next(
        raw_node["inputs"]
        for raw_node in template.values()
        if isinstance(raw_node, dict) and raw_node.get("class_type") == "VAEDecode"
    )
    save_inputs = next(
        raw_node["inputs"]
        for raw_node in template.values()
        if isinstance(raw_node, dict) and raw_node.get("class_type") == "SaveImage"
    )
    if not isinstance(final_sampler_id, str):
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    decode_node_id = next(
        node_id
        for node_id, raw_node in template.items()
        if isinstance(raw_node, dict) and raw_node.get("class_type") == "VAEDecode"
    )
    expected_filename_prefix = (
        "gen-automation-controlled-duo-balanced-v2"
        if isolation_mode == DuoIsolationMode.BALANCED
        else "gen-automation-controlled-duo-strict-v2"
    )
    if decode_inputs != {
        "samples": [final_sampler_id, 0],
        "vae": [checkpoint_node_id, 2],
    } or save_inputs != {
        "filename_prefix": expected_filename_prefix,
        "images": [decode_node_id, 0],
    }:
        raise WorkerInputError("Controlled Duo workflow evidence is invalid")
    return {
        node_id: raw_node for node_id, raw_node in template.items() if node_id != marker_node_id
    }


def _resolve_binding(path: str, bindings: Mapping[str, object]) -> object:
    if not path or len(path) > 256:
        raise WorkerInputError("workflow template contains an invalid binding")
    current: object = bindings
    for component in path.split("."):
        if isinstance(current, Mapping):
            if component not in current:
                raise WorkerInputError("workflow template contains an unknown binding")
            current = current[component]
            continue
        if isinstance(current, list) and component.isdecimal():
            index = int(component)
            if index >= len(current):
                raise WorkerInputError("workflow template contains an unknown binding")
            current = current[index]
            continue
        raise WorkerInputError("workflow template contains an unknown binding")
    if isinstance(current, (dict, list)):
        raise WorkerInputError("workflow bindings must resolve to scalar values")
    return current


def _render_workflow(value: object, bindings: Mapping[str, object]) -> object:
    if isinstance(value, list):
        return [_render_workflow(item, bindings) for item in value]
    if isinstance(value, dict):
        if "$gen" in value:
            if set(value) != {"$gen"} or not isinstance(value["$gen"], str):
                raise WorkerInputError("workflow template contains an invalid binding")
            return _resolve_binding(value["$gen"], bindings)
        return {key: _render_workflow(item, bindings) for key, item in value.items()}
    return value


def _rewrite_branch_links(
    value: object,
    *,
    branch_node_ids: frozenset[str],
    prefix: str,
) -> object:
    if isinstance(value, list):
        if (
            len(value) == 2
            and isinstance(value[0], str)
            and value[0] in branch_node_ids
            and isinstance(value[1], int)
            and not isinstance(value[1], bool)
        ):
            return [f"{prefix}{value[0]}", value[1]]
        return [
            _rewrite_branch_links(item, branch_node_ids=branch_node_ids, prefix=prefix)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _rewrite_branch_links(item, branch_node_ids=branch_node_ids, prefix=prefix)
            for key, item in value.items()
        }
    return value


def _render_multi_prompt_workflow(
    template: dict[str, object],
    output_bindings: tuple[Mapping[str, object], ...],
) -> dict[str, object]:
    if len(output_bindings) < 2:
        raise WorkerInputError("multi-prompt workflow requires multiple outputs")
    rendered_outputs: list[dict[str, object]] = []
    for bindings in output_bindings:
        rendered = _render_workflow(template, bindings)
        if not isinstance(rendered, dict) or set(rendered) != set(template):
            raise WorkerInputError("workflow template is invalid")
        rendered_outputs.append(rendered)

    shared_node_ids = frozenset(
        node_id
        for node_id, raw_node in template.items()
        if isinstance(raw_node, dict)
        and raw_node.get("class_type") in MULTI_PROMPT_SHARED_NODE_CLASSES
    )
    branch_node_ids = frozenset(set(template) - shared_node_ids)
    if not shared_node_ids or not branch_node_ids:
        raise WorkerInputError("workflow template cannot be expanded for multiple prompts")

    result: dict[str, object] = {}
    first = rendered_outputs[0]
    for node_id in sorted(shared_node_ids):
        node = first[node_id]
        if any(rendered[node_id] != node for rendered in rendered_outputs[1:]):
            raise WorkerInputError("shared workflow bindings differ between outputs")
        result[node_id] = node

    for output_index, rendered in enumerate(rendered_outputs):
        prefix = f"output-{output_index:02d}-"
        for node_id in sorted(branch_node_ids):
            rendered_node_id = f"{prefix}{node_id}"
            if len(rendered_node_id) > 128 or rendered_node_id in result:
                raise WorkerInputError("workflow template cannot be expanded for multiple prompts")
            node = _rewrite_branch_links(
                rendered[node_id],
                branch_node_ids=branch_node_ids,
                prefix=prefix,
            )
            if (
                isinstance(node, dict)
                and node.get("class_type") == "SaveImage"
                and isinstance(node.get("inputs"), dict)
            ):
                inputs = dict(node["inputs"])
                filename_prefix = inputs.get("filename_prefix")
                if isinstance(filename_prefix, str):
                    inputs["filename_prefix"] = f"{filename_prefix}-output-{output_index:02d}"
                node = {**node, "inputs": inputs}
            result[rendered_node_id] = node
    return result


def _require_comfy_link(value: object, *, output_index: int) -> tuple[str, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], str)
        or not value[0]
        or isinstance(value[1], bool)
        or value[1] != output_index
    ):
        raise WorkerInputError("workflow LoRA chain is invalid")
    return value[0], output_index


def _replace_chain_references(
    value: object,
    *,
    chain_node_id: str,
    model_source: tuple[str, int],
    clip_source: tuple[str, int] | None,
) -> object:
    if isinstance(value, list):
        if value and value[0] == chain_node_id:
            if len(value) != 2 or isinstance(value[1], bool):
                raise WorkerInputError("workflow LoRA chain is invalid")
            if value[1] == 0:
                return list(model_source)
            if value[1] == 1:
                if clip_source is None:
                    raise WorkerInputError("workflow LoRA chain is invalid")
                return list(clip_source)
            raise WorkerInputError("workflow LoRA chain is invalid")
        return [
            _replace_chain_references(
                item,
                chain_node_id=chain_node_id,
                model_source=model_source,
                clip_source=clip_source,
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _replace_chain_references(
                item,
                chain_node_id=chain_node_id,
                model_source=model_source,
                clip_source=clip_source,
            )
            for key, item in value.items()
        }
    return value


def _expand_bounded_lora_chain(
    workflow: dict[str, object],
    *,
    resolved: _ResolvedJobParameters,
    runtime: _RuntimeArtifactBindings,
) -> dict[str, object]:
    directives = [
        (node_id, node)
        for node_id, node in workflow.items()
        if isinstance(node, dict) and node.get("class_type") == LORA_CHAIN_NODE_CLASS
    ]
    if not directives:
        return workflow
    if len(directives) != 1:
        raise WorkerInputError("workflow LoRA chain is invalid")

    chain_node_id, directive = directives[0]
    inputs = directive.get("inputs")
    if not isinstance(inputs, dict):
        raise WorkerInputError("workflow LoRA chain is invalid")
    model_only = set(inputs) == {"model", "mode"} and inputs.get("mode") == "model_only"
    model_and_clip = set(inputs) == {"model", "clip"}
    if not model_only and not model_and_clip:
        raise WorkerInputError("workflow LoRA chain is invalid")
    model_source = _require_comfy_link(inputs["model"], output_index=0)
    clip_source = _require_comfy_link(inputs["clip"], output_index=1) if model_and_clip else None
    if clip_source is not None and model_source[0] != clip_source[0]:
        raise WorkerInputError("workflow LoRA chain is invalid")
    primary_node = workflow.get(model_source[0])
    expected_primary_class = "UNETLoader" if model_only else "CheckpointLoaderSimple"
    expected_primary_kind = ArtifactKind.DIFFUSION_MODEL if model_only else ArtifactKind.CHECKPOINT
    if (
        runtime.primary_kind != expected_primary_kind
        or not isinstance(primary_node, dict)
        or primary_node.get("class_type") != expected_primary_class
    ):
        raise WorkerInputError("workflow LoRA chain is invalid")

    without_directive = {
        node_id: node for node_id, node in workflow.items() if node_id != chain_node_id
    }
    generated_ids = tuple(
        f"{chain_node_id}-lora-{index}" for index in range(1, len(resolved.loras) + 1)
    )
    if any(node_id in without_directive for node_id in generated_ids):
        raise WorkerInputError("workflow LoRA chain is invalid")

    terminal_model = model_source
    terminal_clip = clip_source
    if generated_ids:
        terminal_model = (generated_ids[-1], 0)
        if not model_only:
            terminal_clip = (generated_ids[-1], 1)
    rendered = {
        node_id: _replace_chain_references(
            node,
            chain_node_id=chain_node_id,
            model_source=terminal_model,
            clip_source=terminal_clip,
        )
        for node_id, node in without_directive.items()
    }

    previous_model = model_source
    previous_clip = clip_source
    for node_id, lora, filename in zip(
        generated_ids,
        resolved.loras,
        runtime.lora_filenames,
        strict=True,
    ):
        if model_only:
            rendered[node_id] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {
                    "model": list(previous_model),
                    "lora_name": filename,
                    "strength_model": lora.weight,
                },
            }
        else:
            if previous_clip is None:
                raise WorkerInputError("workflow LoRA chain is invalid")
            rendered[node_id] = {
                "class_type": "LoraLoader",
                "inputs": {
                    "model": list(previous_model),
                    "clip": list(previous_clip),
                    "lora_name": filename,
                    "strength_model": lora.weight,
                    "strength_clip": lora.weight,
                },
            }
        previous_model = (node_id, 0)
        if not model_only:
            previous_clip = (node_id, 1)
    return rendered


def _validate_runtime_artifact_nodes(
    workflow: dict[str, object],
    *,
    resolved: _ResolvedJobParameters,
    runtime: _RuntimeArtifactBindings,
) -> None:
    checkpoints: list[str] = []
    diffusion_models: list[str] = []
    text_encoders: list[str] = []
    vaes: list[str] = []
    detectors: list[str] = []
    detailer_count = 0
    output_node_count = 0
    loras: dict[str, tuple[object, object]] = {}
    model_only_loras: dict[str, object] = {}
    for raw_node in workflow.values():
        if not isinstance(raw_node, dict):
            raise WorkerInputError("workflow template is invalid")
        node_class = raw_node.get("class_type")
        inputs = raw_node.get("inputs")
        if not isinstance(inputs, dict):
            raise WorkerInputError("workflow template is invalid")
        if node_class == "CheckpointLoaderSimple":
            checkpoint_name = inputs.get("ckpt_name")
            if not isinstance(checkpoint_name, str):
                raise WorkerInputError("workflow artifact binding is invalid")
            checkpoints.append(checkpoint_name)
        elif node_class == "UNETLoader":
            diffusion_name = inputs.get("unet_name")
            if not isinstance(diffusion_name, str):
                raise WorkerInputError("workflow artifact binding is invalid")
            diffusion_models.append(diffusion_name)
        elif node_class == "CLIPLoader":
            text_encoder_name = inputs.get("clip_name")
            if not isinstance(text_encoder_name, str):
                raise WorkerInputError("workflow artifact binding is invalid")
            text_encoders.append(text_encoder_name)
        elif node_class == "VAELoader":
            vae_name = inputs.get("vae_name")
            if not isinstance(vae_name, str):
                raise WorkerInputError("workflow artifact binding is invalid")
            vaes.append(vae_name)
        elif node_class == "LoraLoader":
            lora_name = inputs.get("lora_name")
            if (
                not isinstance(lora_name, str)
                or lora_name in loras
                or lora_name in model_only_loras
            ):
                raise WorkerInputError("workflow artifact binding is invalid")
            loras[lora_name] = (
                inputs.get("strength_model"),
                inputs.get("strength_clip"),
            )
        elif node_class == "LoraLoaderModelOnly":
            lora_name = inputs.get("lora_name")
            if (
                not isinstance(lora_name, str)
                or lora_name in loras
                or lora_name in model_only_loras
            ):
                raise WorkerInputError("workflow artifact binding is invalid")
            model_only_loras[lora_name] = inputs.get("strength_model")
        elif node_class == "UltralyticsDetectorProvider":
            detector_name = inputs.get("model_name")
            if not isinstance(detector_name, str):
                raise WorkerInputError("workflow artifact binding is invalid")
            detectors.append(detector_name)
        elif node_class == "FaceDetailer":
            detailer_count += 1
        elif node_class in {"SaveImage", "SaveImageWebsocket"}:
            output_node_count += 1

    expected_loras = dict(
        zip(runtime.lora_filenames, (lora.weight for lora in resolved.loras), strict=True)
    )
    if runtime.primary_kind == ArtifactKind.CHECKPOINT:
        if (
            checkpoints != [runtime.checkpoint_filename]
            or diffusion_models
            or text_encoders
            or vaes
            or model_only_loras
            or set(loras) != set(expected_loras)
        ):
            raise WorkerInputError("workflow artifact binding is invalid")
        for filename, weight in expected_loras.items():
            if loras[filename] != (weight, weight):
                raise WorkerInputError("workflow artifact binding is invalid")
    else:
        if (
            checkpoints
            or diffusion_models != [runtime.checkpoint_filename]
            or text_encoders != [runtime.text_encoder_filename]
            or vaes != [runtime.vae_filename]
            or loras
            or set(model_only_loras) != set(expected_loras)
        ):
            raise WorkerInputError("workflow artifact binding is invalid")
        for filename, weight in expected_loras.items():
            if model_only_loras[filename] != weight:
                raise WorkerInputError("workflow artifact binding is invalid")
    allowed_detailer_counts = {0, 1}
    if resolved.output_generations:
        allowed_detailer_counts.add(len(resolved.output_generations))
    if detailer_count not in allowed_detailer_counts or bool(detailer_count) != bool(detectors):
        raise WorkerInputError("workflow detector binding is invalid")
    if len(resolved.output_generations) > 1 and output_node_count != len(
        resolved.output_generations
    ):
        raise WorkerInputError("workflow output count is invalid")
    expected_detector = (
        [] if runtime.detector_filename is None else [f"bbox/{runtime.detector_filename}"]
    )
    if detectors and detectors != expected_detector:
        raise WorkerInputError("workflow detector binding is invalid")


async def _load_workflow(
    store: ObjectStore,
    *,
    specification: WorkflowSpecification,
    generation: GenerationParameters,
    bindings: Mapping[str, object],
    output_bindings: tuple[Mapping[str, object], ...] = (),
    max_bytes: int,
) -> dict[str, object]:
    try:
        metadata = await store.head(specification.object_key)
        if metadata is None or metadata.version_id is None or metadata.etag is None:
            raise WorkerInputError("workflow template is unavailable")
        raw = await store.read_bytes(
            specification.object_key,
            max_bytes=max_bytes,
            version_id=metadata.version_id,
            etag=metadata.etag,
        )
    except ObjectStoreError:
        raise WorkerInputError("workflow template is unavailable") from None
    if hashlib.sha256(raw).hexdigest() != specification.sha256:
        raise WorkerInputError("workflow template integrity check failed")
    template = _parse_workflow_template(raw)
    template = _prepare_controlled_duo_template(
        template,
        specification=specification,
        generation=generation,
    )
    try:
        template = prepare_controlled_trio_template(
            template,
            specification=specification,
            generation=generation,
        )
    except ControlledTrioContractError as error:
        raise WorkerInputError(str(error)) from error
    rendered = (
        _render_multi_prompt_workflow(template, output_bindings)
        if len(output_bindings) > 1
        else _render_workflow(template, bindings)
    )
    if not isinstance(rendered, dict):
        raise WorkerInputError("workflow template is invalid")
    return rendered


async def _store_referenced_generate_payload(
    store: ObjectStore,
    *,
    attempt_id: str,
    body: bytes,
    expires_in: int,
) -> tuple[str, str]:
    payload_sha256 = hashlib.sha256(body).hexdigest()
    key = f"{REFERENCED_PAYLOAD_PREFIX}/{attempt_id}/{payload_sha256}.json"
    metadata = {
        "kind": "salad-worker-request-v2",
        "sha256": payload_sha256,
    }
    try:
        stored = await store.write_bytes_if_absent(
            key=key,
            body=body,
            content_type=REFERENCED_PAYLOAD_CONTENT_TYPE,
            metadata=metadata,
            max_bytes=MAX_HARD_REFERENCED_PAYLOAD_BYTES,
        )
    except ObjectAlreadyExistsError:
        try:
            existing_metadata = await store.head(key)
            if existing_metadata is None or existing_metadata.version_id is None:
                raise WorkerInputError("worker request payload storage is unavailable")
            existing = await store.read_bytes(
                key,
                max_bytes=MAX_HARD_REFERENCED_PAYLOAD_BYTES,
                version_id=existing_metadata.version_id,
                etag=existing_metadata.etag,
            )
        except ObjectStoreError:
            raise WorkerInputError("worker request payload storage is unavailable") from None
        if not hmac.compare_digest(existing, body):
            raise WorkerInputError("worker request payload integrity check failed") from None
        stored = existing_metadata
    except ObjectConflictError:
        raise WorkerInputError("worker request payload storage is unavailable") from None
    except ObjectStoreError:
        raise WorkerInputError("worker request payload storage is unavailable") from None

    if (
        stored.byte_size != len(body)
        or stored.key != key
        or stored.content_type != REFERENCED_PAYLOAD_CONTENT_TYPE
        or stored.version_id is None
        or stored.metadata.get("kind") != metadata["kind"]
        or not hmac.compare_digest(stored.metadata.get("sha256", ""), payload_sha256)
    ):
        raise WorkerInputError("worker request payload integrity check failed")
    try:
        url = await store.presign_download(
            key=key,
            expires_in=expires_in,
            version_id=stored.version_id,
        )
    except ObjectStoreError:
        raise WorkerInputError("worker request payload storage is unavailable") from None
    return url, payload_sha256


@dataclass
class SaladWorkerJobInputProvider:
    session: AsyncSession
    store: ObjectStore
    signing_key_id: str
    signing_private_key: SecretStr
    artifact_manifest: ArtifactManifest
    artifact_manifest_sha256: str
    signature_ttl_seconds: int = 7200
    upload_grant_ttl_seconds: int = 10800
    upload_content_type: UploadContentType = "image/png"
    max_upload_bytes: int = 100 * 1024 * 1024
    max_workflow_bytes: int = MAX_WORKFLOW_BYTES
    max_rendered_workflow_bytes: int = MAX_RENDERED_WORKFLOW_BYTES
    max_envelope_bytes: int = MAX_ENVELOPE_BYTES
    now: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if KEY_ID_PATTERN.fullmatch(self.signing_key_id) is None:
            raise ValueError("invalid worker signing key identifier")
        try:
            validate_private_key(self.signing_private_key.get_secret_value())
        except SigningMaterialError:
            raise ValueError("worker signing private key is invalid") from None
        if not hmac.compare_digest(
            self.artifact_manifest.manifest_sha256,
            self.artifact_manifest_sha256,
        ):
            raise ValueError("worker artifact manifest trust anchor does not match")
        if not 5 <= self.signature_ttl_seconds <= 7200:
            raise ValueError("worker signature TTL must be between 5 and 7200 seconds")
        if (
            self.upload_grant_ttl_seconds
            < self.signature_ttl_seconds + MIN_POST_ACCEPTANCE_UPLOAD_SECONDS
            or self.upload_grant_ttl_seconds > 14400
        ):
            raise ValueError("worker upload grant TTL does not cover execution")
        if not 1024 <= self.max_workflow_bytes <= MAX_WORKFLOW_BYTES:
            raise ValueError("workflow byte limit is invalid")
        if (
            not self.max_workflow_bytes
            <= self.max_rendered_workflow_bytes
            <= (MAX_RENDERED_WORKFLOW_BYTES)
        ):
            raise ValueError("rendered workflow byte limit is invalid")
        if not 4096 <= self.max_envelope_bytes <= MAX_ENVELOPE_BYTES:
            raise ValueError("worker envelope byte limit is invalid")

    async def build_job_input(self, context: SaladJobInputContext) -> dict[str, object]:
        resolved = _resolve_job_parameters(context)
        runtime = _resolve_runtime_artifacts(resolved, self.artifact_manifest)
        workflow = await _load_workflow(
            self.store,
            specification=resolved.workflow,
            generation=resolved.generation,
            bindings=resolved.bindings(runtime),
            output_bindings=resolved.output_bindings(runtime),
            max_bytes=self.max_workflow_bytes,
        )
        workflow = _expand_bounded_lora_chain(
            workflow,
            resolved=resolved,
            runtime=runtime,
        )
        _validate_runtime_artifact_nodes(
            workflow,
            resolved=resolved,
            runtime=runtime,
        )
        try:
            require_comfy_workflow_deliverability(workflow)
        except DeliverabilityError:
            raise WorkerInputError("rendered workflow geometry is not deliverable") from None

        rendered_workflow_bytes = len(
            json.dumps(workflow, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        if rendered_workflow_bytes > self.max_rendered_workflow_bytes:
            raise WorkerInputError("rendered workflow exceeds the configured size limit")
        reserved_envelope_bytes = (
            rendered_workflow_bytes
            + context.expected_output_count * MAX_SERIALIZED_UPLOAD_GRANT_BYTES
            + MAX_SIGNED_ENVELOPE_FIXED_OVERHEAD_BYTES
        )
        inline_payload = (
            resolved.worker_request_budget_version == 1
            or context.expected_output_count <= MAX_INLINE_OUTPUTS_PER_SIGNED_GENERATION_JOB
        )
        if (
            context.expected_output_count <= MAX_INLINE_OUTPUTS_PER_SIGNED_GENERATION_JOB
            and reserved_envelope_bytes > self.max_envelope_bytes
        ):
            raise WorkerInputError("rendered workflow cannot fit the signed worker request budget")

        intents = await create_raw_master_upload_intents(
            self.session,
            self.store,
            generation_job_id=context.generation_job_id,
            content_type=self.upload_content_type,
            expires_in=self.upload_grant_ttl_seconds,
            max_bytes=self.max_upload_bytes,
            rotate_incomplete_uploads=True,
            max_serialized_grant_bytes=MAX_SERIALIZED_UPLOAD_GRANT_BYTES,
            commit=False,
            actor="salad-worker-input",
        )
        if len(intents) != context.expected_output_count:
            await self.session.rollback()
            raise WorkerInputError("worker upload grant count is inconsistent")

        grants: list[UploadGrant] = []
        for intent in intents:
            if (
                intent.upload_method != "POST"
                or intent.upload_url is None
                or not intent.upload_fields
                or intent.upload_headers
            ):
                await self.session.rollback()
                raise WorkerInputError("object store returned an unsupported upload grant")
            grant = UploadGrant(
                asset_id=str(intent.asset_id),
                upload_attempt_id=str(intent.upload_attempt_id),
                output_index=intent.output_index,
                content_type=self.upload_content_type,
                url=intent.upload_url,
                fields=dict(intent.upload_fields),
            )
            if (
                len(
                    json.dumps(
                        grant.model_dump(mode="json"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                )
                > MAX_SERIALIZED_UPLOAD_GRANT_BYTES
            ):
                await self.session.rollback()
                raise WorkerInputError("worker upload grant exceeds the request budget")
            grants.append(grant)

        payload = GeneratePayload(
            job_id=str(context.generation_job_id),
            attempt_id=str(context.generation_attempt_id),
            workflow=workflow,
            uploads=grants,
        )
        issued_at = int(self.now())
        if inline_payload:
            envelope: GenerateEnvelope | ReferencedGenerateEnvelope = GenerateEnvelope(
                version="v1",
                key_id=self.signing_key_id,
                issued_at=issued_at,
                expires_at=issued_at + self.signature_ttl_seconds,
                payload=payload,
                signature="A" * 86,
            )
        else:
            payload_bytes = json.dumps(
                payload.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if len(payload_bytes) > MAX_HARD_REFERENCED_PAYLOAD_BYTES:
                await self.session.rollback()
                raise WorkerInputError("worker request payload exceeds the configured size limit")
            try:
                payload_url, payload_sha256 = await _store_referenced_generate_payload(
                    self.store,
                    attempt_id=str(context.generation_attempt_id),
                    body=payload_bytes,
                    expires_in=self.upload_grant_ttl_seconds,
                )
            except WorkerInputError:
                await self.session.rollback()
                raise
            envelope = ReferencedGenerateEnvelope(
                version="v2",
                key_id=self.signing_key_id,
                issued_at=issued_at,
                expires_at=issued_at + self.signature_ttl_seconds,
                payload=GeneratePayloadReference(
                    job_id=payload.job_id,
                    attempt_id=payload.attempt_id,
                    url=payload_url,
                    sha256=payload_sha256,
                    byte_size=len(payload_bytes),
                ),
                signature="A" * 86,
            )
        signature = calculate_signature(
            envelope,
            self.signing_private_key.get_secret_value(),
        )
        signed = envelope.model_copy(update={"signature": signature})
        result = signed.model_dump(mode="json")
        serialized = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
        if len(serialized) > self.max_envelope_bytes:
            await self.session.rollback()
            raise WorkerInputError("worker request exceeds the configured size limit")
        await self.session.commit()
        return result
