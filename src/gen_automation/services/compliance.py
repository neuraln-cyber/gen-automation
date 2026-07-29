import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    ModelArtifactApproval,
    SubjectApproval,
    WorkflowApproval,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import ApprovalStatus, ModelArtifactKind
from gen_automation.domain.release_spec import (
    ArtifactSpecification,
    ReleaseSpecification,
    SubjectSpecification,
)


class ReleaseApprovalError(ValueError):
    """A frozen release is not backed by current server-owned approvals."""


@dataclass(frozen=True)
class ReleaseApprovalSnapshot:
    checks: dict[str, dict[str, Any]]
    sha256: str


def canonical_source_sha256(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _normalized_label(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _approval_error(kind: str) -> ReleaseApprovalError:
    return ReleaseApprovalError(f"{kind} is not present in the active approval registry")


def _validate_subject(
    specification: SubjectSpecification,
    approval: SubjectApproval | None,
) -> dict[str, Any]:
    source_url = str(specification.canonical_source_url)
    if (
        approval is None
        or approval.status != ApprovalStatus.APPROVED
        or approval.canonical_source_url != source_url
        or approval.canonical_source_sha256 != canonical_source_sha256(source_url)
        or _normalized_label(approval.display_name) != _normalized_label(specification.name)
        or approval.canonical_age != specification.canonical_age
        or not approval.clearly_adult
        or not approval.is_fictional
        or approval.is_aged_up_minor
        or not approval.distribution_rights_approved
        or not approval.adult_derivative_rights_approved
        or approval.evidence_sha256 != canonical_sha256(approval.evidence)
        or approval.revoked_at is not None
        or approval.revoked_by_user_id is not None
    ):
        raise _approval_error("subject")
    return {
        "approval_id": str(approval.id),
        "approval_version": approval.approval_version,
        "approved_by_user_id": str(approval.approved_by_user_id),
        "approved_at": approval.approved_at.isoformat(),
        "canonical_source_sha256": approval.canonical_source_sha256,
        "evidence_sha256": approval.evidence_sha256,
    }


def _validate_artifact(
    specification: ArtifactSpecification,
    approval: ModelArtifactApproval | None,
    *,
    expected_kind: ModelArtifactKind,
) -> dict[str, Any]:
    if (
        approval is None
        or approval.status != ApprovalStatus.APPROVED
        or approval.kind != expected_kind
        or approval.artifact_sha256 != specification.sha256
        or _normalized_label(approval.name) != _normalized_label(specification.name)
        or approval.source_url != str(specification.source_url)
        or approval.storage_key != specification.storage_key
        or approval.license_url != str(specification.license_url)
        or not approval.commercial_use_approved
        or not approval.adult_use_approved
        or not approval.safetensors_verified
        or approval.evidence_sha256 != canonical_sha256(approval.evidence)
        or approval.revoked_at is not None
        or approval.revoked_by_user_id is not None
    ):
        raise _approval_error(expected_kind.value)
    return {
        "approval_id": str(approval.id),
        "approval_version": approval.approval_version,
        "approved_by_user_id": str(approval.approved_by_user_id),
        "approved_at": approval.approved_at.isoformat(),
        "artifact_sha256": approval.artifact_sha256,
        "evidence_sha256": approval.evidence_sha256,
        "kind": approval.kind.value,
    }


def _validate_workflow(
    specification: ReleaseSpecification,
    approval: WorkflowApproval | None,
) -> dict[str, Any]:
    workflow = specification.workflow
    if (
        approval is None
        or approval.status != ApprovalStatus.APPROVED
        or approval.workflow_sha256 != workflow.sha256
        or _normalized_label(approval.name) != _normalized_label(workflow.name)
        or approval.version != workflow.version
        or approval.object_key != workflow.object_key
        or approval.evidence_sha256 != canonical_sha256(approval.evidence)
        or approval.revoked_at is not None
        or approval.revoked_by_user_id is not None
    ):
        raise _approval_error("workflow")
    return {
        "approval_id": str(approval.id),
        "approval_version": approval.approval_version,
        "approved_by_user_id": str(approval.approved_by_user_id),
        "approved_at": approval.approved_at.isoformat(),
        "workflow_sha256": approval.workflow_sha256,
        "evidence_sha256": approval.evidence_sha256,
        "reviewed_node_classes": sorted(approval.reviewed_node_classes),
    }


async def validate_release_approvals(
    session: AsyncSession,
    specification: ReleaseSpecification,
) -> ReleaseApprovalSnapshot:
    """Lock and validate authoritative approvals for one frozen release."""

    subject_urls = [str(subject.canonical_source_url) for subject in specification.subjects]
    subject_hashes = [canonical_source_sha256(url) for url in subject_urls]
    subject_rows = list(
        (
            await session.scalars(
                select(SubjectApproval)
                .where(
                    SubjectApproval.canonical_source_sha256.in_(subject_hashes),
                    SubjectApproval.is_current.is_(True),
                )
                .with_for_update(read=True)
            )
        ).all()
    )
    subjects_by_hash = {approval.canonical_source_sha256: approval for approval in subject_rows}
    subject_evidence = [
        _validate_subject(
            subject,
            subjects_by_hash.get(canonical_source_sha256(str(subject.canonical_source_url))),
        )
        for subject in specification.subjects
    ]

    artifact_hashes = [
        specification.checkpoint.sha256,
        *(lora.sha256 for lora in specification.loras),
    ]
    artifact_rows = list(
        (
            await session.scalars(
                select(ModelArtifactApproval)
                .where(
                    ModelArtifactApproval.artifact_sha256.in_(artifact_hashes),
                    ModelArtifactApproval.is_current.is_(True),
                )
                .with_for_update(read=True)
            )
        ).all()
    )
    artifacts_by_hash = {approval.artifact_sha256: approval for approval in artifact_rows}
    checkpoint_evidence = _validate_artifact(
        specification.checkpoint,
        artifacts_by_hash.get(specification.checkpoint.sha256),
        expected_kind=ModelArtifactKind.CHECKPOINT,
    )
    lora_evidence = [
        _validate_artifact(
            lora,
            artifacts_by_hash.get(lora.sha256),
            expected_kind=ModelArtifactKind.LORA,
        )
        for lora in specification.loras
    ]

    workflow_approval = await session.scalar(
        select(WorkflowApproval)
        .where(
            WorkflowApproval.workflow_sha256 == specification.workflow.sha256,
            WorkflowApproval.is_current.is_(True),
        )
        .with_for_update(read=True)
    )
    workflow_evidence = _validate_workflow(specification, workflow_approval)

    checks: dict[str, dict[str, Any]] = {
        "adult_subject_gate": {
            "gate_version": 1,
            "subjects": subject_evidence,
        },
        "artifact_license_gate": {
            "gate_version": 1,
            "checkpoint": checkpoint_evidence,
            "loras": lora_evidence,
        },
        "workflow_integrity_gate": {
            "gate_version": 1,
            "workflow": workflow_evidence,
        },
    }
    return ReleaseApprovalSnapshot(
        checks=checks,
        sha256=canonical_sha256(checks),
    )
