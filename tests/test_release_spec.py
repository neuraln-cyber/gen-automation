from copy import deepcopy

import pytest
from pydantic import ValidationError

from gen_automation.domain.deliverability import MAX_ACCEPTED_IMAGES_PER_RELEASE
from gen_automation.domain.release_spec import ReleaseCreate, ReleaseSpecification
from tests.factories import valid_release_payload


def test_model_artifacts_must_be_safetensors() -> None:
    payload = valid_release_payload()
    payload["specification"]["checkpoint"]["storage_key"] = "models/unsafe.ckpt"  # type: ignore[index]

    with pytest.raises(ValidationError, match="Safetensors"):
        ReleaseCreate.model_validate(payload)


def test_aged_up_minor_is_rejected() -> None:
    payload = valid_release_payload()
    subject = payload["specification"]["subjects"][0]  # type: ignore[index]
    subject["is_aged_up_minor"] = True

    with pytest.raises(ValidationError, match="aged-up"):
        ReleaseCreate.model_validate(payload)


def test_release_full_set_is_bounded_by_patreon_package_capacity() -> None:
    payload = valid_release_payload()
    payload["desired_accepted_count"] = MAX_ACCEPTED_IMAGES_PER_RELEASE
    assert (
        ReleaseCreate.model_validate(payload).desired_accepted_count
        == MAX_ACCEPTED_IMAGES_PER_RELEASE
    )

    payload["desired_accepted_count"] = MAX_ACCEPTED_IMAGES_PER_RELEASE + 1
    with pytest.raises(
        ValidationError,
        match=rf"less than or equal to {MAX_ACCEPTED_IMAGES_PER_RELEASE}",
    ):
        ReleaseCreate.model_validate(payload)


def test_generation_dimensions_accept_latent_multiples_of_eight() -> None:
    payload = valid_release_payload()
    generation = payload["specification"]["generation"]  # type: ignore[index]
    generation["width"] = 1144
    generation["height"] = 1480

    parsed = ReleaseCreate.model_validate(payload)
    assert parsed.specification.generation.width == 1144
    assert parsed.specification.generation.height == 1480

    generation["width"] = 1150
    with pytest.raises(ValidationError, match="multiple of 8"):
        ReleaseCreate.model_validate(payload)


def test_legacy_and_current_generation_jobs_support_twenty_five_outputs() -> None:
    payload = valid_release_payload()
    generation = payload["specification"]["generation"]  # type: ignore[index]
    generation["outputs_per_job"] = 25
    payload["specification"]["planned_job_count"] = 1  # type: ignore[index]

    legacy = ReleaseSpecification.model_validate(payload["specification"])
    assert legacy.generation.outputs_per_job == 25
    assert legacy.worker_request_budget_version == 1

    current = ReleaseCreate.model_validate(payload)
    assert current.specification.generation.outputs_per_job == 25
    assert current.specification.worker_request_budget_version == 2

    generation["outputs_per_job"] = 26
    with pytest.raises(ValidationError, match="less than or equal to 25"):
        ReleaseSpecification.model_validate(payload["specification"])


def test_new_release_marks_current_worker_request_budget() -> None:
    parsed = ReleaseCreate.model_validate(valid_release_payload())

    assert parsed.specification.worker_request_budget_version == 2
    assert parsed.specification.model_dump(mode="json")["worker_request_budget_version"] == 2

    legacy_payload = valid_release_payload()["specification"]
    legacy = ReleaseSpecification.model_validate(legacy_payload)
    assert "worker_request_budget_version" not in legacy.model_dump(mode="json")


def test_release_supports_up_to_sixteen_loras() -> None:
    payload = valid_release_payload()
    checkpoint = payload["specification"]["checkpoint"]  # type: ignore[index]
    loras: list[dict[str, object]] = []
    for index in range(16):
        lora = deepcopy(checkpoint)
        lora.update(
            {
                "name": f"LoRA {index + 1}",
                "storage_key": f"models/lora-{index + 1}.safetensors",
                "sha256": f"{index + 1:064x}",
                "weight": 0.5,
            }
        )
        loras.append(lora)
    payload["specification"]["loras"] = loras  # type: ignore[index]

    assert len(ReleaseCreate.model_validate(payload).specification.loras) == 16

    seventeenth = deepcopy(loras[0])
    seventeenth["name"] = "LoRA 17"
    seventeenth["storage_key"] = "models/lora-17.safetensors"
    seventeenth["sha256"] = "a1" * 32
    loras.append(seventeenth)
    with pytest.raises(ValidationError, match="at most 16"):
        ReleaseCreate.model_validate(payload)


