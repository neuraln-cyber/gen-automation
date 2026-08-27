import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.config import Settings
from gen_automation.db.models import (
    AdminUser,
    ModelArtifactApproval,
    Release,
    ReleaseVersion,
    WorkflowApproval,
)
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    ApprovalStatus,
    GenerationModelFamily,
    ModelArtifactKind,
    ReleasePhase,
)
from gen_automation.domain.release_spec import ReleaseSpecification
from gen_automation.services.compliance import validate_release_approvals
from gen_automation.services.new_sets import (
    NewSetInputError,
    NewSetLoraSelection,
    NewSetSubmission,
    create_and_approve_new_set,
    list_new_set_options,
)
from gen_automation.services.publication import (
    _require_current_compliance_approvals,
    _require_current_publishable_release,
)
from tests.factories import seed_release_approvals, valid_release_payload


async def _seed_anima_approvals(
    session: AsyncSession,
    *,
    owner: AdminUser,
    checkpoint_name: str = "MiaoMiao Anima Base",
    workflow_name: str = "Anima Base",
) -> tuple[ModelArtifactApproval, ModelArtifactApproval, WorkflowApproval]:
    now = datetime.now(UTC)
    artifact_evidence = {"review": "anima experiment approval"}
    workflow_evidence = {"review": "anima workflow approval"}
    checkpoint = ModelArtifactApproval(
        artifact_sha256="d" * 64,
        name=checkpoint_name,
        kind=ModelArtifactKind.CHECKPOINT,
        model_family=GenerationModelFamily.ANIMA,
        source_url="https://models.example.test/miaomiao-anima",
        storage_key="models/anima/miaomiao-base.safetensors",
        license_url="https://models.example.test/miaomiao-anima/license",
        commercial_use_approved=False,
        experiment_only=True,
        adult_use_approved=True,
        safetensors_verified=True,
        evidence=artifact_evidence,
        evidence_sha256=canonical_sha256(artifact_evidence),
        status=ApprovalStatus.APPROVED,
        is_current=True,
        approval_version=1,
        approved_by_user_id=owner.id,
        approved_at=now,
    )
    lora_evidence = {"review": "anima lora experiment approval"}
    lora = ModelArtifactApproval(
        artifact_sha256="e" * 64,
        name="748cm Anima",
        kind=ModelArtifactKind.LORA,
        model_family=GenerationModelFamily.ANIMA,
        source_url="https://models.example.test/748cm-anima",
        storage_key="models/anima/748cm.safetensors",
        license_url="https://models.example.test/748cm-anima/license",
        commercial_use_approved=False,
        experiment_only=True,
        adult_use_approved=True,
        safetensors_verified=True,
        evidence=lora_evidence,
        evidence_sha256=canonical_sha256(lora_evidence),
        status=ApprovalStatus.APPROVED,
        is_current=True,
        approval_version=1,
        approved_by_user_id=owner.id,
        approved_at=now,
    )
    workflow = WorkflowApproval(
        workflow_sha256="f" * 64,
        name=workflow_name,
        version="1",
        model_family=GenerationModelFamily.ANIMA,
        object_key="workflows/anima-base.json",
        reviewed_node_classes=[
            "CLIPLoader",
            "CLIPTextEncode",
            "EmptyLatentImage",
            "KSampler",
            "SaveImage",
            "UNETLoader",
            "VAEDecode",
            "VAELoader",
        ],
        capabilities=[],
        evidence=workflow_evidence,
        evidence_sha256=canonical_sha256(workflow_evidence),
        status=ApprovalStatus.APPROVED,
        is_current=True,
        approval_version=1,
        approved_by_user_id=owner.id,
        approved_at=now,
    )
    session.add_all((checkpoint, lora, workflow))
    await session.commit()
    return checkpoint, lora, workflow


def _selected_option_value(page: str, select_name: str) -> str:
    select_match = re.search(
        rf'<select\b[^>]*\bname="{re.escape(select_name)}"[^>]*>(.*?)</select>',
        page,
        flags=re.DOTALL,
    )
    assert select_match is not None
    selected_match = re.search(
        r'<option\b(?=[^>]*\bselected\b)[^>]*\bvalue="([^"]*)"',
        select_match.group(1),
        flags=re.DOTALL,
    )
    assert selected_match is not None
    return selected_match.group(1)


