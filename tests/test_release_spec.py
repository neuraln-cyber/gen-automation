from copy import deepcopy

import pytest
from pydantic import ValidationError

from gen_automation.domain.deliverability import MAX_ACCEPTED_IMAGES_PER_RELEASE
from gen_automation.domain.release_spec import ReleaseCreate
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


def test_generation_job_accepts_twenty_five_outputs_and_rejects_twenty_six() -> None:
    payload = valid_release_payload()
    generation = payload["specification"]["generation"]  # type: ignore[index]
    generation["outputs_per_job"] = 25
    payload["specification"]["planned_job_count"] = 1  # type: ignore[index]

    parsed = ReleaseCreate.model_validate(payload)
    assert parsed.specification.generation.outputs_per_job == 25

    generation["outputs_per_job"] = 26
    with pytest.raises(ValidationError, match="less than or equal to 25"):
        ReleaseCreate.model_validate(payload)


def test_release_supports_up_to_eight_loras() -> None:
    payload = valid_release_payload()
    checkpoint = payload["specification"]["checkpoint"]  # type: ignore[index]
    loras: list[dict[str, object]] = []
    for index in range(8):
        lora = deepcopy(checkpoint)
        lora.update(
            {
                "name": f"LoRA {index + 1}",
                "storage_key": f"models/lora-{index + 1}.safetensors",
                "sha256": f"{index + 1:x}" * 64,
                "weight": 0.5,
            }
        )
        loras.append(lora)
    payload["specification"]["loras"] = loras  # type: ignore[index]

    assert len(ReleaseCreate.model_validate(payload).specification.loras) == 8

    ninth = deepcopy(loras[0])
    ninth["name"] = "LoRA 9"
    ninth["storage_key"] = "models/lora-9.safetensors"
    ninth["sha256"] = "9" * 64
    loras.append(ninth)
    with pytest.raises(ValidationError, match="at most 8"):
        ReleaseCreate.model_validate(payload)
