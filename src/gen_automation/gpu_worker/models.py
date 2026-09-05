import re
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import SplitResult, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gen_automation.domain.generation_limits import MAX_OUTPUTS_PER_GENERATION_JOB
from gen_automation.domain.signing import SigningMaterialError, validate_public_key

MAX_HARD_BODY_BYTES = 1024 * 1024
MAX_HARD_REFERENCED_PAYLOAD_BYTES = 1024 * 1024
MAX_HARD_OUTPUT_BYTES = 100 * 1024 * 1024
MAX_HARD_TOTAL_OUTPUT_BYTES = 512 * 1024 * 1024
MAX_HARD_OUTPUTS = 32
MAX_HARD_BASE64_CHARACTERS = ((MAX_HARD_OUTPUT_BYTES + 2) // 3) * 4
MAX_UPLOAD_FIELDS = 64
MAX_UPLOAD_FIELD_NAME_LENGTH = 256
MAX_UPLOAD_FIELD_VALUE_LENGTH = 16 * 1024
KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
NODE_CLASS_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
OUTPUT_NODE_CLASSES = frozenset({"SaveImage", "SaveImageWebsocket"})
DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES = frozenset(
    {
        "CheckpointLoaderSimple",
        "CLIPSetLastLayer",
        "CLIPLoader",
        "CLIPTextEncode",
        "ConditioningCombine",
        "ConditioningSetAreaPercentage",
        "ConditioningSetMask",
        "EmptyLatentImage",
        "FaceDetailer",
        "FeatherMask",
        "KSampler",
        "LatentUpscaleBy",
        "LoraLoader",
        "LoraLoaderModelOnly",
        "MaskComposite",
        "SaveImage",
        "SaveImageWebsocket",
        "SetLatentNoiseMask",
        "SolidMask",
        "VAEDecode",
        "VAELoader",
        "UNETLoader",
        "UltralyticsDetectorProvider",
    }
)

# The request reader performs a depth-bounded recursive JSON validation before
# constructing these models. Keeping the workflow's schema opaque here avoids
# Pydantic recursively expanding an effectively unbounded Comfy graph type.
type JsonValue = object
type JsonObject = dict[str, object]

BoundedId = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
]
Base64Text = Annotated[str, StringConstraints(min_length=4, max_length=MAX_HARD_BASE64_CHARACTERS)]
Sha256Text = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RuntimeAdmissionId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
RuntimeWorkerInstanceId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]


class WorkerEnvironment(StrEnum):
    TEST = "test"
    PRODUCTION = "production"


def _parse_https_origin(value: str, *, origin_only: bool) -> tuple[str, int]:
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("invalid HTTPS origin")
    if "\\" in value:
        raise ValueError("invalid HTTPS origin")

    try:
        parsed: SplitResult = urlsplit(value)
        port = parsed.port or 443
    except ValueError:
        raise ValueError("invalid HTTPS origin") from None

    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("invalid HTTPS origin")
    if origin_only and (parsed.path not in {"", "/"} or parsed.query):
        raise ValueError("configured upload origin must not include a path or query")
    if not 1 <= port <= 65535:
        raise ValueError("invalid HTTPS origin")
    return parsed.hostname.lower(), port