def _command(
    *,
    subject_id: UUID,
    checkpoint_id: UUID,
    workflow_id: UUID,
    lora_id: UUID | None = None,
    slug: str,
) -> NewSetSubmission:
    return NewSetSubmission(
        slug=slug,
        title=slug.replace("-", " ").title(),
        subject_approval_id=subject_id,
        checkpoint_approval_id=checkpoint_id,
        workflow_approval_id=workflow_id,
        loras=(
            (NewSetLoraSelection(approval_id=lora_id, weight=0.5),) if lora_id is not None else ()
        ),
        prompt="masterpiece, best quality, score_7, safe, 1girl, fully clothed",
        negative_prompt="worst quality, low quality, nsfw, nude",
        seed=1234,
        width=896,
        height=1152,
        cfg=4.5,
        steps=30,
        sampler="euler",
        scheduler="normal",
        outputs_per_job=1,
        planned_job_count=1,
        desired_accepted_count=1,
    )


@pytest.mark.asyncio
async def test_all_model_families_are_available_in_new_set(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'families.db').as_posix()}")
    await database.create_schema()
    try:
        async with database.sessions() as session:
            payload = valid_release_payload()
            owner = await seed_release_approvals(session, payload)
            legacy_specification = ReleaseSpecification.model_validate(payload["specification"])
            legacy_frozen = payload["specification"]
            assert isinstance(legacy_frozen, dict)
            legacy_version = ReleaseVersion(
                id=uuid4(),
                release_id=uuid4(),
                version_no=1,
                specification=legacy_frozen,
                specification_sha256=canonical_sha256(legacy_frozen),
                created_by="legacy-test",
                created_at=datetime.now(UTC),
            )
            await _require_current_compliance_approvals(session, legacy_version)
            legacy_snapshot = await validate_release_approvals(
                session,
                legacy_specification,
            )
            checkpoint, lora, workflow = await _seed_anima_approvals(session, owner=owner)
            normal = await list_new_set_options(session)
            experiment = await list_new_set_options(session, experiment_mode=True)

        artifact_gate = legacy_snapshot.checks["artifact_license_gate"]
        assert "experiment_only" not in artifact_gate
        assert "model_family" not in artifact_gate
        assert "experiment_only" not in artifact_gate["checkpoint"]
        assert "model_family" not in artifact_gate["checkpoint"]
        assert "model_family" not in legacy_snapshot.checks["workflow_integrity_gate"]["workflow"]
        assert {option.model_family for option in normal.checkpoints} == {
            GenerationModelFamily.ILLUSTRIOUS,
            GenerationModelFamily.ANIMA,
        }
        assert GenerationModelFamily.ANIMA in normal.model_families
        assert {option.model_family for option in experiment.checkpoints} == {
            GenerationModelFamily.ILLUSTRIOUS,
            GenerationModelFamily.ANIMA,
        }
        assert GenerationModelFamily.ANIMA in experiment.model_families
        illustrious_checkpoint = next(
            option
            for option in normal.checkpoints
            if option.model_family == GenerationModelFamily.ILLUSTRIOUS
        )
        illustrious_workflow = next(
            option
            for option in normal.workflows
            if option.model_family == GenerationModelFamily.ILLUSTRIOUS
        )

        subject_id = normal.subjects[0].approval_id
        async with database.sessions() as session:
            normal_result = await create_and_approve_new_set(
                session,
                command=_command(
                    subject_id=subject_id,
                    checkpoint_id=checkpoint.id,
                    workflow_id=workflow.id,
                    slug="normal-anima-allowed",
                ),
                idempotency_key="normal-anima-allowed",
                settings=Settings(),
                actor="fixture-owner",
            )
        assert normal_result.release.slug == "normal-anima-allowed"

        async with database.sessions() as session:
            with pytest.raises(NewSetInputError, match="same model family"):
                await create_and_approve_new_set(
                    session,
                    command=_command(
                        subject_id=subject_id,
                        checkpoint_id=checkpoint.id,
                        workflow_id=illustrious_workflow.approval_id,
                        slug="mixed-family-blocked",
                    ),
                    idempotency_key="mixed-family-blocked",
                    settings=Settings(),
                    actor="fixture-owner",
                    experiment_mode=True,
                )

        async with database.sessions() as session:
            with pytest.raises(NewSetInputError, match="same model family"):
                await create_and_approve_new_set(
                    session,
                    command=_command(
                        subject_id=subject_id,
                        checkpoint_id=illustrious_checkpoint.approval_id,
                        workflow_id=illustrious_workflow.approval_id,
                        lora_id=lora.id,
                        slug="mixed-lora-family-blocked",
                    ),
                    idempotency_key="mixed-lora-family-blocked",
                    settings=Settings(),
                    actor="fixture-owner",
                    experiment_mode=True,
                )

        async with database.sessions() as session:
            result = await create_and_approve_new_set(
                session,
                command=_command(
                    subject_id=subject_id,
                    checkpoint_id=checkpoint.id,
                    workflow_id=workflow.id,
                    lora_id=lora.id,
                    slug="anima-experiment",
                ),
                idempotency_key="anima-experiment-create",
                settings=Settings(),
                actor="fixture-owner",
                experiment_mode=True,
            )
        assert result.release.slug == "anima-experiment"
        async with database.sessions() as session:
            version = await session.scalar(
                select(ReleaseVersion).where(ReleaseVersion.release_id == result.release.id)
            )
            assert version is not None
            specification = ReleaseSpecification.model_validate(version.specification)
            assert specification.experiment_only is True
            assert specification.checkpoint.commercial_use_approved is False
            assert specification.checkpoint.experiment_only is True
            assert specification.loras[0].experiment_only is True
    finally:
        await database.dispose()


