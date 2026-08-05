"""Immutable, leakage-safe dataset manifests for personalized visual learning.

The builder is deliberately read-only: it records versioned S3 image identities
already present in the database and never downloads, uploads, signs, or submits
anything.  Label selection and the train/holdout boundary are delegated to the
same public helpers used by semantic-learning readiness.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import Asset, SemanticAssessment
from gen_automation.domain.canonical import canonical_json_bytes, canonical_sha256
from gen_automation.domain.enums import (
    AssetKind,
    AssetState,
    SemanticAssessmentState,
    SemanticGroundTruth,
    SemanticIssueCode,
)
from gen_automation.services.semantic_learning_readiness import (
    SEMANTIC_LEARNING_READINESS_SCHEMA_VERSION,
    SOURCE_EXPLICIT,
    SOURCE_INFERRED_ANATOMY_REJECT,
    SOURCE_INFERRED_REVIEW_ACCEPT,
    SemanticLearningSample,
    load_semantic_learning_samples,
    resolve_semantic_profile_learning_samples,
    select_semantic_learning_dataset_partition,
    summarize_semantic_learning_readiness,
)

SEMANTIC_VISUAL_DATASET_MANIFEST_SCHEMA_VERSION = "semantic-anatomy-visual-dataset-manifest/v1"
SEMANTIC_VISUAL_DATASET_ENTRY_SCHEMA_VERSION = "semantic-anatomy-visual-dataset-entry/v1"

_SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_NONEMPTY = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_SUPPORTED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

BinaryLabel = Literal["anatomy_good", "anatomy_defect"]
DatasetMembership = Literal["train", "untouched_holdout"]
DatasetGroupKey = Literal["release_id", "generation_job_id"]
FeedbackSource = Literal[
    "explicit",
    "inferred_review_accept",
    "inferred_anatomy_reject",
]
ImageMediaType = Literal["image/jpeg", "image/png", "image/webp"]

__all__ = (
    "SEMANTIC_VISUAL_DATASET_ENTRY_SCHEMA_VERSION",
    "SEMANTIC_VISUAL_DATASET_MANIFEST_SCHEMA_VERSION",
    "SemanticVisualAssetBinding",
    "SemanticVisualAssetIdentity",
    "SemanticVisualDatasetCohorts",
    "SemanticVisualDatasetEntry",
    "SemanticVisualDatasetIdentityError",
    "SemanticVisualDatasetManifest",
    "SemanticVisualDatasetManifestError",
    "SemanticVisualDatasetNotReadyError",
    "build_semantic_visual_dataset_manifest",
    "build_semantic_visual_dataset_manifest_from_database",
    "semantic_visual_dataset_manifest_bytes",
)


class SemanticVisualDatasetManifestError(RuntimeError):
    """Base error for visual-learning dataset construction."""


class SemanticVisualDatasetNotReadyError(SemanticVisualDatasetManifestError):
    """Readiness has not selected an untouched chronological holdout."""


class SemanticVisualDatasetIdentityError(SemanticVisualDatasetManifestError):
    """A persisted label, assessment, or immutable asset identity drifted."""


class _FrozenStrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class SemanticVisualAssetIdentity(_FrozenStrictModel):
    """One immutable S3 image object; never a URL or credential-bearing value."""

    storage_backend: Literal["s3"] = "s3"
    bucket: Annotated[str, StringConstraints(min_length=3, max_length=63)]
    object_key: _NONEMPTY = Field(repr=False)
    object_version_id: _NONEMPTY = Field(repr=False)
    sha256: _SHA256
    exact_size_bytes: int = Field(ge=1)
    media_type: ImageMediaType

    @field_validator("bucket")
    @classmethod
    def validate_bucket(cls, value: str) -> str:
        if _BUCKET.fullmatch(value) is None:
            raise ValueError("visual dataset S3 bucket is invalid")
        return value

    @field_validator("object_key", "object_version_id")
    @classmethod
    def validate_object_locator(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("visual dataset object identity is invalid")
        lowered = value.lower()
        if lowered.startswith(("http://", "https://")) or "x-amz-credential" in lowered:
            raise ValueError("visual dataset manifest cannot contain a signed URL")
        return value

    @field_validator("object_version_id")
    @classmethod
    def validate_versioned_object(cls, value: str) -> str:
        if value.lower() == "null":
            raise ValueError("visual dataset object must have an immutable S3 version")
        return value


class SemanticVisualAssetBinding(_FrozenStrictModel):
    """Assessment-to-object binding validated before manifest construction."""

    assessment_id: UUID
    asset_id: UUID
    semantic_profile_sha256: _SHA256
    asset: SemanticVisualAssetIdentity


class SemanticVisualDatasetCohorts(_FrozenStrictModel):
    checkpoint_sha256: _SHA256 | None = None
    lora_stack_sha256: _SHA256 | None = None
    workflow_sha256: _SHA256 | None = None
    style_stack_sha256: _SHA256 | None = None


class SemanticVisualDatasetEntry(_FrozenStrictModel):
    schema_version: Literal["semantic-anatomy-visual-dataset-entry/v1"] = (
        "semantic-anatomy-visual-dataset-entry/v1"
    )
    feedback_id: UUID
    assessment_id: UUID
    asset_id: UUID
    asset: SemanticVisualAssetIdentity
    binary_label: BinaryLabel
    owner_issue_code: SemanticIssueCode | None = None
    source: FeedbackSource
    release_id: UUID
    generation_job_id: UUID | None = None
    generated_at: datetime
    labeled_at: datetime
    cohorts: SemanticVisualDatasetCohorts
    membership: DatasetMembership
    group_key: DatasetGroupKey
    group_id: UUID

    @field_validator("generated_at", "labeled_at")
    @classmethod
    def validate_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("visual dataset timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_label_and_group(self) -> SemanticVisualDatasetEntry:
        if self.binary_label == "anatomy_good" and self.owner_issue_code is not None:
            raise ValueError("anatomy-good visual labels cannot carry a defect issue code")
        expected_group_id = (
            self.release_id if self.group_key == "release_id" else self.generation_job_id
        )
        if expected_group_id is None or expected_group_id != self.group_id:
            raise ValueError("visual dataset group identity does not match its sample")
        return self


class SemanticVisualDatasetManifest(_FrozenStrictModel):
    """Canonical owner/profile-scoped input contract for a future visual trainer."""

    schema_version: Literal["semantic-anatomy-visual-dataset-manifest/v1"] = (
        "semantic-anatomy-visual-dataset-manifest/v1"
    )
    readiness_schema_version: Literal["semantic-learning-readiness/v1"] = (
        "semantic-learning-readiness/v1"
    )
    owner_user_id: UUID
    semantic_profile_sha256: _SHA256
    readiness_dataset_sha256: _SHA256
    group_key: DatasetGroupKey
    cutoff_at: datetime
    entries: tuple[SemanticVisualDatasetEntry, ...]
    training_count: int = Field(ge=1)
    training_good_count: int = Field(ge=1)
    training_defect_count: int = Field(ge=1)
    holdout_count: int = Field(ge=1)
    holdout_good_count: int = Field(ge=1)
    holdout_defect_count: int = Field(ge=1)
    asset_manifest_sha256: _SHA256
    label_manifest_sha256: _SHA256
    split_manifest_sha256: _SHA256
    manifest_sha256: _SHA256

    @field_validator("cutoff_at")
    @classmethod
    def validate_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("visual dataset cutoff must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_identity(self) -> SemanticVisualDatasetManifest:
        if self.readiness_schema_version != SEMANTIC_LEARNING_READINESS_SCHEMA_VERSION:
            raise ValueError("visual dataset readiness schema is unsupported")
        if self.training_good_count + self.training_defect_count != self.training_count:
            raise ValueError("visual dataset training counts are inconsistent")
        if self.holdout_good_count + self.holdout_defect_count != self.holdout_count:
            raise ValueError("visual dataset holdout counts are inconsistent")
        training = tuple(entry for entry in self.entries if entry.membership == "train")
        holdout = tuple(entry for entry in self.entries if entry.membership == "untouched_holdout")
        if len(training) != self.training_count or len(holdout) != self.holdout_count:
            raise ValueError("visual dataset membership counts are inconsistent")
        if any(entry.group_key != self.group_key for entry in self.entries):
            raise ValueError("visual dataset entries use mixed group contracts")
        if {entry.asset.sha256 for entry in training} & {entry.asset.sha256 for entry in holdout}:
            raise ValueError("visual dataset content crosses training and holdout")
        if {entry.group_id for entry in training} & {entry.group_id for entry in holdout}:
            raise ValueError("visual dataset group crosses training and holdout")
        digests = _dataset_digests(
            owner_user_id=self.owner_user_id,
            semantic_profile_sha256=self.semantic_profile_sha256,
            readiness_dataset_sha256=self.readiness_dataset_sha256,
            group_key=self.group_key,
            cutoff_at=self.cutoff_at,
            entries=self.entries,
        )
        if not all(
            hmac.compare_digest(actual, expected)
            for actual, expected in (
                (self.asset_manifest_sha256, digests[0]),
                (self.label_manifest_sha256, digests[1]),
                (self.split_manifest_sha256, digests[2]),
                (self.manifest_sha256, digests[3]),
            )
        ):
            raise ValueError("visual dataset canonical digest does not match")
        return self


async def build_semantic_visual_dataset_manifest_from_database(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    profile_sha256: str,
) -> SemanticVisualDatasetManifest:
    """Build a deterministic manifest from stored owner labels and asset snapshots."""

    samples = await load_semantic_learning_samples(
        session,
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
    )
    assessment_ids = {sample.assessment_id for sample in samples}
    if not assessment_ids:
        raise SemanticVisualDatasetNotReadyError("no visual learning labels are available")
    rows = (
        await session.execute(
            select(SemanticAssessment, Asset)
            .join(Asset, Asset.id == SemanticAssessment.asset_id)
            .where(SemanticAssessment.id.in_(assessment_ids))
            .order_by(SemanticAssessment.id)
        )
    ).all()
    bindings: dict[UUID, SemanticVisualAssetBinding] = {}
    for assessment, asset in rows:
        bindings[assessment.id] = _binding_from_database(assessment, asset)
    if set(bindings) != assessment_ids:
        raise SemanticVisualDatasetIdentityError(
            "one or more labeled assessment assets are missing"
        )
    return build_semantic_visual_dataset_manifest(
        samples=samples,
        asset_bindings=bindings,
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
    )


def build_semantic_visual_dataset_manifest(
    *,
    samples: Iterable[SemanticLearningSample],
    asset_bindings: Mapping[UUID, SemanticVisualAssetBinding],
    owner_user_id: UUID,
    profile_sha256: str,
) -> SemanticVisualDatasetManifest:
    """Build and self-verify a canonical manifest from already loaded identities."""

    raw_samples = tuple(samples)
    resolved = resolve_semantic_profile_learning_samples(
        raw_samples,
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
    )
    report = summarize_semantic_learning_readiness(raw_samples)
    profile = next(
        (
            item
            for item in report.profiles
            if item.owner_user_id == owner_user_id and item.profile_sha256 == profile_sha256
        ),
        None,
    )
    if profile is None:
        raise SemanticVisualDatasetNotReadyError("no visual learning profile is available")
    partition = select_semantic_learning_dataset_partition(resolved.binary)
    if partition is None:
        raise SemanticVisualDatasetNotReadyError(
            "readiness has not selected an untouched chronological group holdout"
        )
    selected_ids = {sample.feedback_id for sample in resolved.binary}
    partitioned_ids = {
        sample.feedback_id for sample in (*partition.training, *partition.untouched_holdout)
    }
    if selected_ids != partitioned_ids:
        raise SemanticVisualDatasetIdentityError(
            "the readiness-selected split does not cover every binary label"
        )
    if partition.group_key not in {"release_id", "generation_job_id"}:
        raise SemanticVisualDatasetIdentityError("visual dataset group contract is unsupported")
    group_key = cast(DatasetGroupKey, partition.group_key)

    entries = tuple(
        _entry_from_sample(
            sample,
            binding=_required_binding(asset_bindings, sample),
            membership=cast(DatasetMembership, membership),
            group_key=group_key,
        )
        for membership, split_samples in (
            ("train", partition.training),
            ("untouched_holdout", partition.untouched_holdout),
        )
        for sample in sorted(split_samples, key=_manifest_sample_sort_key)
    )
    training = tuple(entry for entry in entries if entry.membership == "train")
    holdout = tuple(entry for entry in entries if entry.membership == "untouched_holdout")
    digests = _dataset_digests(
        owner_user_id=owner_user_id,
        semantic_profile_sha256=profile_sha256,
        readiness_dataset_sha256=profile.dataset_sha256,
        group_key=group_key,
        cutoff_at=partition.cutoff_at,
        entries=entries,
    )
    return SemanticVisualDatasetManifest(
        owner_user_id=owner_user_id,
        semantic_profile_sha256=profile_sha256,
        readiness_dataset_sha256=profile.dataset_sha256,
        group_key=group_key,
        cutoff_at=partition.cutoff_at,
        entries=entries,
        training_count=len(training),
        training_good_count=sum(entry.binary_label == "anatomy_good" for entry in training),
        training_defect_count=sum(entry.binary_label == "anatomy_defect" for entry in training),
        holdout_count=len(holdout),
        holdout_good_count=sum(entry.binary_label == "anatomy_good" for entry in holdout),
        holdout_defect_count=sum(entry.binary_label == "anatomy_defect" for entry in holdout),
        asset_manifest_sha256=digests[0],
        label_manifest_sha256=digests[1],
        split_manifest_sha256=digests[2],
        manifest_sha256=digests[3],
    )


def semantic_visual_dataset_manifest_bytes(
    manifest: SemanticVisualDatasetManifest,
) -> bytes:
    """Return stable JSON bytes after the model's digest validator has run."""

    return canonical_json_bytes(manifest)


