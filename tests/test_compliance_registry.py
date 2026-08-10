from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from gen_automation.db.models import (
    AdminUser,
    AuditEvent,
    IdempotencyRecord,
    ModelArtifactApproval,
    SubjectApproval,
    WorkflowApproval,
)
from gen_automation.db.session import Database
from gen_automation.domain.compliance_registry import (
    ApprovalEvidence,
    ApprovalRevoke,
    ModelArtifactApprovalCreate,
    SubjectApprovalCreate,
    WorkflowApprovalCreate,
)
from gen_automation.domain.enums import AdminRole, ApprovalStatus, ModelArtifactKind
from gen_automation.services.compliance_registry import (
    ComplianceRegistryConflictError,
    approve_model_artifact,
    approve_subject,
    approve_workflow,
    list_current_approvals,
    revoke_subject,
)

NOW = datetime(2026, 7, 28, 18, 0, tzinfo=UTC)


@pytest.fixture
async def registry_database(tmp_path: Path) -> AsyncIterator[tuple[Database, AdminUser, AdminUser]]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'compliance-registry.db').as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        owner = AdminUser(
            username_normalized="owner@example.test",
            display_name="Owner",
            password_hash="disabled-test-password-hash",  # noqa: S106
            role=AdminRole.OWNER,
            is_active=True,
            failed_login_count=0,
            password_changed_at=NOW,
            credential_version=1,
            lock_version=1,
        )
        reviewer = AdminUser(
            username_normalized="reviewer@example.test",
            display_name="Reviewer",
            password_hash="disabled-test-password-hash",  # noqa: S106
            role=AdminRole.REVIEWER,
            is_active=True,
            failed_login_count=0,
            password_changed_at=NOW,
            credential_version=1,
            lock_version=1,
        )
        session.add_all((owner, reviewer))
        await session.commit()
        owner_id = owner.id
        reviewer_id = reviewer.id
    try:
        async with database.sessions() as session:
            stored_owner = await session.get(AdminUser, owner_id)
            stored_reviewer = await session.get(AdminUser, reviewer_id)
            assert stored_owner is not None
            assert stored_reviewer is not None
            yield database, stored_owner, stored_reviewer
    finally:
        await database.dispose()


def _evidence(summary: str = "Reviewed authoritative age and rights records.") -> ApprovalEvidence:
    return ApprovalEvidence(
        summary=summary,
        source_urls=["https://rights.example.test/record"],
        document_sha256s=["a" * 64],
        internal_reference="LEGAL-2026-001",
    )


def _subject(*, evidence: ApprovalEvidence | None = None) -> SubjectApprovalCreate:
    return SubjectApprovalCreate(
        slug="fictional-adult",
        display_name="Fictional Adult",
        canonical_source_url="https://canon.example.test/characters/adult",
        canonical_age=25,
        clearly_adult=True,
        is_fictional=True,
        is_aged_up_minor=False,
        distribution_rights_approved=True,
        adult_derivative_rights_approved=True,
        evidence=evidence or _evidence(),
    )


def _artifact() -> ModelArtifactApprovalCreate:
    return ModelArtifactApprovalCreate(
        artifact_sha256="b" * 64,
        name="Illustrious checkpoint",
        kind=ModelArtifactKind.CHECKPOINT,
        source_url="https://models.example.test/illustrious",
        storage_key="models/checkpoints/illustrious.safetensors",
        license_url="https://models.example.test/illustrious/license",
        commercial_use_approved=True,
        adult_use_approved=True,
        safetensors_verified=True,
        evidence=_evidence("Reviewed model licence and Safetensors scan."),
    )


def _workflow() -> WorkflowApprovalCreate:
    return WorkflowApprovalCreate(
        workflow_sha256="c" * 64,
        name="Production workflow",
        version="1.0.0",
        object_key="workflows/production-v1.json",
        reviewed_node_classes=["VAEDecode", "KSampler", "CLIPTextEncode"],
        capabilities=["controlled_duo_v2"],
        evidence=_evidence("Reviewed pinned workflow node graph."),
    )


