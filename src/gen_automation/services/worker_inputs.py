import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.release_spec import (
    ArtifactSpecification,
    GenerationParameters,
    LoraSpecification,
    WorkflowSpecification,
)
from gen_automation.domain.signing import SigningMaterialError, validate_private_key
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
type UploadContentType = Literal["image/png", "image/jpeg", "image/webp"]


class WorkerInputError(Exception):
    """A redacted failure safe for the provider orchestration layer."""


@dataclass(frozen=True)
class _ResolvedJobParameters:
    checkpoint: ArtifactSpecification
    loras: tuple[LoraSpecification, ...]
    workflow: WorkflowSpecification
    generation: GenerationParameters

    def bindings(self) -> dict[str, object]:
        return {
            "checkpoint": self.checkpoint.model_dump(mode="json"),
            "loras": [lora.model_dump(mode="json") for lora in self.loras],
            "workflow": self.workflow.model_dump(mode="json"),
            "generation": self.generation.model_dump(mode="json"),
        }


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
        workflow = await _load_workflow(
            self.store,
            specification=resolved.workflow,
            bindings=resolved.bindings(),
            max_bytes=self.max_workflow_bytes,
        )

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
