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
from gen_automation.domain.canonical import canonical_json_bytes
from gen_automation.domain.compliance_registry import ApprovalRevoke
from gen_automation.domain.enums import (
    AdminRole,
    GenerationModelFamily,
    ModelArtifactKind,
)
from gen_automation.gpu_worker.artifacts import (
    ArtifactKind,
    ModelArtifactSpec,
    create_artifact_manifest,
)
from gen_automation.services.artifact_onboarding import (
    ArtifactOnboardingError,
    _load_base_manifest,
    _require_remote_artifact,
    _union_artifact_catalog,
    onboard_artifacts,
    parse_onboarding_plan,
)
from gen_automation.services.compliance_registry import revoke_model_artifact
from gen_automation.storage.memory import MemoryObjectStore

NOW = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BASE_WORKFLOW_SHA256 = "1d099e8ed6a73ddf30cce4b8a5970aa17de16377fd248f5a654a32f65fba9834"
BASE_WORKFLOW_KEY = f"workflows/by-sha256/{BASE_WORKFLOW_SHA256}.json"
ANIMA_WORKFLOW_PATH = REPOSITORY_ROOT / "workflows/anima-base-v1.json"
ANIMA_WORKFLOW_SHA256 = hashlib.sha256(ANIMA_WORKFLOW_PATH.read_bytes()).hexdigest()
ANIMA_WORKFLOW_KEY = f"workflows/by-sha256/{ANIMA_WORKFLOW_SHA256}.json"


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
                        "model_family": "illustrious",
                        "source_url": "https://models.example.test/illustrious-v1",
                        "license_url": "https://models.example.test/illustrious-v1/license",
                        "commercial_use_approved": True,
                        "experiment_only": False,
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
                        "model_family": "illustrious",
                        "source_url": "https://models.example.test/style-lora-v1",
                        "license_url": "https://models.example.test/style-lora-v1/license",
                        "commercial_use_approved": True,
                        "experiment_only": False,
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
                    "model_family": "illustrious",
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


def _catalog_artifact(
    *,
    logical_name: str,
    kind: ArtifactKind,
    key: str,
    filename: str,
    body: bytes,
    version_id: str,
    sha256: str | None = None,
) -> ModelArtifactSpec:
    digest = sha256 or hashlib.sha256(body).hexdigest()
    return ModelArtifactSpec(
        logical_name=logical_name,
        kind=kind,
        source_object_id=key,
        source_object_version_id=version_id,
        downloader_key=None,
        sha256=digest,
        exact_size_bytes=len(body),
        max_size_bytes=len(body),
        target_filename=filename,
    )


def _write_catalog(path: Path, *entries: ModelArtifactSpec) -> None:
    path.write_bytes(canonical_json_bytes(create_artifact_manifest(entries)) + b"\n")


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
                now=first.artifact_approvals[0].approved_at,
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


async def test_onboarding_adds_to_verified_pinned_catalog(tmp_path: Path) -> None:
    retained = _safetensors_bytes("retained-checkpoint")
    checkpoint = _safetensors_bytes("new-checkpoint")
    lora = _safetensors_bytes("new-lora")
    detector = _detector_bytes()
    artifacts = MemoryObjectStore(bucket="worker-artifacts")
    retained_key = "worker/checkpoints/retained-v1.safetensors"
    _stage_artifact(artifacts, key=retained_key, body=retained)
    retained_version = artifacts.objects[retained_key].version_id
    retained_entry = _catalog_artifact(
        logical_name="retained-v1",
        kind=ArtifactKind.CHECKPOINT,
        key=retained_key,
        filename="retained-v1.safetensors",
        body=retained,
        version_id=retained_version,
    )
    base_manifest_path = tmp_path / "current-deployed-artifact-manifest.json"
    _write_catalog(base_manifest_path, retained_entry)
    plan = _plan(
        tmp_path,
        checkpoint=checkpoint,
        lora=lora,
        detector=detector,
    ).model_copy(update={"base_manifest_path": str(base_manifest_path)})
    for key, body in (
        ("worker/checkpoints/illustrious-v1.safetensors", checkpoint),
        ("worker/loras/style-lora-v1.safetensors", lora),
        ("worker/detectors/face-yolov8m.pt", detector),
    ):
        _stage_artifact(artifacts, key=key, body=body)

    database = await _database(tmp_path)
    try:
        async with database.sessions() as session:
            result = await onboard_artifacts(
                session,
                plan=plan,
                plan_directory=REPOSITORY_ROOT,
                artifact_store=artifacts,
                workflow_store=MemoryObjectStore(bucket="workflow-assets"),
            )
    finally:
        await database.dispose()

    assert len(result.manifest.artifacts) == 4
    retained_result = next(
        entry for entry in result.manifest.artifacts if entry.logical_name == "retained-v1"
    )
    assert retained_result == retained_entry
    assert result.manifest == create_artifact_manifest(
        (
            retained_entry,
            *tuple(entry for entry in result.manifest.artifacts if entry != retained_entry),
        )
    )


