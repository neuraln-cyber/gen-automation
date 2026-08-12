from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr

from gen_automation.i2v_worker.app import create_i2v_worker_app
from gen_automation.i2v_worker.settings import I2VWorkerSettings

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflows/dasiwa-wan22-i2v-v1.api.json"
SHA = "a" * 64


def _settings(tmp_path: Path) -> I2VWorkerSettings:
    models = []
    for role, install_path in (
        ("diffusion_model_high", "models/diffusion_models/high.safetensors"),
        ("diffusion_model_low", "models/diffusion_models/low.safetensors"),
        ("text_encoder", "models/text_encoders/text.safetensors"),
        ("vae", "models/vae/Wan/vae.safetensors"),
    ):
        models.append(
            {
                "role": role,
                "bucket": "models",
                "key": f"worker/i2v/sha256/{SHA}",
                "version_id": "v1",
                "byte_size": 1,
                "sha256": SHA,
                "install_path": install_path,
            }
        )
    return I2VWorkerSettings(
        model_objects_json=SecretStr(json.dumps(models)),
        environment="test",
        comfy_root=tmp_path / "comfy",
        runtime_root=tmp_path / "runtime",
        workflow_template=WORKFLOW,
        queue_worker_enabled=False,
    )


class _Comfy:
    async def ready(self) -> bool:
        return True

    async def execute(self, _workflow: dict[str, Any], _output: Path) -> tuple[Path, ...]:
        return (Path("frame.png"),)


class _Supervisor:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.failed = False
        self.comfy_client = _Comfy() if ready else None
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


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
            "object_version_id": "input-v1",
            "sha256": SHA,
            "content_type": "image/png",
            "width": 576,
            "height": 1024,
            "byte_size": 1,
        },
        "positive_prompt": "subtle natural motion",
        "negative_prompt": "jitter",
        "settings_snapshot": {},
        "input_grant": {
            "method": "GET",
            "url": "http://input.test/source",
            "expires_at": expires,
        },
        "output_grant": {
            "method": "PUT",
            "url": "http://output.test/video",
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


def test_health_is_early_but_readiness_waits_for_models_and_comfy(tmp_path: Path) -> None:
    supervisor = _Supervisor(ready=False)
    app = create_i2v_worker_app(_settings(tmp_path), supervisor=supervisor)  # type: ignore[arg-type]

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert client.post("/jobs/i2v", json=_job()).status_code == 503
    assert supervisor.started and supervisor.stopped


def test_generation_returns_exact_wire_result_and_cleans_runtime(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings = _settings(tmp_path)
    supervisor = _Supervisor()

    async def download(*_args: Any, **_kwargs: Any) -> None:
        destination = _args[2]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x")

    def encode(_frames: Any, generation: Any, job_root: Path) -> tuple[Path, dict[str, Any]]:
        output = job_root / "output.mp4"
        output.write_bytes(b"video")
        return output, {
            "width": generation.width,
            "height": generation.height,
            "frame_count": generation.frame_count,
            "fps": float(generation.fps),
            "duration_ms": 5063,
            "codec": "h264",
            "pixel_format": "yuv420p",
            "faststart": True,
        }

    async def upload(*_args: Any, **_kwargs: Any) -> tuple[str, int, str]:
        return "output-v1", 5, SHA

    monkeypatch.setattr("gen_automation.i2v_worker.app.download_input", download)
    monkeypatch.setattr("gen_automation.i2v_worker.app.encode_video", encode)
    monkeypatch.setattr("gen_automation.i2v_worker.app.upload_video", upload)
    app = create_i2v_worker_app(settings, supervisor=supervisor)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.post("/jobs/i2v", json=_job())

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["schema"] == "i2v-salad-result/v1"
    assert result["output"]["frame_count"] == 81
    assert result["output"]["metadata"]["codec"] == "h264"
    assert list((settings.runtime_root / "jobs").glob("*")) == []


def test_worker_rejects_unbounded_or_invalid_request_bodies(tmp_path: Path) -> None:
    app = create_i2v_worker_app(
        _settings(tmp_path),
        supervisor=_Supervisor(),  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        wrong_type = client.post(
            "/jobs/i2v",
            content=b"{}",
            headers={"content-type": "text/plain"},
        )
        assert wrong_type.status_code == 415
        assert client.post("/jobs/i2v", json={}).status_code == 400
