from copy import deepcopy
from uuid import UUID

import pytest
from pydantic import ValidationError

from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.near_black_recovery import (
    NearBlackRecoveryPlanError,
    NearBlackSeedRecoveryPlan,
    apply_near_black_seed_recovery_plan,
    build_near_black_seed_recovery_plan,
    derive_near_black_seed_v1,
    require_near_black_seed_recovery_plan,
)

JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
AUDIT_EVENT_ID = UUID("00000000-0000-0000-0000-000000000002")
ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000003")


@pytest.mark.parametrize(
    ("job_id", "output_index", "original_seed", "grant_ordinal", "counter", "expected"),
    [
        (JOB_ID, 1, 42, 1, 0, 3717610135558578023),
        (JOB_ID, 1, 42, 1, 1, 9166568739857870357),
        (
            UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            24,
            (2**63) - 1,
            1,
            0,
            1335388783687741029,
        ),
        (
            UUID("01a03cd9-b2d8-7c14-af38-ae3f1c33f373"),
            1,
            8137071832741896094,
            1,
            0,
            6165496323322712690,
        ),
    ],
)
def test_v1_seed_derivation_has_frozen_golden_vectors(
    job_id: UUID,
    output_index: int,
    original_seed: int,
    grant_ordinal: int,
    counter: int,
    expected: int,
) -> None:
    assert (
        derive_near_black_seed_v1(
            generation_job_id=job_id,
            output_index=output_index,
            original_seed=original_seed,
            grant_ordinal=grant_ordinal,
            collision_counter=counter,
        )
        == expected
    )


def _collision_plan() -> NearBlackSeedRecoveryPlan:
    return build_near_black_seed_recovery_plan(
        generation_job_id=JOB_ID,
        source_grant_audit_event_id=AUDIT_EVENT_ID,
        source_generation_attempt_id=ATTEMPT_ID,
        source_attempt_no=4,
        grant_ordinal=1,
        failed_output_index=1,
        expected_output_count=3,
        uploaded_output_indices=(0,),
        original_seeds=(99, 42, 3717610135558578023),
    )


def _require_plan(
    value: NearBlackSeedRecoveryPlan | dict[str, object],
) -> NearBlackSeedRecoveryPlan:
    return require_near_black_seed_recovery_plan(
        value,
        generation_job_id=JOB_ID,
        expected_output_count=3,
        original_seeds=(99, 42, 3717610135558578023),
        source_grant_audit_event_id=AUDIT_EVENT_ID,
        source_generation_attempt_id=ATTEMPT_ID,
        source_attempt_no=4,
        grant_ordinal=1,
        target_attempt_no=5,
    )


def _rehash_stored_plan(stored: dict[str, object]) -> None:
    payload = deepcopy(stored)
    removed_digest = payload.pop("plan_sha256")
    assert isinstance(removed_digest, str)
    stored["plan_sha256"] = canonical_sha256(payload)


def _effective_seed_map_sha256(effective_seeds: tuple[int, ...]) -> str:
    return canonical_sha256(
        {
            "version": "v1",
            "seeds": [
                {"output_index": output_index, "seed": seed}
                for output_index, seed in enumerate(effective_seeds)
            ],
        }
    )


def test_plan_resolves_collisions_against_every_original_seed_in_order() -> None:
    plan = _collision_plan()

    assert [
        (
            rewrite.output_index,
            rewrite.original_seed,
            rewrite.recovery_seed,
            rewrite.collision_counter,
        )
        for rewrite in plan.seed_rewrites
    ] == [
        (1, 42, 9166568739857870357, 1),
        (2, 3717610135558578023, 1094060445869509466, 0),
    ]
    assert plan.seed_map_sha256 == (
        "315dbe98f961c17ad26504de1b7e9a136883485c5dfc7fccc8605f7f03b23c51"
    )
    assert plan.plan_sha256 == ("3684decddf09d18430fd643268684bc22b0c169ccbbf9374bff3d983a266a559")


def test_plan_round_trip_is_strict_canonical_and_immutable() -> None:
    plan = _collision_plan()
    stored = plan.model_dump(mode="json")

    assert _require_plan(stored) == plan
    assert plan.uploaded_output_indices == (0,)
    assert isinstance(plan.seed_rewrites, tuple)
    with pytest.raises(ValidationError):
        plan.plan_sha256 = "0" * 64  # type: ignore[misc]


