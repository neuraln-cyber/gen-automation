from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from gen_automation.api.routes.i2v import retry_job


class _Result:
    def __init__(self, row: object) -> None:
        self.row = row

    def one_or_none(self) -> object:
        return self.row


class _Session:
    def __init__(self, row: object) -> None:
        self.row = row

    async def execute(self, _statement: object) -> _Result:
        return _Result(self.row)


class _Request:
    class _App:
        class _State:
            class _Settings:
                i2v_hires_profile_enabled = True
                i2v_lora_profile_enabled = True

            settings = _Settings()

        state = _State()

    app = _App()


class _Principal:
    user_id = uuid4()


@pytest.mark.asyncio
async def test_retry_rejects_legacy_arbitrary_filename_loras_before_requeue() -> None:
    with pytest.raises(HTTPException) as caught:
        await retry_job(  # type: ignore[arg-type]
            job_id=uuid4(),
            request=_Request(),
            session=_Session(
                (
                    {
                        "loras": [
                            {
                                "high": "arbitrary-high.safetensors",
                                "low": "arbitrary-low.safetensors",
                                "strength": 1,
                            }
                        ]
                    },
                    "motion",
                )
            ),
            principal=_Principal(),
        )

    assert caught.value.status_code == 422
    assert "frozen I2V job has invalid LoRA settings" in str(caught.value.detail)


@pytest.mark.asyncio
async def test_retry_rejects_frozen_dream_prompt_conflict_before_requeue() -> None:
    with pytest.raises(HTTPException) as caught:
        await retry_job(  # type: ignore[arg-type]
            job_id=uuid4(),
            request=_Request(),
            session=_Session(
                (
                    {
                        "loras": [
                            {
                                "catalog_id": "dr34ml4y-aio-nsfw-wan22-v2",
                                "strength": 0.7,
                            }
                        ]
                    },
                    "m15510n4ry followed by bl0wj0b",
                )
            ),
            principal=_Principal(),
        )

    assert caught.value.status_code == 422
    assert "mutually exclusive" in str(caught.value.detail)
