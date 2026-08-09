from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum

from gen_automation.config import Settings
from gen_automation.domain.enums import ModelArtifactKind
from gen_automation.gpu_worker.artifacts import ArtifactKind, ArtifactManifest
from gen_automation.gpu_worker.bootstrap import (
    WorkerBootstrapConfigurationError,
    load_artifact_manifest,
)
from gen_automation.services.budgets import usd_to_microusd
from gen_automation.services.new_sets import ArtifactOption, NewSetOptions

_SECONDS_PER_HOUR = 60 * 60
_MAX_BIGINT = (1 << 63) - 1


class ExperimentArtifactReadinessStatus(StrEnum):
    WARM_READY = "warm_ready"
    RESTART_REQUIRED = "restart_required"


class ExperimentArtifactReadinessReason(StrEnum):
    WORKER_MANIFEST_UNAVAILABLE = "worker_manifest_unavailable"
    ARTIFACT_NOT_IN_WORKER_MANIFEST = "artifact_not_in_worker_manifest"


@dataclass(frozen=True, slots=True)
class ExperimentArtifactReadiness:
    option: ArtifactOption
    kind: ModelArtifactKind
    status: ExperimentArtifactReadinessStatus
    reason: ExperimentArtifactReadinessReason | None

    @property
    def warm_ready(self) -> bool:
        return self.status is ExperimentArtifactReadinessStatus.WARM_READY


@dataclass(frozen=True, slots=True)
class ExperimentModelReadiness:
    manifest_sha256: str | None
    checkpoints: tuple[ExperimentArtifactReadiness, ...]
    loras: tuple[ExperimentArtifactReadiness, ...]

    @property
    def manifest_available(self) -> bool:
        return self.manifest_sha256 is not None

    @property
    def warm_ready_count(self) -> int:
        return sum(item.warm_ready for item in (*self.checkpoints, *self.loras))

    @property
    def restart_required_count(self) -> int:
        return len(self.checkpoints) + len(self.loras) - self.warm_ready_count


@dataclass(frozen=True, slots=True)
class ExperimentSessionCostEstimate:
    rate_ceiling_microusd_per_hour: int
    idle_ttl_seconds: int
    hard_max_duration_seconds: int
    elapsed_session_seconds: int
    idle_remaining_seconds: int
    hard_remaining_seconds: int
    commitment_seconds: int
    initial_idle_commitment_microusd: int
    session_max_microusd: int
    remaining_commitment_microusd: int


def classify_experiment_model_readiness(
    settings: Settings,
    options: NewSetOptions,
    *,
    manifest_override: ArtifactManifest | None = None,
) -> ExperimentModelReadiness:
    """Classify catalog artifacts against the exact configured worker manifest.

    A digest is warm-ready only when the bounded, internally verified manifest also
    matches the separately configured manifest digest and contains the artifact
    under the expected kind. Any unavailable or inconsistent manifest fails closed.
    """

    manifest = manifest_override or _configured_worker_manifest(settings)
    manifest_sha256 = manifest.manifest_sha256 if manifest is not None else None
    checkpoint_hashes = _manifest_hashes(manifest, ArtifactKind.CHECKPOINT)
    lora_hashes = _manifest_hashes(manifest, ArtifactKind.LORA)
    unavailable = manifest is None
    return ExperimentModelReadiness(
        manifest_sha256=manifest_sha256,
        checkpoints=tuple(
            _classify_artifact(
                option,
                kind=ModelArtifactKind.CHECKPOINT,
                warm_hashes=checkpoint_hashes,
                manifest_unavailable=unavailable,
            )
            for option in options.checkpoints
        ),
        loras=tuple(
            _classify_artifact(
                option,
                kind=ModelArtifactKind.LORA,
                warm_hashes=lora_hashes,
                manifest_unavailable=unavailable,
            )
            for option in options.loras
        ),
    )