async def test_onboarding_rejects_unverifiable_retained_catalog_entry(tmp_path: Path) -> None:
    retained = _safetensors_bytes("retained-checkpoint")
    checkpoint = _safetensors_bytes("new-checkpoint")
    lora = _safetensors_bytes("new-lora")
    detector = _detector_bytes()
    artifacts = MemoryObjectStore(bucket="worker-artifacts")
    retained_key = "worker/checkpoints/retained-v1.safetensors"
    _stage_artifact(artifacts, key=retained_key, body=retained)
    retained_entry = _catalog_artifact(
        logical_name="retained-v1",
        kind=ArtifactKind.CHECKPOINT,
        key=retained_key,
        filename="retained-v1.safetensors",
        body=retained,
        version_id="unavailable-version",
    )
    base_manifest_path = tmp_path / "current-deployed-artifact-manifest.json"
    _write_catalog(base_manifest_path, retained_entry)
    plan = _plan(
        tmp_path,
        checkpoint=checkpoint,
        lora=lora,
        detector=detector,
    ).model_copy(update={"base_manifest_path": str(base_manifest_path)})

    database = await _database(tmp_path)
    try:
        async with database.sessions() as session:
            with pytest.raises(ArtifactOnboardingError, match="retained version is unavailable"):
                await onboard_artifacts(
                    session,
                    plan=plan,
                    plan_directory=tmp_path,
                    artifact_store=artifacts,
                    workflow_store=MemoryObjectStore(bucket="workflow-assets"),
                )
    finally:
        await database.dispose()


async def test_retained_catalog_rechecks_exact_remote_metadata() -> None:
    body = _safetensors_bytes("retained-checkpoint")
    store = MemoryObjectStore(bucket="worker-artifacts")
    key = "worker/checkpoints/retained-v1.safetensors"
    _stage_artifact(store, key=key, body=body)
    entry = _catalog_artifact(
        logical_name="retained-v1",
        kind=ArtifactKind.CHECKPOINT,
        key=key,
        filename="retained-v1.safetensors",
        body=body,
        version_id=store.objects[key].version_id,
        sha256="f" * 64,
    )
    with pytest.raises(ArtifactOnboardingError, match="size/SHA metadata"):
        await _require_remote_artifact(store, entry, retained=True)


