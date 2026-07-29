import hashlib
import hmac
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.deliverability import (
    DeliverabilityError,
    require_comfy_workflow_deliverability,
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
    GenerateEnvelope,
    GeneratePayload,
    UploadGrant,
)
from gen_automation.gpu_worker.security import calculate_signature
from gen_automation.services.assets import create_raw_master_upload_intents
from gen_automation.services.salad import SaladJobInputContext
from gen_automation.storage.base import ObjectStore, ObjectStoreError

MAX_WORKFLOW_BYTES = 192 * 1024
MAX_ENVELOPE_BYTES = 256 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_ITEMS = 50_000
MIN_POST_ACCEPTANCE_UPLOAD_SECONDS = 3600
MAX_RUNTIME_LORAS = 4
LORA_CHAIN_NODE_CLASS = "GenAutomationLoraChain"
type UploadContentType = Literal["image/png", "image/jpeg", "image/webp"]


class WorkerInputError(Exception):
    """A redacted failure safe for the provider orchestration layer."""


@dataclass(frozen=True)
class _ResolvedJobParameters:
    checkpoint: ArtifactSpecification
    loras: tuple[LoraSpecification, ...]
    workflow: WorkflowSpecification
    generation: GenerationParameters

    def bindings(self, runtime: "_RuntimeArtifactBindings") -> dict[str, object]:
        checkpoint = self.checkpoint.model_dump(mode="json")
        checkpoint["runtime_filename"] = runtime.checkpoint_filename
        loras: list[dict[str, object]] = []
        for lora, filename in zip(self.loras, runtime.lora_filenames, strict=True):
            value = lora.model_dump(mode="json")
            value["runtime_filename"] = filename
            loras.append(value)
        bindings: dict[str, object] = {
            "checkpoint": checkpoint,
            "loras": loras,
            "workflow": self.workflow.model_dump(mode="json"),
            "generation": self.generation.model_dump(mode="json"),
        }
        if runtime.detector_filename is not None:
            bindings["detector"] = {
                "runtime_filename": runtime.detector_filename,
                "comfy_name": f"bbox/{runtime.detector_filename}",
            }
        return bindings


@dataclass(frozen=True)
class _RuntimeArtifactBindings:
    checkpoint_filename: str
    lora_filenames: tuple[str, ...]
    detector_filename: str | None


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
        raise WorkerInputError("generation supports at most four LoRAs")
    if len({lora.sha256 for lora in resolved.loras}) != len(resolved.loras):
        raise WorkerInputError("generation artifacts do not match the worker manifest")

    checkpoint = _resolve_manifest_artifact(
        manifest,
        resolved.checkpoint,
        kind=ArtifactKind.CHECKPOINT,
    )
    loras = tuple(
        _resolve_manifest_artifact(manifest, lora, kind=ArtifactKind.LORA)
        for lora in resolved.loras
    )
    detectors = tuple(
        artifact for artifact in manifest.artifacts if artifact.kind == ArtifactKind.DETECTOR
    )
    if len(detectors) > 1:
        raise WorkerInputError("generation supports at most one face detector")
    return _RuntimeArtifactBindings(
        checkpoint_filename=checkpoint.target_filename,
        lora_filenames=tuple(lora.target_filename for lora in loras),
        detector_filename=detectors[0].target_filename if detectors else None,
    )


def _require_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise WorkerInputError("generation parameters are invalid")
    return value


