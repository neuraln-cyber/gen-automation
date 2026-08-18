import hashlib
import json
import math
import os
import stat
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AdminUser,
    ModelArtifactApproval,
    WorkflowApproval,
)
from gen_automation.domain.artifact_onboarding import (
    ArtifactOnboardingEntry,
    ArtifactOnboardingPlan,
    WorkflowOnboardingEntry,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.compliance_registry import (
    ModelArtifactApprovalCreate,
    WorkflowApprovalCreate,
)
from gen_automation.domain.enums import AdminRole, ModelArtifactKind
from gen_automation.domain.release_spec import GenerationParameters, WorkflowSpecification
from gen_automation.gpu_worker.artifacts import (
    ArtifactBootstrapError,
    ArtifactKind,
    ArtifactManifest,
    ModelArtifactSpec,
    create_artifact_manifest,
    inspect_local_artifact,
)
from gen_automation.gpu_worker.models import (
    DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES,
    validate_approved_workflow,
)
from gen_automation.services.authentication import normalize_username
from gen_automation.services.compliance_registry import (
    ApprovalMutationResult,
    approve_model_artifact,
    approve_workflow,
)
from gen_automation.services.controlled_trio import (
    CONTROLLED_TRIO_MARKER_NODE_CLASS,
    ControlledTrioContractError,
    prepare_controlled_trio_template,
)
from gen_automation.services.worker_inputs import (
    CONTROLLED_DUO_MARKER_NODE_CLASS,
    LORA_CHAIN_NODE_CLASS,
    MAX_WORKFLOW_BYTES,
    WorkerInputError,
    _parse_workflow_template,
    _prepare_controlled_duo_template,
)
from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectConflictError,
    ObjectMetadata,
    ObjectStore,
    ObjectStoreError,
)

MAX_PLAN_BYTES = 256 * 1024
MAX_BASE_MANIFEST_BYTES = 256 * 1024
MAX_PLAN_DEPTH = 32
MAX_PLAN_ITEMS = 4_096
_ONBOARDING_NODE_CLASSES = DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES | {
    CONTROLLED_DUO_MARKER_NODE_CLASS,
    CONTROLLED_TRIO_MARKER_NODE_CLASS,
    LORA_CHAIN_NODE_CLASS,
}


class ArtifactOnboardingError(Exception):
    """Safe, operator-facing onboarding failure."""


@dataclass(frozen=True, slots=True)
class OnboardedWorkflow:
    name: str
    version: str
    model_family: str
    object_key: str
    sha256: str
    reviewed_node_classes: tuple[str, ...]
    capabilities: tuple[str, ...]
    approval: ApprovalMutationResult


@dataclass(frozen=True, slots=True)
class ArtifactOnboardingResult:
    manifest: ArtifactManifest
    artifact_approvals: tuple[ApprovalMutationResult, ...]
    workflows: tuple[OnboardedWorkflow, ...]


@dataclass(frozen=True, slots=True)
class _ValidatedWorkflow:
    entry: WorkflowOnboardingEntry
    body: bytes
    sha256: str
    node_classes: tuple[str, ...]


def parse_onboarding_plan(raw: bytes) -> ArtifactOnboardingPlan:
    if not raw or len(raw) > MAX_PLAN_BYTES:
        raise ArtifactOnboardingError("onboarding plan size is invalid")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        _validate_json_shape(value, counter=[0])
        return ArtifactOnboardingPlan.model_validate(value)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        UnicodeEncodeError,
        ValidationError,
        ValueError,
    ):
        raise ArtifactOnboardingError("onboarding plan is invalid") from None