def test_catalog_union_deduplicates_exact_entries_and_rejects_conflicts() -> None:
    body = _safetensors_bytes("retained")
    retained = _catalog_artifact(
        logical_name="retained-model",
        kind=ArtifactKind.CHECKPOINT,
        key="worker/checkpoints/retained.safetensors",
        filename="retained-model.safetensors",
        body=body,
        version_id="retained-version",
    )
    base = create_artifact_manifest((retained,))
    assert _union_artifact_catalog(base, [retained]) == base

    additions = (
        retained.model_copy(
            update={
                "logical_name": "addition-a",
                "source_object_id": "worker/checkpoints/addition-a.safetensors",
                "source_object_version_id": "addition-a-version",
                "sha256": "1" * 64,
                "target_filename": "addition-a.safetensors",
            }
        ),
        retained.model_copy(
            update={
                "logical_name": "addition-b",
                "source_object_id": "worker/checkpoints/addition-b.safetensors",
                "source_object_version_id": "addition-b-version",
                "sha256": "2" * 64,
                "target_filename": "addition-b.safetensors",
            }
        ),
    )
    assert _union_artifact_catalog(base, list(additions)) == _union_artifact_catalog(
        base,
        list(reversed(additions)),
    )

    conflicts = (
        retained.model_copy(
            update={
                "logical_name": "different-name",
                "source_object_id": "worker/checkpoints/different-name.safetensors",
                "target_filename": "different-name.safetensors",
            }
        ),
        retained.model_copy(
            update={
                "sha256": "a" * 64,
                "source_object_id": "worker/checkpoints/name-conflict.safetensors",
                "target_filename": "name-conflict.safetensors",
            }
        ),
        retained.model_copy(
            update={
                "sha256": "b" * 64,
                "logical_name": "target-conflict",
                "source_object_id": "worker/checkpoints/target-conflict.safetensors",
            }
        ),
        retained.model_copy(
            update={
                "sha256": "c" * 64,
                "logical_name": "source-conflict",
                "target_filename": "source-conflict.safetensors",
            }
        ),
    )
    for conflict in conflicts:
        with pytest.raises(ArtifactOnboardingError, match="retained catalog"):
            _union_artifact_catalog(base, [conflict])

    duplicate_source = retained.model_copy(
        update={
            "logical_name": "second-retained-model",
            "source_object_version_id": "second-version",
            "sha256": "d" * 64,
            "target_filename": "second-retained-model.safetensors",
        }
    )
    ambiguous_base = create_artifact_manifest((retained, duplicate_source))
    with pytest.raises(ArtifactOnboardingError, match="source identity"):
        _union_artifact_catalog(ambiguous_base, [])


def test_base_catalog_loader_rejects_noncanonical_or_duplicate_json(tmp_path: Path) -> None:
    path = tmp_path / "base-manifest.json"
    path.write_text(
        '{"version":"v1","version":"v1","artifacts":[],"manifest_sha256":"' + ("0" * 64) + '"}',
        encoding="utf-8",
    )
    with pytest.raises(ArtifactOnboardingError, match="base artifact manifest is invalid"):
        _load_base_manifest(str(path), plan_directory=tmp_path)


