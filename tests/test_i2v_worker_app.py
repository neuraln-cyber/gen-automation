from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from gen_automation.i2v_worker.app import create_i2v_worker_app
from gen_automation.i2v_worker.lora_catalog import LORA_ARTIFACTS_BY_ROLE
from gen_automation.i2v_worker.settings import I2VWorkerSettings

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflows/dasiwa-wan22-i2v-v1.api.json"
SHA = "a" * 64


def _settings(tmp_path: Path, *, lora_worker_enabled: bool = True) -> I2VWorkerSettings:
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
    if lora_worker_enabled:
        for role, artifact in LORA_ARTIFACTS_BY_ROLE.items():
            models.append(
                {
                    "role": role,
                    "bucket": "models",
                    "key": f"worker/i2v/sha256/{artifact.sha256}",
                    "version_id": "v1",
                    "byte_size": artifact.byte_size,
                    "sha256": artifact.sha256,
                    "install_path": artifact.install_path,
                }
            )
    return I2VWorkerSettings(
        model_objects_json=SecretStr(json.dumps(models)),
        environment="test",
        comfy_root=tmp_path / "comfy",
        runtime_root=tmp_path / "runtime",
        workflow_template=WORKFLOW,
        queue_worker_enabled=False,
        lora_worker_enabled=lora_worker_enabled,
        source_revision="b" * 40,
        private_manifest_source_sha256="c" * 64,
    )


class _Comfy:
    async def ready(self) -> bool:
        return True

    async def execute(self, _workflow: dict[str, Any], _output: Path) -> tuple[Path, ...]:
        return (Path("frame.png"),)


class _Supervisor:
    def __init__(self, *, ready: bool = True, face_ready: bool = True) -> None:
        self.ready = ready
        self.failed = False
        self.comfy_client = _Comfy() if ready else None
        self.face_detector = object() if ready and face_ready else None
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


def test_ready_reports_nonsecret_exact_worker_capability_identity(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_i2v_worker_app(
        settings,
        supervisor=_Supervisor(),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    capability = response.json()["capability"]
    assert capability == {
        "schema": "gen-automation/i2v-worker-capability/v1",
        "lora_worker_enabled": True,
        "private_manifest_source_sha256": "c" * 64,
        "model_objects_sha256": settings.model_objects_sha256,
        "artifact_identity_sha256": settings.artifact_identity_sha256,
        "model_roles": [item.role for item in settings.model_objects],
        "source_revision": "b" * 40,
        "face_stabilizer": {
            "schema": "gen-automation/i2v-face-stabilizer-capability/v2",
            "algorithm": "dasiwa-static-source-head-single-blink/v2",
            "guard_profile": "static-head-near-white-v2",
            "guard_profile_sha256": (
                "a365a25d5bfc151c4b7ff81a8bd806422b7988af17802f4a717c16dd70df1345"
            ),
            "detector_revision": "7db835de7a3a052eb4d68d241ae9f2cf28a0b509",
            "detector_wheel_sha256": (
                "9a6a8c1384b7a57fab8ce9988f814271ff88bac52a9dd871490a28b61dff7692"
            ),
            "yolo_sha256": ("23bbc708146bcbc1c910f00fe152adbc70d7658d875a0121eaf4ee61d978b2c4"),
            "hrnet_sha256": ("e71271376406a743c01528a0460637fcc06e72aeeea583f85007cc72dc8b7a4a"),
            "opencv_sha256": ("211e581f5a4670acbbe08fff36a35e9946039d2eea28b80394632d036d1be527"),
        },
    }
    assert "model_objects_json" not in response.text.casefold()
    assert "storage_bucket" not in response.text.casefold()

    with TestClient(app) as client:
        exact = client.get(
            f"/ready/capability/{'c' * 64}/{settings.artifact_identity_sha256}/{'b' * 40}"
        )
        wrong = client.get(
            f"/ready/capability/{'d' * 64}/{settings.artifact_identity_sha256}/{'b' * 40}"
        )
    assert exact.status_code == 200
    assert wrong.status_code == 404


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

    def prepare(source: Path, destination: Path, **_kwargs: Any) -> None:
        destination.write_bytes(source.read_bytes())

    def encode(
        _frames: Any,
        generation: Any,
        job_root: Path,
        *,
        source_width: int,
        source_height: int,
    ) -> tuple[Path, dict[str, Any]]:
        output = job_root / "output.mp4"
        output.write_bytes(b"video")
        assert (source_width, source_height) == (576, 1024)
        return output, {
            "width": generation.width,
            "height": generation.height,
            "frame_count": generation.frame_count,
            "fps": float(generation.fps),
            "duration_ms": 5063,
            "codec": "h264",
            "pixel_format": "yuv420p",
            "faststart": True,
            "native_width": generation.width,
            "native_height": generation.height,
            "upscale": generation.upscale,
            "loop_mode": "none",
            "loop_count": 1,
            "source_fit": "contain_edge_pad",
            "match_source_aspect": generation.match_source_aspect,
        }

    async def upload(*_args: Any, **_kwargs: Any) -> tuple[str, int, str]:
        return "output-v1", 5, SHA

    monkeypatch.setattr("gen_automation.i2v_worker.app.download_input", download)
    monkeypatch.setattr("gen_automation.i2v_worker.app.prepare_input_image", prepare)
    monkeypatch.setattr("gen_automation.i2v_worker.app.encode_video", encode)
    monkeypatch.setattr("gen_automation.i2v_worker.app.upload_video", upload)
    monkeypatch.setattr(
        "gen_automation.i2v_worker.app.preflight_source_face",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("off preflight")),
    )
    monkeypatch.setattr(
        "gen_automation.i2v_worker.app.stabilize_face_frames",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("off stabilize")),
    )
    app = create_i2v_worker_app(settings, supervisor=supervisor)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.post("/jobs/i2v", json=_job())

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["schema"] == "i2v-salad-result/v1"
    assert result["output"]["frame_count"] == 81
    assert result["output"]["metadata"]["codec"] == "h264"
    assert result["output"]["metadata"]["loras"] == []
    assert result["output"]["metadata"]["effective_positive_prompt"] == ("subtle natural motion")
    assert result["output"]["metadata"]["effective_negative_prompt"] == "jitter"
    assert result["output"]["metadata"]["face_fidelity"] == "off"
    assert list((settings.runtime_root / "jobs").glob("*")) == []


