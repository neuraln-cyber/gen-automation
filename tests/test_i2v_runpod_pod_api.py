from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr

from gen_automation.i2v_worker.runpod_pod_api import create_runpod_pod_app
from gen_automation.i2v_worker.settings import I2VWorkerSettings

SHA = "a" * 64
KEY = "b" * 64
JOB_ID = "rpod_podabc123_" + "c" * 32


def _settings(tmp_path: Path) -> I2VWorkerSettings:
    objects = [
        {
            "role": role,
            "bucket": "models",
            "key": f"worker/i2v/sha256/{SHA}",
            "version_id": "v1",
            "byte_size": 1,
            "sha256": SHA,
            "install_path": install_path,
        }
        for role, install_path in (
            ("diffusion_model_high", "models/diffusion_models/high.safetensors"),
            ("diffusion_model_low", "models/diffusion_models/low.safetensors"),
            ("text_encoder", "models/text_encoders/text.safetensors"),
            ("vae", "models/vae/Wan/vae.safetensors"),
        )
    ]
    return I2VWorkerSettings(
        model_objects_json=SecretStr(json.dumps(objects)),
        environment="test",
        comfy_root=tmp_path / "comfy",
        runtime_root=tmp_path / "runtime",
        workflow_template=tmp_path / "workflow.json",
        lora_worker_enabled=False,
    )


def _event(job_id: str = JOB_ID) -> dict[str, object]:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    return {
        "id": job_id,
        "input": {
            "schema": "i2v-runpod-input/v1",
            "submission_key": "d" * 64,
            "job": {
                "schema": "i2v-job/v2",
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
                    "width": 768,
                    "height": 992,
                    "byte_size": 1,
                },
                "positive_prompt": "subtle motion",
                "negative_prompt": "talking",
                "settings_snapshot": {},
                "input_grant": {
                    "method": "GET",
                    "url": "https://private.example/input",
                    "expires_at": expires,
                },
                "output_grant": {
                    "method": "PUT",
                    "url": "https://private.example/output",
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
            },
            "claim": {
                "method": "POST",
                "url": "https://staging.example/api/v1/i2v/runpod/claim",
                "bearer_token": "claim-token-" + "x" * 40,
                "expires_at": expires,
            },
            "model_grants": [
                {
                    "role": "diffusion_model_high",
                    "method": "GET",
                    "url": "https://private.example/model",
                    "expires_at": expires,
                    "byte_size": 1,
                    "sha256": SHA,
                }
            ],
        },
    }


class _Handler:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def __call__(self, event: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"accepted": event["id"]}

    def close(self) -> None:
        self.closed = True


def _wait_terminal(client: TestClient, job_id: str) -> dict[str, object]:
    for _ in range(100):
        response = client.get(f"/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {KEY}"})
        payload = response.json()
        if payload["status"] != "IN_PROGRESS":
            return payload
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_pod_api_is_authenticated_idempotent_and_reports_terminal_output(
    tmp_path: Path,
) -> None:
    handler = _Handler()
    app = create_runpod_pod_app(
        settings=_settings(tmp_path),
        api_key=KEY,
        handler=handler,  # type: ignore[arg-type]
    )
    event = _event()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 401
        ready = client.get("/ready", headers={"Authorization": f"Bearer {KEY}"})
        assert ready.status_code == 200
        assert ready.json()["provider"] == "runpod-pod"
        submitted = client.post(
            f"/v1/jobs/{JOB_ID}",
            headers={"Authorization": f"Bearer {KEY}"},
            json=event,
        )
        assert submitted.status_code == 202
        duplicate = client.post(
            f"/v1/jobs/{JOB_ID}",
            headers={"Authorization": f"Bearer {KEY}"},
            json=event,
        )
        assert duplicate.status_code == 202
        terminal = _wait_terminal(client, JOB_ID)
        assert terminal["status"] == "COMPLETED"
        assert terminal["output"] == {"accepted": JOB_ID}
        assert handler.calls == 1
    assert handler.closed


def test_pod_api_rejects_identity_conflict_without_second_execution(tmp_path: Path) -> None:
    handler = _Handler()
    app = create_runpod_pod_app(
        settings=_settings(tmp_path),
        api_key=KEY,
        handler=handler,  # type: ignore[arg-type]
    )
    event = _event()
    with TestClient(app) as client:
        first = client.post(
            f"/v1/jobs/{JOB_ID}",
            headers={"Authorization": f"Bearer {KEY}"},
            json=event,
        )
        assert first.status_code == 202
        changed = json.loads(json.dumps(event))
        changed["input"]["job"]["positive_prompt"] = "different"  # type: ignore[index]
        conflict = client.post(
            f"/v1/jobs/{JOB_ID}",
            headers={"Authorization": f"Bearer {KEY}"},
            json=changed,
        )
        assert conflict.status_code == 409
        _wait_terminal(client, JOB_ID)
        assert handler.calls == 1


class _BlockingHandler(_Handler):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, event: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError
        return {"accepted": event["id"]}


def test_pod_api_enforces_single_flight(tmp_path: Path) -> None:
    handler = _BlockingHandler()
    app = create_runpod_pod_app(
        settings=_settings(tmp_path),
        api_key=KEY,
        handler=handler,  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        first = client.post(
            f"/v1/jobs/{JOB_ID}",
            headers={"Authorization": f"Bearer {KEY}"},
            json=_event(),
        )
        assert first.status_code == 202
        assert handler.started.wait(timeout=1)
        second_id = "rpod_podabc123_" + "e" * 32
        second = client.post(
            f"/v1/jobs/{second_id}",
            headers={"Authorization": f"Bearer {KEY}"},
            json=_event(second_id),
        )
        assert second.status_code == 409
        handler.release.set()
        _wait_terminal(client, JOB_ID)