async def test_registry_versions_replays_revokes_and_audits(
    registry_database: tuple[Database, AdminUser, AdminUser],
) -> None:
    database, owner, _reviewer = registry_database
    async with database.sessions() as session:
        first = await approve_subject(
            session,
            command=_subject(),
            actor_user_id=owner.id,
            idempotency_key="subject-create-v1",
            now=NOW,
        )
    assert first.approval_version == 1
    assert first.status == ApprovalStatus.APPROVED
    assert first.replayed is False

    async with database.sessions() as session:
        replay = await approve_subject(
            session,
            command=_subject(),
            actor_user_id=owner.id,
            idempotency_key="subject-create-v1",
            now=NOW,
        )
        reaffirmed = await approve_subject(
            session,
            command=_subject(),
            actor_user_id=owner.id,
            idempotency_key="subject-reaffirm-v1",
            now=NOW,
        )
    assert replay.approval_id == first.approval_id
    assert replay.replayed is True
    assert reaffirmed.approval_id == first.approval_id
    assert reaffirmed.approval_version == 1

    changed_evidence = _evidence("Renewed age and commercial-rights review.")
    async with database.sessions() as session:
        replacement = await approve_subject(
            session,
            command=_subject(evidence=changed_evidence),
            actor_user_id=owner.id,
            idempotency_key="subject-create-v2",
            now=NOW,
        )
    assert replacement.approval_version == 2
    assert replacement.approval_id != first.approval_id

    async with database.sessions() as session:
        history = list(
            (
                await session.scalars(
                    select(SubjectApproval).order_by(SubjectApproval.approval_version)
                )
            ).all()
        )
        assert [row.status for row in history] == [
            ApprovalStatus.REVOKED,
            ApprovalStatus.APPROVED,
        ]
        assert [row.is_current for row in history] == [False, True]
        current = await list_current_approvals(session, registry="subject")
    assert [row.approval_id for row in current] == [replacement.approval_id]

    revocation = ApprovalRevoke(
        expected_approval_version=2,
        reason_code="rights_expired",
        note="Renewal is pending.",
    )
    async with database.sessions() as session:
        revoked = await revoke_subject(
            session,
            approval_id=replacement.approval_id,
            command=revocation,
            actor_user_id=owner.id,
            idempotency_key="subject-revoke-v2",
            now=NOW,
        )
        replayed_revocation = await revoke_subject(
            session,
            approval_id=replacement.approval_id,
            command=revocation,
            actor_user_id=owner.id,
            idempotency_key="subject-revoke-v2",
            now=NOW,
        )
    assert revoked.status == ApprovalStatus.REVOKED
    assert revoked.is_current is False
    assert replayed_revocation.replayed is True

    async with database.sessions() as session:
        assert await list_current_approvals(session, registry="subject") == ()
        actions = list(
            (
                await session.scalars(
                    select(AuditEvent.action).order_by(AuditEvent.occurred_at, AuditEvent.id)
                )
            ).all()
        )
    assert actions.count("compliance.subject.approved") == 2
    assert actions.count("compliance.subject.reaffirmed") == 1
    assert actions.count("compliance.subject.revoked") == 1


async def test_artifact_workflow_and_actor_guards(
    registry_database: tuple[Database, AdminUser, AdminUser],
) -> None:
    database, owner, reviewer = registry_database
    async with database.sessions() as session:
        artifact = await approve_model_artifact(
            session,
            command=_artifact(),
            actor_user_id=owner.id,
            idempotency_key="artifact-create",
            now=NOW,
        )
        workflow = await approve_workflow(
            session,
            command=_workflow(),
            actor_user_id=owner.id,
            idempotency_key="workflow-create",
            now=NOW,
        )
    assert artifact.identity_sha256 == "b" * 64
    assert workflow.identity_sha256 == "c" * 64

    async with database.sessions() as session:
        stored_artifact = await session.get(ModelArtifactApproval, artifact.approval_id)
        stored_workflow = await session.get(WorkflowApproval, workflow.approval_id)
        assert stored_artifact is not None
        assert stored_workflow is not None
        assert stored_artifact.evidence_sha256 == artifact.evidence_sha256
        assert stored_workflow.reviewed_node_classes == [
            "CLIPTextEncode",
            "KSampler",
            "VAEDecode",
        ]
        assert stored_workflow.capabilities == ["controlled_duo_v2"]

    async with database.sessions() as session:
        with pytest.raises(
            ComplianceRegistryConflictError,
            match="active administrator",
        ):
            await approve_subject(
                session,
                command=_subject(),
                actor_user_id=reviewer.id,
                idempotency_key="reviewer-forbidden",
                now=NOW,
            )

    async with database.sessions() as session:
        with pytest.raises(
            ComplianceRegistryConflictError,
            match="another request",
        ):
            await approve_model_artifact(
                session,
                command=_artifact().model_copy(update={"name": "Different name"}),
                actor_user_id=owner.id,
                idempotency_key="artifact-create",
                now=NOW,
            )