def _binding_from_database(
    assessment: SemanticAssessment,
    asset: Asset,
) -> SemanticVisualAssetBinding:
    if assessment.state != SemanticAssessmentState.COMPLETED:
        raise SemanticVisualDatasetIdentityError("visual dataset assessment is incomplete")
    if asset.state != AssetState.AVAILABLE or asset.kind != AssetKind.RAW_MASTER:
        raise SemanticVisualDatasetIdentityError("visual dataset asset is not an available master")
    snapshot = (
        assessment.asset_storage_backend,
        assessment.asset_storage_bucket,
        assessment.asset_object_key,
        assessment.asset_object_version_id,
        assessment.asset_sha256,
        assessment.asset_byte_size,
        assessment.asset_content_type,
    )
    current = (
        asset.storage_backend,
        asset.storage_bucket,
        asset.object_key,
        asset.object_version_id,
        asset.sha256,
        asset.byte_size,
        asset.content_type,
    )
    if snapshot != current:
        raise SemanticVisualDatasetIdentityError(
            "visual dataset asset identity drifted from its semantic assessment"
        )
    if assessment.asset_content_type not in _SUPPORTED_MEDIA_TYPES:
        raise SemanticVisualDatasetIdentityError("visual dataset media type is unsupported")
    if assessment.asset_storage_backend != "s3":
        raise SemanticVisualDatasetIdentityError("visual dataset asset is not stored in S3")
    try:
        identity = SemanticVisualAssetIdentity(
            storage_backend="s3",
            bucket=assessment.asset_storage_bucket,
            object_key=assessment.asset_object_key,
            object_version_id=assessment.asset_object_version_id,
            sha256=assessment.asset_sha256,
            exact_size_bytes=assessment.asset_byte_size,
            media_type=cast(ImageMediaType, assessment.asset_content_type),
        )
    except ValueError as error:
        raise SemanticVisualDatasetIdentityError(
            "visual dataset asset is not immutable versioned S3 input"
        ) from error
    return SemanticVisualAssetBinding(
        assessment_id=assessment.id,
        asset_id=assessment.asset_id,
        semantic_profile_sha256=assessment.profile_sha256,
        asset=identity,
    )


