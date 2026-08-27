from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from gen_automation.domain.canonical import canonical_json_bytes, canonical_sha256
from gen_automation.domain.generation_limits import MAX_OUTPUTS_PER_GENERATION_JOB

NEAR_BLACK_SEED_RECOVERY_METADATA_KEY = "near_black_seed_recovery"
NEAR_BLACK_SEED_RECOVERY_VERSION: Literal["v1"] = "v1"

_DERIVATION_DOMAIN = b"gen-automation:near-black-seed:v1"
_MAX_SEED = (2**63) - 1
_MAX_U32 = (2**32) - 1
_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

CanonicalUuidText = Annotated[str, StringConstraints(pattern=_UUID_PATTERN)]
Sha256Text = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
OutputIndex = Annotated[int, Field(ge=0, lt=MAX_OUTPUTS_PER_GENERATION_JOB)]
Seed = Annotated[int, Field(ge=0, le=_MAX_SEED)]
Uint32 = Annotated[int, Field(ge=0, le=_MAX_U32)]
PositiveUint32 = Annotated[int, Field(ge=1, le=_MAX_U32)]


class NearBlackRecoveryPlanError(ValueError):
    """The deterministic near-black recovery contract is invalid."""


class NearBlackSeedRewrite(BaseModel):
    """One deterministic replacement in the failed output suffix."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    output_index: OutputIndex
    original_seed: Seed
    recovery_seed: Seed
    collision_counter: Uint32


class NearBlackSeedRecoveryPlan(BaseModel):
    """Immutable, auditable seed-recovery semantics for one generation job."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal["v1"] = NEAR_BLACK_SEED_RECOVERY_VERSION
    generation_job_id: CanonicalUuidText
    source_grant_audit_event_id: CanonicalUuidText
    source_generation_attempt_id: CanonicalUuidText
    source_attempt_no: PositiveUint32
    grant_ordinal: PositiveUint32
    failed_output_index: OutputIndex
    expected_output_count: int = Field(ge=1, le=MAX_OUTPUTS_PER_GENERATION_JOB)
    uploaded_output_indices: tuple[OutputIndex, ...]
    seed_rewrites: tuple[NearBlackSeedRewrite, ...]
    seed_map_sha256: Sha256Text
    plan_sha256: Sha256Text

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        expected_uploaded = tuple(range(self.failed_output_index))
        if self.uploaded_output_indices not in ((), expected_uploaded):
            raise ValueError("uploaded outputs are not an exact progressive prefix")

        expected_rewrite_indices = tuple(
            range(self.failed_output_index, self.expected_output_count)
        )
        rewrite_indices = tuple(rewrite.output_index for rewrite in self.seed_rewrites)
        if rewrite_indices != expected_rewrite_indices:
            raise ValueError("seed rewrites are not the exact failed suffix")

        original_suffix = tuple(rewrite.original_seed for rewrite in self.seed_rewrites)
        recovery_suffix = tuple(rewrite.recovery_seed for rewrite in self.seed_rewrites)
        if len(set(original_suffix)) != len(original_suffix):
            raise ValueError("seed rewrite originals are not unique")
        if len(set(recovery_suffix)) != len(recovery_suffix):
            raise ValueError("recovery seeds are not unique")
        if set(original_suffix).intersection(recovery_suffix):
            raise ValueError("a recovery seed collides with an original suffix seed")

        job_id = UUID(self.generation_job_id)
        for rewrite in self.seed_rewrites:
            expected_seed = derive_near_black_seed_v1(
                generation_job_id=job_id,
                output_index=rewrite.output_index,
                original_seed=rewrite.original_seed,
                grant_ordinal=self.grant_ordinal,
                collision_counter=rewrite.collision_counter,
            )
            if rewrite.recovery_seed != expected_seed:
                raise ValueError("a recovery seed does not match the v1 derivation")

        expected_plan_sha256 = canonical_sha256(_plan_payload(self))
        if not hmac.compare_digest(self.plan_sha256, expected_plan_sha256):
            raise ValueError("the recovery plan digest is invalid")
        return self