async def onboard_artifacts(
    session: AsyncSession,
    *,
    plan: ArtifactOnboardingPlan,
    plan_directory: Path,
    artifact_store: ObjectStore,
    workflow_store: ObjectStore,
) -> ArtifactOnboardingResult:
    """Validate inventory, ensure workflow objects, and idempotently approve it."""

    actor = await _active_owner(session, username=plan.owner_username)
    retained_manifest = (
        _load_base_manifest(plan.base_manifest_path, plan_directory=plan_directory)
        if plan.base_manifest_path is not None
        else None
    )
    if retained_manifest is not None:
        for retained_entry in retained_manifest.artifacts:
            await _require_remote_artifact(
                artifact_store,
                retained_entry,
                retained=True,
            )
    manifest_entries: list[ModelArtifactSpec] = []
    for entry in plan.artifacts:
        manifest_entry = _manifest_entry(entry, plan_directory=plan_directory)
        object_version_id = await _require_remote_artifact(artifact_store, manifest_entry)
        try:
            manifest_entry = ModelArtifactSpec.model_validate(
                {
                    **manifest_entry.model_dump(mode="python"),
                    "source_object_version_id": object_version_id,
                },
                strict=True,
            )
        except ValidationError:
            raise ArtifactOnboardingError(
                f"worker artifact {manifest_entry.logical_name!r} has an invalid object version"
            ) from None
        manifest_entries.append(manifest_entry)
    try:
        manifest = _union_artifact_catalog(retained_manifest, manifest_entries)
    except ValidationError:
        raise ArtifactOnboardingError("artifact inventory cannot form a worker manifest") from None

    validated_workflows = tuple(
        _validate_workflow(entry, plan_directory=plan_directory) for entry in plan.workflows
    )
    if any("FaceDetailer" in workflow.node_classes for workflow in validated_workflows) and not any(
        entry.kind == ArtifactKind.DETECTOR for entry in manifest.artifacts
    ):
        raise ArtifactOnboardingError("a FaceDetailer workflow requires one detector artifact")
    for workflow in validated_workflows:
        await _ensure_workflow_object(workflow_store, workflow)

    artifact_approvals: list[ApprovalMutationResult] = []
    for entry, manifest_entry in zip(plan.artifacts, manifest_entries, strict=True):
        semantic_kind = _approval_kind(entry.kind)
        if semantic_kind is None:
            continue
        approval_plan = entry.approval
        if approval_plan is None:
            raise ArtifactOnboardingError("model approval configuration is unavailable")
        model_command = ModelArtifactApprovalCreate(
            artifact_sha256=manifest_entry.sha256,
            name=approval_plan.name,
            kind=semantic_kind,
            model_family=approval_plan.model_family,
            source_url=approval_plan.source_url,
            storage_key=entry.object_key,
            license_url=approval_plan.license_url,
            commercial_use_approved=approval_plan.commercial_use_approved,
            experiment_only=approval_plan.experiment_only,
            adult_use_approved=approval_plan.adult_use_approved,
            safetensors_verified=approval_plan.safetensors_verified,
            evidence=approval_plan.evidence,
        )
        artifact_approvals.append(
            await approve_model_artifact(
                session,
                command=model_command,
                actor_user_id=actor.id,
                idempotency_key=_idempotency_key(
                    action="artifact",
                    actor_id=str(actor.id),
                    command=model_command.model_dump(mode="json"),
                    registry_state=await _model_registry_state(
                        session,
                        sha256=manifest_entry.sha256,
                    ),
                ),
            )
        )

    onboarded_workflows: list[OnboardedWorkflow] = []
    for workflow in validated_workflows:
        workflow_command = WorkflowApprovalCreate(
            workflow_sha256=workflow.sha256,
            name=workflow.entry.name,
            version=workflow.entry.version,
            model_family=workflow.entry.model_family,
            object_key=workflow.entry.object_key,
            reviewed_node_classes=list(workflow.node_classes),
            capabilities=workflow.entry.capabilities,
            evidence=workflow.entry.evidence,
        )
        workflow_approval = await approve_workflow(
            session,
            command=workflow_command,
            actor_user_id=actor.id,
            idempotency_key=_idempotency_key(
                action="workflow",
                actor_id=str(actor.id),
                command=workflow_command.model_dump(mode="json"),
                registry_state=await _workflow_registry_state(
                    session,
                    sha256=workflow.sha256,
                ),
            ),
        )
        onboarded_workflows.append(
            OnboardedWorkflow(
                name=workflow.entry.name,
                version=workflow.entry.version,
                model_family=workflow.entry.model_family.value,
                object_key=workflow.entry.object_key,
                sha256=workflow.sha256,
                reviewed_node_classes=workflow.node_classes,
                capabilities=tuple(str(item) for item in workflow.entry.capabilities),
                approval=workflow_approval,
            )
        )

    return ArtifactOnboardingResult(
        manifest=manifest,
        artifact_approvals=tuple(artifact_approvals),
        workflows=tuple(onboarded_workflows),
    )