class WorkerSettings(BaseModel):
    """Security and resource limits for one worker process.

    There are intentionally no key or upload-origin defaults. Production also
    refuses weak shared keys, so a partially configured container cannot start.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: WorkerEnvironment = WorkerEnvironment.PRODUCTION
    verification_keys: dict[str, str]
    allowed_upload_origin: str
    artifact_manifest_sha256: Sha256Text
    runtime_admission_id: RuntimeAdmissionId
    runtime_worker_instance_id: RuntimeWorkerInstanceId
    max_body_bytes: int = Field(default=256 * 1024, ge=1024, le=MAX_HARD_BODY_BYTES)
    max_signature_ttl_seconds: int = Field(default=7200, ge=5, le=7200)
    clock_skew_seconds: int = Field(default=15, ge=0, le=60)
    max_outputs: int = Field(
        default=MAX_OUTPUTS_PER_GENERATION_JOB,
        ge=1,
        le=MAX_HARD_OUTPUTS,
    )
    max_output_bytes: int = Field(
        default=25 * 1024 * 1024,
        ge=1024,
        le=MAX_HARD_OUTPUT_BYTES,
    )
    max_total_output_bytes: int = Field(
        default=256 * 1024 * 1024,
        ge=1024,
        le=MAX_HARD_TOTAL_OUTPUT_BYTES,
    )
    max_image_dimension: int = Field(default=16_384, ge=64, le=65_536)
    max_image_pixels: int = Field(default=64_000_000, ge=4_096, le=256_000_000)
    max_replay_entries: int = Field(default=256, ge=1, le=1024)
    upload_timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    readiness_timeout_seconds: float = Field(default=1.0, ge=0.05, le=5.0)
    approved_workflow_node_classes: frozenset[str] = Field(
        default=DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES,
        min_length=1,
        max_length=128,
    )

    @model_validator(mode="after")
    def validate_security_boundary(self) -> "WorkerSettings":
        if not self.verification_keys:
            raise ValueError("at least one verification key is required")
        if len(self.verification_keys) > 8:
            raise ValueError("too many active verification keys")
        for key_id, public_key in self.verification_keys.items():
            if KEY_ID_PATTERN.fullmatch(key_id) is None:
                raise ValueError("invalid verification key identifier")
            try:
                validate_public_key(public_key)
            except SigningMaterialError:
                raise ValueError("invalid Ed25519 verification key") from None

        _parse_https_origin(self.allowed_upload_origin, origin_only=True)
        if self.max_total_output_bytes < self.max_output_bytes:
            raise ValueError("the total output limit must cover at least one output")
        if any(
            NODE_CLASS_PATTERN.fullmatch(node_class) is None
            for node_class in self.approved_workflow_node_classes
        ):
            raise ValueError("approved workflow node classes are invalid")
        if not self.approved_workflow_node_classes.intersection(OUTPUT_NODE_CLASSES):
            raise ValueError("an approved output node class is required")
        return self

    @property
    def upload_origin(self) -> tuple[str, int]:
        return _parse_https_origin(self.allowed_upload_origin, origin_only=True)


class UploadGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    asset_id: BoundedId
    upload_attempt_id: BoundedId
    output_index: int = Field(ge=0, le=MAX_HARD_OUTPUTS - 1)
    content_type: Literal["image/png", "image/jpeg", "image/webp"]
    url: Annotated[str, StringConstraints(min_length=9, max_length=4096)]
    fields: dict[str, str]

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, value: dict[str, str]) -> dict[str, str]:
        if not 1 <= len(value) <= MAX_UPLOAD_FIELDS:
            raise ValueError("invalid upload form fields")
        for name, field_value in value.items():
            if (
                not name
                or len(name) > MAX_UPLOAD_FIELD_NAME_LENGTH
                or len(field_value) > MAX_UPLOAD_FIELD_VALUE_LENGTH
                or any(ord(character) < 32 for character in name)
                or name.lower() == "file"
            ):
                raise ValueError("invalid upload form field")
        return value


class GeneratePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    job_id: BoundedId
    attempt_id: BoundedId
    artifact_manifest_sha256: Sha256Text
    runtime_admission_id: RuntimeAdmissionId
    runtime_worker_instance_id: RuntimeWorkerInstanceId
    workflow: JsonObject
    uploads: list[UploadGrant] = Field(min_length=1, max_length=MAX_HARD_OUTPUTS)

    @field_validator("uploads")
    @classmethod
    def validate_unique_outputs(cls, value: list[UploadGrant]) -> list[UploadGrant]:
        indices = [grant.output_index for grant in value]
        if len(indices) != len(set(indices)):
            raise ValueError("upload output indices must be unique")
        if set(indices) != set(range(len(value))):
            raise ValueError("upload output indices must be contiguous")
        if len({grant.asset_id for grant in value}) != len(value):
            raise ValueError("asset identifiers must be unique")
        if len({grant.upload_attempt_id for grant in value}) != len(value):
            raise ValueError("upload attempt identifiers must be unique")
        return value


class GenerateEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal["v1"]
    key_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)
    payload: GeneratePayload
    signature: Annotated[
        str,
        StringConstraints(
            min_length=86,
            max_length=86,
            pattern=r"^[A-Za-z0-9_-]{86}$",
        ),
    ]


class GeneratePayloadReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    job_id: BoundedId
    attempt_id: BoundedId
    url: Annotated[str, StringConstraints(min_length=9, max_length=8192)] = Field(repr=False)
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    byte_size: int = Field(ge=1, le=MAX_HARD_REFERENCED_PAYLOAD_BYTES)


class ReferencedGenerateEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal["v2"]
    key_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)
    payload: GeneratePayloadReference
    signature: Annotated[
        str,
        StringConstraints(
            min_length=86,
            max_length=86,
            pattern=r"^[A-Za-z0-9_-]{86}$",
        ),
    ]


type SignedGenerateEnvelope = GenerateEnvelope | ReferencedGenerateEnvelope


class ComfyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    output_index: int = Field(ge=0, le=MAX_HARD_OUTPUTS - 1)
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    data_base64: Base64Text


class UploadedOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    upload_attempt_id: str
    output_index: int
    status: Literal["uploaded"] = "uploaded"


class GenerateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["v1"] = "v1"
    job_id: str
    attempt_id: str
    status: Literal["succeeded"] = "succeeded"
    outputs: list[UploadedOutput]


class GenerateFailureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["v1"] = "v1"
    job_id: str
    attempt_id: str
    status: Literal["failed"] = "failed"
    code: Literal["near_black_output"] = "near_black_output"
    failed_output_index: int = Field(ge=0, le=MAX_HARD_OUTPUTS - 1)
    outputs: list[UploadedOutput] = Field(max_length=MAX_HARD_OUTPUTS)

    @model_validator(mode="after")
    def validate_prior_outputs(self) -> "GenerateFailureResponse":
        indices = [output.output_index for output in self.outputs]
        # Progressive graphs upload every earlier branch before advancing;
        # legacy batched graphs decode the whole batch before any upload.
        # Those are the only two valid failure shapes.
        if indices not in ([], list(range(self.failed_output_index))):
            raise ValueError("failure response outputs are invalid")
        return self


type GenerateWorkerResponse = GenerateResponse | GenerateFailureResponse


def validate_upload_url(url: str, allowed_origin: tuple[str, int]) -> None:
    if _parse_https_origin(url, origin_only=False) != allowed_origin:
        raise ValueError("upload URL origin is not allowed")


def validate_approved_workflow(
    workflow: JsonObject,
    approved_node_classes: frozenset[str],
) -> None:
    """Default-deny every Comfy node class before the graph reaches ComfyUI."""

    if not isinstance(workflow, dict) or not workflow:
        raise ValueError("invalid workflow")
    has_output = False
    for raw_node in workflow.values():
        if not isinstance(raw_node, dict):
            raise ValueError("invalid workflow")
        node_class = raw_node.get("class_type")
        if not isinstance(node_class, str) or node_class not in approved_node_classes:
            raise ValueError("invalid workflow")
        if node_class in OUTPUT_NODE_CLASSES:
            has_output = True
    if not has_output:
        raise ValueError("invalid workflow")
