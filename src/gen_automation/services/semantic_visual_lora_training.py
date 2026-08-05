"""Fail-closed planning for a personalized anatomy visual-LoRA challenger.

This module is intentionally incapable of submitting a RunPod job.  It binds a
readiness-qualified owner/profile dataset, a leakage-safe split, immutable input
artifacts, and bounded RunPod execution limits into one deterministic request.
A later provider adapter must durably claim the idempotency key before making a
paid mutation and must preserve the shadow-only output policy.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import SemanticLearningPolicy
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.semantic.contract import assessment_profile_sha256
from gen_automation.services.semantic_learning_readiness import (
    LORA_MINIMUM_BATCHES,
    LORA_MINIMUM_COMPLETED_REVIEW_SETS,
    LORA_MINIMUM_DEFECT,
    LORA_MINIMUM_EXPLICIT,
    LORA_MINIMUM_GOOD,
    LORA_MINIMUM_ISSUE_CODED_DEFECTS,
    LORA_MINIMUM_READY_ISSUE_FAMILIES,
    LORA_MINIMUM_SAMPLES,
    LORA_MINIMUM_SAMPLES_PER_ISSUE_FAMILY,
    META_EVALUATION_MAXIMUM_ZERO_ERROR_FALSE_REJECT_UPPER_MICROS,
    META_EVALUATION_MINIMUM_HOLDOUT_DEFECT,
    META_EVALUATION_MINIMUM_TRAINING_DEFECT,
    META_EVALUATION_MINIMUM_TRAINING_GOOD,
    SEMANTIC_LEARNING_READINESS_SCHEMA_VERSION,
    SemanticLearningReadinessReport,
    SemanticProfileLearningReadiness,
    build_semantic_learning_readiness_report,
)

VISUAL_LORA_DATASET_SCHEMA_VERSION = "semantic-anatomy-visual-lora-dataset/v1"
VISUAL_LORA_SPLIT_SCHEMA_VERSION = "semantic-anatomy-visual-lora-split/v1"
VISUAL_LORA_RECIPE_SCHEMA_VERSION = "semantic-anatomy-visual-lora-recipe/v1"
VISUAL_LORA_REQUEST_SCHEMA_VERSION = "semantic-anatomy-visual-lora-runpod-request/v1"
VISUAL_LORA_PLAN_SCHEMA_VERSION = "semantic-anatomy-visual-lora-runpod-plan/v1"

VISUAL_LORA_MIN_RUNTIME_SECONDS = 15 * 60
VISUAL_LORA_MAX_RUNTIME_SECONDS = 8 * 60 * 60
VISUAL_LORA_MAX_HOURLY_COST_MICROUSD = 5_000_000
VISUAL_LORA_MAX_TOTAL_COST_MICROUSD = 25_000_000
VISUAL_LORA_MAX_INPUT_ARTIFACT_BYTES = 64 * 1024 * 1024 * 1024
VISUAL_LORA_MAX_OUTPUT_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024

_SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_MODEL_REVISION = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40,64}$")]
_IMAGE_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_GPU_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,99}$")

__all__ = (
    "ImmutableTrainingObject",
    "RunPodVisualLoraTrainingPlan",
    "RunPodVisualLoraTrainingRequest",
    "VisualLoraDatasetIdentity",
    "VisualLoraInputArtifacts",
    "VisualLoraRunPodLimits",
    "VisualLoraShadowOutput",
    "VisualLoraSplitIdentity",
    "VisualLoraStandingPolicyAdmission",
    "VisualLoraStandingPolicyFacts",
    "VisualLoraTrainerIdentity",
    "VisualLoraTrainingError",
    "VisualLoraTrainingGate",
    "VisualLoraTrainingIdentityError",
    "VisualLoraTrainingNotReadyError",
    "VisualLoraTrainingPolicyError",
    "build_runpod_visual_lora_training_plan",
    "build_runpod_visual_lora_training_plan_from_database",
    "evaluate_visual_lora_standing_policy_admission",
    "evaluate_visual_lora_training_gate",
    "maximum_visual_lora_cost_microusd",
    "require_matching_visual_lora_replay",
)


class VisualLoraTrainingError(RuntimeError):
    """Base error for visual-LoRA admission and request construction."""


class VisualLoraTrainingNotReadyError(VisualLoraTrainingError):
    """The requested owner/profile has not passed all training gates."""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        self.blockers = blockers
        super().__init__("visual LoRA training is blocked: " + "; ".join(blockers))


class VisualLoraTrainingIdentityError(VisualLoraTrainingError):
    """An immutable dataset, split, or artifact identity does not match."""


class VisualLoraTrainingPolicyError(VisualLoraTrainingError):
    """The persisted standing owner policy does not authorize this bounded run."""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        self.blockers = blockers
        super().__init__("visual LoRA standing policy blocks training: " + "; ".join(blockers))


class _FrozenStrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class VisualLoraDatasetIdentity(_FrozenStrictModel):
    """Logical identity and readiness counts for the exported binary dataset."""

    schema_version: Literal["semantic-anatomy-visual-lora-dataset/v1"] = (
        "semantic-anatomy-visual-lora-dataset/v1"
    )
    owner_user_id: UUID
    semantic_profile_sha256: _SHA256
    readiness_dataset_sha256: _SHA256
    label_manifest_sha256: _SHA256
    assessment_model: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    assessment_model_revision: _MODEL_REVISION
    binary_label_count: int = Field(ge=1)
    anatomy_good_count: int = Field(ge=1)
    anatomy_defect_count: int = Field(ge=1)
    issue_coded_defect_count: int = Field(ge=0)
    explicit_label_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> VisualLoraDatasetIdentity:
        if self.anatomy_good_count + self.anatomy_defect_count != self.binary_label_count:
            raise ValueError("visual LoRA dataset class counts do not cover the binary dataset")
        if self.explicit_label_count > self.binary_label_count:
            raise ValueError("visual LoRA explicit label count exceeds the binary dataset")
        if self.issue_coded_defect_count > self.anatomy_defect_count:
            raise ValueError("visual LoRA issue-coded count exceeds the defect dataset")
        expected_profile = assessment_profile_sha256(
            model=self.assessment_model,
            model_revision=self.assessment_model_revision,
        )
        if not hmac.compare_digest(expected_profile, self.semantic_profile_sha256):
            raise ValueError("visual LoRA assessment model does not match the semantic profile")
        return self


class VisualLoraSplitIdentity(_FrozenStrictModel):
    """Exact group-aware chronological split consumed by training and evaluation."""

    schema_version: Literal["semantic-anatomy-visual-lora-split/v1"] = (
        "semantic-anatomy-visual-lora-split/v1"
    )
    dataset_label_manifest_sha256: _SHA256
    split_manifest_sha256: _SHA256
    group_key: Literal["release_id", "generation_job_id"]
    cutoff_at: datetime
    training_count: int = Field(ge=1)
    training_good_count: int = Field(ge=1)
    training_defect_count: int = Field(ge=1)
    holdout_count: int = Field(ge=1)
    holdout_good_count: int = Field(ge=1)
    holdout_defect_count: int = Field(ge=1)

    @field_validator("cutoff_at")
    @classmethod
    def validate_utc_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("visual LoRA split cutoff must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_counts(self) -> VisualLoraSplitIdentity:
        if self.training_good_count + self.training_defect_count != self.training_count:
            raise ValueError("visual LoRA training split class counts are inconsistent")
        if self.holdout_good_count + self.holdout_defect_count != self.holdout_count:
            raise ValueError("visual LoRA holdout split class counts are inconsistent")
        return self


class ImmutableTrainingObject(_FrozenStrictModel):
    """One versioned object-store input without a URL or credential."""

    storage_backend: Literal["s3"] = "s3"
    bucket: Annotated[str, StringConstraints(min_length=3, max_length=63)]
    object_key: Annotated[str, StringConstraints(min_length=1, max_length=1024)] = Field(repr=False)
    object_version_id: Annotated[str, StringConstraints(min_length=1, max_length=1024)] = Field(
        repr=False
    )
    sha256: _SHA256
    exact_size_bytes: int = Field(ge=1, le=VISUAL_LORA_MAX_INPUT_ARTIFACT_BYTES)
    content_type: Literal[
        "application/json",
        "application/x-tar",
        "application/zip",
        "application/zstd",
    ]

    @field_validator("bucket")
    @classmethod
    def validate_bucket(cls, value: str) -> str:
        if _BUCKET.fullmatch(value) is None or ".." in value:
            raise ValueError("visual LoRA artifact bucket is invalid")
        return value

    @field_validator("object_key")
    @classmethod
    def validate_object_key(cls, value: str) -> str:
        if (
            value != value.strip()
            or value.startswith(("/", "\\"))
            or "://" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("visual LoRA artifact object key is invalid")
        return value

    @field_validator("object_version_id")
    @classmethod
    def validate_object_version(cls, value: str) -> str:
        if (
            value != value.strip()
            or value.casefold() == "null"
            or any(ord(character) < 33 or ord(character) == 127 for character in value)
        ):
            raise ValueError("visual LoRA artifact version is invalid")
        return value


class VisualLoraInputArtifacts(_FrozenStrictModel):
    dataset_archive: ImmutableTrainingObject
    split_manifest: ImmutableTrainingObject
    recipe_manifest: ImmutableTrainingObject

    @model_validator(mode="after")
    def validate_roles_and_uniqueness(self) -> VisualLoraInputArtifacts:
        if self.dataset_archive.content_type not in {
            "application/x-tar",
            "application/zip",
            "application/zstd",
        }:
            raise ValueError("visual LoRA dataset artifact must be an archive")
        if self.split_manifest.content_type != "application/json":
            raise ValueError("visual LoRA split manifest must be JSON")
        if self.recipe_manifest.content_type != "application/json":
            raise ValueError("visual LoRA recipe manifest must be JSON")
        identities = {
            (item.bucket, item.object_key, item.object_version_id)
            for item in (
                self.dataset_archive,
                self.split_manifest,
                self.recipe_manifest,
            )
        }
        if len(identities) != 3:
            raise ValueError("visual LoRA input artifacts must have distinct object identities")
        return self


class VisualLoraTrainerIdentity(_FrozenStrictModel):
    trainer_contract_version: Literal["semantic-anatomy-visual-lora-trainer/v1"]
    container_image: Annotated[str, StringConstraints(min_length=80, max_length=512)]
    base_model: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    base_model_revision: _MODEL_REVISION

    @field_validator("container_image")
    @classmethod
    def validate_pinned_image(cls, value: str) -> str:
        if _IMAGE_DIGEST.fullmatch(value) is None:
            raise ValueError("visual LoRA trainer image must be pinned by SHA-256 digest")
        return value


class VisualLoraRunPodLimits(_FrozenStrictModel):
    """A single-GPU, scale-to-zero, absolute spend envelope."""

    gpu_type_ids: tuple[Annotated[str, StringConstraints(min_length=1, max_length=100)], ...] = (
        Field(min_length=1, max_length=8)
    )
    gpu_count: Literal[1] = 1
    workers_min: Literal[0] = 0
    workers_max: Literal[1] = 1
    max_runtime_seconds: int = Field(
        ge=VISUAL_LORA_MIN_RUNTIME_SECONDS,
        le=VISUAL_LORA_MAX_RUNTIME_SECONDS,
    )
    max_hourly_cost_microusd: int = Field(
        ge=1,
        le=VISUAL_LORA_MAX_HOURLY_COST_MICROUSD,
    )
    max_cost_microusd: int = Field(ge=1, le=VISUAL_LORA_MAX_TOTAL_COST_MICROUSD)
    automatic_provider_mutation_retries: Literal[0] = 0

    @field_validator("gpu_type_ids")
    @classmethod
    def validate_gpu_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        invalid = any(_GPU_TYPE.fullmatch(item) is None for item in value)
        if len(set(value)) != len(value) or invalid:
            raise ValueError("visual LoRA RunPod GPU type list is invalid")
        return value

    @model_validator(mode="after")
    def validate_cost_envelope(self) -> VisualLoraRunPodLimits:
        expected = maximum_visual_lora_cost_microusd(
            max_hourly_cost_microusd=self.max_hourly_cost_microusd,
            max_runtime_seconds=self.max_runtime_seconds,
        )
        if self.max_cost_microusd != expected:
            raise ValueError("visual LoRA total cost cap does not match the hourly/runtime cap")
        return self


class VisualLoraShadowOutput(_FrozenStrictModel):
    """Destination and non-authoritative lifecycle of the resulting adapter."""

    deployment_mode: Literal["shadow"] = "shadow"
    auto_promote: Literal[False] = False
    may_change_review_decisions: Literal[False] = False
    requires_champion_challenger_evaluation: Literal[True] = True
    storage_backend: Literal["s3"] = "s3"
    bucket: Annotated[str, StringConstraints(min_length=3, max_length=63)]
    object_key: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    write_mode: Literal["if_absent"] = "if_absent"
    artifact_format: Literal["safetensors"] = "safetensors"
    max_size_bytes: int = Field(ge=1, le=VISUAL_LORA_MAX_OUTPUT_ARTIFACT_BYTES)

    @field_validator("bucket")
    @classmethod
    def validate_bucket(cls, value: str) -> str:
        if _BUCKET.fullmatch(value) is None or ".." in value:
            raise ValueError("visual LoRA output bucket is invalid")
        return value

    @field_validator("object_key")
    @classmethod
    def validate_object_key(cls, value: str) -> str:
        if (
            value != value.strip()
            or value.startswith(("/", "\\"))
            or "://" in value
            or not value.casefold().endswith(".safetensors")
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("visual LoRA output object key is invalid")
        return value


class VisualLoraStandingPolicyFacts(_FrozenStrictModel):
    """Persisted owner policy facts used for unattended training admission."""

    schema_version: Literal["semantic-learning-policy/v1"] = "semantic-learning-policy/v1"
    persisted: Literal[True] = True
    owner_user_id: UUID
    learning_enabled: bool
    auto_train_visual: bool
    max_visual_run_microusd: int = Field(ge=1, le=VISUAL_LORA_MAX_TOTAL_COST_MICROUSD)
    lock_version: int = Field(ge=1)


class RunPodVisualLoraTrainingRequest(_FrozenStrictModel):
    schema_version: Literal["semantic-anatomy-visual-lora-runpod-request/v1"] = (
        "semantic-anatomy-visual-lora-runpod-request/v1"
    )
    provider: Literal["runpod"] = "runpod"
    job_kind: Literal["visual_lora_training"] = "visual_lora_training"
    dataset: VisualLoraDatasetIdentity
    split: VisualLoraSplitIdentity
    artifacts: VisualLoraInputArtifacts
    trainer: VisualLoraTrainerIdentity
    recipe_schema_version: Literal["semantic-anatomy-visual-lora-recipe/v1"] = (
        "semantic-anatomy-visual-lora-recipe/v1"
    )
    recipe_sha256: _SHA256
    limits: VisualLoraRunPodLimits
    output: VisualLoraShadowOutput
    request_sha256: _SHA256
    idempotency_key: Annotated[str, StringConstraints(min_length=79, max_length=79)]

    @model_validator(mode="after")
    def validate_integrity(self) -> RunPodVisualLoraTrainingRequest:
        expected = canonical_sha256(_request_identity(self))
        if not hmac.compare_digest(expected, self.request_sha256):
            raise ValueError("visual LoRA request digest is invalid")
        expected_key = f"visual-lora-v1:{expected}"
        if not hmac.compare_digest(expected_key, self.idempotency_key):
            raise ValueError("visual LoRA request idempotency key is invalid")
        return self


class RunPodVisualLoraTrainingPlan(_FrozenStrictModel):
    """A sealed dry plan; this type has no provider submission capability."""

    schema_version: Literal["semantic-anatomy-visual-lora-runpod-plan/v1"] = (
        "semantic-anatomy-visual-lora-runpod-plan/v1"
    )
    mutates_runpod: Literal[False] = False
    provider_spend_started: Literal[False] = False
    provider_submission_available: Literal[False] = False
    requires_durable_idempotency_claim: Literal[True] = True
    requires_persisted_standing_policy: Literal[True] = True
    per_run_confirmation_required: Literal[False] = False
    standing_policy_authorizes_bounded_training: Literal[True] = True
    standing_policy: VisualLoraStandingPolicyFacts
    request: RunPodVisualLoraTrainingRequest

    @model_validator(mode="after")
    def validate_policy_admission(self) -> RunPodVisualLoraTrainingPlan:
        admission = evaluate_visual_lora_standing_policy_admission(
            self.standing_policy,
            owner_user_id=self.request.dataset.owner_user_id,
            plan_max_cost_microusd=self.request.limits.max_cost_microusd,
        )
        if not admission.admitted:
            raise ValueError("visual LoRA plan is not authorized by its standing policy")
        return self


@dataclass(frozen=True, slots=True)
class VisualLoraTrainingGate:
    admitted: bool
    blockers: tuple[str, ...]
    readiness_dataset_sha256: str | None


@dataclass(frozen=True, slots=True)
class VisualLoraStandingPolicyAdmission:
    admitted: bool
    blockers: tuple[str, ...]
    policy_lock_version: int | None
    policy_max_visual_run_microusd: int | None


def maximum_visual_lora_cost_microusd(
    *,
    max_hourly_cost_microusd: int,
    max_runtime_seconds: int,
) -> int:
    if (
        isinstance(max_hourly_cost_microusd, bool)
        or isinstance(max_runtime_seconds, bool)
        or max_hourly_cost_microusd <= 0
        or max_runtime_seconds <= 0
    ):
        raise ValueError("visual LoRA cost inputs must be positive integers")
    return (max_hourly_cost_microusd * max_runtime_seconds + 3599) // 3600


def evaluate_visual_lora_standing_policy_admission(
    policy: VisualLoraStandingPolicyFacts | None,
    *,
    owner_user_id: UUID,
    plan_max_cost_microusd: int,
) -> VisualLoraStandingPolicyAdmission:
    """Purely evaluate a persisted policy; never prompt, mutate, or contact RunPod."""

    if (
        isinstance(plan_max_cost_microusd, bool)
        or plan_max_cost_microusd < 1
        or plan_max_cost_microusd > VISUAL_LORA_MAX_TOTAL_COST_MICROUSD
    ):
        raise ValueError("visual LoRA plan maximum cost is invalid")
    if policy is None:
        return VisualLoraStandingPolicyAdmission(
            admitted=False,
            blockers=("persisted standing learning policy is required",),
            policy_lock_version=None,
            policy_max_visual_run_microusd=None,
        )
    blockers: list[str] = []
    if policy.owner_user_id != owner_user_id:
        blockers.append("standing policy belongs to a different owner")
    if not policy.learning_enabled:
        blockers.append("learning is disabled in the standing policy")
    if not policy.auto_train_visual:
        blockers.append("automatic visual training is disabled in the standing policy")
    if policy.max_visual_run_microusd < plan_max_cost_microusd:
        blockers.append("standing policy cost cap is below the plan maximum")
    return VisualLoraStandingPolicyAdmission(
        admitted=not blockers,
        blockers=tuple(blockers),
        policy_lock_version=policy.lock_version,
        policy_max_visual_run_microusd=policy.max_visual_run_microusd,
    )


def evaluate_visual_lora_training_gate(
    report: SemanticLearningReadinessReport,
    *,
    owner_user_id: UUID,
    profile_sha256: str,
) -> VisualLoraTrainingGate:
    """Evaluate admission without creating artifacts, spending, or mutating state."""

    blockers: list[str] = []
    if report.schema_version != SEMANTIC_LEARNING_READINESS_SCHEMA_VERSION:
        blockers.append("semantic learning readiness schema is unsupported")
    matches = tuple(
        profile
        for profile in report.profiles
        if profile.owner_user_id == owner_user_id and profile.profile_sha256 == profile_sha256
    )
    if len(matches) != 1:
        blockers.append("exactly one owner/profile readiness result is required")
        return VisualLoraTrainingGate(False, tuple(blockers), None)
    profile = matches[0]
    blockers.extend(_independent_readiness_blockers(profile))
    if not profile.lora.ready or profile.lora.blockers:
        blockers.extend(f"readiness: {item}" for item in profile.lora.blockers)
        if not profile.lora.blockers:
            blockers.append("readiness: visual LoRA phase is not ready")
    if not profile.meta_evaluation.ready or profile.meta_evaluation.blockers:
        blockers.extend(f"evaluation: {item}" for item in profile.meta_evaluation.blockers)
        if not profile.meta_evaluation.blockers:
            blockers.append("evaluation: promotion-capable holdout is not ready")
    return VisualLoraTrainingGate(
        admitted=not blockers,
        blockers=tuple(dict.fromkeys(blockers)),
        readiness_dataset_sha256=profile.dataset_sha256,
    )


def _independent_readiness_blockers(
    profile: SemanticProfileLearningReadiness,
) -> tuple[str, ...]:
    """Recheck safety-critical counts instead of trusting a ready boolean alone."""

    blockers: list[str] = []
    requirements = (
        (profile.binary_labeled_count, LORA_MINIMUM_SAMPLES, "binary labels"),
        (profile.anatomy_good_count, LORA_MINIMUM_GOOD, "good labels"),
        (profile.anatomy_defect_count, LORA_MINIMUM_DEFECT, "defect labels"),
        (
            profile.issue_coded_defect_count,
            LORA_MINIMUM_ISSUE_CODED_DEFECTS,
            "owner-confirmed defect subtypes",
        ),
        (profile.explicit_label_count, LORA_MINIMUM_EXPLICIT, "explicit labels"),
        (
            profile.split.completed_review_set_count,
            LORA_MINIMUM_COMPLETED_REVIEW_SETS,
            "completed review sets",
        ),
        (profile.split.generation_batch_count, LORA_MINIMUM_BATCHES, "generation batches"),
    )
    for actual, minimum, label in requirements:
        if actual < minimum:
            blockers.append(f"readiness count is below {minimum} {label}")

    ready_families = sum(
        item.count >= LORA_MINIMUM_SAMPLES_PER_ISSUE_FAMILY
        for item in profile.owner_issue_family_counts
    )
    if ready_families < LORA_MINIMUM_READY_ISSUE_FAMILIES:
        blockers.append("readiness issue-family diversity is insufficient")

    holdout = profile.split.evaluation_holdout
    if holdout is None:
        blockers.append("readiness has no untouched temporal holdout")
        return tuple(blockers)
    if holdout.training_good_count < META_EVALUATION_MINIMUM_TRAINING_GOOD:
        blockers.append("readiness holdout leaves too few training good labels")
    if holdout.training_defect_count < META_EVALUATION_MINIMUM_TRAINING_DEFECT:
        blockers.append("readiness holdout leaves too few training defect labels")
    if holdout.holdout_defect_count < META_EVALUATION_MINIMUM_HOLDOUT_DEFECT:
        blockers.append("readiness holdout has too few defect labels")
    if (
        holdout.zero_false_reject_upper_micros is None
        or holdout.zero_false_reject_upper_micros
        > META_EVALUATION_MAXIMUM_ZERO_ERROR_FALSE_REJECT_UPPER_MICROS
    ):
        blockers.append("readiness holdout cannot bound false rejection safely")
    return tuple(blockers)


def build_runpod_visual_lora_training_plan(
    report: SemanticLearningReadinessReport,
    *,
    owner_user_id: UUID,
    profile_sha256: str,
    dataset: VisualLoraDatasetIdentity,
    split: VisualLoraSplitIdentity,
    artifacts: VisualLoraInputArtifacts,
    trainer: VisualLoraTrainerIdentity,
    recipe_sha256: str,
    limits: VisualLoraRunPodLimits,
    output: VisualLoraShadowOutput,
    standing_policy: VisualLoraStandingPolicyFacts,
) -> RunPodVisualLoraTrainingPlan:
    """Seal a deterministic dry plan after every current readiness gate passes."""

    gate = evaluate_visual_lora_training_gate(
        report,
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
    )
    if not gate.admitted:
        raise VisualLoraTrainingNotReadyError(gate.blockers)
    policy_admission = evaluate_visual_lora_standing_policy_admission(
        standing_policy,
        owner_user_id=owner_user_id,
        plan_max_cost_microusd=limits.max_cost_microusd,
    )
    if not policy_admission.admitted:
        raise VisualLoraTrainingPolicyError(policy_admission.blockers)
    profile = _matching_profile(report, owner_user_id, profile_sha256)
    _require_dataset_identity(profile, dataset, owner_user_id, profile_sha256)
    _require_split_identity(profile, dataset, split)
    _require_artifact_identity(dataset, split, artifacts, recipe_sha256)
    _require_output_isolated(artifacts, output)
    if (
        trainer.base_model != dataset.assessment_model
        or trainer.base_model_revision != dataset.assessment_model_revision
    ):
        raise VisualLoraTrainingIdentityError(
            "visual LoRA trainer base model does not match the assessment dataset"
        )

    unsealed = {
        "schema_version": VISUAL_LORA_REQUEST_SCHEMA_VERSION,
        "provider": "runpod",
        "job_kind": "visual_lora_training",
        "dataset": dataset.model_dump(mode="json"),
        "split": split.model_dump(mode="json"),
        "artifacts": artifacts.model_dump(mode="json"),
        "trainer": trainer.model_dump(mode="json"),
        "recipe_schema_version": VISUAL_LORA_RECIPE_SCHEMA_VERSION,
        "recipe_sha256": recipe_sha256,
        "limits": limits.model_dump(mode="json"),
        "output": output.model_dump(mode="json"),
    }
    request_sha256 = canonical_sha256(unsealed)
    request = RunPodVisualLoraTrainingRequest(
        dataset=dataset,
        split=split,
        artifacts=artifacts,
        trainer=trainer,
        recipe_sha256=recipe_sha256,
        limits=limits,
        output=output,
        request_sha256=request_sha256,
        idempotency_key=f"visual-lora-v1:{request_sha256}",
    )
    return RunPodVisualLoraTrainingPlan(
        standing_policy=standing_policy,
        request=request,
    )


async def build_runpod_visual_lora_training_plan_from_database(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    profile_sha256: str,
    dataset: VisualLoraDatasetIdentity,
    split: VisualLoraSplitIdentity,
    artifacts: VisualLoraInputArtifacts,
    trainer: VisualLoraTrainerIdentity,
    recipe_sha256: str,
    limits: VisualLoraRunPodLimits,
    output: VisualLoraShadowOutput,
) -> RunPodVisualLoraTrainingPlan:
    """Read current owner/profile readiness and build a dry plan without writes."""

    report = await build_semantic_learning_readiness_report(
        session,
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
    )
    policy = await session.get(SemanticLearningPolicy, owner_user_id)
    standing_policy = (
        None
        if policy is None
        else VisualLoraStandingPolicyFacts(
            owner_user_id=policy.owner_user_id,
            learning_enabled=policy.learning_enabled,
            auto_train_visual=policy.auto_train_visual,
            max_visual_run_microusd=policy.max_visual_run_microusd,
            lock_version=policy.lock_version,
        )
    )
    if standing_policy is None:
        admission = evaluate_visual_lora_standing_policy_admission(
            None,
            owner_user_id=owner_user_id,
            plan_max_cost_microusd=limits.max_cost_microusd,
        )
        raise VisualLoraTrainingPolicyError(admission.blockers)
    return build_runpod_visual_lora_training_plan(
        report,
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
        dataset=dataset,
        split=split,
        artifacts=artifacts,
        trainer=trainer,
        recipe_sha256=recipe_sha256,
        limits=limits,
        output=output,
        standing_policy=standing_policy,
    )


def require_matching_visual_lora_replay(
    existing: RunPodVisualLoraTrainingRequest,
    requested: RunPodVisualLoraTrainingRequest,
) -> RunPodVisualLoraTrainingRequest:
    """Resolve a durable claim replay, rejecting every identity conflict."""

    if (
        existing.idempotency_key != requested.idempotency_key
        or existing.request_sha256 != requested.request_sha256
        or existing != requested
    ):
        raise VisualLoraTrainingIdentityError(
            "visual LoRA idempotency claim belongs to a different immutable request"
        )
    return existing


def _matching_profile(
    report: SemanticLearningReadinessReport,
    owner_user_id: UUID,
    profile_sha256: str,
) -> SemanticProfileLearningReadiness:
    matches = tuple(
        item
        for item in report.profiles
        if item.owner_user_id == owner_user_id and item.profile_sha256 == profile_sha256
    )
    if len(matches) != 1:
        raise VisualLoraTrainingIdentityError("visual LoRA owner/profile readiness is ambiguous")
    return matches[0]


def _require_dataset_identity(
    profile: SemanticProfileLearningReadiness,
    dataset: VisualLoraDatasetIdentity,
    owner_user_id: UUID,
    profile_sha256: str,
) -> None:
    if (
        dataset.owner_user_id != owner_user_id
        or dataset.semantic_profile_sha256 != profile_sha256
        or dataset.readiness_dataset_sha256 != profile.dataset_sha256
        or dataset.binary_label_count != profile.binary_labeled_count
        or dataset.anatomy_good_count != profile.anatomy_good_count
        or dataset.anatomy_defect_count != profile.anatomy_defect_count
        or dataset.issue_coded_defect_count != profile.issue_coded_defect_count
        or dataset.explicit_label_count != profile.explicit_label_count
    ):
        raise VisualLoraTrainingIdentityError(
            "visual LoRA dataset identity does not match current owner/profile readiness"
        )


def _require_split_identity(
    profile: SemanticProfileLearningReadiness,
    dataset: VisualLoraDatasetIdentity,
    split: VisualLoraSplitIdentity,
) -> None:
    holdout = profile.split.evaluation_holdout
    if holdout is None:
        raise VisualLoraTrainingIdentityError("visual LoRA readiness has no evaluation holdout")
    if (
        split.dataset_label_manifest_sha256 != dataset.label_manifest_sha256
        or split.training_count + split.holdout_count != dataset.binary_label_count
        or split.group_key != holdout.group_key
        or split.cutoff_at != holdout.cutoff_at.astimezone(UTC)
        or split.training_count != holdout.training_count
        or split.training_good_count != holdout.training_good_count
        or split.training_defect_count != holdout.training_defect_count
        or split.holdout_count != holdout.holdout_count
        or split.holdout_good_count != holdout.holdout_good_count
        or split.holdout_defect_count != holdout.holdout_defect_count
    ):
        raise VisualLoraTrainingIdentityError(
            "visual LoRA split identity does not match the readiness holdout"
        )


def _require_artifact_identity(
    dataset: VisualLoraDatasetIdentity,
    split: VisualLoraSplitIdentity,
    artifacts: VisualLoraInputArtifacts,
    recipe_sha256: str,
) -> None:
    if artifacts.split_manifest.sha256 != split.split_manifest_sha256:
        raise VisualLoraTrainingIdentityError(
            "visual LoRA split object does not match the logical split manifest"
        )
    if artifacts.recipe_manifest.sha256 != recipe_sha256:
        raise VisualLoraTrainingIdentityError(
            "visual LoRA recipe object does not match the requested recipe"
        )
    # The archive byte digest and logical label-manifest digest intentionally
    # differ; both are bound into the request.  Reject an obviously swapped role.
    if artifacts.dataset_archive.sha256 in {
        split.split_manifest_sha256,
        recipe_sha256,
    } or dataset.label_manifest_sha256 in {
        split.split_manifest_sha256,
        recipe_sha256,
    }:
        raise VisualLoraTrainingIdentityError("visual LoRA artifact roles are not isolated")


def _require_output_isolated(
    artifacts: VisualLoraInputArtifacts,
    output: VisualLoraShadowOutput,
) -> None:
    input_objects = {
        (item.bucket, item.object_key)
        for item in (
            artifacts.dataset_archive,
            artifacts.split_manifest,
            artifacts.recipe_manifest,
        )
    }
    if (output.bucket, output.object_key) in input_objects:
        raise VisualLoraTrainingIdentityError("visual LoRA output would overwrite an input")


def _request_identity(request: RunPodVisualLoraTrainingRequest) -> dict[str, object]:
    value = request.model_dump(mode="json", exclude={"request_sha256", "idempotency_key"})
    return dict(value)
