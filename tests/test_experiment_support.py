from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from gen_automation.config import Settings
from gen_automation.gpu_worker.artifacts import (
    ArtifactKind,
    ModelArtifactSpec,
    create_artifact_manifest,
)
from gen_automation.services.experiment_support import (
    ExperimentArtifactReadinessReason,
    ExperimentArtifactReadinessStatus,
    classify_experiment_model_readiness,
    estimate_experiment_session_cost,
    estimate_experiment_session_cost_from_settings,
)
from gen_automation.services.new_sets import ArtifactOption, NewSetOptions


def _artifact(
    logical_name: str,
    kind: ArtifactKind,
    sha256: str,
) -> ModelArtifactSpec:
    return ModelArtifactSpec(
        logical_name=logical_name,
        kind=kind,
        source_object_id=f"models/{logical_name}.safetensors",
        source_object_version_id="version-1",
        sha256=sha256,
        exact_size_bytes=100,
        max_size_bytes=100,
        target_filename=f"{logical_name}.safetensors",
    )


def _option(value: int, name: str, sha256: str) -> ArtifactOption:
    return ArtifactOption(
        approval_id=UUID(int=value),
        name=name,
        sha256=sha256,
    )


def _options(
    *,
    checkpoints: tuple[ArtifactOption, ...],
    loras: tuple[ArtifactOption, ...],
) -> NewSetOptions:
    return NewSetOptions(
        subjects=(),
        checkpoints=checkpoints,
        loras=loras,
        workflows=(),
        wildcards=(),
    )


def test_readiness_requires_exact_hash_and_artifact_kind() -> None:
    checkpoint_hash = "a" * 64
    lora_hash = "b" * 64
    manifest = create_artifact_manifest(
        (
            _artifact("checkpoint", ArtifactKind.CHECKPOINT, checkpoint_hash),
            _artifact("style", ArtifactKind.LORA, lora_hash),
        )
    )
    settings = Settings(
        salad_worker_model_manifest_json=manifest.model_dump_json(),
        salad_worker_model_manifest_sha256=manifest.manifest_sha256,
    )
    result = classify_experiment_model_readiness(
        settings,
        _options(
            checkpoints=(
                _option(1, "Warm checkpoint", checkpoint_hash),
                _option(2, "Missing checkpoint", "c" * 64),
                _option(3, "Wrong-kind digest", lora_hash),
            ),
            loras=(
                _option(4, "Warm LoRA", lora_hash),
                _option(5, "Missing LoRA", "d" * 64),
            ),
        ),
    )

    assert result.manifest_available is True
    assert result.manifest_sha256 == manifest.manifest_sha256
    assert [item.status for item in result.checkpoints] == [
        ExperimentArtifactReadinessStatus.WARM_READY,
        ExperimentArtifactReadinessStatus.RESTART_REQUIRED,
        ExperimentArtifactReadinessStatus.RESTART_REQUIRED,
    ]
    assert result.checkpoints[0].reason is None
    assert result.checkpoints[1].reason is (
        ExperimentArtifactReadinessReason.ARTIFACT_NOT_IN_WORKER_MANIFEST
    )
    assert result.checkpoints[2].reason is (
        ExperimentArtifactReadinessReason.ARTIFACT_NOT_IN_WORKER_MANIFEST
    )
    assert result.loras[0].warm_ready is True
    assert result.loras[1].warm_ready is False
    assert result.warm_ready_count == 2
    assert result.restart_required_count == 3


@pytest.mark.parametrize(
    "settings",
    (
        Settings(),
        Settings(
            salad_worker_model_manifest_json="not-json",
            salad_worker_model_manifest_sha256="f" * 64,
        ),
    ),
)
def test_unavailable_manifest_fails_closed_without_exposing_parse_errors(
    settings: Settings,
) -> None:
    result = classify_experiment_model_readiness(
        settings,
        _options(
            checkpoints=(_option(1, "Checkpoint", "a" * 64),),
            loras=(_option(2, "LoRA", "b" * 64),),
        ),
    )

    assert result.manifest_available is False
    assert result.manifest_sha256 is None
    assert all(
        item.reason is ExperimentArtifactReadinessReason.WORKER_MANIFEST_UNAVAILABLE
        for item in (*result.checkpoints, *result.loras)
    )