def _duo_release_payload() -> dict[str, object]:
    payload = valid_release_payload()
    specification = payload["specification"]
    assert isinstance(specification, dict)
    subjects = specification["subjects"]
    generation = specification["generation"]
    assert isinstance(subjects, list)
    assert isinstance(generation, dict)
    second_subject = deepcopy(subjects[0])
    second_subject.update(
        {
            "name": "Second Approved Adult Character",
            "canonical_source_url": "https://example.com/second-character",
        }
    )
    subjects.append(second_subject)
    workflow = specification["workflow"]
    assert isinstance(workflow, dict)
    workflow["capabilities"] = ["regional_prompting_v1"]
    generation.update(
        {
            "composition_mode": "duo",
            "character_a_prompt": "first adult character, on the left",
            "character_b_prompt": "second adult character, on the right",
        }
    )
    return payload


def test_duo_release_requires_exactly_two_distinct_subjects() -> None:
    payload = _duo_release_payload()
    specification = payload["specification"]
    assert isinstance(specification, dict)
    subjects = specification["subjects"]
    assert isinstance(subjects, list)

    subjects.pop()
    with pytest.raises(ValidationError, match="exactly two distinct subjects"):
        ReleaseCreate.model_validate(payload)

    subjects.append(deepcopy(subjects[0]))
    with pytest.raises(ValidationError, match="exactly two distinct subjects"):
        ReleaseCreate.model_validate(payload)


def test_release_batches_cannot_mix_single_and_duo_composition() -> None:
    payload = _duo_release_payload()
    specification = payload["specification"]
    assert isinstance(specification, dict)
    duo_generation = specification["generation"]
    assert isinstance(duo_generation, dict)
    single_generation = deepcopy(duo_generation)
    single_generation.update(
        {
            "composition_mode": "single",
            "character_a_prompt": "",
            "character_b_prompt": "",
        }
    )
    specification.update(
        {
            "schema_version": 2,
            "generation_batches": [
                {
                    "name": "Duo batch",
                    "image_count": 4,
                    "generation": deepcopy(duo_generation),
                },
                {
                    "name": "Single batch",
                    "image_count": 4,
                    "generation": single_generation,
                },
            ],
            "planned_job_count": 2,
        }
    )

    with pytest.raises(ValidationError, match="same composition mode"):
        ReleaseCreate.model_validate(payload)


def test_multi_output_prompt_text_has_an_early_envelope_budget() -> None:
    payload = _duo_release_payload()
    specification = payload["specification"]
    assert isinstance(specification, dict)
    generation = specification["generation"]
    assert isinstance(generation, dict)
    generation["character_a_prompt"] = "a" * 16_000
    generation["character_b_prompt"] = "b" * 16_000

    with pytest.raises(ValidationError, match="too large for one multi-output"):
        ReleaseCreate.model_validate(payload)


def test_new_release_composition_must_match_subjects_and_workflow() -> None:
    payload = valid_release_payload()
    specification = payload["specification"]
    assert isinstance(specification, dict)
    subjects = specification["subjects"]
    workflow = specification["workflow"]
    assert isinstance(subjects, list)
    assert isinstance(workflow, dict)

    subjects.append(
        {
            **deepcopy(subjects[0]),
            "name": "Second Approved Adult Character",
            "canonical_source_url": "https://example.com/second-character",
        }
    )
    with pytest.raises(ValidationError, match="exactly one subject"):
        ReleaseCreate.model_validate(payload)

    subjects.pop()
    workflow["capabilities"] = ["controlled_trio_v1"]
    with pytest.raises(ValidationError, match="non-regional workflow"):
        ReleaseCreate.model_validate(payload)


def test_new_legacy_duo_requires_regional_workflow_capability() -> None:
    payload = _duo_release_payload()
    workflow = payload["specification"]["workflow"]  # type: ignore[index]
    assert isinstance(workflow, dict)
    workflow["capabilities"] = []

    with pytest.raises(ValidationError, match="requires regional prompting"):
        ReleaseCreate.model_validate(payload)