def test_plan_applies_only_the_failed_suffix_without_mutating_inputs() -> None:
    plan = _collision_plan()
    original = [99, 42, 3717610135558578023]
    snapshot = deepcopy(original)

    effective = apply_near_black_seed_recovery_plan(
        plan,
        generation_job_id=JOB_ID,
        expected_output_count=3,
        original_seeds=original,
        source_grant_audit_event_id=AUDIT_EVENT_ID,
        source_generation_attempt_id=ATTEMPT_ID,
        source_attempt_no=4,
        grant_ordinal=1,
        target_attempt_no=5,
    )

    assert original == snapshot
    assert effective == (
        99,
        9166568739857870357,
        1094060445869509466,
    )


def test_plan_can_be_reused_by_later_retries_with_identical_provenance() -> None:
    plan = _collision_plan()

    required = require_near_black_seed_recovery_plan(
        plan,
        generation_job_id=JOB_ID,
        expected_output_count=3,
        original_seeds=(99, 42, 3717610135558578023),
        source_grant_audit_event_id=AUDIT_EVENT_ID,
        source_generation_attempt_id=ATTEMPT_ID,
        source_attempt_no=4,
        grant_ordinal=1,
        target_attempt_no=6,
    )

    assert required == plan


@pytest.mark.parametrize(
    ("uploaded", "failed_index"),
    [
        ((1,), 1),
        ((0,), 2),
        ((0, 2), 2),
    ],
)
def test_plan_rejects_nonempty_nonexact_uploaded_prefixes(
    uploaded: tuple[int, ...],
    failed_index: int,
) -> None:
    with pytest.raises(NearBlackRecoveryPlanError, match="exact progressive prefix"):
        build_near_black_seed_recovery_plan(
            generation_job_id=JOB_ID,
            source_grant_audit_event_id=AUDIT_EVENT_ID,
            source_generation_attempt_id=ATTEMPT_ID,
            source_attempt_no=4,
            grant_ordinal=1,
            failed_output_index=failed_index,
            expected_output_count=3,
            uploaded_output_indices=uploaded,
            original_seeds=(10, 11, 12),
        )


@pytest.mark.parametrize(
    "original_seeds",
    [
        (1, 1),
        (-1, 2),
        (True, 2),
        (1, 2**63),
        (1,),
    ],
)
def test_plan_rejects_ambiguous_or_invalid_original_seeds(
    original_seeds: tuple[int, ...],
) -> None:
    with pytest.raises(NearBlackRecoveryPlanError):
        build_near_black_seed_recovery_plan(
            generation_job_id=JOB_ID,
            source_grant_audit_event_id=AUDIT_EVENT_ID,
            source_generation_attempt_id=ATTEMPT_ID,
            source_attempt_no=1,
            grant_ordinal=1,
            failed_output_index=0,
            expected_output_count=2,
            uploaded_output_indices=(),
            original_seeds=original_seeds,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "v2"),
        ("generation_job_id", "00000000-0000-0000-0000-000000000004"),
        ("failed_output_index", 0),
        ("uploaded_output_indices", []),
        ("seed_map_sha256", "0" * 64),
        ("plan_sha256", "f" * 64),
    ],
)
def test_persisted_plan_rejects_identity_shape_and_digest_tampering(
    field: str,
    value: object,
) -> None:
    stored = _collision_plan().model_dump(mode="json")
    stored[field] = value

    with pytest.raises(NearBlackRecoveryPlanError):
        _require_plan(stored)


def test_persisted_plan_rejects_rewrite_tampering_and_extra_fields() -> None:
    stored = _collision_plan().model_dump(mode="json")
    rewrites = deepcopy(stored["seed_rewrites"])
    assert isinstance(rewrites, list)
    assert isinstance(rewrites[0], dict)
    rewrites[0]["recovery_seed"] += 1
    stored["seed_rewrites"] = rewrites
    with pytest.raises(NearBlackRecoveryPlanError):
        _require_plan(stored)

    stored = _collision_plan().model_dump(mode="json")
    stored["untrusted"] = True
    with pytest.raises(NearBlackRecoveryPlanError):
        _require_plan(stored)


def test_public_require_rejects_rehashed_false_seed_map() -> None:
    stored = _collision_plan().model_dump(mode="json")
    stored["seed_map_sha256"] = "0" * 64
    _rehash_stored_plan(stored)

    with pytest.raises(NearBlackRecoveryPlanError, match="immutable job seeds"):
        _require_plan(stored)