def estimate_experiment_session_cost(
    *,
    rate_ceiling_microusd_per_hour: int,
    idle_ttl_seconds: int,
    hard_max_duration_seconds: int,
    elapsed_session_seconds: int = 0,
    idle_remaining_seconds: int | None = None,
) -> ExperimentSessionCostEstimate:
    """Return conservative warm-session ceilings using integer micro-USD math.

    The remaining commitment covers only the lesser of the live idle lease and
    absolute session time remaining. It therefore represents future warm exposure,
    not already accrued provider spend.
    """

    _require_positive_int(rate_ceiling_microusd_per_hour, name="rate ceiling")
    _require_positive_int(idle_ttl_seconds, name="idle TTL")
    _require_positive_int(hard_max_duration_seconds, name="hard maximum duration")
    _require_nonnegative_int(elapsed_session_seconds, name="elapsed session duration")
    if idle_ttl_seconds > hard_max_duration_seconds:
        raise ValueError("idle TTL cannot exceed the hard maximum duration")

    resolved_idle_remaining = (
        idle_ttl_seconds if idle_remaining_seconds is None else idle_remaining_seconds
    )
    _require_nonnegative_int(resolved_idle_remaining, name="idle time remaining")
    if resolved_idle_remaining > idle_ttl_seconds:
        raise ValueError("idle time remaining cannot exceed the idle TTL")

    hard_remaining = max(hard_max_duration_seconds - elapsed_session_seconds, 0)
    commitment_seconds = min(resolved_idle_remaining, hard_remaining)
    initial_commitment_seconds = min(idle_ttl_seconds, hard_max_duration_seconds)
    initial_idle_commitment = _prorated_ceiling(
        rate_ceiling_microusd_per_hour,
        initial_commitment_seconds,
    )
    session_max = _prorated_ceiling(
        rate_ceiling_microusd_per_hour,
        hard_max_duration_seconds,
    )
    remaining_commitment = _prorated_ceiling(
        rate_ceiling_microusd_per_hour,
        commitment_seconds,
    )
    return ExperimentSessionCostEstimate(
        rate_ceiling_microusd_per_hour=rate_ceiling_microusd_per_hour,
        idle_ttl_seconds=idle_ttl_seconds,
        hard_max_duration_seconds=hard_max_duration_seconds,
        elapsed_session_seconds=elapsed_session_seconds,
        idle_remaining_seconds=resolved_idle_remaining,
        hard_remaining_seconds=hard_remaining,
        commitment_seconds=commitment_seconds,
        initial_idle_commitment_microusd=initial_idle_commitment,
        session_max_microusd=session_max,
        remaining_commitment_microusd=remaining_commitment,
    )


def estimate_experiment_session_cost_from_settings(
    settings: Settings,
    *,
    idle_ttl_seconds: int,
    hard_max_duration_seconds: int,
    elapsed_session_seconds: int = 0,
    idle_remaining_seconds: int | None = None,
) -> ExperimentSessionCostEstimate:
    return estimate_experiment_session_cost(
        rate_ceiling_microusd_per_hour=usd_to_microusd(settings.salad_max_hourly_cost_usd),
        idle_ttl_seconds=idle_ttl_seconds,
        hard_max_duration_seconds=hard_max_duration_seconds,
        elapsed_session_seconds=elapsed_session_seconds,
        idle_remaining_seconds=idle_remaining_seconds,
    )


def _configured_worker_manifest(settings: Settings) -> ArtifactManifest | None:
    manifest_json = settings.salad_worker_model_manifest_json
    expected_sha256 = settings.salad_worker_model_manifest_sha256
    if manifest_json is None or expected_sha256 is None:
        return None
    expected = expected_sha256.get_secret_value()
    try:
        manifest = load_artifact_manifest(manifest_json.get_secret_value())
    except (TypeError, ValueError, WorkerBootstrapConfigurationError):
        return None
    if not hmac.compare_digest(manifest.manifest_sha256, expected):
        return None
    return manifest


def _manifest_hashes(
    manifest: ArtifactManifest | None,
    kind: ArtifactKind,
) -> frozenset[str]:
    if manifest is None:
        return frozenset()
    return frozenset(artifact.sha256 for artifact in manifest.artifacts if artifact.kind is kind)


def _classify_artifact(
    option: ArtifactOption,
    *,
    kind: ModelArtifactKind,
    warm_hashes: frozenset[str],
    manifest_unavailable: bool,
) -> ExperimentArtifactReadiness:
    if option.sha256 in warm_hashes:
        return ExperimentArtifactReadiness(
            option=option,
            kind=kind,
            status=ExperimentArtifactReadinessStatus.WARM_READY,
            reason=None,
        )
    reason = (
        ExperimentArtifactReadinessReason.WORKER_MANIFEST_UNAVAILABLE
        if manifest_unavailable
        else ExperimentArtifactReadinessReason.ARTIFACT_NOT_IN_WORKER_MANIFEST
    )
    return ExperimentArtifactReadiness(
        option=option,
        kind=kind,
        status=ExperimentArtifactReadinessStatus.RESTART_REQUIRED,
        reason=reason,
    )


def _prorated_ceiling(rate_microusd_per_hour: int, duration_seconds: int) -> int:
    amount = (
        rate_microusd_per_hour * duration_seconds + _SECONDS_PER_HOUR - 1
    ) // _SECONDS_PER_HOUR
    if amount > _MAX_BIGINT:
        raise ValueError("estimated session cost exceeds the supported range")
    return amount


def _require_positive_int(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_nonnegative_int(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
