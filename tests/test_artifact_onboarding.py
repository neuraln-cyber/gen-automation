import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from gen_automation.artifact_onboarding_cli import _validate_output_paths
from gen_automation.db.models import (
    AdminUser,
    ModelArtifactApproval,
    WorkflowApproval,
)
from gen_automation.db.session import Database
from gen_automation.domain.artifact_onboarding import ArtifactOnboardingPlan
from gen_automation.domain.compliance_registry import ApprovalRevoke
from gen_automation.domain.enums import AdminRole
from gen_automation.gpu_worker.artifacts import ArtifactKind
from gen_automation.services.artifact_onboarding import (
    ArtifactOnboardingError,
    onboard_artifacts,
    parse_onboarding_plan,
)
from gen_automation.services.compliance_registry import revoke_model_artifact
from gen_automation.storage.memory import MemoryObjectStore

NOW = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BASE_WORKFLOW_SHA256 = "901a50003bfb9aa17c6117a29fc1232a678dcadc19f70a895fe6edf69ccf3fca"
BASE_WORKFLOW_KEY = f"workflows/by-sha256/{BASE_WORKFLOW_SHA256}.json"


def _safetensors_bytes(label: str) -> bytes:
    header = json.dumps(
        {
            label: {
                "dtype": "F32",
                "shape": [1],
                "data_offsets": [0, 4],
            }
        },
        separators=(",", ":"),
    ).encode()
    return len(header).to_bytes(8, "little") + header + b"\0\0\0\0"


def _detector_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("archive/data.pkl", b"trusted-test-detector")
        archive.writestr("archive/version", b"3")
    return buffer.getvalue()


def _evidence(summary: str) -> dict[str, object]:
    return {
        "summary": summary,
        "source_urls": ["https://rights.example.test/review"],
        "document_sha256s": [],
        "internal_reference": "MODEL-2026-001",
    }


def _plan(
    tmp_path: Path,
    *,
    checkpoint: bytes,
    lora: bytes,
    detector: bytes,
    workflow_path: str = "workflows/illustrious-sdxl-base-v1.json",
) -> ArtifactOnboardingPlan:
    checkpoint_path = tmp_path / "illustrious-v1.safetensors"
    lora_path = tmp_path / "style-lora-v1.safetensors"
    detector_path = tmp_path / "face-yolov8m.pt"
    checkpoint_path.write_bytes(checkpoint)
    lora_path.write_bytes(lora)
    detector_path.write_bytes(detector)
    return ArtifactOnboardingPlan.model_validate(
        {
            "version": "v1",
            "artifacts": [
                {
                    "logical_name": "illustrious-v1",
                    "kind": "checkpoint",
                    "object_key": "worker/checkpoints/illustrious-v1.safetensors",
                    "local_path": str(checkpoint_path),
                    "approval": {
                        "name": "Illustrious v1",
                        "source_url": "https://models.example.test/illustrious-v1",
                        "license_url": "https://models.example.test/illustrious-v1/license",
                        "commercial_use_approved": True,
                        "adult_use_approved": True,
                        "safetensors_verified": True,
                        "evidence": _evidence("Reviewed checkpoint source and license."),
                    },
                },
                {
                    "logical_name": "style-lora-v1",
                    "kind": "lora",
                    "object_key": "worker/loras/style-lora-v1.safetensors",
                    "local_path": str(lora_path),
                    "approval": {
                        "name": "Style LoRA v1",
                        "source_url": "https://models.example.test/style-lora-v1",
                        "license_url": "https://models.example.test/style-lora-v1/license",
                        "commercial_use_approved": True,
                        "adult_use_approved": True,
                        "safetensors_verified": True,
                        "evidence": _evidence("Reviewed LoRA source and license."),
                    },
                },
                {
                    "logical_name": "face-detector-v1",
                    "kind": "detector",
                    "object_key": "worker/detectors/face-yolov8m.pt",
                    "local_path": str(detector_path),
                },
            ],
            "workflows": [
                {
                    "name": "Illustrious base",
                    "version": "1",
                    "object_key": BASE_WORKFLOW_KEY,
                    "local_path": workflow_path,
                    "evidence": _evidence("Reviewed the exact bundled ComfyUI graph."),
                }
            ],
        }
    )