def test_registry_commands_fail_closed_on_unsafe_inputs() -> None:
    subject_payload = _subject().model_dump(mode="json")
    subject_payload["is_aged_up_minor"] = True
    with pytest.raises(ValidationError):
        SubjectApprovalCreate.model_validate(subject_payload)

    artifact_payload = _artifact().model_dump(mode="json")
    artifact_payload["storage_key"] = "../checkpoint.safetensors"
    with pytest.raises(ValidationError):
        ModelArtifactApprovalCreate.model_validate(artifact_payload)

    workflow_payload = _workflow().model_dump(mode="json")
    workflow_payload["reviewed_node_classes"] = ["KSampler", "KSampler"]
    with pytest.raises(ValidationError):
        WorkflowApprovalCreate.model_validate(workflow_payload)

    workflow_payload = _workflow().model_dump(mode="json")
    workflow_payload["capabilities"] = ["controlled_duo_v2", "controlled_trio_v1"]
    with pytest.raises(ValidationError, match="cannot declare both"):
        WorkflowApprovalCreate.model_validate(workflow_payload)

    workflow_payload["capabilities"] = ["controlled_trio_v1", "duo_strict_isolation"]
    with pytest.raises(ValidationError, match="requires Controlled Duo v2"):
        WorkflowApprovalCreate.model_validate(workflow_payload)

    workflow_payload["capabilities"] = ["controlled_duo_v2", "duo_high_quality"]
    with pytest.raises(ValidationError, match="high duo quality is not implemented"):
        WorkflowApprovalCreate.model_validate(workflow_payload)

    workflow_payload["capabilities"] = ["controlled_duo_v2"]
    workflow_payload["reviewed_node_classes"] = [
        "ConditioningCombine",
        "ConditioningSetAreaPercentage",
    ]
    with pytest.raises(ValidationError, match="cannot be mixed"):
        WorkflowApprovalCreate.model_validate(workflow_payload)

    with pytest.raises(ValidationError):
        ApprovalEvidence(
            summary="No evidence reference",
            source_urls=[],
            document_sha256s=[],
        )

    with pytest.raises(ValidationError):
        ApprovalEvidence(
            summary="Secret-bearing source URL",
            source_urls=["https://user:secret@rights.example.test/record"],
        )

    whitespace_payload = _subject().model_dump(mode="json")
    whitespace_payload["display_name"] = "   "
    with pytest.raises(ValidationError):
        SubjectApprovalCreate.model_validate(whitespace_payload)

    with pytest.raises(ValidationError):
        ApprovalRevoke(
            expected_approval_version=1,
            reason_code="rights_expired",
            note="   ",
        )


async def test_current_registry_reads_fail_closed_on_tampered_evidence(
    registry_database: tuple[Database, AdminUser, AdminUser],
) -> None:
    database, owner, _reviewer = registry_database
    async with database.sessions() as session:
        await approve_subject(
            session,
            command=_subject(),
            actor_user_id=owner.id,
            idempotency_key="subject-before-tamper",
            now=NOW,
        )

    async with database.sessions() as session:
        stored = await session.scalar(select(SubjectApproval).where(SubjectApproval.is_current))
        assert stored is not None
        stored.evidence = {
            **stored.evidence,
            "summary": "Tampered without updating its evidence digest.",
        }
        await session.commit()

    async with database.sessions() as session:
        with pytest.raises(ComplianceRegistryConflictError, match="integrity"):
            await list_current_approvals(session, registry="subject")


async def test_idempotency_replay_rejects_naive_persisted_timestamp(
    registry_database: tuple[Database, AdminUser, AdminUser],
) -> None:
    database, owner, _reviewer = registry_database
    command = _subject()
    async with database.sessions() as session:
        await approve_subject(
            session,
            command=command,
            actor_user_id=owner.id,
            idempotency_key="subject-invalid-replay",
            now=NOW,
        )

    async with database.sessions() as session:
        record = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.idempotency_key == "subject-invalid-replay"
            )
        )
        assert record is not None
        record.response_body = {
            **record.response_body,
            "approved_at": "2026-07-28T18:00:00",
        }
        await session.commit()

    async with database.sessions() as session:
        with pytest.raises(ComplianceRegistryConflictError, match="idempotency record"):
            await approve_subject(
                session,
                command=command,
                actor_user_id=owner.id,
                idempotency_key="subject-invalid-replay",
                now=NOW,
            )
