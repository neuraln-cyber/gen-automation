from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from gen_automation.i2v_worker.models import GenerationSettings, I2VJob, ModelObject
from gen_automation.i2v_worker.settings import I2VWorkerSettings
from gen_automation.i2v_worker.supervisor import _comfy_command
from gen_automation.i2v_worker.workflow import load_workflow_template, render_workflow

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile.i2v-worker"
LOCK = ROOT / "requirements-i2v-worker.lock"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
WORKFLOW = ROOT / "workflows/dasiwa-wan22-i2v-v1.api.json"
SHA = "a" * 64


def _objects() -> list[dict[str, object]]:
    return [
        {
            "role": role,
            "bucket": "private-models",
            "key": f"worker/i2v/sha256/{index:064x}",
            "version_id": f"version-{index}",
            "byte_size": index,
            "sha256": f"{index:064x}",
            "install_path": path,
        }
        for index, (role, path) in enumerate(
            (
                ("diffusion_model_high", "models/diffusion_models/high.safetensors"),
                ("diffusion_model_low", "models/diffusion_models/low.safetensors"),
                ("text_encoder", "models/text_encoders/text.safetensors"),
                ("vae", "models/vae/Wan/vae.safetensors"),
            ),
            start=1,
        )
    ]


def _settings(tmp_path: Path) -> I2VWorkerSettings:
    return I2VWorkerSettings(
        model_objects_json=SecretStr(json.dumps(_objects())),
        environment="test",
        comfy_root=tmp_path / "comfy",
        runtime_root=tmp_path / "runtime",
        workflow_template=WORKFLOW,
        comfy_python=tmp_path / "venv/python",
        comfy_main=tmp_path / "comfy/main.py",
        queue_worker_enabled=False,
    )


def _job() -> dict[str, object]:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    return {
        "schema": "i2v-salad-job/v1",
        "job_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "request_sha256": SHA,
        "input_snapshot": {
            "storage_backend": "s3",
            "storage_bucket": "inputs",
            "object_key": "i2v/input.png",
            "object_version_id": "v1",
            "sha256": SHA,
            "content_type": "image/png",
            "width": 576,
            "height": 1024,
            "byte_size": 100,
        },
        "positive_prompt": "gentle natural motion",
        "negative_prompt": "camera shake",
        "settings_snapshot": {},
        "input_grant": {"method": "GET", "url": "https://example.test/in", "expires_at": expires},
        "output_grant": {
            "method": "PUT",
            "url": "https://example.test/out",
            "headers": {
                "Content-Type": "video/mp4",
                "Cache-Control": "private, no-store, max-age=0",
                "x-amz-server-side-encryption": "AES256",
            },
            "storage_backend": "s3",
            "storage_bucket": "outputs",
            "object_key": "i2v/output.mp4",
            "expires_at": expires,
        },
    }


def test_job_contract_is_strict_and_uses_wire_schema_alias() -> None:
    parsed = I2VJob.model_validate_json(json.dumps(_job()), strict=True)

    assert parsed.schema_version == "i2v-salad-job/v1"
    assert parsed.model_dump(mode="json", by_alias=True)["schema"] == "i2v-salad-job/v1"
    invalid = _job()
    invalid["unexpected"] = True
    with pytest.raises(ValidationError):
        I2VJob.model_validate_json(json.dumps(invalid), strict=True)


def test_baseline_accepts_wan_shape_but_experimental_features_fail_closed() -> None:
    assert GenerationSettings().frame_count == 81
    with pytest.raises(ValidationError):
        GenerationSettings(frame_count=80)
    with pytest.raises(ValidationError):
        GenerationSettings(loras=[{"filename": "unreviewed.safetensors"}])
    with pytest.raises(ValidationError):
        GenerationSettings(tiled_vae=True)


def test_model_objects_are_exact_versioned_and_confined_to_comfy_models() -> None:
    assert ModelObject.model_validate(_objects()[0]).install_path.startswith("models/")
    invalid = dict(_objects()[0])
    invalid["install_path"] = "models/../main.py"
    with pytest.raises(ValidationError):
        ModelObject.model_validate(invalid)


