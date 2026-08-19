from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from gen_automation.db.models import AdminUser, ManagedLoraArtifact, ModelArtifactApproval
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AdminRole,
    ApprovalStatus,
    GenerationModelFamily,
    LoraImportSource,
    ManagedLoraLifecycle,
    ModelArtifactKind,
)
from gen_automation.gpu_worker.artifacts import (
    ArtifactKind,
    ModelArtifactSpec,
    create_artifact_manifest,
)
from gen_automation.services.managed_artifact_manifest import (
    ManagedArtifactManifestError,
    build_effective_artifact_manifest,
)

NOW = datetime(2026, 8, 9, 18, 0, tzinfo=UTC)
BUCKET = "test-managed-models"


@pytest.fixture
async def manifest_database(tmp_path: Path) -> AsyncIterator[tuple[Database, UUID]]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'managed-manifest.db').as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        owner = AdminUser(
            username_normalized="manifest-owner@example.test",
            display_name="Manifest Owner",
            password_hash="disabled-test-password-hash",  # noqa: S106
            role=AdminRole.OWNER,
            is_active=True,
            failed_login_count=0,
            password_changed_at=NOW,
            credential_version=1,
            lock_version=1,
        )
        session.add(owner)
        await session.commit()
        owner_id = owner.id
    try:
        yield database, owner_id
    finally:
        await database.dispose()


def _baseline(*, sha256: str = "f" * 64, kind: ArtifactKind = ArtifactKind.CHECKPOINT):
    return create_artifact_manifest(
        (
            ModelArtifactSpec(
                logical_name="baseline-model",
                kind=kind,
                source_object_id="models/baseline.safetensors",
                source_object_version_id="version-1",
                sha256=sha256,
                exact_size_bytes=1024,
                max_size_bytes=1024,
                target_filename="baseline.safetensors",
            ),
        )
    )


def _multi_family_baseline():
    return create_artifact_manifest(
        tuple(
            ModelArtifactSpec(
                logical_name=name,
                kind=kind,
                source_object_id=f"models/{name}.safetensors",
                source_object_version_id="version-1",
                sha256=sha256,
                exact_size_bytes=1024,
                max_size_bytes=1024,
                target_filename=f"{name}.safetensors",
            )
            for name, kind, sha256 in (
                ("illustrious", ArtifactKind.CHECKPOINT, "a" * 64),
                ("illustrious-style", ArtifactKind.LORA, "b" * 64),
                ("anima", ArtifactKind.DIFFUSION_MODEL, "c" * 64),
                ("anima-text", ArtifactKind.TEXT_ENCODER, "d" * 64),
                ("anima-vae", ArtifactKind.VAE, "e" * 64),
                ("anima-style", ArtifactKind.LORA, "f" * 64),
            )
        )
    )


async def _add_family_approval(
    database: Database,
    *,
    owner_id: UUID,
    sha256: str,
    name: str,
    kind: ModelArtifactKind,
    family: GenerationModelFamily,
) -> None:
    experiment_only = family == GenerationModelFamily.ANIMA
    async with database.sessions() as session:
        session.add(
            ModelArtifactApproval(
                artifact_sha256=sha256,
                name=name,
                kind=kind,
                model_family=family,
                source_url=f"https://models.example.test/{name}",
                storage_key=f"models/{name}.safetensors",
                license_url=f"https://models.example.test/{name}/license",
                commercial_use_approved=not experiment_only,
                experiment_only=experiment_only,
                adult_use_approved=True,
                safetensors_verified=True,
                evidence={"summary": "Test family approval"},
                evidence_sha256="8" * 64,
                status=ApprovalStatus.APPROVED,
                is_current=True,
                approval_version=1,
                approved_by_user_id=owner_id,
                approved_at=NOW,
            )
        )
        await session.commit()