def test_public_apply_rejects_rehashed_nonminimal_collision_counter() -> None:
    stored = _collision_plan().model_dump(mode="json")
    rewrites = deepcopy(stored["seed_rewrites"])
    assert isinstance(rewrites, list)
    assert isinstance(rewrites[0], dict)
    nonminimal_seed = derive_near_black_seed_v1(
        generation_job_id=JOB_ID,
        output_index=1,
        original_seed=42,
        grant_ordinal=1,
        collision_counter=2,
    )
    rewrites[0]["collision_counter"] = 2
    rewrites[0]["recovery_seed"] = nonminimal_seed
    stored["seed_rewrites"] = rewrites
    stored["seed_map_sha256"] = _effective_seed_map_sha256(
        (99, nonminimal_seed, 1094060445869509466)
    )
    _rehash_stored_plan(stored)

    with pytest.raises(NearBlackRecoveryPlanError, match="immutable job seeds"):
        apply_near_black_seed_recovery_plan(
            stored,
            generation_job_id=JOB_ID,
            expected_output_count=3,
            original_seeds=(99, 42, 3717610135558578023),
            source_grant_audit_event_id=AUDIT_EVENT_ID,
            source_generation_attempt_id=ATTEMPT_ID,
            source_attempt_no=4,
            grant_ordinal=1,
            target_attempt_no=5,
        )


def test_plan_must_match_immutable_job_seeds_and_precede_target_attempt() -> None:
    plan = _collision_plan()

    with pytest.raises(NearBlackRecoveryPlanError, match="immutable job seeds"):
        require_near_black_seed_recovery_plan(
            plan,
            generation_job_id=JOB_ID,
            expected_output_count=3,
            original_seeds=(100, 42, 3717610135558578023),
            source_grant_audit_event_id=AUDIT_EVENT_ID,
            source_generation_attempt_id=ATTEMPT_ID,
            source_attempt_no=4,
            grant_ordinal=1,
            target_attempt_no=5,
        )
    with pytest.raises(NearBlackRecoveryPlanError, match="precede"):
        require_near_black_seed_recovery_plan(
            plan,
            generation_job_id=JOB_ID,
            expected_output_count=3,
            original_seeds=(99, 42, 3717610135558578023),
            source_grant_audit_event_id=AUDIT_EVENT_ID,
            source_generation_attempt_id=ATTEMPT_ID,
            source_attempt_no=4,
            grant_ordinal=1,
            target_attempt_no=4,
        )


@pytest.mark.parametrize(
    (
        "source_grant_audit_event_id",
        "source_generation_attempt_id",
        "source_attempt_no",
        "grant_ordinal",
        "message",
    ),
    [
        (
            UUID("00000000-0000-0000-0000-000000000004"),
            ATTEMPT_ID,
            4,
            1,
            "grant identity",
        ),
        (
            AUDIT_EVENT_ID,
            UUID("00000000-0000-0000-0000-000000000004"),
            4,
            1,
            "attempt identity",
        ),
        (AUDIT_EVENT_ID, ATTEMPT_ID, 3, 1, "source attempt"),
        (AUDIT_EVENT_ID, ATTEMPT_ID, 4, 2, "grant ordinal"),
    ],
)
def test_plan_must_match_its_exact_source_audit_identity(
    source_grant_audit_event_id: UUID,
    source_generation_attempt_id: UUID,
    source_attempt_no: int,
    grant_ordinal: int,
    message: str,
) -> None:
    with pytest.raises(NearBlackRecoveryPlanError, match=message):
        require_near_black_seed_recovery_plan(
            _collision_plan(),
            generation_job_id=JOB_ID,
            expected_output_count=3,
            original_seeds=(99, 42, 3717610135558578023),
            source_grant_audit_event_id=source_grant_audit_event_id,
            source_generation_attempt_id=source_generation_attempt_id,
            source_attempt_no=source_attempt_no,
            grant_ordinal=grant_ordinal,
            target_attempt_no=5,
        )


def test_public_require_rejects_a_different_canonical_grant_plan() -> None:
    different_plan = build_near_black_seed_recovery_plan(
        generation_job_id=JOB_ID,
        source_grant_audit_event_id=UUID("00000000-0000-0000-0000-000000000004"),
        source_generation_attempt_id=ATTEMPT_ID,
        source_attempt_no=4,
        grant_ordinal=1,
        failed_output_index=1,
        expected_output_count=3,
        uploaded_output_indices=(0,),
        original_seeds=(99, 42, 3717610135558578023),
    )

    with pytest.raises(NearBlackRecoveryPlanError, match="grant identity"):
        _require_plan(different_plan)


def test_batched_failure_with_no_uploaded_prefix_is_valid() -> None:
    plan = build_near_black_seed_recovery_plan(
        generation_job_id=JOB_ID,
        source_grant_audit_event_id=AUDIT_EVENT_ID,
        source_generation_attempt_id=ATTEMPT_ID,
        source_attempt_no=1,
        grant_ordinal=1,
        failed_output_index=2,
        expected_output_count=3,
        uploaded_output_indices=(),
        original_seeds=(10, 11, 12),
    )

    assert plan.uploaded_output_indices == ()
    assert [rewrite.output_index for rewrite in plan.seed_rewrites] == [2]
