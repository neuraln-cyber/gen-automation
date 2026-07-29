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
    with pytest.raises(ValidationError, match="less than or equal to 100"):
        ReleaseCreate.model_validate(payload)
