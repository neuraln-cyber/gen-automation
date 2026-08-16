from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from gen_automation.i2v_worker.artifacts import ModelBootstrapError
from gen_automation.i2v_worker.runpod_models import RunPodModelGrant
from gen_automation.i2v_worker.runpod_volume import RunPodVolumeBootstrapper
from gen_automation.i2v_worker.settings import I2VWorkerSettings


class _Response:
    status_code = 206

    def __init__(self, body: bytes, start: int, end: int, total: int) -> None:
        self._body = body
        self.headers = {
            "content-range": f"bytes {start}-{end}/{total}",
            "content-length": str(len(body)),
        }

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_bytes(self) -> tuple[bytes, ...]:
        return (self._body,)


class _Client:
    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies
        self.calls: list[tuple[str, int, int]] = []

    def stream(self, _method: str, url: str, *, headers: dict[str, str]) -> _Response:
        body = self.bodies[url]
        raw_range = headers["Range"].removeprefix("bytes=")
        start, end = (int(value) for value in raw_range.split("-", 1))
        self.calls.append((url, start, end))
        return _Response(body[start : end + 1], start, end, len(body))

    def close(self) -> None:
        raise AssertionError("injected client must not be closed")


def _settings(tmp_path: Path) -> tuple[I2VWorkerSettings, dict[str, bytes]]:
    bodies = {
        "https://models.example/high": b"high-model",
        "https://models.example/low": b"low-model",
        "https://models.example/text": b"text-model",
        "https://models.example/vae": b"vae-model",
    }
    roles = (
        ("diffusion_model_high", "models/diffusion_models/high.safetensors", "high"),
        ("diffusion_model_low", "models/diffusion_models/low.safetensors", "low"),
        ("text_encoder", "models/text_encoders/text.safetensors", "text"),
        ("vae", "models/vae/Wan/vae.safetensors", "vae"),
    )
    objects: list[dict[str, Any]] = []
    for role, install_path, name in roles:
        body = bodies[f"https://models.example/{name}"]
        digest = hashlib.sha256(body).hexdigest()
        objects.append(
            {
                "role": role,
                "bucket": "private-models",
                "key": f"worker/i2v/sha256/{digest}",
                "version_id": f"version-{name}",
                "byte_size": len(body),
                "sha256": digest,
                "install_path": install_path,
            }
        )
    return (
        I2VWorkerSettings(
            model_objects_json=SecretStr(json.dumps(objects)),
            environment="test",
            comfy_root=tmp_path / "comfy",
            runtime_root=tmp_path / "runtime",
            volume_root=tmp_path / "volume",
            artifact_chunk_bytes=1024 * 1024,
        ),
        bodies,
    )


@pytest.mark.skipif(os.name == "nt", reason="Windows symlinks require developer mode")
def test_volume_hydrates_once_and_reuses_verified_immutable_cache(tmp_path: Path) -> None:
    settings, bodies = _settings(tmp_path)
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    grants = tuple(
        RunPodModelGrant(
            role=model.role,
            url=f"https://models.example/{name}",
            expires_at=expires_at,
            byte_size=model.byte_size,
            sha256=model.sha256,
        )
        for model, name in zip(
            settings.model_objects,
            ("high", "low", "text", "vae"),
            strict=True,
        )
    )
    client = _Client(bodies)
    bootstrapper = RunPodVolumeBootstrapper(settings, http_client=client)  # type: ignore[arg-type]

    first = bootstrapper.bootstrap(grants)
    second = bootstrapper.bootstrap(grants)

    assert first == second
    assert len(client.calls) == 4
    assert all(path.is_symlink() for path in first)
    assert [path.read_bytes() for path in first] == list(bodies.values())


def test_volume_execution_claim_rejects_same_paid_submission_twice(tmp_path: Path) -> None:
    settings, _bodies = _settings(tmp_path)
    bootstrapper = RunPodVolumeBootstrapper(settings)
    submission_key = "a" * 64

    bootstrapper.claim_execution(
        submission_key=submission_key,
        provider_job_id="runpod-job-1",
    )

    with pytest.raises(ModelBootstrapError, match="already claimed"):
        bootstrapper.claim_execution(
            submission_key=submission_key,
            provider_job_id="runpod-job-1",
        )


@pytest.mark.skipif(os.name == "nt", reason="Windows symlinks require developer mode")
def test_volume_adopts_preseeded_files_without_network_download(tmp_path: Path) -> None:
    settings, bodies = _settings(tmp_path)
    root = settings.volume_root / "gen-automation/i2v" / settings.artifact_identity_sha256
    root.mkdir(parents=True)
    for model, body in zip(settings.model_objects, bodies.values(), strict=True):
        (root / f"{model.sha256}.safetensors").write_bytes(body)
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    grants = tuple(
        RunPodModelGrant(
            role=model.role,
            url=f"https://models.example/{name}",
            expires_at=expires_at,
            byte_size=model.byte_size,
            sha256=model.sha256,
        )
        for model, name in zip(
            settings.model_objects,
            ("high", "low", "text", "vae"),
            strict=True,
        )
    )
    client = _Client(bodies)
    bootstrapper = RunPodVolumeBootstrapper(settings, http_client=client)  # type: ignore[arg-type]

    installed = bootstrapper.bootstrap(grants)

    assert client.calls == []
    assert all(path.is_symlink() for path in installed)
    assert all(
        (root / f"{model.sha256}.safetensors").stat().st_mode & 0o222 == 0
        for model in settings.model_objects
    )