def test_settings_require_every_baseline_role_once(tmp_path: Path) -> None:
    assert len(_settings(tmp_path).model_objects) == 4
    with pytest.raises(ValidationError):
        I2VWorkerSettings(
            model_objects_json=SecretStr(json.dumps(_objects()[:3])),
            comfy_root=tmp_path / "comfy",
            runtime_root=tmp_path / "runtime",
        )


def test_workflow_renders_all_runtime_values_without_mutating_template() -> None:
    template = load_workflow_template(WORKFLOW)
    original = json.dumps(template, sort_keys=True)
    job_id, attempt_id = uuid4(), uuid4()
    rendered, seed, prefix = render_workflow(
        template,
        input_filename="source.png",
        positive_prompt="slow hip sway",
        negative_prompt="jitter",
        settings=GenerationSettings(seed=42),
        job_id=job_id,
        attempt_id=attempt_id,
    )

    assert seed == 42
    assert rendered["1"]["inputs"]["image"] == "source.png"
    assert rendered["11"]["inputs"]["end_at_step"] == 2
    assert rendered["12"]["inputs"]["start_at_step"] == 2
    assert rendered["14"]["inputs"]["filename_prefix"] == prefix
    assert "$i2v" not in json.dumps(rendered)
    assert json.dumps(template, sort_keys=True) == original


def test_comfy_command_uses_supported_base_directory_and_no_custom_nodes(tmp_path: Path) -> None:
    command = _comfy_command(_settings(tmp_path))

    assert command[command.index("--base-directory") + 1] == (tmp_path / "comfy").as_posix()
    assert "--models-directory" not in command
    assert "--disable-all-custom-nodes" in command
    assert "--highvram" in command


def test_image_is_model_free_pinned_and_non_root() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    go_image = (
        "golang:1.26.2-alpine@"
        "sha256:f85330846cde1e57ca9ec309382da3b8e6ae3ab943d2739500e08c86393a21b1"
    )
    pytorch_image = (
        "pytorch/pytorch:2.9.1-cuda12.8-cudnn9-runtime@"
        "sha256:7b324d212a4450795b49edba9949b7cdc72429148a64e974334bfe5774d51385"
    )

    assert from_lines == [
        f"FROM {go_image} AS salad-queue-worker-builder",
        f"FROM {pytorch_image}",
    ]
    assert all("@sha256:" in line for line in from_lines)
    assert "c2bcbecd82ec5ae66594340b395c24ef0217b238" in dockerfile
    assert "USER 10002:10002" in dockerfile
    assert "COPY i2v-models" not in dockerfile
    assert not re.search(r"(?:civitai|huggingface)\.com/.+safetensors", dockerfile)
    assert "--start-period=60m" in dockerfile
    assert "--system-site-packages" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--no-deps" in dockerfile
    assert "sys.version_info[:2] == (3, 11)" in dockerfile
    assert "--python-version 3.11" in LOCK.read_text(encoding="utf-8")
    for package in ("torch==2.9.1", "torchvision==0.24.1", "torchaudio==2.9.1"):
        assert package in LOCK.read_text(encoding="utf-8")
    assert "'/opt/i2v-venv/' not in module.__file__" in dockerfile
    assert (
        hashlib.sha256(
            (ROOT / "patches/salad-queue-worker/strict-http-status.patch").read_bytes()
        ).hexdigest()
        in dockerfile
    )


def test_ci_builds_smokes_and_scans_the_model_free_worker() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "docker build" in workflow
    assert "--file Dockerfile.i2v-worker" in workflow
    assert "--tag gen-automation-i2v-worker:test" in workflow
    assert "import gen_automation.i2v_worker.main" in workflow
    assert "image: gen-automation-i2v-worker:test" in workflow
    assert "output-file: i2v-worker.spdx.json" in workflow
    assert "sbom: i2v-worker.spdx.json" in workflow