def _require_bounded_int(
    value: int,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or value > maximum:
        raise NearBlackRecoveryPlanError(f"{name} is outside the recovery contract")
    return value


def _require_uuid(value: UUID, *, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise NearBlackRecoveryPlanError(f"{name} is outside the recovery contract")
    return value


def derive_near_black_seed_v1(
    *,
    generation_job_id: UUID,
    output_index: int,
    original_seed: int,
    grant_ordinal: int,
    collision_counter: int,
) -> int:
    """Derive one stable, non-negative 63-bit recovery seed."""

    job_id = _require_uuid(generation_job_id, name="generation_job_id")
    bounded_output_index = _require_bounded_int(
        output_index,
        name="output_index",
        minimum=0,
        maximum=_MAX_U32,
    )
    bounded_original_seed = _require_bounded_int(
        original_seed,
        name="original_seed",
        minimum=0,
        maximum=_MAX_SEED,
    )
    bounded_grant_ordinal = _require_bounded_int(
        grant_ordinal,
        name="grant_ordinal",
        minimum=1,
        maximum=_MAX_U32,
    )
    bounded_collision_counter = _require_bounded_int(
        collision_counter,
        name="collision_counter",
        minimum=0,
        maximum=_MAX_U32,
    )
    digest = hashlib.sha256(
        _DERIVATION_DOMAIN
        + job_id.bytes
        + bounded_output_index.to_bytes(4, "big")
        + bounded_original_seed.to_bytes(8, "big")
        + bounded_grant_ordinal.to_bytes(4, "big")
        + bounded_collision_counter.to_bytes(4, "big")
    ).digest()
    return int.from_bytes(digest[:8], "big") & _MAX_SEED


def _seed_map_payload(
    effective_seeds: Sequence[int],
) -> dict[str, object]:
    return {
        "version": NEAR_BLACK_SEED_RECOVERY_VERSION,
        "seeds": [
            {
                "output_index": output_index,
                "seed": seed,
            }
            for output_index, seed in enumerate(effective_seeds)
        ],
    }


def _plan_payload(plan: NearBlackSeedRecoveryPlan) -> dict[str, object]:
    return {
        "version": plan.version,
        "generation_job_id": plan.generation_job_id,
        "source_grant_audit_event_id": plan.source_grant_audit_event_id,
        "source_generation_attempt_id": plan.source_generation_attempt_id,
        "source_attempt_no": plan.source_attempt_no,
        "grant_ordinal": plan.grant_ordinal,
        "failed_output_index": plan.failed_output_index,
        "expected_output_count": plan.expected_output_count,
        "uploaded_output_indices": list(plan.uploaded_output_indices),
        "seed_rewrites": [rewrite.model_dump(mode="json") for rewrite in plan.seed_rewrites],
        "seed_map_sha256": plan.seed_map_sha256,
    }


def _validated_original_seeds(
    original_seeds: Sequence[int],
    *,
    expected_output_count: int,
) -> tuple[int, ...]:
    if isinstance(original_seeds, (str, bytes, bytearray)):
        raise NearBlackRecoveryPlanError("original seeds are outside the recovery contract")
    seeds = tuple(
        _require_bounded_int(
            seed,
            name="original_seed",
            minimum=0,
            maximum=_MAX_SEED,
        )
        for seed in original_seeds
    )
    if len(seeds) != expected_output_count:
        raise NearBlackRecoveryPlanError("original seed count does not match the job")
    if len(set(seeds)) != len(seeds):
        raise NearBlackRecoveryPlanError("original job seeds are not unique")
    return seeds


def build_near_black_seed_recovery_plan(
    *,
    generation_job_id: UUID,
    source_grant_audit_event_id: UUID,
    source_generation_attempt_id: UUID,
    source_attempt_no: int,
    grant_ordinal: int,
    failed_output_index: int,
    expected_output_count: int,
    uploaded_output_indices: Sequence[int],
    original_seeds: Sequence[int],
) -> NearBlackSeedRecoveryPlan:
    """Build the sole canonical v1 plan for an exact near-black failure."""

    job_id = _require_uuid(generation_job_id, name="generation_job_id")
    audit_event_id = _require_uuid(
        source_grant_audit_event_id,
        name="source_grant_audit_event_id",
    )
    attempt_id = _require_uuid(
        source_generation_attempt_id,
        name="source_generation_attempt_id",
    )
    bounded_source_attempt_no = _require_bounded_int(
        source_attempt_no,
        name="source_attempt_no",
        minimum=1,
        maximum=_MAX_U32,
    )
    bounded_grant_ordinal = _require_bounded_int(
        grant_ordinal,
        name="grant_ordinal",
        minimum=1,
        maximum=_MAX_U32,
    )
    bounded_expected_count = _require_bounded_int(
        expected_output_count,
        name="expected_output_count",
        minimum=1,
        maximum=MAX_OUTPUTS_PER_GENERATION_JOB,
    )
    bounded_failed_index = _require_bounded_int(
        failed_output_index,
        name="failed_output_index",
        minimum=0,
        maximum=bounded_expected_count - 1,
    )
    seeds = _validated_original_seeds(
        original_seeds,
        expected_output_count=bounded_expected_count,
    )
    uploaded = tuple(
        _require_bounded_int(
            output_index,
            name="uploaded_output_index",
            minimum=0,
            maximum=bounded_expected_count - 1,
        )
        for output_index in uploaded_output_indices
    )
    if uploaded not in ((), tuple(range(bounded_failed_index))):
        raise NearBlackRecoveryPlanError("uploaded outputs are not an exact progressive prefix")

    used_seeds = set(seeds)
    rewrites: list[NearBlackSeedRewrite] = []
    for output_index in range(bounded_failed_index, bounded_expected_count):
        collision_counter = 0
        while True:
            recovery_seed = derive_near_black_seed_v1(
                generation_job_id=job_id,
                output_index=output_index,
                original_seed=seeds[output_index],
                grant_ordinal=bounded_grant_ordinal,
                collision_counter=collision_counter,
            )
            if recovery_seed not in used_seeds:
                break
            if collision_counter == _MAX_U32:
                raise NearBlackRecoveryPlanError("recovery seed collision space was exhausted")
            collision_counter += 1
        used_seeds.add(recovery_seed)
        rewrites.append(
            NearBlackSeedRewrite(
                output_index=output_index,
                original_seed=seeds[output_index],
                recovery_seed=recovery_seed,
                collision_counter=collision_counter,
            )
        )

    seed_rewrites = tuple(rewrites)
    effective_seeds = list(seeds)
    for rewrite in seed_rewrites:
        effective_seeds[rewrite.output_index] = rewrite.recovery_seed
    seed_map_sha256 = canonical_sha256(_seed_map_payload(effective_seeds))
    provisional = NearBlackSeedRecoveryPlan.model_construct(
        version=NEAR_BLACK_SEED_RECOVERY_VERSION,
        generation_job_id=str(job_id),
        source_grant_audit_event_id=str(audit_event_id),
        source_generation_attempt_id=str(attempt_id),
        source_attempt_no=bounded_source_attempt_no,
        grant_ordinal=bounded_grant_ordinal,
        failed_output_index=bounded_failed_index,
        expected_output_count=bounded_expected_count,
        uploaded_output_indices=uploaded,
        seed_rewrites=seed_rewrites,
        seed_map_sha256=seed_map_sha256,
        plan_sha256="0" * 64,
    )
    plan_sha256 = canonical_sha256(_plan_payload(provisional))
    try:
        return NearBlackSeedRecoveryPlan(
            version=NEAR_BLACK_SEED_RECOVERY_VERSION,
            generation_job_id=str(job_id),
            source_grant_audit_event_id=str(audit_event_id),
            source_generation_attempt_id=str(attempt_id),
            source_attempt_no=bounded_source_attempt_no,
            grant_ordinal=bounded_grant_ordinal,
            failed_output_index=bounded_failed_index,
            expected_output_count=bounded_expected_count,
            uploaded_output_indices=uploaded,
            seed_rewrites=seed_rewrites,
            seed_map_sha256=seed_map_sha256,
            plan_sha256=plan_sha256,
        )
    except ValidationError as error:
        raise NearBlackRecoveryPlanError("the recovery plan is invalid") from error


def _parse_near_black_seed_recovery_plan(
    value: NearBlackSeedRecoveryPlan | Mapping[str, object],
) -> NearBlackSeedRecoveryPlan:
    """Structurally decode a plan; this is not an executable trust decision."""

    raw: object = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise NearBlackRecoveryPlanError("the recovery plan is invalid")
    try:
        return NearBlackSeedRecoveryPlan.model_validate_json(
            canonical_json_bytes(dict(raw)),
            strict=True,
        )
    except (TypeError, ValueError, ValidationError, RecursionError) as error:
        raise NearBlackRecoveryPlanError("the recovery plan is invalid") from error


def require_near_black_seed_recovery_plan(
    value: NearBlackSeedRecoveryPlan | Mapping[str, object],
    *,
    generation_job_id: UUID,
    expected_output_count: int,
    original_seeds: Sequence[int],
    source_grant_audit_event_id: UUID,
    source_generation_attempt_id: UUID,
    source_attempt_no: int,
    grant_ordinal: int,
    target_attempt_no: int,
) -> NearBlackSeedRecoveryPlan:
    """Validate a persisted plan against immutable job inputs and attempt order."""

    plan = _parse_near_black_seed_recovery_plan(value)
    job_id = _require_uuid(generation_job_id, name="generation_job_id")
    if plan.generation_job_id != str(job_id):
        raise NearBlackRecoveryPlanError("the recovery plan belongs to another job")
    bounded_expected_count = _require_bounded_int(
        expected_output_count,
        name="expected_output_count",
        minimum=1,
        maximum=MAX_OUTPUTS_PER_GENERATION_JOB,
    )
    if plan.expected_output_count != bounded_expected_count:
        raise NearBlackRecoveryPlanError("the recovery plan output count is invalid")
    audit_event_id = _require_uuid(
        source_grant_audit_event_id,
        name="source_grant_audit_event_id",
    )
    if plan.source_grant_audit_event_id != str(audit_event_id):
        raise NearBlackRecoveryPlanError("the recovery plan grant identity is invalid")
    attempt_id = _require_uuid(
        source_generation_attempt_id,
        name="source_generation_attempt_id",
    )
    if plan.source_generation_attempt_id != str(attempt_id):
        raise NearBlackRecoveryPlanError("the recovery plan attempt identity is invalid")
    bounded_source_attempt_no = _require_bounded_int(
        source_attempt_no,
        name="source_attempt_no",
        minimum=1,
        maximum=_MAX_U32,
    )
    if plan.source_attempt_no != bounded_source_attempt_no:
        raise NearBlackRecoveryPlanError("the recovery plan source attempt is invalid")
    bounded_grant_ordinal = _require_bounded_int(
        grant_ordinal,
        name="grant_ordinal",
        minimum=1,
        maximum=_MAX_U32,
    )
    if plan.grant_ordinal != bounded_grant_ordinal:
        raise NearBlackRecoveryPlanError("the recovery plan grant ordinal is invalid")
    bounded_target_attempt_no = _require_bounded_int(
        target_attempt_no,
        name="target_attempt_no",
        minimum=1,
        maximum=_MAX_U32,
    )
    if plan.source_attempt_no >= bounded_target_attempt_no:
        raise NearBlackRecoveryPlanError("the recovery plan does not precede the target attempt")
    seeds = _validated_original_seeds(
        original_seeds,
        expected_output_count=bounded_expected_count,
    )
    rebuilt = build_near_black_seed_recovery_plan(
        generation_job_id=job_id,
        source_grant_audit_event_id=UUID(plan.source_grant_audit_event_id),
        source_generation_attempt_id=UUID(plan.source_generation_attempt_id),
        source_attempt_no=plan.source_attempt_no,
        grant_ordinal=plan.grant_ordinal,
        failed_output_index=plan.failed_output_index,
        expected_output_count=plan.expected_output_count,
        uploaded_output_indices=plan.uploaded_output_indices,
        original_seeds=seeds,
    )
    if rebuilt != plan:
        raise NearBlackRecoveryPlanError("the recovery plan does not match the immutable job seeds")
    return plan


def apply_near_black_seed_recovery_plan(
    value: NearBlackSeedRecoveryPlan | Mapping[str, object],
    *,
    generation_job_id: UUID,
    expected_output_count: int,
    original_seeds: Sequence[int],
    source_grant_audit_event_id: UUID,
    source_generation_attempt_id: UUID,
    source_attempt_no: int,
    grant_ordinal: int,
    target_attempt_no: int,
) -> tuple[int, ...]:
    """Return the effective seed tuple without mutating the original inputs."""

    seeds = tuple(original_seeds)
    plan = require_near_black_seed_recovery_plan(
        value,
        generation_job_id=generation_job_id,
        expected_output_count=expected_output_count,
        original_seeds=seeds,
        source_grant_audit_event_id=source_grant_audit_event_id,
        source_generation_attempt_id=source_generation_attempt_id,
        source_attempt_no=source_attempt_no,
        grant_ordinal=grant_ordinal,
        target_attempt_no=target_attempt_no,
    )
    effective_seeds = list(seeds)
    for rewrite in plan.seed_rewrites:
        effective_seeds[rewrite.output_index] = rewrite.recovery_seed
    return tuple(effective_seeds)
