from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from gen_automation.i2v_worker.artifacts import ModelBootstrapError, S3ModelBootstrapper
from gen_automation.i2v_worker.models import ModelObject
from gen_automation.i2v_worker.settings import I2VWorkerSettings


class _Body(io.BytesIO):
    pass


class _S3:
    def __init__(self, content: bytes, version_id: str) -> None:
        self.content = content
        self.version_id = version_id
        self.ranges: list[str] = []

    def get_object(self, **kwargs: str) -> dict[str, object]:
        assert kwargs["VersionId"] == self.version_id
        raw_range = kwargs["Range"]
        self.ranges.append(raw_range)
        start, end = (int(value) for value in raw_range.removeprefix("bytes=").split("-"))
        part = self.content[start : end + 1]
        return {
            "VersionId": self.version_id,
            "ContentLength": len(part),
            "ContentRange": f"bytes {start}-{end}/{len(self.content)}",
            "Body": _Body(part),
        }


def _settings(tmp_path: Path, model: ModelObject) -> I2VWorkerSettings:
    roles = [
        "diffusion_model_high",
        "diffusion_model_low",
        "text_encoder",
        "vae",
    ]
    values = []
    for role in roles:
        value = model.model_copy(
            update={
                "role": role,
                "key": f"worker/i2v/sha256/{model.sha256}",
                "install_path": {
                    "diffusion_model_high": "models/diffusion_models/high.safetensors",
                    "diffusion_model_low": "models/diffusion_models/low.safetensors",
                    "text_encoder": "models/text_encoders/text.safetensors",
                    "vae": "models/vae/Wan/vae.safetensors",
                }[role],
            }
        )
        values.append(value.model_dump(mode="json"))
    return I2VWorkerSettings(
        model_objects_json=SecretStr(json.dumps(values)),
        environment="test",
        comfy_root=tmp_path / "comfy",
        runtime_root=tmp_path / "runtime",
        artifact_chunk_bytes=1024 * 1024,
    )


@pytest.mark.asyncio
async def test_model_download_is_version_pinned_resumable_and_hash_verified(tmp_path: Path) -> None:
    content = b"verified-model-bytes"
    model = ModelObject(
        role="diffusion_model_high",
        bucket="models",
        key="worker/i2v/sha256/" + hashlib.sha256(content).hexdigest(),
        version_id="version-1",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        install_path="models/diffusion_models/model.safetensors",
    )
    settings = _settings(tmp_path, model)
    installed_model = settings.model_objects[0]
    target = settings.comfy_root / installed_model.install_path
    target.parent.mkdir(parents=True)
    partial = target.with_name(f".{target.name}.partial")
    partial.write_bytes(content[:8])
    client = _S3(content, model.version_id)

    materialized = await S3ModelBootstrapper(settings, client=client).bootstrap()

    assert materialized[0].read_bytes() == content
    assert client.ranges[0] == f"bytes=8-{len(content) - 1}"
    assert not partial.exists()


@pytest.mark.asyncio
async def test_existing_wrong_model_fails_closed_without_overwrite(tmp_path: Path) -> None:
    content = b"correct"
    model = ModelObject(
        role="diffusion_model_high",
        bucket="models",
        key="worker/i2v/sha256/" + hashlib.sha256(content).hexdigest(),
        version_id="version-1",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        install_path="models/diffusion_models/model.safetensors",
    )
    settings = _settings(tmp_path, model)
    target = settings.comfy_root / settings.model_objects[0].install_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"wrong!!")

    with pytest.raises(ModelBootstrapError):
        await S3ModelBootstrapper(settings, client=_S3(content, model.version_id)).bootstrap()
    assert target.read_bytes() == b"wrong!!"