def test_shared_new_set_page_exposes_anima_in_normal_and_experiment_modes(
    client: TestClient,
) -> None:
    database = client.app.state.database
    assert client.portal is not None

    async def seed() -> None:
        async with database.sessions() as session:
            owner = await seed_release_approvals(session, valid_release_payload())
            await _seed_anima_approvals(session, owner=owner)

    client.portal.call(seed)
    normal = client.get("/dashboard/new-set")
    experiment = client.get("/dashboard/experiments/new")
    script = client.get("/static/dashboard.js")

    assert normal.status_code == 200
    assert experiment.status_code == 200
    assert script.status_code == 200
    assert "MiaoMiao Anima Base" in normal.text
    assert "data-model-family-picker" in normal.text
    assert 'value="anima"' in normal.text
    assert 'data-model-family="anima"' in normal.text
    assert "Anima is available here" in normal.text
    assert "MiaoMiao Anima Base" in experiment.text
    assert 'value="anima"' in experiment.text
    assert 'data-model-family="anima"' in experiment.text
    assert "explicit warm-session controls" in experiment.text
    assert "initializeModelFamilyPicker" in script.text
    assert "gen-automation:model-family-changed" in script.text
    assert 'width: "896"' in script.text
    assert 'height: "1152"' in script.text
    assert 'cfg: "4.5"' in script.text
    assert 'sampler: "euler"' in script.text
    assert 'scheduler: "normal"' in script.text
    assert 'form.dataset.applyingAutomationProfile !== "true"' in script.text
    assert "control.readOnly = ignored" in script.text
    assert "Not used by the Anima workflow." in script.text


def test_fresh_experiment_form_prefers_a_coherent_illustrious_pair(
    client: TestClient,
) -> None:
    database = client.app.state.database
    assert client.portal is not None

    async def seed() -> tuple[str, str]:
        async with database.sessions() as session:
            owner = await seed_release_approvals(session, valid_release_payload())
            anima_checkpoint, _, anima_workflow = await _seed_anima_approvals(
                session,
                owner=owner,
                checkpoint_name="AAA Anima Base",
                workflow_name="AAA Anima Workflow",
            )
            options = await list_new_set_options(session, experiment_mode=True)
            assert options.checkpoints[0].approval_id == anima_checkpoint.id
            assert options.workflows[0].approval_id == anima_workflow.id
            illustrious_checkpoint = next(
                option
                for option in options.checkpoints
                if option.model_family == GenerationModelFamily.ILLUSTRIOUS
            )
            illustrious_workflow = next(
                option
                for option in options.workflows
                if option.model_family == GenerationModelFamily.ILLUSTRIOUS
            )
            return (
                str(illustrious_checkpoint.approval_id),
                str(illustrious_workflow.approval_id),
            )

    illustrious_checkpoint_id, illustrious_workflow_id = client.portal.call(seed)
    page = client.get("/dashboard/experiments/new")

    assert page.status_code == 200
    assert _selected_option_value(page.text, "checkpoint_id") == illustrious_checkpoint_id
    assert _selected_option_value(page.text, "workflow_id") == illustrious_workflow_id


def test_experiment_only_release_publication_is_owner_controlled() -> None:
    payload = valid_release_payload()
    raw_specification = payload["specification"]
    assert isinstance(raw_specification, dict)
    checkpoint = raw_specification["checkpoint"]
    assert isinstance(checkpoint, dict)
    checkpoint["commercial_use_approved"] = False
    checkpoint["experiment_only"] = True
    specification = ReleaseSpecification.model_validate(raw_specification)
    frozen = specification.model_dump(mode="json")
    release_id = uuid4()
    release = Release(
        id=release_id,
        project_id=uuid4(),
        slug="anima-evaluation",
        title="Anima evaluation",
        phase=ReleasePhase.READY_TO_PUBLISH,
        current_version_no=1,
        desired_accepted_count=1,
        lock_version=1,
    )
    version = ReleaseVersion(
        id=uuid4(),
        release_id=release_id,
        version_no=1,
        specification=frozen,
        specification_sha256=canonical_sha256(frozen),
        created_by="test",
        created_at=datetime.now(UTC),
    )

    _require_current_publishable_release(release, version)
