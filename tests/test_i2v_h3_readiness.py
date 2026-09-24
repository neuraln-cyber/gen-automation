"""Small CPU-only contracts; no provider calls, weights or GPU spend."""

import json
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from gen_automation.api.routes.i2v import _validate_generation_profile
from gen_automation.config import Settings
from gen_automation.domain.i2v import I2VOutputSnapshot
from gen_automation.i2v_worker.models import GenerationSettings
from gen_automation.i2v_worker.settings import I2VWorkerSettings
from gen_automation.i2v_worker.workflow import load_workflow_template, render_workflow
from gen_automation.services.i2v_media import I2VMediaStorageError, presign_i2v_output_download
from gen_automation.services.i2v_runtime import I2VRuntimeConfig
from gen_automation.storage.base import ObjectStore

ROOT = Path(__file__).parents[1]


def h3_settings(**changes: object) -> GenerationSettings:
    return GenerationSettings.model_validate(
        {
            "profile": "minimax_h3",
            "frame_count": 124,
            "fps": 24,
            "scheduler": "simple",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"frame_count": 81},
        {"fps": 16},
        {"steps": 6},
        {"scheduler": "linear_quadratic"},
        {"width": 1376, "height": 768},
        {"loop": True},
        {"face_fidelity": "stable_expression"},
        {"upscale": "source"},
        {"video_shift": 13},
        {"audio_shift": 2},
    ],
)
def test_h3_rejects_unsupported_or_expensive_legacy_settings(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        h3_settings(**changes)


def test_h3_binds_native_workflow_without_wan_models_or_custom_nodes() -> None:
    template = load_workflow_template(
        ROOT / "workflows/dasiwa-minimax-h3-i2v-v1.api.json", profile="minimax_h3"
    )
    roles = ("diffusion_model", "text_encoder", "video_vae", "audio_vae")
    rendered, seed, _ = render_workflow(
        template,
        input_filename="input.png",
        positive_prompt="Leaves sway; birds sing.",
        negative_prompt="",
        settings=h3_settings(seed=42),
        job_id=uuid4(),
        attempt_id=uuid4(),
        model_paths={role: f"{role}.safetensors" for role in roles},
    )
    assert seed == 42
    assert "$i2v" not in json.dumps(rendered)
    assert rendered["14"]["class_type"] == "SaveVideo"
    assert rendered["16"]["inputs"]["audio"] == ["15", 0]
    assert rendered["6"]["inputs"]["length"] == 124
    assert rendered["11"]["inputs"]["scheduler"] == "simple"
    assert rendered["7"]["inputs"]["shift_video"] == 8
    assert not any("Wan" in node["class_type"] for node in rendered.values())


def test_h3_rejects_negative_guidance_and_wrong_profile_before_queue() -> None:
    settings = Settings.model_construct(i2v_profile="minimax_h3")
    with pytest.raises(HTTPException, match="different video model"):
        _validate_generation_profile(settings, {}, "")
    with pytest.raises(HTTPException, match="positive guidance"):
        _validate_generation_profile(settings, h3_settings().model_dump(), "jitter")
    _validate_generation_profile(settings, h3_settings().model_dump(), "")


def test_h3_dashboard_is_salad_specific_and_does_not_offer_wan_controls(client: TestClient) -> None:
    client.app.state.settings = client.app.state.settings.model_copy(
        update={
            "i2v_profile": "minimax_h3",
            "i2v_runpod_enabled": False,
        }
    )
    page = client.get("/dashboard/animations")
    assert page.status_code == 200
    assert "Salad video worker" in page.text
    assert "DaSiWa MiniMax H3 Turbo" in page.text
    assert "Motion &amp; audio direction" in page.text or "Motion & audio direction" in page.text
    assert 'name="video_shift"' in page.text
    assert 'name="high_end_step"' not in page.text
    assert 'name="face_fidelity"' not in page.text
    assert 'name="runpod_authorization"' not in page.text
    assert "data-worker-resume" in page.text
    assert 'value="124" selected' in page.text


def test_private_model_delivery_cannot_be_required_without_a_route() -> None:
    with pytest.raises(ValidationError, match="private model delivery is required"):
        I2VWorkerSettings(model_objects_json=SecretStr("[]"), require_private_delivery=True)


def test_private_model_delivery_rejects_empty_manifest_without_index_error() -> None:
    with pytest.raises(ValidationError, match="one regional S3 origin"):
        I2VWorkerSettings(
            model_objects_json=SecretStr("[]"), model_delivery_domain="d123abc.cloudfront.net"
        )


def test_disabled_worker_resume_cannot_enable_video(client: TestClient) -> None:
    response = client.post(
        "/api/v1/i2v/worker:resume",
        json={
            "deployment_id": str(uuid4()),
            "expected_guard_at": "2026-09-24T00:00:00+00:00",
        },
        headers={"X-CSRF-Token": "development"},
    )
    assert response.status_code == 409
    assert not client.app.state.settings.i2v_enabled


def test_no_new_usage_or_execution_caps_are_enabled_by_default() -> None:
    settings = Settings()
    assert settings.i2v_startup_timeout_seconds is None
    assert settings.i2v_execution_timeout_seconds is None
    assert I2VRuntimeConfig.__dataclass_fields__["startup_timeout_seconds"].default is None
    assert I2VRuntimeConfig.__dataclass_fields__["execution_timeout_seconds"].default is None


@pytest.mark.asyncio
@pytest.mark.parametrize("attachment", [False, True])
async def test_private_video_downloads_never_request_direct_s3_attachment(attachment: bool) -> None:
    domain = "d123abc.cloudfront.net"
    store = AsyncMock()
    store.backend = "s3"
    store.bucket = "assets"
    store.presign_download.return_value = f"https://{domain}/assets/i2v/outputs/video.mp4"
    output = I2VOutputSnapshot.model_construct(
        storage_backend="s3",
        storage_bucket="assets",
        object_key="i2v/outputs/video.mp4",
        object_version_id="exact-version",
        job_id=uuid4(),
    )
    url = await presign_i2v_output_download(
        cast(ObjectStore, store),
        output=output,
        expires_in=120,
        attachment=attachment,
        delivery_domain=domain,
    )
    assert url.startswith(f"https://{domain}/")
    store.presign_download.assert_awaited_once_with(
        key=output.object_key,
        version_id="exact-version",
        expires_in=120,
        download_name=None,
    )
    store.presign_download.return_value = "https://assets.s3.eu-central-1.amazonaws.com/video.mp4"
    with pytest.raises(I2VMediaStorageError, match="private video delivery"):
        await presign_i2v_output_download(
            cast(ObjectStore, store),
            output=output,
            expires_in=120,
            delivery_domain=domain,
        )