async def test_runtime_manifest_selects_only_the_requested_model_family(
    manifest_database: tuple[Database, UUID],
) -> None:
    database, owner_id = manifest_database
    for sha256, name, kind, family in (
        ("a" * 64, "illustrious", ModelArtifactKind.CHECKPOINT, GenerationModelFamily.ILLUSTRIOUS),
        ("b" * 64, "illustrious-style", ModelArtifactKind.LORA, GenerationModelFamily.ILLUSTRIOUS),
        ("c" * 64, "anima", ModelArtifactKind.CHECKPOINT, GenerationModelFamily.ANIMA),
        ("f" * 64, "anima-style", ModelArtifactKind.LORA, GenerationModelFamily.ANIMA),
    ):
        await _add_family_approval(
            database,
            owner_id=owner_id,
            sha256=sha256,
            name=name,
            kind=kind,
            family=family,
        )
    async with database.sessions() as session:
        illustrious = await build_effective_artifact_manifest(
            session,
            baseline=_multi_family_baseline(),
            expected_bucket=BUCKET,
            required_checkpoint_sha256="a" * 64,
            required_lora_sha256s=("b" * 64,),
        )
        anima = await build_effective_artifact_manifest(
            session,
            baseline=_multi_family_baseline(),
            expected_bucket=BUCKET,
            required_checkpoint_sha256="c" * 64,
            required_lora_sha256s=(),
        )
        anima_with_style_selected = await build_effective_artifact_manifest(
            session,
            baseline=_multi_family_baseline(),
            expected_bucket=BUCKET,
            required_checkpoint_sha256="c" * 64,
            required_lora_sha256s=("f" * 64,),
        )

    assert {(artifact.kind, artifact.sha256) for artifact in illustrious.manifest.artifacts} == {
        (ArtifactKind.CHECKPOINT, "a" * 64),
        (ArtifactKind.LORA, "b" * 64),
    }
    assert {(artifact.kind, artifact.sha256) for artifact in anima.manifest.artifacts} == {
        (ArtifactKind.DIFFUSION_MODEL, "c" * 64),
        (ArtifactKind.LORA, "f" * 64),
        (ArtifactKind.TEXT_ENCODER, "d" * 64),
        (ArtifactKind.VAE, "e" * 64),
    }
    assert anima.sha256 == anima_with_style_selected.sha256


async def test_runtime_manifest_rejects_missing_primary_or_anima_support(
    manifest_database: tuple[Database, UUID],
) -> None:
    database, owner_id = manifest_database
    await _add_family_approval(
        database,
        owner_id=owner_id,
        sha256="c" * 64,
        name="anima",
        kind=ModelArtifactKind.CHECKPOINT,
        family=GenerationModelFamily.ANIMA,
    )
    async with database.sessions() as session:
        with pytest.raises(ManagedArtifactManifestError, match="required primary model"):
            await build_effective_artifact_manifest(
                session,
                baseline=_multi_family_baseline(),
                expected_bucket=BUCKET,
                required_checkpoint_sha256="9" * 64,
                required_lora_sha256s=(),
            )
        with pytest.raises(ManagedArtifactManifestError, match="support artifacts"):
            await build_effective_artifact_manifest(
                session,
                baseline=create_artifact_manifest(
                    tuple(
                        artifact
                        for artifact in _multi_family_baseline().artifacts
                        if artifact.kind != ArtifactKind.VAE
                    )
                ),
                expected_bucket=BUCKET,
                required_checkpoint_sha256="c" * 64,
                required_lora_sha256s=(),
            )