def _resolve_job_parameters(context: SaladJobInputContext) -> _ResolvedJobParameters:
    parameters = _require_mapping(context.parameters)
    if canonical_sha256(dict(parameters)) != context.parameters_sha256:
        raise WorkerInputError("generation parameter integrity check failed")
    if parameters.get("schema_version") != 1:
        raise WorkerInputError("generation parameter schema is unsupported")
    if parameters.get("release_version_id") != str(context.release_version_id):
        raise WorkerInputError("generation parameter identity check failed")

    try:
        checkpoint = ArtifactSpecification.model_validate(parameters.get("checkpoint"))
        loras_raw = parameters.get("loras")
        if not isinstance(loras_raw, list):
            raise WorkerInputError("generation parameters are invalid")
        loras = tuple(LoraSpecification.model_validate(item) for item in loras_raw)
        workflow = WorkflowSpecification.model_validate(parameters.get("workflow"))
        generation = GenerationParameters.model_validate(parameters.get("generation"))
    except ValidationError:
        raise WorkerInputError("generation parameters are invalid") from None
    if generation.outputs_per_job != context.expected_output_count:
        raise WorkerInputError("generation output count is inconsistent")
    return _ResolvedJobParameters(
        checkpoint=checkpoint,
        loras=loras,
        workflow=workflow,
        generation=generation,
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
    clip_source: tuple[str, int],
) -> object:
    if isinstance(value, list):
        if value and value[0] == chain_node_id:
            if len(value) != 2 or isinstance(value[1], bool):
                raise WorkerInputError("workflow LoRA chain is invalid")
            if value[1] == 0:
                return list(model_source)
            if value[1] == 1:
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
    if not isinstance(inputs, dict) or set(inputs) != {"model", "clip"}:
        raise WorkerInputError("workflow LoRA chain is invalid")
    model_source = _require_comfy_link(inputs["model"], output_index=0)
    clip_source = _require_comfy_link(inputs["clip"], output_index=1)
    if model_source[0] != clip_source[0]:
        raise WorkerInputError("workflow LoRA chain is invalid")
    checkpoint_node = workflow.get(model_source[0])
    if (
        not isinstance(checkpoint_node, dict)
        or checkpoint_node.get("class_type") != "CheckpointLoaderSimple"
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
        previous_clip = (node_id, 1)
    return rendered


def _validate_runtime_artifact_nodes(
    workflow: dict[str, object],
    *,
    resolved: _ResolvedJobParameters,
    runtime: _RuntimeArtifactBindings,
) -> None:
    checkpoints: list[str] = []
    detectors: list[str] = []
    detailer_count = 0
    loras: dict[str, tuple[object, object]] = {}
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
        elif node_class == "LoraLoader":
            lora_name = inputs.get("lora_name")
            if not isinstance(lora_name, str) or lora_name in loras:
                raise WorkerInputError("workflow artifact binding is invalid")
            loras[lora_name] = (
                inputs.get("strength_model"),
                inputs.get("strength_clip"),
            )
        elif node_class == "UltralyticsDetectorProvider":
            detector_name = inputs.get("model_name")
            if not isinstance(detector_name, str):
                raise WorkerInputError("workflow artifact binding is invalid")
            detectors.append(detector_name)
        elif node_class == "FaceDetailer":
            detailer_count += 1

    if checkpoints != [runtime.checkpoint_filename]:
        raise WorkerInputError("workflow artifact binding is invalid")
    expected_loras = dict(
        zip(runtime.lora_filenames, (lora.weight for lora in resolved.loras), strict=True)
    )
    if set(loras) != set(expected_loras):
        raise WorkerInputError("workflow artifact binding is invalid")
    for filename, weight in expected_loras.items():
        if loras[filename] != (weight, weight):
            raise WorkerInputError("workflow artifact binding is invalid")
    if detailer_count not in {0, 1} or bool(detailer_count) != bool(detectors):
        raise WorkerInputError("workflow detector binding is invalid")
    expected_detector = (
        [] if runtime.detector_filename is None else [f"bbox/{runtime.detector_filename}"]
    )
    if detectors and detectors != expected_detector:
        raise WorkerInputError("workflow detector binding is invalid")


async def _load_workflow(
    store: ObjectStore,
    *,
    specification: WorkflowSpecification,
    bindings: Mapping[str, object],
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
    rendered = _render_workflow(_parse_workflow_template(raw), bindings)
    if not isinstance(rendered, dict):
        raise WorkerInputError("workflow template is invalid")
    return rendered


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
        if not 4096 <= self.max_envelope_bytes <= MAX_ENVELOPE_BYTES:
            raise ValueError("worker envelope byte limit is invalid")

    async def build_job_input(self, context: SaladJobInputContext) -> dict[str, object]:
        resolved = _resolve_job_parameters(context)
        runtime = _resolve_runtime_artifacts(resolved, self.artifact_manifest)
        workflow = await _load_workflow(
            self.store,
            specification=resolved.workflow,
            bindings=resolved.bindings(runtime),
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

        intents = await create_raw_master_upload_intents(
            self.session,
            self.store,
            generation_job_id=context.generation_job_id,
            content_type=self.upload_content_type,
            expires_in=self.upload_grant_ttl_seconds,
            max_bytes=self.max_upload_bytes,
            rotate_incomplete_uploads=True,
            actor="salad-worker-input",
        )
        if len(intents) != context.expected_output_count:
            raise WorkerInputError("worker upload grant count is inconsistent")

        grants: list[UploadGrant] = []
        for intent in intents:
            if (
                intent.upload_method != "POST"
                or intent.upload_url is None
                or not intent.upload_fields
                or intent.upload_headers
            ):
                raise WorkerInputError("object store returned an unsupported upload grant")
            grants.append(
                UploadGrant(
                    asset_id=str(intent.asset_id),
                    upload_attempt_id=str(intent.upload_attempt_id),
                    output_index=intent.output_index,
                    content_type=self.upload_content_type,
                    url=intent.upload_url,
                    fields=dict(intent.upload_fields),
                )
            )

        issued_at = int(self.now())
        envelope = GenerateEnvelope(
            version="v1",
            key_id=self.signing_key_id,
            issued_at=issued_at,
            expires_at=issued_at + self.signature_ttl_seconds,
            payload=GeneratePayload(
                job_id=str(context.generation_job_id),
                attempt_id=str(context.generation_attempt_id),
                workflow=workflow,
                uploads=grants,
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
            raise WorkerInputError("worker request exceeds the configured size limit")
        return result
