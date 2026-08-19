"""Build a demand-scoped worker manifest from the immutable artifact catalog.

The deployment manifest remains the authority for every selectable primary,
support artifact, detector, and operator-managed LoRA. Runtime callers select
one primary, the pinned static LoRAs approved for that family, and the exact
dynamic LoRA stack; Anima additionally receives the pinned text encoder and
VAE. Dashboard onboarding may append only immutable active LoRAs with current
approval. Nothing here performs provider or storage I/O.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.config import Settings
from gen_automation.db.models import ManagedLoraArtifact, ModelArtifactApproval
from gen_automation.domain.enums import (
    ApprovalStatus,
    GenerationModelFamily,
    ManagedLoraLifecycle,
    ModelArtifactKind,
)
from gen_automation.domain.runtime_bindings import WORKER_DYNAMIC_MANIFEST_MAX_BYTES
from gen_automation.gpu_worker.artifacts import (
    ArtifactKind,
    ArtifactManifest,
    ModelArtifactSpec,
    create_artifact_manifest,
)
from gen_automation.gpu_worker.bootstrap import load_artifact_manifest


class ManagedArtifactManifestError(RuntimeError):
    """The catalog cannot be represented by the immutable worker contract."""


_WORKER_RUNTIME_FREE_SPACE_BYTES = 4 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class EffectiveArtifactManifest:
    manifest: ArtifactManifest
    manifest_json: str
    managed_lora_sha256s: frozenset[str]

    @property
    def sha256(self) -> str:
        return self.manifest.manifest_sha256


def default_checkpoint_sha256_from_settings(settings: Settings) -> str:
    """Return the sole legacy checkpoint used for a model-agnostic warm start."""

    raw_manifest = settings.salad_worker_model_manifest_json
    expected_sha256 = settings.salad_worker_model_manifest_sha256
    if raw_manifest is None or expected_sha256 is None:
        raise ManagedArtifactManifestError("worker artifact configuration is unavailable")
    baseline = load_artifact_manifest(raw_manifest.get_secret_value())
    if baseline.manifest_sha256 != expected_sha256.get_secret_value():
        raise ManagedArtifactManifestError("worker artifact manifest trust anchor changed")
    checkpoints = tuple(
        artifact for artifact in baseline.artifacts if artifact.kind == ArtifactKind.CHECKPOINT
    )
    if len(checkpoints) != 1:
        raise ManagedArtifactManifestError("default worker checkpoint is not uniquely pinned")
    return checkpoints[0].sha256


async def effective_artifact_manifest_from_settings(
    session: AsyncSession,
    *,
    settings: Settings,
    additional_artifact_ids: Collection[UUID] = (),
    required_checkpoint_sha256: str | None = None,
    required_lora_sha256s: Collection[str] | None = None,
) -> EffectiveArtifactManifest:
    """Load the pinned catalog and build the exact manifest needed by one job.

    ``None`` retains the catalog-wide validation mode used by administrative
    callers.  A concrete collection is the runtime mode: the worker manifest
    contains one primary, its small family-compatible static LoRA bundle, only
    the dynamic LoRAs required by one compatible batch, and family-specific
    support artifacts. This preserves warm prompt/style iteration without
    downloading the other family's multi-gigabyte core.
    """

    raw_manifest = settings.salad_worker_model_manifest_json
    expected_sha256 = settings.salad_worker_model_manifest_sha256
    artifact_bucket = settings.salad_worker_artifact_bucket
    if raw_manifest is None or expected_sha256 is None or artifact_bucket is None:
        raise ManagedArtifactManifestError("worker artifact configuration is unavailable")
    baseline = load_artifact_manifest(raw_manifest.get_secret_value())
    if baseline.manifest_sha256 != expected_sha256.get_secret_value():
        raise ManagedArtifactManifestError("worker artifact manifest trust anchor changed")
    effective = await build_effective_artifact_manifest(
        session,
        baseline=baseline,
        expected_bucket=artifact_bucket.get_secret_value(),
        additional_artifact_ids=additional_artifact_ids,
        required_checkpoint_sha256=required_checkpoint_sha256,
        required_lora_sha256s=required_lora_sha256s,
    )
    artifact_bytes = sum(artifact.max_size_bytes for artifact in effective.manifest.artifacts)
    usable_storage_bytes = settings.salad_container_storage_bytes - _WORKER_RUNTIME_FREE_SPACE_BYTES
    if usable_storage_bytes <= 0 or artifact_bytes > usable_storage_bytes:
        raise ManagedArtifactManifestError(
            "worker artifact selection exceeds the safe container-storage budget"
        )
    if len(effective.manifest_json.encode("utf-8")) > WORKER_DYNAMIC_MANIFEST_MAX_BYTES:
        raise ManagedArtifactManifestError("worker artifact manifest is too large")
    return effective


async def build_effective_artifact_manifest(
    session: AsyncSession,
    *,
    baseline: ArtifactManifest,
    expected_bucket: str,
    additional_artifact_ids: Collection[UUID] = (),
    required_checkpoint_sha256: str | None = None,
    required_lora_sha256s: Collection[str] | None = None,
) -> EffectiveArtifactManifest:
    """Select one family stack and merge its managed LoRAs deterministically."""

    additional_ids = tuple(additional_artifact_ids)
    required_hashes = None if required_lora_sha256s is None else frozenset(required_lora_sha256s)
    if required_checkpoint_sha256 is not None and (
        len(required_checkpoint_sha256) != 64
        or required_checkpoint_sha256 != required_checkpoint_sha256.lower()
        or any(character not in "0123456789abcdef" for character in required_checkpoint_sha256)
    ):
        raise ManagedArtifactManifestError("required primary model identity is invalid")
    if required_hashes is not None and any(
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
        for value in required_hashes
    ):
        raise ManagedArtifactManifestError("required LoRA identity is invalid")
    all_baseline_artifacts = list(baseline.artifacts)
    selected_family: GenerationModelFamily | None = None
    family_static_lora_hashes: frozenset[str] | None = None
    primary: ModelArtifactSpec | None = None
    if required_checkpoint_sha256 is not None:
        primary_matches = [
            artifact
            for artifact in all_baseline_artifacts
            if artifact.kind in {ArtifactKind.CHECKPOINT, ArtifactKind.DIFFUSION_MODEL}
            and artifact.sha256 == required_checkpoint_sha256
        ]
        if len(primary_matches) != 1:
            raise ManagedArtifactManifestError(
                "required primary model is not present in the pinned baseline"
            )
        primary = primary_matches[0]
        selected_family = (
            GenerationModelFamily.ANIMA
            if primary.kind == ArtifactKind.DIFFUSION_MODEL
            else GenerationModelFamily.ILLUSTRIOUS
        )
        primary_approval = await session.scalar(
            select(ModelArtifactApproval.id).where(
                ModelArtifactApproval.artifact_sha256 == primary.sha256,
                ModelArtifactApproval.kind == ModelArtifactKind.CHECKPOINT,
                ModelArtifactApproval.model_family == selected_family,
                ModelArtifactApproval.status == ApprovalStatus.APPROVED,
                ModelArtifactApproval.is_current.is_(True),
                or_(
                    ModelArtifactApproval.commercial_use_approved.is_(True),
                    ModelArtifactApproval.experiment_only.is_(True),
                ),
                ModelArtifactApproval.adult_use_approved.is_(True),
                ModelArtifactApproval.safetensors_verified.is_(True),
            )
        )
        if primary_approval is None:
            raise ManagedArtifactManifestError(
                "required primary model family approval is unavailable"
            )
        baseline_lora_hashes = tuple(
            artifact.sha256
            for artifact in all_baseline_artifacts
            if artifact.kind == ArtifactKind.LORA
        )
        if baseline_lora_hashes:
            approved_family_lora_hashes = await session.scalars(
                select(ModelArtifactApproval.artifact_sha256).where(
                    ModelArtifactApproval.artifact_sha256.in_(baseline_lora_hashes),
                    ModelArtifactApproval.kind == ModelArtifactKind.LORA,
                    ModelArtifactApproval.model_family == selected_family,
                    ModelArtifactApproval.status == ApprovalStatus.APPROVED,
                    ModelArtifactApproval.is_current.is_(True),
                    or_(
                        ModelArtifactApproval.commercial_use_approved.is_(True),
                        ModelArtifactApproval.experiment_only.is_(True),
                    ),
                    ModelArtifactApproval.adult_use_approved.is_(True),
                    ModelArtifactApproval.safetensors_verified.is_(True),
                )
            )
            family_static_lora_hashes = frozenset(approved_family_lora_hashes.all())
        else:
            family_static_lora_hashes = frozenset()

    active_or_draining = or_(
        ManagedLoraArtifact.lifecycle == ManagedLoraLifecycle.ACTIVE,
        and_(
            ManagedLoraArtifact.lifecycle == ManagedLoraLifecycle.RETIRING,
            ManagedLoraArtifact.activated_at.is_not(None),
            ManagedLoraArtifact.retired_at.is_(None),
        ),
    )
    selected_catalog = (
        active_or_draining
        if required_hashes is None
        else and_(
            active_or_draining,
            ManagedLoraArtifact.artifact_sha256.in_(tuple(required_hashes)),
        )
    )
    lifecycle_predicate = (
        or_(selected_catalog, ManagedLoraArtifact.id.in_(additional_ids))
        if additional_ids
        else selected_catalog
    )
    managed_approval_predicates = [
        ModelArtifactApproval.status == ApprovalStatus.APPROVED,
        ModelArtifactApproval.is_current.is_(True),
        ModelArtifactApproval.kind == ModelArtifactKind.LORA,
        or_(
            ModelArtifactApproval.commercial_use_approved.is_(True),
            ModelArtifactApproval.experiment_only.is_(True),
        ),
        ModelArtifactApproval.adult_use_approved.is_(True),
        ModelArtifactApproval.safetensors_verified.is_(True),
    ]
    if selected_family is not None:
        managed_approval_predicates.append(ModelArtifactApproval.model_family == selected_family)
    rows = list(
        (
            await session.execute(
                select(ManagedLoraArtifact, ModelArtifactApproval)
                .join(
                    ModelArtifactApproval,
                    ModelArtifactApproval.id == ManagedLoraArtifact.approval_id,
                )
                .where(
                    lifecycle_predicate,
                    *managed_approval_predicates,
                )
                .order_by(
                    ManagedLoraArtifact.display_name,
                    ManagedLoraArtifact.artifact_sha256,
                )
            )
        ).all()
    )
    artifacts = list(all_baseline_artifacts)
    if required_checkpoint_sha256 is not None:
        if primary is None or family_static_lora_hashes is None:
            raise ManagedArtifactManifestError("required primary model family is unavailable")
        if primary.kind == ArtifactKind.DIFFUSION_MODEL:
            text_encoders = [
                artifact
                for artifact in all_baseline_artifacts
                if artifact.kind == ArtifactKind.TEXT_ENCODER
            ]
            vaes = [
                artifact for artifact in all_baseline_artifacts if artifact.kind == ArtifactKind.VAE
            ]
            if len(text_encoders) != 1 or len(vaes) != 1:
                raise ManagedArtifactManifestError(
                    "Anima runtime support artifacts are not uniquely pinned"
                )
        artifacts = [
            artifact
            for artifact in all_baseline_artifacts
            if (
                (
                    artifact.kind in {ArtifactKind.CHECKPOINT, ArtifactKind.DIFFUSION_MODEL}
                    and artifact.sha256 == required_checkpoint_sha256
                )
                or (
                    artifact.kind == ArtifactKind.LORA
                    and artifact.sha256 in family_static_lora_hashes
                )
                or artifact.kind == ArtifactKind.DETECTOR
                or (
                    primary.kind == ArtifactKind.DIFFUSION_MODEL
                    and artifact.kind in {ArtifactKind.TEXT_ENCODER, ArtifactKind.VAE}
                )
            )
        ]
    baseline_hashes = {artifact.sha256 for artifact in all_baseline_artifacts}
    available_lora_hashes = {
        artifact.sha256 for artifact in artifacts if artifact.kind == ArtifactKind.LORA
    }
    baseline_names = {artifact.logical_name.casefold() for artifact in all_baseline_artifacts}
    baseline_targets = {
        (artifact.kind, artifact.target_filename.casefold()) for artifact in all_baseline_artifacts
    }
    managed_hashes: set[str] = set()
    for managed, approval in rows:
        if managed.storage_bucket != expected_bucket:
            raise ManagedArtifactManifestError("managed LoRA storage bucket is invalid")
        if managed.artifact_sha256 != approval.artifact_sha256:
            raise ManagedArtifactManifestError("managed LoRA approval identity is invalid")
        if managed.object_key != approval.storage_key:
            raise ManagedArtifactManifestError("managed LoRA storage approval is invalid")
        if managed.artifact_sha256 in baseline_hashes:
            raise ManagedArtifactManifestError("managed LoRA duplicates a baseline artifact")
        logical_name = f"managed-{managed.artifact_sha256[:20]}"
        name_key = logical_name.casefold()
        target_key = (ArtifactKind.LORA, managed.target_filename.casefold())
        if name_key in baseline_names or target_key in baseline_targets:
            raise ManagedArtifactManifestError("managed LoRA conflicts with the worker manifest")
        artifacts.append(
            ModelArtifactSpec(
                logical_name=logical_name,
                kind=ArtifactKind.LORA,
                source_object_id=managed.object_key,
                source_object_version_id=managed.object_version_id,
                sha256=managed.artifact_sha256,
                exact_size_bytes=managed.byte_size,
                max_size_bytes=managed.byte_size,
                target_filename=managed.target_filename,
            )
        )
        baseline_hashes.add(managed.artifact_sha256)
        baseline_names.add(name_key)
        baseline_targets.add(target_key)
        managed_hashes.add(managed.artifact_sha256)
        available_lora_hashes.add(managed.artifact_sha256)

    if required_hashes is not None and not required_hashes.issubset(available_lora_hashes):
        raise ManagedArtifactManifestError(
            "required LoRA is not present in the active catalog or pinned baseline"
        )

    try:
        manifest = create_artifact_manifest(artifacts)
    except (TypeError, ValueError) as error:
        raise ManagedArtifactManifestError("effective worker manifest is invalid") from error
    manifest_json = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return EffectiveArtifactManifest(
        manifest=manifest,
        manifest_json=manifest_json,
        managed_lora_sha256s=frozenset(managed_hashes),
    )


async def active_managed_lora_approval_ids(session: AsyncSession) -> frozenset[UUID]:
    """Return approval IDs that the managed catalog currently permits for new work."""

    values = await session.scalars(
        select(ManagedLoraArtifact.approval_id).where(
            ManagedLoraArtifact.lifecycle == ManagedLoraLifecycle.ACTIVE
        )
    )
    return frozenset(values)
