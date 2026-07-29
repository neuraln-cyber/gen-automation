from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AdminUser,
    ModelArtifactApproval,
    SubjectApproval,
    WorkflowApproval,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    ApprovalStatus,
    ModelArtifactKind,
)
from gen_automation.domain.release_spec import ReleaseSpecification
from gen_automation.services.compliance import canonical_source_sha256


def valid_release_payload() -> dict[str, object]:
    checksum = "a" * 64
    return {
        "slug": "release-one",
        "title": "Release One",
        "desired_accepted_count": 10,
        "specification": {
            "schema_version": 1,
            "subjects": [
                {
                    "name": "Approved Adult Character",
                    "canonical_source_url": "https://example.com/character",
                    "canonical_age": 25,
                    "clearly_adult_approved": True,
                    "adult_approval_evidence": "Official profile states age 25.",
                    "is_real_person": False,
                    "is_aged_up_minor": False,
                }
            ],
            "checkpoint": {
                "name": "Illustrious",
                "source_url": "https://example.com/model",
                "storage_key": "models/illustrious.safetensors",
                "sha256": checksum,
                "license_url": "https://example.com/model-license",
                "commercial_use_approved": True,
                "adult_use_approved": True,
            },
            "loras": [],
            "workflow": {
                "name": "production-v1",
                "version": "1",
                "object_key": "workflows/production-v1.json",
                "sha256": checksum,
            },
            "generation": {
                "prompt": "production test prompt",
                "negative_prompt": "low quality",
                "seed": 1234,
                "width": 1024,
                "height": 1024,
                "steps": 28,
                "sampler": "euler",
                "scheduler": "normal",
                "outputs_per_job": 4,
            },
            "planned_job_count": 3,
        },
    }


async def seed_release_approvals(
    session: AsyncSession,
    payload: dict[str, object],
) -> AdminUser:
    specification = ReleaseSpecification.model_validate(payload["specification"])
    now = datetime.now(UTC)
    user = await session.scalar(
        select(AdminUser).where(AdminUser.username_normalized == "fixture-owner")
    )
    if user is None:
        user = AdminUser(
            username_normalized="fixture-owner",
            display_name="Fixture Owner",
            password_hash="disabled-test-password-hash",  # noqa: S106
            role=AdminRole.OWNER,
            is_active=True,
            failed_login_count=0,
            password_changed_at=now,
            lock_version=1,
        )
        session.add(user)
        await session.flush()

    for subject in specification.subjects:
        source_url = str(subject.canonical_source_url)
        source_sha256 = canonical_source_sha256(source_url)
        existing_subject = await session.scalar(
            select(SubjectApproval).where(
                SubjectApproval.canonical_source_sha256 == source_sha256,
                SubjectApproval.is_current.is_(True),
            )
        )
        if existing_subject is None:
            evidence = {
                "source": source_url,
                "adult_character_evidence": subject.adult_approval_evidence,
                "rights_record": "fixture-rights-record",
            }
            session.add(
                SubjectApproval(
                    slug="approved-adult-character",
                    display_name=subject.name,
                    canonical_source_url=source_url,
                    canonical_source_sha256=source_sha256,
                    canonical_age=subject.canonical_age,
                    clearly_adult=True,
                    is_fictional=True,
                    is_aged_up_minor=False,
                    distribution_rights_approved=True,
                    adult_derivative_rights_approved=True,
                    evidence=evidence,
                    evidence_sha256=canonical_sha256(evidence),
                    status=ApprovalStatus.APPROVED,
                    is_current=True,
                    approval_version=1,
                    approved_by_user_id=user.id,
                    approved_at=now,
                )
            )

    artifacts = [
        (specification.checkpoint, ModelArtifactKind.CHECKPOINT),
        *((lora, ModelArtifactKind.LORA) for lora in specification.loras),
    ]
    for artifact, kind in artifacts:
        existing_artifact = await session.scalar(
            select(ModelArtifactApproval).where(
                ModelArtifactApproval.artifact_sha256 == artifact.sha256,
                ModelArtifactApproval.is_current.is_(True),
            )
        )
        if existing_artifact is None:
            evidence = {
                "license": str(artifact.license_url),
                "rights_record": "fixture-model-rights-record",
            }
            session.add(
                ModelArtifactApproval(
                    artifact_sha256=artifact.sha256,
                    name=artifact.name,
                    kind=kind,
                    source_url=str(artifact.source_url),
                    storage_key=artifact.storage_key,
                    license_url=str(artifact.license_url),
                    commercial_use_approved=True,
                    adult_use_approved=True,
                    safetensors_verified=True,
                    evidence=evidence,
                    evidence_sha256=canonical_sha256(evidence),
                    status=ApprovalStatus.APPROVED,
                    is_current=True,
                    approval_version=1,
                    approved_by_user_id=user.id,
                    approved_at=now,
                )
            )

    workflow = specification.workflow
    existing_workflow = await session.scalar(
        select(WorkflowApproval).where(
            WorkflowApproval.workflow_sha256 == workflow.sha256,
            WorkflowApproval.is_current.is_(True),
        )
    )
    if existing_workflow is None:
        evidence = {
            "review": "fixture-reviewed-workflow",
            "object_key": workflow.object_key,
        }
        session.add(
            WorkflowApproval(
                workflow_sha256=workflow.sha256,
                name=workflow.name,
                version=workflow.version,
                object_key=workflow.object_key,
                reviewed_node_classes=["CheckpointLoaderSimple", "KSampler", "SaveImage"],
                evidence=evidence,
                evidence_sha256=canonical_sha256(evidence),
                status=ApprovalStatus.APPROVED,
                is_current=True,
                approval_version=1,
                approved_by_user_id=user.id,
                approved_at=now,
            )
        )

    await session.commit()
    return user