def test_generation_records_effective_face_fidelity_contract(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings = _settings(tmp_path)
    supervisor = _Supervisor()
    captured: dict[str, Any] = {}
    events: list[str] = []

    async def download(*_args: Any, **_kwargs: Any) -> None:
        destination = _args[2]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x")

    def prepare(source: Path, destination: Path, **_kwargs: Any) -> None:
        events.append("prepare")
        destination.write_bytes(source.read_bytes())

    async def execute(workflow: dict[str, Any], _output: Path) -> tuple[Path, ...]:
        events.append("execute")
        captured["workflow"] = workflow
        return (Path("frame.png"),)

    def encode(
        _frames: Any,
        generation: Any,
        job_root: Path,
        **_kwargs: Any,
    ) -> tuple[Path, dict[str, Any]]:
        events.append("encode")
        assert _frames == (Path("stabilized.png"),)
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
            "native_width": generation.width,
            "native_height": generation.height,
            "upscale": generation.upscale,
            "loop_mode": "none",
            "loop_count": 1,
            "source_fit": "contain_edge_pad",
            "match_source_aspect": generation.match_source_aspect,
        }

    async def upload(*_args: Any, **_kwargs: Any) -> tuple[str, int, str]:
        events.append("upload")
        return "output-v1", 5, SHA

    source_token = object()

    def preflight_source(*_args: Any, **_kwargs: Any) -> object:
        events.append("source_preflight")
        captured["preflight_before_execute"] = "workflow" not in captured
        return source_token

    def stabilize(
        _prepared: Path,
        frames: tuple[Path, ...],
        destination: Path,
        **kwargs: Any,
    ) -> Any:
        events.append("stabilize")
        captured["stabilizer_frames"] = frames
        captured["stabilizer_destination"] = destination
        captured["source_token"] = kwargs["source_analysis"]
        return SimpleNamespace(
            frames=(Path("stabilized.png"),),
            metadata={"schema": "gen-automation/i2v-face-stabilization/v2", "blink_events": 1},
        )

    assert supervisor.comfy_client is not None
    supervisor.comfy_client.execute = execute  # type: ignore[assignment]
    monkeypatch.setattr("gen_automation.i2v_worker.app.download_input", download)
    monkeypatch.setattr("gen_automation.i2v_worker.app.prepare_input_image", prepare)
    monkeypatch.setattr("gen_automation.i2v_worker.app.encode_video", encode)
    monkeypatch.setattr("gen_automation.i2v_worker.app.upload_video", upload)
    monkeypatch.setattr("gen_automation.i2v_worker.app.preflight_source_face", preflight_source)
    monkeypatch.setattr("gen_automation.i2v_worker.app.stabilize_face_frames", stabilize)
    app = create_i2v_worker_app(settings, supervisor=supervisor)  # type: ignore[arg-type]
    job = _job()
    job["settings_snapshot"] = {"face_fidelity": "stable_expression"}

    with TestClient(app) as client:
        response = client.post("/jobs/i2v", json=job)

    assert response.status_code == 200, response.text
    metadata = response.json()["output"]["metadata"]
    assert metadata["face_fidelity"] == "stable_expression"
    assert "one subtle natural blink" in metadata["effective_positive_prompt"]
    assert "expression change" in metadata["effective_negative_prompt"]
    assert captured["workflow"]["11"]["class_type"] == "KSamplerWithNAG (Advanced)"
    assert captured["preflight_before_execute"] is True
    assert captured["source_token"] is source_token
    assert captured["stabilizer_frames"] == (Path("frame.png"),)
    assert metadata["face_stabilization"] == {
        "schema": "gen-automation/i2v-face-stabilization/v2",
        "blink_events": 1,
    }
    assert events == [
        "prepare",
        "source_preflight",
        "execute",
        "stabilize",
        "encode",
        "upload",
    ]


