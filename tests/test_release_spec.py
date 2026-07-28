import pytest
from pydantic import ValidationError

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