def _required_binding(
    bindings: Mapping[UUID, SemanticVisualAssetBinding],
    sample: SemanticLearningSample,
) -> SemanticVisualAssetBinding:
    try:
        binding = bindings[sample.assessment_id]
    except KeyError as error:
        raise SemanticVisualDatasetIdentityError(
            "visual dataset assessment asset is missing"
        ) from error
    if binding.assessment_id != sample.assessment_id or binding.asset_id != sample.asset_id:
        raise SemanticVisualDatasetIdentityError(
            "visual dataset assessment-to-asset binding changed"
        )
    if binding.semantic_profile_sha256 != sample.profile_sha256:
        raise SemanticVisualDatasetIdentityError("visual dataset profile identity changed")
    if not hmac.compare_digest(binding.asset.sha256, sample.asset_sha256):
        raise SemanticVisualDatasetIdentityError("visual dataset content identity changed")
    return binding


def _entry_from_sample(
    sample: SemanticLearningSample,
    *,
    binding: SemanticVisualAssetBinding,
    membership: DatasetMembership,
    group_key: str,
) -> SemanticVisualDatasetEntry:
    if group_key == "release_id":
        group_id = sample.release_id
    elif group_key == "generation_job_id" and sample.generation_job_id is not None:
        group_id = sample.generation_job_id
    else:
        raise SemanticVisualDatasetIdentityError("visual dataset group identity is missing")
    if sample.ground_truth not in (
        SemanticGroundTruth.ANATOMY_GOOD,
        SemanticGroundTruth.ANATOMY_DEFECT,
    ):
        raise SemanticVisualDatasetIdentityError("visual dataset contains a non-binary label")
    if sample.source not in {
        SOURCE_EXPLICIT,
        SOURCE_INFERRED_REVIEW_ACCEPT,
        SOURCE_INFERRED_ANATOMY_REJECT,
    }:
        raise SemanticVisualDatasetIdentityError("visual dataset feedback source is unsupported")
    return SemanticVisualDatasetEntry(
        feedback_id=sample.feedback_id,
        assessment_id=sample.assessment_id,
        asset_id=sample.asset_id,
        asset=binding.asset,
        binary_label=sample.ground_truth.value,  # type: ignore[arg-type]
        owner_issue_code=sample.owner_issue_code,
        source=sample.source,  # type: ignore[arg-type]
        release_id=sample.release_id,
        generation_job_id=sample.generation_job_id,
        generated_at=sample.generated_at,
        labeled_at=sample.labeled_at,
        cohorts=SemanticVisualDatasetCohorts(
            checkpoint_sha256=sample.checkpoint_cohort,
            lora_stack_sha256=sample.lora_stack_cohort,
            workflow_sha256=sample.workflow_cohort,
            style_stack_sha256=sample.style_cohort,
        ),
        membership=membership,
        group_key=group_key,  # type: ignore[arg-type]
        group_id=group_id,
    )