def _approval_kind(kind: ArtifactKind) -> ModelArtifactKind | None:
    """Map worker layout roles onto release-selectable compliance roles."""

    if kind in {ArtifactKind.CHECKPOINT, ArtifactKind.DIFFUSION_MODEL}:
        return ModelArtifactKind.CHECKPOINT
    if kind == ArtifactKind.LORA:
        return ModelArtifactKind.LORA
    if kind in {ArtifactKind.DETECTOR, ArtifactKind.TEXT_ENCODER, ArtifactKind.VAE}:
        return None
    raise ArtifactOnboardingError("artifact kind has no compliance-registry mapping")


def _load_base_manifest(
    value: str,
    *,
    plan_directory: Path,
) -> ArtifactManifest:
    path = _resolve_local_path(plan_directory, value)
    body = _read_regular_file(
        path,
        max_bytes=MAX_BASE_MANIFEST_BYTES,
        subject="base artifact manifest",
    )
    try:
        parsed = json.loads(
            body,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        _validate_json_shape(parsed, counter=[0])
        normalized = json.dumps(
            parsed,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return ArtifactManifest.model_validate_json(normalized, strict=True)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        UnicodeEncodeError,
        ValidationError,
        ValueError,
    ):
        raise ArtifactOnboardingError("base artifact manifest is invalid") from None


def _union_artifact_catalog(
    retained_manifest: ArtifactManifest | None,
    new_entries: list[ModelArtifactSpec],
) -> ArtifactManifest:
    retained_entries = list(retained_manifest.artifacts) if retained_manifest is not None else []
    catalog = list(retained_entries)
    by_digest = {entry.sha256: entry for entry in retained_entries}
    by_name = {entry.logical_name.casefold(): entry for entry in retained_entries}
    by_target = {
        (entry.kind, entry.target_filename.casefold()): entry for entry in retained_entries
    }
    by_source = {
        entry.source_object_id: entry
        for entry in retained_entries
        if entry.source_object_id is not None
    }
    if len(by_source) != len(retained_entries):
        raise ArtifactOnboardingError(
            "retained artifact catalog contains a duplicate or non-object source identity"
        )
    for entry in new_entries:
        source_collision = (
            by_source.get(entry.source_object_id) if entry.source_object_id is not None else None
        )
        colliding = {
            candidate
            for candidate in (
                by_digest.get(entry.sha256),
                by_name.get(entry.logical_name.casefold()),
                by_target.get((entry.kind, entry.target_filename.casefold())),
                source_collision,
            )
            if candidate is not None
        }
        if colliding:
            if len(colliding) == 1 and entry in colliding:
                continue
            raise ArtifactOnboardingError(
                f"new worker artifact {entry.logical_name!r} conflicts with the retained catalog"
            )
        catalog.append(entry)
        by_digest[entry.sha256] = entry
        by_name[entry.logical_name.casefold()] = entry
        by_target[(entry.kind, entry.target_filename.casefold())] = entry
        if entry.source_object_id is not None:
            by_source[entry.source_object_id] = entry
    return create_artifact_manifest(catalog)


async def _active_owner(session: AsyncSession, *, username: str | None) -> AdminUser:
    statement = select(AdminUser).where(
        AdminUser.role == AdminRole.OWNER,
        AdminUser.is_active.is_(True),
    )
    if username is not None:
        try:
            normalized_username = normalize_username(username)
        except ValueError:
            raise ArtifactOnboardingError("owner_username is invalid") from None
        statement = statement.where(AdminUser.username_normalized == normalized_username)
    owners = tuple((await session.scalars(statement.order_by(AdminUser.id))).all())
    if not owners:
        raise ArtifactOnboardingError("an active owner account is required")
    if len(owners) != 1:
        raise ArtifactOnboardingError(
            "multiple active owners exist; set owner_username in the onboarding plan"
        )
    return owners[0]


def _manifest_entry(
    entry: ArtifactOnboardingEntry,
    *,
    plan_directory: Path,
) -> ModelArtifactSpec:
    sha256 = entry.sha256
    exact_size_bytes = entry.exact_size_bytes
    if entry.local_path is not None:
        local_path = _resolve_local_path(plan_directory, entry.local_path)
        try:
            inspected = inspect_local_artifact(local_path, kind=entry.kind)
        except ArtifactBootstrapError as error:
            raise ArtifactOnboardingError(
                f"local {entry.kind.value} {entry.logical_name!r} failed validation"
            ) from error
        if sha256 is not None and sha256 != inspected.sha256:
            raise ArtifactOnboardingError(
                f"local {entry.kind.value} {entry.logical_name!r} SHA-256 does not match the plan"
            )
        if exact_size_bytes is not None and exact_size_bytes != inspected.exact_size_bytes:
            raise ArtifactOnboardingError(
                f"local {entry.kind.value} {entry.logical_name!r} size does not match the plan"
            )
        sha256 = inspected.sha256
        exact_size_bytes = inspected.exact_size_bytes
    if sha256 is None or exact_size_bytes is None:
        raise ArtifactOnboardingError("artifact digest inventory is incomplete")
    max_size_bytes = entry.max_size_bytes or exact_size_bytes
    if max_size_bytes < exact_size_bytes:
        raise ArtifactOnboardingError(
            f"{entry.kind.value} {entry.logical_name!r} exceeds its configured maximum size"
        )
    target_filename = entry.target_filename or PurePosixPath(entry.object_key).name
    try:
        return ModelArtifactSpec(
            logical_name=entry.logical_name,
            kind=entry.kind,
            source_object_id=entry.object_key,
            downloader_key=None,
            sha256=sha256,
            exact_size_bytes=exact_size_bytes,
            max_size_bytes=max_size_bytes,
            target_filename=target_filename,
        )
    except ValidationError:
        raise ArtifactOnboardingError(
            f"{entry.kind.value} {entry.logical_name!r} has invalid worker metadata"
        ) from None


async def _require_remote_artifact(
    store: ObjectStore,
    artifact: ModelArtifactSpec,
    *,
    retained: bool = False,
) -> str:
    object_key = artifact.source_object_id
    if object_key is None:
        message = (
            "retained catalog artifacts require an object key"
            if retained
            else "artifact object key is unavailable"
        )
        raise ArtifactOnboardingError(message)
    expected_version_id = artifact.source_object_version_id if retained else None
    if retained and expected_version_id is None:
        raise ArtifactOnboardingError(
            f"retained worker artifact {artifact.logical_name!r} is not version-pinned"
        )
    try:
        metadata = await store.head(object_key, version_id=expected_version_id)
    except ObjectStoreError:
        raise ArtifactOnboardingError(
            f"could not inspect worker artifact {artifact.logical_name!r}"
        ) from None
    if metadata is None:
        state = "retained version is unavailable" if retained else "is not uploaded"
        raise ArtifactOnboardingError(f"worker artifact {artifact.logical_name!r} {state}")
    if (
        metadata.byte_size != artifact.exact_size_bytes
        or metadata.metadata.get("sha256") != artifact.sha256
    ):
        raise ArtifactOnboardingError(
            f"worker artifact {artifact.logical_name!r} size/SHA metadata does not match"
        )
    if (
        metadata.version_id is None
        or not metadata.version_id.strip()
        or metadata.version_id.casefold() == "null"
    ):
        raise ArtifactOnboardingError(
            f"worker artifact {artifact.logical_name!r} is not in versioned storage"
        )
    if retained and metadata.version_id != expected_version_id:
        raise ArtifactOnboardingError(
            f"retained worker artifact {artifact.logical_name!r} changed object version"
        )
    return metadata.version_id


def _validate_workflow(
    entry: WorkflowOnboardingEntry,
    *,
    plan_directory: Path,
) -> _ValidatedWorkflow:
    path = _resolve_local_path(plan_directory, entry.local_path)
    body = _read_regular_file(path, max_bytes=MAX_WORKFLOW_BYTES, subject="workflow")
    try:
        graph = _parse_workflow_template(body)
        validate_approved_workflow(graph, _ONBOARDING_NODE_CLASSES)
        _validate_controlled_composition_onboarding_evidence(graph, entry=entry)
    except (WorkerInputError, ValueError):
        raise ArtifactOnboardingError(f"workflow {entry.name!r} failed graph validation") from None
    sha256 = hashlib.sha256(body).hexdigest()
    if sha256 not in entry.object_key.casefold():
        raise ArtifactOnboardingError(
            f"workflow {entry.name!r} requires a content-addressed object key"
        )
    node_classes = tuple(
        sorted(
            {
                node["class_type"]
                for node in graph.values()
                if isinstance(node, dict)
                and isinstance(node.get("class_type"), str)
                and node.get("class_type")
                not in {
                    CONTROLLED_DUO_MARKER_NODE_CLASS,
                    CONTROLLED_TRIO_MARKER_NODE_CLASS,
                }
            }
        )
    )
    return _ValidatedWorkflow(
        entry=entry,
        body=body,
        sha256=sha256,
        node_classes=node_classes,
    )


def _validate_controlled_composition_onboarding_evidence(
    graph: Mapping[str, object],
    *,
    entry: WorkflowOnboardingEntry,
) -> None:
    duo_markers = [
        node
        for node in graph.values()
        if isinstance(node, Mapping) and node.get("class_type") == CONTROLLED_DUO_MARKER_NODE_CLASS
    ]
    trio_markers = [
        node
        for node in graph.values()
        if isinstance(node, Mapping) and node.get("class_type") == CONTROLLED_TRIO_MARKER_NODE_CLASS
    ]
    capabilities = {str(capability) for capability in entry.capabilities}
    declares_controlled_duo = "controlled_duo_v2" in capabilities
    declares_controlled_trio = "controlled_trio_v1" in capabilities
    if declares_controlled_duo and declares_controlled_trio:
        raise WorkerInputError("controlled composition onboarding evidence is invalid")
    if not declares_controlled_duo:
        if duo_markers or capabilities.intersection({"duo_strict_isolation", "duo_high_quality"}):
            raise WorkerInputError("Controlled Duo onboarding evidence is invalid")
    else:
        if len(duo_markers) != 1 or trio_markers:
            raise WorkerInputError("Controlled Duo onboarding evidence is invalid")
        inputs = duo_markers[0].get("inputs")
        if not isinstance(inputs, Mapping):
            raise WorkerInputError("Controlled Duo onboarding evidence is invalid")
        isolation_mode = inputs.get("isolation_mode")
        if (
            inputs.get("contract_version") != 2
            or inputs.get("mask_topology") != "disjoint_preset_rectangles_v1"
            or isolation_mode not in {"balanced", "strict"}
            or ("duo_strict_isolation" in capabilities) != (isolation_mode == "strict")
            or "duo_high_quality" in capabilities
        ):
            raise WorkerInputError("Controlled Duo onboarding evidence is invalid")
        specification = _controlled_onboarding_workflow_specification(entry)
        generation = _controlled_onboarding_generation(
            composition_mode="duo",
            isolation_mode=str(isolation_mode),
        )
        _prepare_controlled_duo_template(
            deepcopy(dict(graph)),
            specification=specification,
            generation=generation,
        )
        return

    if not declares_controlled_trio:
        if trio_markers:
            raise WorkerInputError("Controlled Trio onboarding evidence is invalid")
        return
    if len(trio_markers) != 1:
        raise WorkerInputError("Controlled Trio onboarding evidence is invalid")
    inputs = trio_markers[0].get("inputs")
    if not isinstance(inputs, Mapping):
        raise WorkerInputError("Controlled Trio onboarding evidence is invalid")
    if (
        inputs.get("contract_version") != 1
        or inputs.get("mask_topology") != "three_disjoint_regions_v1"
        or inputs.get("isolation_mode") != "balanced"
        or capabilities.intersection({"duo_strict_isolation", "duo_high_quality"})
    ):
        raise WorkerInputError("Controlled Trio onboarding evidence is invalid")
    try:
        prepare_controlled_trio_template(
            deepcopy(dict(graph)),
            specification=_controlled_onboarding_workflow_specification(entry),
            generation=_controlled_onboarding_generation(
                composition_mode="trio",
                isolation_mode="balanced",
            ),
        )
    except ControlledTrioContractError:
        raise WorkerInputError("Controlled Trio onboarding evidence is invalid") from None


def _controlled_onboarding_workflow_specification(
    entry: WorkflowOnboardingEntry,
) -> WorkflowSpecification:
    return WorkflowSpecification(
        name="Controlled composition onboarding validation",
        version="1",
        object_key="workflows/onboarding-validation.json",
        sha256="0" * 64,
        capabilities=tuple(entry.capabilities),
    )


def _controlled_onboarding_generation(
    *,
    composition_mode: str,
    isolation_mode: str,
) -> GenerationParameters:
    trio = composition_mode == "trio"
    return GenerationParameters.model_validate(
        {
            "composition_mode": composition_mode,
            "duo_contract_version": 3 if trio else 2,
            "composition_preset_id": "trio_flexible" if trio else "flexible",
            "prompt": "controlled composition topology validation",
            "character_a_prompt": "adult character A",
            "character_b_prompt": "adult character B",
            "character_c_prompt": "adult character C" if trio else "",
            "duo_isolation_mode": isolation_mode,
            "duo_quality_mode": "standard",
            "seed": 1,
            "width": 1024,
            "height": 1024,
            "steps": 20,
            "sampler": "euler",
            "scheduler": "normal",
            "outputs_per_job": 1,
        }
    )


async def _ensure_workflow_object(
    store: ObjectStore,
    workflow: _ValidatedWorkflow,
) -> None:
    try:
        metadata = await store.head(workflow.entry.object_key)
        if metadata is None:
            try:
                metadata = await store.write_bytes_if_absent(
                    key=workflow.entry.object_key,
                    body=workflow.body,
                    content_type="application/json",
                    metadata={
                        "sha256": workflow.sha256,
                        "purpose": "comfy-workflow-template",
                    },
                    max_bytes=MAX_WORKFLOW_BYTES,
                )
            except (ObjectAlreadyExistsError, ObjectConflictError):
                metadata = await store.head(workflow.entry.object_key)
        await _verify_workflow_object(store, workflow, metadata)
    except ObjectStoreError:
        raise ArtifactOnboardingError(
            f"workflow {workflow.entry.name!r} could not be stored or verified"
        ) from None


async def _verify_workflow_object(
    store: ObjectStore,
    workflow: _ValidatedWorkflow,
    metadata: ObjectMetadata | None,
) -> None:
    if (
        metadata is None
        or metadata.byte_size != len(workflow.body)
        or metadata.version_id is None
        or metadata.etag is None
        or metadata.metadata.get("sha256") != workflow.sha256
    ):
        raise ArtifactOnboardingError(
            f"workflow {workflow.entry.name!r} object metadata does not match"
        )
    body = await store.read_bytes(
        workflow.entry.object_key,
        max_bytes=MAX_WORKFLOW_BYTES,
        version_id=metadata.version_id,
        etag=metadata.etag,
    )
    if body != workflow.body or hashlib.sha256(body).hexdigest() != workflow.sha256:
        raise ArtifactOnboardingError(f"workflow {workflow.entry.name!r} object bytes do not match")


def _resolve_local_path(plan_directory: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = plan_directory / path
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ArtifactOnboardingError(f"local file {value!r} is unavailable") from None


def _read_regular_file(path: Path, *, max_bytes: int, subject: str) -> bytes:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise OSError
        if before.st_size <= 0 or before.st_size > max_bytes:
            raise ArtifactOnboardingError(f"{subject} file size is invalid")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise OSError
            with os.fdopen(descriptor, "rb", closefd=True) as file_object:
                descriptor = -1
                body = file_object.read(max_bytes + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except ArtifactOnboardingError:
        raise
    except OSError:
        raise ArtifactOnboardingError(f"{subject} file is unavailable") from None
    if len(body) != before.st_size or len(body) > max_bytes:
        raise ArtifactOnboardingError(f"{subject} file changed while it was read")
    return body


def _idempotency_key(
    *,
    action: str,
    actor_id: str,
    command: dict[str, object],
    registry_state: dict[str, object] | None,
) -> str:
    digest = canonical_sha256(
        {
            "schema": "artifact-onboarding-idempotency/v1",
            "action": action,
            "actor_id": actor_id,
            "command": command,
            "registry_state": registry_state,
        }
    )
    return f"artifact-onboarding:{action}:v1:{digest}"


async def _model_registry_state(
    session: AsyncSession,
    *,
    sha256: str,
) -> dict[str, object] | None:
    approval = await session.scalar(
        select(ModelArtifactApproval)
        .where(ModelArtifactApproval.artifact_sha256 == sha256)
        .order_by(
            ModelArtifactApproval.approval_version.desc(),
            ModelArtifactApproval.id.desc(),
        )
        .limit(1)
    )
    return _registry_state(approval)


async def _workflow_registry_state(
    session: AsyncSession,
    *,
    sha256: str,
) -> dict[str, object] | None:
    approval = await session.scalar(
        select(WorkflowApproval)
        .where(WorkflowApproval.workflow_sha256 == sha256)
        .order_by(
            WorkflowApproval.approval_version.desc(),
            WorkflowApproval.id.desc(),
        )
        .limit(1)
    )
    return _registry_state(approval)


def _registry_state(
    approval: ModelArtifactApproval | WorkflowApproval | None,
) -> dict[str, object] | None:
    if approval is None:
        return None
    if isinstance(approval, ModelArtifactApproval):
        record: dict[str, object] = {
            "adult_use_approved": approval.adult_use_approved,
            "artifact_sha256": approval.artifact_sha256,
            "commercial_use_approved": approval.commercial_use_approved,
            "experiment_only": approval.experiment_only,
            "evidence": approval.evidence,
            "kind": approval.kind.value,
            "model_family": approval.model_family.value,
            "license_url": approval.license_url,
            "name": approval.name,
            "safetensors_verified": approval.safetensors_verified,
            "source_url": approval.source_url,
            "storage_key": approval.storage_key,
        }
    else:
        record = {
            "evidence": approval.evidence,
            "name": approval.name,
            "model_family": approval.model_family.value,
            "object_key": approval.object_key,
            "capabilities": approval.capabilities,
            "reviewed_node_classes": approval.reviewed_node_classes,
            "version": approval.version,
            "workflow_sha256": approval.workflow_sha256,
        }
    return {
        "approval_id": str(approval.id),
        "approval_version": approval.approval_version,
        "evidence_sha256": approval.evidence_sha256,
        "is_current": approval.is_current,
        "record_sha256": canonical_sha256(record),
        "status": approval.status.value,
    }


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON property")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("invalid JSON constant")


def _validate_json_shape(value: object, *, depth: int = 0, counter: list[int]) -> None:
    counter[0] += 1
    if counter[0] > MAX_PLAN_ITEMS or depth > MAX_PLAN_DEPTH:
        raise ValueError("onboarding plan is too complex")
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("onboarding plan contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_shape(item, depth=depth + 1, counter=counter)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 512:
                raise ValueError("onboarding plan contains an invalid key")
            _validate_json_shape(item, depth=depth + 1, counter=counter)
        return
    raise ValueError("onboarding plan contains an invalid value")