def test_stable_expression_source_contract_fails_before_comfy_or_upload(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings = _settings(tmp_path)
    supervisor = _Supervisor()
    calls = {"execute": 0, "upload": 0}

    async def download(*_args: Any, **_kwargs: Any) -> None:
        destination = _args[2]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x")

    def prepare(source: Path, destination: Path, **_kwargs: Any) -> None:
        destination.write_bytes(source.read_bytes())

    def reject_source(*_args: Any, **_kwargs: Any) -> None:
        from gen_automation.i2v_worker.face_stabilizer import (
            FaceStabilizationError,
            FaceStabilizationReason,
        )

        raise FaceStabilizationError(FaceStabilizationReason.SOURCE_CONTRACT)

    async def execute(*_args: Any, **_kwargs: Any) -> tuple[Path, ...]:
        calls["execute"] += 1
        return (Path("frame.png"),)

    async def upload(*_args: Any, **_kwargs: Any) -> tuple[str, int, str]:
        calls["upload"] += 1
        return "output-v1", 5, SHA

    assert supervisor.comfy_client is not None
    supervisor.comfy_client.execute = execute  # type: ignore[method-assign]
    monkeypatch.setattr("gen_automation.i2v_worker.app.download_input", download)
    monkeypatch.setattr("gen_automation.i2v_worker.app.prepare_input_image", prepare)
    monkeypatch.setattr("gen_automation.i2v_worker.app.preflight_source_face", reject_source)
    monkeypatch.setattr("gen_automation.i2v_worker.app.upload_video", upload)
    app = create_i2v_worker_app(settings, supervisor=supervisor)  # type: ignore[arg-type]
    job = _job()
    job["settings_snapshot"] = {"face_fidelity": "stable_expression"}

    with TestClient(app) as client:
        response = client.post("/jobs/i2v", json=job)

    assert response.status_code == 422
    assert response.json() == {"detail": "stable-expression face contract failed"}
    assert calls == {"execute": 0, "upload": 0}
    assert list((settings.runtime_root / "jobs").glob("*")) == []


def test_readiness_fails_closed_without_loaded_face_stabilizer(tmp_path: Path) -> None:
    app = create_i2v_worker_app(
        _settings(tmp_path),
        supervisor=_Supervisor(face_ready=False),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert client.post("/jobs/i2v", json=_job()).status_code == 503


def test_stable_expression_post_render_guard_fails_without_encode_or_upload(
    tmp_path: Path,
    monkeypatch: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gen_automation.i2v_worker.face_stabilizer import (
        FaceStabilizationError,
        FaceStabilizationReason,
    )

    settings = _settings(tmp_path)
    supervisor = _Supervisor()
    calls = {"execute": 0, "encode": 0, "upload": 0}
    caplog.set_level("WARNING", logger="gen_automation.i2v_worker.app")
    app_logger = logging.getLogger("gen_automation.i2v_worker.app")
    app_logger.addHandler(caplog.handler)

    async def download(*_args: Any, **_kwargs: Any) -> None:
        destination = _args[2]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x")

    def prepare(source: Path, destination: Path, **_kwargs: Any) -> None:
        destination.write_bytes(source.read_bytes())

    async def execute(*_args: Any, **_kwargs: Any) -> tuple[Path, ...]:
        calls["execute"] += 1
        return (Path("frame.png"),)

    def reject_frames(
        _prepared: Path,
        _frames: tuple[Path, ...],
        destination: Path,
        **_kwargs: Any,
    ) -> None:
        destination.mkdir(parents=True)
        (destination / "partial.png").write_bytes(b"partial")
        raise FaceStabilizationError(FaceStabilizationReason.POSE_GUARD) from RuntimeError(
            "sensitive pose details"
        )

    def encode(*_args: Any, **_kwargs: Any) -> None:
        calls["encode"] += 1

    async def upload(*_args: Any, **_kwargs: Any) -> tuple[str, int, str]:
        calls["upload"] += 1
        return "output-v1", 5, SHA

    assert supervisor.comfy_client is not None
    supervisor.comfy_client.execute = execute  # type: ignore[method-assign]
    monkeypatch.setattr("gen_automation.i2v_worker.app.download_input", download)
    monkeypatch.setattr("gen_automation.i2v_worker.app.prepare_input_image", prepare)
    monkeypatch.setattr(
        "gen_automation.i2v_worker.app.preflight_source_face",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr("gen_automation.i2v_worker.app.stabilize_face_frames", reject_frames)
    monkeypatch.setattr("gen_automation.i2v_worker.app.encode_video", encode)
    monkeypatch.setattr("gen_automation.i2v_worker.app.upload_video", upload)
    app = create_i2v_worker_app(settings, supervisor=supervisor)  # type: ignore[arg-type]
    job = _job()
    job["settings_snapshot"] = {"face_fidelity": "stable_expression"}

    try:
        with TestClient(app) as client:
            response = client.post("/jobs/i2v", json=job)
    finally:
        app_logger.removeHandler(caplog.handler)

    assert response.status_code == 422
    assert response.json() == {"detail": "stable-expression face contract failed"}
    assert calls == {"execute": 1, "encode": 0, "upload": 0}
    assert list((settings.runtime_root / "jobs").glob("*")) == []
    assert "reason_code=pose_guard status=422" in caplog.text
    assert "sensitive pose details" not in caplog.text


def test_stable_expression_internal_failure_is_generic_500_and_safely_logged(
    tmp_path: Path,
    monkeypatch: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gen_automation.i2v_worker.face_stabilizer import FaceStabilizationError

    settings = _settings(tmp_path)
    supervisor = _Supervisor()
    caplog.set_level("WARNING", logger="gen_automation.i2v_worker.app")
    app_logger = logging.getLogger("gen_automation.i2v_worker.app")
    app_logger.addHandler(caplog.handler)

    async def download(*_args: Any, **_kwargs: Any) -> None:
        destination = _args[2]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x")

    def prepare(source: Path, destination: Path, **_kwargs: Any) -> None:
        destination.write_bytes(source.read_bytes())

    async def execute(*_args: Any, **_kwargs: Any) -> tuple[Path, ...]:
        return (Path("frame.png"),)

    def fail_internal(*_args: Any, **_kwargs: Any) -> None:
        raise FaceStabilizationError("C:/sensitive/frame-000042.png")

    assert supervisor.comfy_client is not None
    supervisor.comfy_client.execute = execute  # type: ignore[method-assign]
    monkeypatch.setattr("gen_automation.i2v_worker.app.download_input", download)
    monkeypatch.setattr("gen_automation.i2v_worker.app.prepare_input_image", prepare)
    monkeypatch.setattr(
        "gen_automation.i2v_worker.app.preflight_source_face",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr("gen_automation.i2v_worker.app.stabilize_face_frames", fail_internal)
    app = create_i2v_worker_app(settings, supervisor=supervisor)  # type: ignore[arg-type]
    job = _job()
    job["settings_snapshot"] = {"face_fidelity": "stable_expression"}

    try:
        with TestClient(app) as client:
            response = client.post("/jobs/i2v", json=job)
    finally:
        app_logger.removeHandler(caplog.handler)

    assert response.status_code == 500
    assert response.json() == {"detail": "generation failed"}
    assert "reason_code=internal status=500" in caplog.text
    assert "C:/sensitive/frame-000042.png" not in response.text
    assert "C:/sensitive/frame-000042.png" not in caplog.text
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


def test_baseline_worker_rejects_reviewed_lora_job_before_execution(tmp_path: Path) -> None:
    settings = _settings(tmp_path, lora_worker_enabled=False)
    supervisor = _Supervisor()
    app = create_i2v_worker_app(settings, supervisor=supervisor)  # type: ignore[arg-type]
    job = _job()
    job["settings_snapshot"] = {
        "loras": [{"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3}]
    }

    with TestClient(app) as client:
        response = client.post("/jobs/i2v", json=job)

    assert response.status_code == 409


def test_worker_rejects_mutually_exclusive_dream_terms_before_execution(tmp_path: Path) -> None:
    app = create_i2v_worker_app(
        _settings(tmp_path),
        supervisor=_Supervisor(),  # type: ignore[arg-type]
    )
    job = _job()
    job["positive_prompt"] = "m15510n4ry then bl0wj0b"
    job["settings_snapshot"] = {
        "loras": [{"catalog_id": "dr34ml4y-aio-nsfw-wan22-v2", "strength": 0.7}]
    }

    with TestClient(app) as client:
        response = client.post("/jobs/i2v", json=job)

    assert response.status_code == 400