def _manifest_sample_sort_key(
    sample: SemanticLearningSample,
) -> tuple[datetime, str, str]:
    return sample.generated_at, sample.asset_sha256, str(sample.feedback_id)


def _dataset_digests(
    *,
    owner_user_id: UUID,
    semantic_profile_sha256: str,
    readiness_dataset_sha256: str,
    group_key: DatasetGroupKey,
    cutoff_at: datetime,
    entries: tuple[SemanticVisualDatasetEntry, ...],
) -> tuple[str, str, str, str]:
    assets = [
        {
            "asset_id": str(entry.asset_id),
            **entry.asset.model_dump(mode="json"),
        }
        for entry in entries
    ]
    labels = [
        {
            "feedback_id": str(entry.feedback_id),
            "assessment_id": str(entry.assessment_id),
            "asset_sha256": entry.asset.sha256,
            "binary_label": entry.binary_label,
            "owner_issue_code": (
                entry.owner_issue_code.value if entry.owner_issue_code is not None else None
            ),
            "source": entry.source,
            "cohorts": entry.cohorts.model_dump(mode="json"),
        }
        for entry in entries
    ]
    asset_manifest_sha256 = canonical_sha256(assets)
    label_manifest_sha256 = canonical_sha256(labels)
    split_identity = {
        "group_key": group_key,
        "cutoff_at": cutoff_at.astimezone(UTC).isoformat(),
        "label_manifest_sha256": label_manifest_sha256,
        "memberships": [
            {
                "feedback_id": str(entry.feedback_id),
                "asset_sha256": entry.asset.sha256,
                "membership": entry.membership,
                "group_id": str(entry.group_id),
            }
            for entry in entries
        ],
    }
    split_manifest_sha256 = canonical_sha256(split_identity)
    manifest_identity = {
        "schema_version": SEMANTIC_VISUAL_DATASET_MANIFEST_SCHEMA_VERSION,
        "readiness_schema_version": SEMANTIC_LEARNING_READINESS_SCHEMA_VERSION,
        "owner_user_id": str(owner_user_id),
        "semantic_profile_sha256": semantic_profile_sha256,
        "readiness_dataset_sha256": readiness_dataset_sha256,
        "asset_manifest_sha256": asset_manifest_sha256,
        "label_manifest_sha256": label_manifest_sha256,
        "split_manifest_sha256": split_manifest_sha256,
        "entries": [entry.model_dump(mode="json") for entry in entries],
    }
    return (
        asset_manifest_sha256,
        label_manifest_sha256,
        split_manifest_sha256,
        canonical_sha256(manifest_identity),
    )