async def _add_managed(
    database: Database,
    *,
    owner_id: UUID,
    index: int,
    lifecycle: ManagedLoraLifecycle = ManagedLoraLifecycle.ACTIVE,
    activated: bool = True,
    experiment_only: bool = False,
) -> str:
    sha256 = f"{index:064x}"
    key = f"worker/managed-loras/sha256/{sha256}.safetensors"
    async with database.sessions() as session:
        approval = ModelArtifactApproval(
            artifact_sha256=sha256,
            name=f"Managed LoRA {index}",
            kind=ModelArtifactKind.LORA,
            source_url=f"https://models.example.test/{index}",
            storage_key=key,
            license_url=f"https://models.example.test/{index}/license",
            commercial_use_approved=not experiment_only,
            experiment_only=experiment_only,
            adult_use_approved=True,
            safetensors_verified=True,
            evidence={"summary": "Test approval"},
            evidence_sha256="e" * 64,
            status=ApprovalStatus.APPROVED,
            is_current=True,
            approval_version=1,
            approved_by_user_id=owner_id,
            approved_at=NOW,
        )
        session.add(approval)
        await session.flush()
        session.add(
            ManagedLoraArtifact(
                artifact_sha256=sha256,
                display_name=f"Managed LoRA {index}",
                source_type=LoraImportSource.MANUAL,
                canonical_source_url=f"https://models.example.test/{index}",
                license_url=f"https://models.example.test/{index}/license",
                provenance={"schema": "managed-lora-provenance/v1"},
                storage_bucket=BUCKET,
                object_key=key,
                object_version_id=f"version-{index}",
                object_etag=f"etag-{index}",
                byte_size=1024,
                target_filename=f"managed-{sha256}.safetensors",
                approval_id=approval.id,
                trigger_words=[],
                lifecycle=lifecycle,
                purge_requested=False,
                registered_by_user_id=owner_id,
                activated_at=NOW if activated else None,
                retirement_requested_at=(
                    NOW if lifecycle == ManagedLoraLifecycle.RETIRING else None
                ),
                retired_at=None,
                lock_version=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.commit()
    return sha256


async def test_selected_experiment_only_managed_lora_is_runnable(
    manifest_database: tuple[Database, UUID],
) -> None:
    database, owner_id = manifest_database
    selected = await _add_managed(
        database,
        owner_id=owner_id,
        index=73,
        experiment_only=True,
    )
    async with database.sessions() as session:
        effective = await build_effective_artifact_manifest(
            session,
            baseline=_baseline(),
            expected_bucket=BUCKET,
            required_lora_sha256s=(selected,),
        )
    assert effective.managed_lora_sha256s == frozenset({selected})
    assert {artifact.sha256 for artifact in effective.manifest.artifacts} == {
        "f" * 64,
        selected,
    }


async def test_required_lora_cannot_be_satisfied_by_a_wrong_kind_baseline(
    manifest_database: tuple[Database, UUID],
) -> None:
    database, _owner_id = manifest_database
    shared_sha256 = "a" * 64
    async with database.sessions() as session:
        with pytest.raises(ManagedArtifactManifestError, match="required LoRA"):
            await build_effective_artifact_manifest(
                session,
                baseline=_baseline(
                    sha256=shared_sha256,
                    kind=ArtifactKind.CHECKPOINT,
                ),
                expected_bucket=BUCKET,
                required_lora_sha256s=(shared_sha256,),
            )


async def test_per_batch_manifest_ignores_the_rest_of_a_large_active_library(
    manifest_database: tuple[Database, UUID],
) -> None:
    database, owner_id = manifest_database
    hashes = [
        await _add_managed(database, owner_id=owner_id, index=index) for index in range(1, 66)
    ]
    selected = hashes[37]
    async with database.sessions() as session:
        effective = await build_effective_artifact_manifest(
            session,
            baseline=_baseline(),
            expected_bucket=BUCKET,
            required_lora_sha256s=(selected,),
        )
    assert effective.managed_lora_sha256s == frozenset({selected})
    assert {artifact.sha256 for artifact in effective.manifest.artifacts} == {
        "f" * 64,
        selected,
    }


async def test_only_a_previously_activated_retiring_lora_can_drain_in_manifest(
    manifest_database: tuple[Database, UUID],
) -> None:
    database, owner_id = manifest_database
    draining = await _add_managed(
        database,
        owner_id=owner_id,
        index=101,
        lifecycle=ManagedLoraLifecycle.RETIRING,
        activated=True,
    )
    never_activated = await _add_managed(
        database,
        owner_id=owner_id,
        index=102,
        lifecycle=ManagedLoraLifecycle.RETIRING,
        activated=False,
    )
    async with database.sessions() as session:
        effective = await build_effective_artifact_manifest(
            session,
            baseline=_baseline(),
            expected_bucket=BUCKET,
            required_lora_sha256s=(draining,),
        )
        with pytest.raises(ManagedArtifactManifestError, match="required LoRA"):
            await build_effective_artifact_manifest(
                session,
                baseline=_baseline(),
                expected_bucket=BUCKET,
                required_lora_sha256s=(never_activated,),
            )
    assert effective.managed_lora_sha256s == frozenset({draining})