async def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'onboarding.db').as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        session.add(
            AdminUser(
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
        )
        await session.commit()
    return database


def _stage_artifact(
    store: MemoryObjectStore,
    *,
    key: str,
    body: bytes,
) -> None:
    store.put_for_test(
        key,
        body,
        content_type="application/octet-stream",
        metadata={"sha256": hashlib.sha256(body).hexdigest()},
    )


async def test_onboarding_builds_manifest_uploads_workflow_and_replays(
    tmp_path: Path,
) -> None:
    checkpoint = _safetensors_bytes("checkpoint")
    lora = _safetensors_bytes("lora")
    detector = _detector_bytes()
    plan = _plan(
        tmp_path,
        checkpoint=checkpoint,
        lora=lora,
        detector=detector,
    )
    artifacts = MemoryObjectStore(bucket="worker-artifacts")
    workflows = MemoryObjectStore(bucket="workflow-assets")
    for key, body in (
        ("worker/checkpoints/illustrious-v1.safetensors", checkpoint),
        ("worker/loras/style-lora-v1.safetensors", lora),
        ("worker/detectors/face-yolov8m.pt", detector),
    ):
        _stage_artifact(artifacts, key=key, body=body)

    database = await _database(tmp_path)
    try:
        async with database.sessions() as session:
            first = await onboard_artifacts(
                session,
                plan=plan,
                plan_directory=REPOSITORY_ROOT,
                artifact_store=artifacts,
                workflow_store=workflows,
            )
        async with database.sessions() as session:
            reaffirmed = await onboard_artifacts(
                session,
                plan=plan,
                plan_directory=REPOSITORY_ROOT,
                artifact_store=artifacts,
                workflow_store=workflows,
            )
        async with database.sessions() as session:
            replay = await onboard_artifacts(
                session,
                plan=plan,
                plan_directory=REPOSITORY_ROOT,
                artifact_store=artifacts,
                workflow_store=workflows,
            )
            artifact_count = await session.scalar(
                select(func.count()).select_from(ModelArtifactApproval)
            )
            workflow_count = await session.scalar(
                select(func.count()).select_from(WorkflowApproval)
            )
            owner_id = await session.scalar(
                select(AdminUser.id).where(AdminUser.role == AdminRole.OWNER)
            )
        assert owner_id is not None
        async with database.sessions() as session:
            await revoke_model_artifact(
                session,
                approval_id=first.artifact_approvals[0].approval_id,
                command=ApprovalRevoke(
                    expected_approval_version=1,
                    reason_code="operator_test",
                ),
                actor_user_id=owner_id,
                idempotency_key="test-revoke-before-onboarding-rerun",
                now=NOW,
            )
        async with database.sessions() as session:
            recovered = await onboard_artifacts(
                session,
                plan=plan,
                plan_directory=REPOSITORY_ROOT,
                artifact_store=artifacts,
                workflow_store=workflows,
            )
            recovered_artifact_count = await session.scalar(
                select(func.count()).select_from(ModelArtifactApproval)
            )
    finally:
        await database.dispose()

    assert [entry.kind for entry in first.manifest.artifacts] == [
        ArtifactKind.CHECKPOINT,
        ArtifactKind.DETECTOR,
        ArtifactKind.LORA,
    ]
    assert first.manifest == replay.manifest
    assert all(entry.source_object_version_id for entry in first.manifest.artifacts)
    assert artifact_count == 2
    assert workflow_count == 1
    assert all(not approval.replayed for approval in reaffirmed.artifact_approvals)
    assert reaffirmed.workflows[0].approval.replayed is False
    assert all(approval.replayed for approval in replay.artifact_approvals)
    assert replay.workflows[0].approval.replayed is True
    assert recovered.artifact_approvals[0].approval_version == 2
    assert recovered.artifact_approvals[0].is_current is True
    assert recovered_artifact_count == 3
    stored_workflow = workflows.objects[BASE_WORKFLOW_KEY]
    assert (
        stored_workflow.body
        == (REPOSITORY_ROOT / "workflows/illustrious-sdxl-base-v1.json").read_bytes()
    )


async def test_onboarding_rejects_remote_artifact_metadata_mismatch(
    tmp_path: Path,
) -> None:
    checkpoint = _safetensors_bytes("checkpoint")
    lora = _safetensors_bytes("lora")
    detector = _detector_bytes()
    plan = _plan(
        tmp_path,
        checkpoint=checkpoint,
        lora=lora,
        detector=detector,
    )
    artifacts = MemoryObjectStore()
    _stage_artifact(
        artifacts,
        key="worker/checkpoints/illustrious-v1.safetensors",
        body=checkpoint,
    )
    artifacts.put_for_test(
        "worker/loras/style-lora-v1.safetensors",
        lora,
        metadata={"sha256": "0" * 64},
    )
    _stage_artifact(
        artifacts,
        key="worker/detectors/face-yolov8m.pt",
        body=detector,
    )
    database = await _database(tmp_path)
    try:
        async with database.sessions() as session:
            with pytest.raises(ArtifactOnboardingError, match="size/SHA metadata"):
                await onboard_artifacts(
                    session,
                    plan=plan,
                    plan_directory=REPOSITORY_ROOT,
                    artifact_store=artifacts,
                    workflow_store=MemoryObjectStore(),
                )
    finally:
        await database.dispose()


async def test_onboarding_rejects_unapproved_workflow_nodes(tmp_path: Path) -> None:
    invalid_workflow = tmp_path / "unsafe.json"
    invalid_workflow.write_text(
        json.dumps(
            {
                "1": {"class_type": "ExecuteAnything", "inputs": {}},
                "2": {"class_type": "SaveImage", "inputs": {}},
            }
        ),
        encoding="utf-8",
    )
    checkpoint = _safetensors_bytes("checkpoint")
    lora = _safetensors_bytes("lora")
    detector = _detector_bytes()
    plan = _plan(
        tmp_path,
        checkpoint=checkpoint,
        lora=lora,
        detector=detector,
        workflow_path=str(invalid_workflow),
    )
    artifacts = MemoryObjectStore()
    for key, body in (
        ("worker/checkpoints/illustrious-v1.safetensors", checkpoint),
        ("worker/loras/style-lora-v1.safetensors", lora),
        ("worker/detectors/face-yolov8m.pt", detector),
    ):
        _stage_artifact(artifacts, key=key, body=body)
    database = await _database(tmp_path)
    try:
        async with database.sessions() as session:
            with pytest.raises(ArtifactOnboardingError, match="graph validation"):
                await onboard_artifacts(
                    session,
                    plan=plan,
                    plan_directory=REPOSITORY_ROOT,
                    artifact_store=artifacts,
                    workflow_store=MemoryObjectStore(),
                )
    finally:
        await database.dispose()


def test_plan_parser_rejects_duplicate_json_properties() -> None:
    with pytest.raises(ArtifactOnboardingError, match="plan is invalid"):
        parse_onboarding_plan(b'{"version":"v1","version":"v1","artifacts":[]}')


def test_plan_requires_a_checkpoint_and_at_most_one_detector() -> None:
    base = {
        "version": "v1",
        "artifacts": [
            {
                "logical_name": "detector-one",
                "kind": "detector",
                "object_key": "worker/detectors/one.pt",
                "sha256": "1" * 64,
                "exact_size_bytes": 64,
            }
        ],
    }
    with pytest.raises(ValueError, match="checkpoint"):
        ArtifactOnboardingPlan.model_validate(base)

    base["artifacts"].extend(
        [
            {
                "logical_name": "checkpoint",
                "kind": "checkpoint",
                "object_key": "worker/checkpoints/model.safetensors",
                "sha256": "2" * 64,
                "exact_size_bytes": 64,
                "approval": {
                    "name": "Checkpoint",
                    "source_url": "https://models.example.test/model",
                    "license_url": "https://models.example.test/license",
                    "commercial_use_approved": True,
                    "adult_use_approved": True,
                    "safetensors_verified": True,
                    "evidence": _evidence("Reviewed checkpoint."),
                },
            },
            {
                "logical_name": "detector-two",
                "kind": "detector",
                "object_key": "worker/detectors/two.pt",
                "sha256": "3" * 64,
                "exact_size_bytes": 64,
            },
        ]
    )
    with pytest.raises(ValueError, match="at most one detector"):
        ArtifactOnboardingPlan.model_validate(base)


def test_cli_outputs_cannot_overwrite_inputs_or_each_other(tmp_path: Path) -> None:
    plan = _plan(
        tmp_path,
        checkpoint=_safetensors_bytes("checkpoint"),
        lora=_safetensors_bytes("lora"),
        detector=_detector_bytes(),
        workflow_path=str(REPOSITORY_ROOT / "workflows/illustrious-sdxl-base-v1.json"),
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ArtifactOnboardingError, match="overwrite onboarding inputs"):
        _validate_output_paths(
            plan_path=plan_path.resolve(),
            plan=plan,
            plan_directory=tmp_path,
            manifest_path=plan_path.resolve(),
            sha256_path=(tmp_path / "manifest.sha256").resolve(),
        )
    same_output = (tmp_path / "same-output").resolve()
    with pytest.raises(ArtifactOnboardingError, match="different files"):
        _validate_output_paths(
            plan_path=plan_path.resolve(),
            plan=plan,
            plan_directory=tmp_path,
            manifest_path=same_output,
            sha256_path=same_output,
        )