def test_configured_manifest_digest_mismatch_fails_closed() -> None:
    manifest = create_artifact_manifest(
        (_artifact("checkpoint", ArtifactKind.CHECKPOINT, "a" * 64),)
    )
    result = classify_experiment_model_readiness(
        Settings(
            salad_worker_model_manifest_json=manifest.model_dump_json(),
            salad_worker_model_manifest_sha256="f" * 64,
        ),
        _options(
            checkpoints=(_option(1, "Checkpoint", "a" * 64),),
            loras=(),
        ),
    )

    assert result.manifest_available is False
    assert result.checkpoints[0].reason is (
        ExperimentArtifactReadinessReason.WORKER_MANIFEST_UNAVAILABLE
    )


def test_session_estimate_uses_conservative_integer_microusd_ceiling() -> None:
    estimate = estimate_experiment_session_cost(
        rate_ceiling_microusd_per_hour=350_000,
        idle_ttl_seconds=15 * 60,
        hard_max_duration_seconds=90 * 60,
        elapsed_session_seconds=20 * 60,
        idle_remaining_seconds=7 * 60,
    )

    assert estimate.initial_idle_commitment_microusd == 87_500
    assert estimate.session_max_microusd == 525_000
    assert estimate.hard_remaining_seconds == 70 * 60
    assert estimate.commitment_seconds == 7 * 60
    assert estimate.remaining_commitment_microusd == 40_834


def test_session_estimate_clamps_commitment_at_absolute_expiry() -> None:
    estimate = estimate_experiment_session_cost(
        rate_ceiling_microusd_per_hour=350_000,
        idle_ttl_seconds=15 * 60,
        hard_max_duration_seconds=90 * 60,
        elapsed_session_seconds=89 * 60,
        idle_remaining_seconds=15 * 60,
    )
    expired = estimate_experiment_session_cost(
        rate_ceiling_microusd_per_hour=350_000,
        idle_ttl_seconds=15 * 60,
        hard_max_duration_seconds=90 * 60,
        elapsed_session_seconds=90 * 60,
        idle_remaining_seconds=15 * 60,
    )

    assert estimate.commitment_seconds == 60
    assert estimate.remaining_commitment_microusd == 5_834
    assert expired.hard_remaining_seconds == 0
    assert expired.remaining_commitment_microusd == 0


def test_settings_estimate_converts_decimal_rate_exactly() -> None:
    estimate = estimate_experiment_session_cost_from_settings(
        Settings(salad_max_hourly_cost_usd=Decimal("0.35")),
        idle_ttl_seconds=15 * 60,
        hard_max_duration_seconds=90 * 60,
    )

    assert estimate.rate_ceiling_microusd_per_hour == 350_000
    assert estimate.initial_idle_commitment_microusd == 87_500
    assert estimate.session_max_microusd == 525_000


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"rate_ceiling_microusd_per_hour": 0}, "rate ceiling"),
        ({"idle_ttl_seconds": 0}, "idle TTL"),
        ({"hard_max_duration_seconds": 0}, "hard maximum"),
        ({"elapsed_session_seconds": -1}, "elapsed session"),
        ({"idle_remaining_seconds": -1}, "idle time remaining"),
        ({"idle_remaining_seconds": 901}, "cannot exceed"),
        ({"idle_ttl_seconds": 901, "hard_max_duration_seconds": 900}, "cannot exceed"),
    ),
)
def test_session_estimate_rejects_invalid_cost_inputs(
    updates: dict[str, int],
    message: str,
) -> None:
    values = {
        "rate_ceiling_microusd_per_hour": 350_000,
        "idle_ttl_seconds": 900,
        "hard_max_duration_seconds": 5_400,
        "elapsed_session_seconds": 0,
        "idle_remaining_seconds": 900,
    }
    values.update(updates)

    with pytest.raises(ValueError, match=message):
        estimate_experiment_session_cost(**values)