async def test_onboarding_maps_anima_layout_to_selectable_approvals(
    tmp_path: Path,
) -> None:
    diffusion_model = _safetensors_bytes("anima-diffusion-model")
    text_encoder = _safetensors_bytes("anima-text-encoder")
    vae = _safetensors_bytes("anima-vae")
    lora = _safetensors_bytes("anima-lora")
    local_artifacts = {
        "worker/diffusion-models/miaomiao-anima-base.safetensors": (
            tmp_path / "miaomiao-anima-base.safetensors",
            diffusion_model,
        ),
        "worker/text-encoders/qwen-3-06b-base.safetensors": (
            tmp_path / "qwen-3-06b-base.safetensors",
            text_encoder,
        ),
        "worker/vae/qwen-image-vae.safetensors": (
            tmp_path / "qwen-image-vae.safetensors",
            vae,
        ),
        "worker/loras/748cm-anima.safetensors": (
            tmp_path / "748cm-anima.safetensors",
            lora,
        ),
    }
    for path, body in local_artifacts.values():
        path.write_bytes(body)

    illustrious = _safetensors_bytes("retained-illustrious-checkpoint")
    illustrious_key = "worker/checkpoints/retained-illustrious.safetensors"
    artifacts = MemoryObjectStore(bucket="worker-artifacts")
    _stage_artifact(artifacts, key=illustrious_key, body=illustrious)
    illustrious_entry = _catalog_artifact(
        logical_name="retained-illustrious",
        kind=ArtifactKind.CHECKPOINT,
        key=illustrious_key,
        filename="retained-illustrious.safetensors",
        body=illustrious,
        version_id=artifacts.objects[illustrious_key].version_id,
    )
    base_manifest_path = tmp_path / "current-deployed-artifact-manifest.json"
    _write_catalog(base_manifest_path, illustrious_entry)

    plan = ArtifactOnboardingPlan.model_validate(
        {
            "version": "v1",
            "base_manifest_path": str(base_manifest_path),
            "artifacts": [
                {
                    "logical_name": "miaomiao-anima-base",
                    "kind": "diffusion_model",
                    "object_key": "worker/diffusion-models/miaomiao-anima-base.safetensors",
                    "local_path": str(
                        local_artifacts["worker/diffusion-models/miaomiao-anima-base.safetensors"][
                            0
                        ]
                    ),
                    "approval": {
                        "name": "MiaoMiao Anima Base",
                        "model_family": "anima",
                        "source_url": "https://models.example.test/miaomiao-anima-base",
                        "license_url": "https://models.example.test/anima-license",
                        "commercial_use_approved": False,
                        "experiment_only": True,
                        "adult_use_approved": True,
                        "safetensors_verified": True,
                        "evidence": _evidence("Test-only Anima rights approval."),
                    },
                },
                {
                    "logical_name": "qwen-3-06b-base",
                    "kind": "text_encoder",
                    "object_key": "worker/text-encoders/qwen-3-06b-base.safetensors",
                    "local_path": str(
                        local_artifacts["worker/text-encoders/qwen-3-06b-base.safetensors"][0]
                    ),
                },
                {
                    "logical_name": "qwen-image-vae",
                    "kind": "vae",
                    "object_key": "worker/vae/qwen-image-vae.safetensors",
                    "local_path": str(local_artifacts["worker/vae/qwen-image-vae.safetensors"][0]),
                },
                {
                    "logical_name": "748cm-anima",
                    "kind": "lora",
                    "object_key": "worker/loras/748cm-anima.safetensors",
                    "local_path": str(local_artifacts["worker/loras/748cm-anima.safetensors"][0]),
                    "approval": {
                        "name": "748cm Anima",
                        "model_family": "anima",
                        "source_url": "https://models.example.test/748cm-anima",
                        "license_url": "https://models.example.test/748cm-anima/license",
                        "commercial_use_approved": False,
                        "experiment_only": True,
                        "adult_use_approved": True,
                        "safetensors_verified": True,
                        "evidence": _evidence("Test-only Anima LoRA rights approval."),
                    },
                },
            ],
            "workflows": [
                {
                    "name": "Anima base",
                    "version": "1",
                    "model_family": "anima",
                    "object_key": ANIMA_WORKFLOW_KEY,
                    "local_path": str(ANIMA_WORKFLOW_PATH),
                    "evidence": _evidence("Reviewed the exact bundled Anima graph."),
                }
            ],
        }
    )
    for key, (_, body) in local_artifacts.items():
        _stage_artifact(artifacts, key=key, body=body)

    database = await _database(tmp_path)
    try:
        async with database.sessions() as session:
            result = await onboard_artifacts(
                session,
                plan=plan,
                plan_directory=REPOSITORY_ROOT,
                artifact_store=artifacts,
                workflow_store=MemoryObjectStore(bucket="workflow-assets"),
            )
        async with database.sessions() as session:
            approvals = tuple(
                (
                    await session.scalars(
                        select(ModelArtifactApproval).order_by(ModelArtifactApproval.name)
                    )
                ).all()
            )
            workflow = await session.scalar(select(WorkflowApproval))
    finally:
        await database.dispose()

    assert [entry.kind for entry in result.manifest.artifacts] == [
        ArtifactKind.CHECKPOINT,
        ArtifactKind.DIFFUSION_MODEL,
        ArtifactKind.LORA,
        ArtifactKind.TEXT_ENCODER,
        ArtifactKind.VAE,
    ]
    assert result.manifest.artifacts[0] == illustrious_entry
    assert len(result.artifact_approvals) == 2
    assert {(approval.name, approval.kind, approval.model_family) for approval in approvals} == {
        (
            "MiaoMiao Anima Base",
            ModelArtifactKind.CHECKPOINT,
            GenerationModelFamily.ANIMA,
        ),
        ("748cm Anima", ModelArtifactKind.LORA, GenerationModelFamily.ANIMA),
    }
    assert all(approval.experiment_only for approval in approvals)
    assert all(not approval.commercial_use_approved for approval in approvals)
    assert workflow is not None
    assert workflow.model_family == GenerationModelFamily.ANIMA
    assert result.workflows[0].model_family == "anima"


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


def test_anima_example_is_exact_and_experiment_only() -> None:
    plan = ArtifactOnboardingPlan.model_validate_json(
        (REPOSITORY_ROOT / "examples/anima-artifact-onboarding-plan.template.json").read_bytes()
    )
    selectable = tuple(entry for entry in plan.artifacts if entry.approval is not None)
    support = tuple(entry for entry in plan.artifacts if entry.approval is None)

    assert plan.base_manifest_path == "../catalog/current-deployed-artifact-manifest.json"
    assert {entry.kind for entry in selectable} == {
        ArtifactKind.DIFFUSION_MODEL,
        ArtifactKind.LORA,
    }
    assert {entry.kind for entry in support} == {
        ArtifactKind.TEXT_ENCODER,
        ArtifactKind.VAE,
    }
    assert all(
        entry.approval is not None
        and entry.approval.model_family == GenerationModelFamily.ANIMA
        and entry.approval.experiment_only
        and not entry.approval.commercial_use_approved
        for entry in selectable
    )
    assert plan.workflows[0].model_family == GenerationModelFamily.ANIMA
    assert plan.workflows[0].object_key == ANIMA_WORKFLOW_KEY


def test_plan_requires_a_primary_model_and_at_most_one_detector() -> None:
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
    with pytest.raises(ValueError, match="primary model"):
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


def test_anima_plan_requires_manifest_only_support_artifacts_without_approvals() -> None:
    primary = {
        "logical_name": "anima",
        "kind": "diffusion_model",
        "object_key": "worker/diffusion-models/anima.safetensors",
        "sha256": "4" * 64,
        "exact_size_bytes": 64,
        "approval": {
            "name": "Anima",
            "model_family": "anima",
            "source_url": "https://models.example.test/anima",
            "license_url": "https://models.example.test/anima/license",
            "commercial_use_approved": False,
            "experiment_only": True,
            "adult_use_approved": True,
            "safetensors_verified": True,
            "evidence": _evidence("Test-only Anima approval."),
        },
    }
    text_encoder = {
        "logical_name": "qwen",
        "kind": "text_encoder",
        "object_key": "worker/text-encoders/qwen.safetensors",
        "sha256": "5" * 64,
        "exact_size_bytes": 64,
    }
    vae = {
        "logical_name": "vae",
        "kind": "vae",
        "object_key": "worker/vae/vae.safetensors",
        "sha256": "6" * 64,
        "exact_size_bytes": 64,
    }
    plan = ArtifactOnboardingPlan.model_validate(
        {"version": "v1", "artifacts": [primary, text_encoder, vae]}
    )
    assert plan.artifacts[1].approval is None
    assert plan.artifacts[2].approval is None

    with pytest.raises(ValueError, match="exactly one text encoder and one VAE"):
        ArtifactOnboardingPlan.model_validate(
            {"version": "v1", "artifacts": [primary, text_encoder]}
        )

    with pytest.raises(ValueError, match="not model approval"):
        ArtifactOnboardingPlan.model_validate(
            {
                "version": "v1",
                "artifacts": [
                    primary,
                    {**text_encoder, "approval": primary["approval"]},
                    vae,
                ],
            }
        )

    non_commercial_without_scope = {
        **primary,
        "approval": {
            **primary["approval"],
            "experiment_only": False,
        },
    }
    with pytest.raises(ValueError, match="experiment-only"):
        ArtifactOnboardingPlan.model_validate(
            {
                "version": "v1",
                "artifacts": [non_commercial_without_scope, text_encoder, vae],
            }
        )

    string_scope = {
        **primary,
        "approval": {
            **primary["approval"],
            "commercial_use_approved": "false",
        },
    }
    with pytest.raises(ValueError, match="valid boolean"):
        ArtifactOnboardingPlan.model_validate(
            {"version": "v1", "artifacts": [string_scope, text_encoder, vae]}
        )


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

    base_manifest_path = tmp_path / "current-deployed-artifact-manifest.json"
    base_manifest_path.write_text("{}", encoding="utf-8")
    additive_plan = plan.model_copy(update={"base_manifest_path": str(base_manifest_path)})
    with pytest.raises(ArtifactOnboardingError, match="overwrite onboarding inputs"):
        _validate_output_paths(
            plan_path=plan_path.resolve(),
            plan=additive_plan,
            plan_directory=tmp_path,
            manifest_path=base_manifest_path.resolve(),
            sha256_path=(tmp_path / "manifest.sha256").resolve(),
        )
