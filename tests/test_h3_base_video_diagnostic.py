"""Opt-in base/final comparison, with no GPU jobs or external services."""

import copy
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from gen_automation.api.routes.i2v import _validate_generation_profile
from gen_automation.config import Settings
from gen_automation.domain.i2v import (
    I2VAttemptSnapshot,
    I2VJobSnapshot,
    I2VOutputRegistration,
    I2VOutputSnapshot,
)
from gen_automation.i2v_worker.app import _run_job
from gen_automation.i2v_worker.comfy import ComfyError, _result_paths
from gen_automation.i2v_worker.models import GenerationSettings, I2VJob
from gen_automation.services.authentication import SessionUnauthorizedError
from gen_automation.services.i2v_media import (
    I2VMediaConflictError,
    I2VSignedGrantBuilder,
    base_video_snapshot,
    presign_i2v_output_download,
)
from gen_automation.services.i2v_runtime import _validate_fresh_grants, _worker_settings_snapshot
from gen_automation.storage.memory import MemoryObjectStore
from tests.test_h3_upscale import MODEL_PATHS, ROOT, _workflow
from tests.test_i2v_h3_readiness import h3_settings
from tests.test_i2v_worker_app import _job
from tests.test_i2v_worker_comfy import _client


def diagnostic_settings(**kwargs):
    return h3_settings(
        width=1152,
        height=1504,
        match_source_resolution=True,
        h3_save_base_video=True,
        seed=12345,
        **kwargs,
    )


def test_option_is_off_by_default_and_gated_during_rollout():
    assert not h3_settings().h3_save_base_video
    assert "h3_save_base_video" not in _worker_settings_snapshot({"h3_save_base_video": False})
    for raw in (
        {"h3_save_base_video": True},
        {**h3_settings().model_dump(), "h3_save_base_video": True},
    ):
        with pytest.raises(ValidationError):
            GenerationSettings.model_validate(raw)
    cfg = Settings.model_construct(i2v_profile="minimax_h3", i2v_h3_source_resolution_enabled=True)
    with pytest.raises(HTTPException, match="diagnostics await"):
        _validate_generation_profile(cfg, diagnostic_settings().model_dump(), "")
    cfg.i2v_h3_diagnostics_enabled = True
    _validate_generation_profile(cfg, diagnostic_settings().model_dump(), "")


def test_graph_retains_one_sampling_run_and_delays_base_decode_until_after_refinement():
    selection = {"artifact_id": str(uuid4()), "sha256": "d" * 64, "strength": 1.0}
    enabled = diagnostic_settings(h3_loras=[selection])
    regular = _workflow(enabled.model_copy(update={"h3_save_base_video": False}))
    graph = _workflow(enabled)
    # Output prefixes contain unique attempt IDs. Every inference node is otherwise identical.
    for key in regular:
        if key != "14":
            assert graph[key] == regular[key]
    assert set(graph) - set(regular) == {"h3-base-decode", "h3-base-create", "h3-base-save"}
    assert graph["h3-base-decode"]["inputs"] == {
        "samples": ["12", 0],
        "vae": ["4", 0],
        "after_refinement": ["13", 0],
    }
    assert graph["h3-base-create"]["inputs"]["audio"] == ["15", 0]
    assert graph["8"]["inputs"]["noise_seed"] == 12345
    assert graph["h3-lora-0"]["inputs"]["strength_model"] == 1.0


def _history(tmp_path):
    for name in ("final.mp4", "base.mp4"):
        (tmp_path / name).write_bytes(b"test")
    return {
        node: {"images": [{"filename": name, "type": "output", "subfolder": ""}]}
        for node, name in (("14", "final.mp4"), ("h3-base-save", "base.mp4"))
    }


def test_output_selection_requires_both_distinct_safe_files(tmp_path):
    outputs = _history(tmp_path)
    assert _result_paths(outputs, tmp_path, diagnostic=True) == (
        tmp_path / "final.mp4",
        tmp_path / "base.mp4",
    )
    assert _result_paths(outputs, tmp_path, diagnostic=False) == (tmp_path / "final.mp4",)
    for bad in ({"14": outputs["14"]}, {"14": outputs["14"], "h3-base-save": outputs["14"]}):
        with pytest.raises(ComfyError):
            _result_paths(bad, tmp_path, diagnostic=True)
    outputs["h3-base-save"]["images"][0]["filename"] = "../outside.mp4"
    with pytest.raises(ComfyError):
        _result_paths(outputs, tmp_path, diagnostic=True)


async def test_comfy_waits_for_diagnostic_output_without_resubmitting(tmp_path):
    outputs = _history(tmp_path)
    posts = []
    polls = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            return httpx2.Response(200, json={"prompt_id": "test"})
        polls.append(request)
        return httpx2.Response(
            200,
            json={
                "test": {
                    "status": {"completed": len(polls) > 1},
                    "outputs": outputs if len(polls) > 1 else {"14": outputs["14"]},
                }
            },
        )

    client = await _client(handler)
    try:
        assert len(await client.execute({"h3-base-save": {}}, tmp_path)) == 2
        assert len(posts) == 1 and len(polls) == 2
    finally:
        await client.close()


def _snapshots():
    job = I2VJobSnapshot.model_construct(
        job_id=uuid4(),
        request_sha256="a" * 64,
        input_snapshot={
            "storage_backend": "s3",
            "storage_bucket": "private",
            "object_key": "input",
            "object_version_id": "v1",
            "width": 1144,
            "height": 1480,
        },
        settings_snapshot=diagnostic_settings().model_dump(mode="json"),
    )
    return job, I2VAttemptSnapshot.model_construct(attempt_id=uuid4())


async def test_grants_are_opt_in_attempt_bound_and_checksum_verified():
    store = MemoryObjectStore(bucket="private")
    store.backend = "s3"
    job, attempt = _snapshots()
    store.put_for_test("input", b"input", content_type="image/png")
    job.input_snapshot["object_version_id"] = (await store.head("input")).version_id
    builder = I2VSignedGrantBuilder(store=store, expires_in=3600)
    grants = await builder.build(job=job, attempt=attempt)
    assert (
        grants["base_video_grant"]["object_key"]
        == grants["output_grant"]["object_key"][:-4] + ".base.mp4"
    )
    for grant in grants.values():
        grant["url"] = grant["url"].replace("memory://", "https://", 1)
    _validate_fresh_grants(
        grants, job=job, attempt=attempt, output_prefix="i2v/outputs", now=datetime.now(UTC)
    )
    body = b"private-test-video"
    outputs = {}
    for kind, width, height in (("output_grant", 1144, 1480), ("base_video_grant", 768, 992)):
        grant = grants[kind]
        metadata = {
            k.removeprefix("x-amz-meta-"): v
            for k, v in grant["headers"].items()
            if k.startswith("x-amz-meta-")
        }
        store.put_for_test(grant["object_key"], body, content_type="video/mp4", metadata=metadata)
        stored = await store.head(grant["object_key"])
        outputs[kind] = dict(
            storage_backend="s3",
            storage_bucket="private",
            object_key=grant["object_key"],
            object_version_id=stored.version_id,
            sha256=hashlib.sha256(body).hexdigest(),
            content_type="video/mp4",
            width=width,
            height=height,
            frame_count=124,
            fps=24,
            duration_ms=5167,
            byte_size=len(body),
            metadata={"stage": "pre_upscale", "seed": 12345}
            if kind == "base_video_grant"
            else {"seed": 12345},
        )
    outputs["output_grant"]["metadata"]["h3_base_video"] = outputs["base_video_grant"]
    result = I2VOutputRegistration.model_validate(outputs["output_grant"])
    await builder.verify_output(job=job, attempt=attempt, output=result)
    for field, value in (("object_key", "another-owner.mp4"), ("sha256", "b" * 64), ("width", 992)):
        bad = copy.deepcopy(result.model_dump())
        bad["metadata"]["h3_base_video"][field] = value
        with pytest.raises(I2VMediaConflictError):
            await builder.verify_output(
                job=job, attempt=attempt, output=I2VOutputRegistration.model_validate(bad)
            )
    snapshot = I2VOutputSnapshot.model_construct(
        **result.model_dump(),
        output_id=uuid4(),
        job_id=job.job_id,
        attempt_id=attempt.attempt_id,
        created_at=datetime.now(UTC),
    )
    base = base_video_snapshot(snapshot)
    assert base.width == 768 and base.height == 992
    store.presign_download = AsyncMock(return_value="https://private.cloudfront.net/video")
    await presign_i2v_output_download(
        store,
        output=base,
        expires_in=300,
        attachment=True,
        delivery_domain="private.cloudfront.net",
    )
    assert store.presign_download.await_args.kwargs["key"].endswith(".base.mp4")
    assert store.presign_download.await_args.kwargs["download_name"] is None
    regular = job.model_copy(update={"settings_snapshot": {"profile": "minimax_h3"}})
    assert "base_video_grant" not in await builder.build(job=regular, attempt=attempt)
    with pytest.raises(I2VMediaConflictError):
        await builder.verify_output(job=regular, attempt=attempt, output=result)


async def test_worker_finalizes_both_sizes_once_and_cleans_up(tmp_path, monkeypatch):
    from gen_automation.i2v_worker.workflow import load_workflow_template

    raw = _job()
    raw["settings_snapshot"] = diagnostic_settings().model_dump(mode="json")
    raw["input_snapshot"].update(width=1144, height=1480)
    raw["base_video_grant"] = {**raw["output_grant"], "object_key": "i2v/output.base.mp4"}
    job = I2VJob.model_validate(raw)
    runtime = tmp_path / "runtime"
    calls = []

    async def download(_grant, _snapshot, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"source")

    def prepare(_source, destination, **kwargs):
        destination.write_bytes(b"prepared")

    def finalize(paths, settings, root, **kwargs):
        calls.append((paths[0].name, settings.width, settings.height, settings.seed))
        path = root / "video.mp4"
        path.write_bytes(b"video")
        return path, dict(
            width=kwargs["source_width"],
            height=kwargs["source_height"],
            frame_count=124,
            fps=24,
            duration_ms=5167,
            codec="h264",
            pixel_format="yuv420p",
            faststart=True,
            native_width=768,
            native_height=992,
            upscale="none",
            loop_mode="none",
            loop_count=1,
            source_fit="contain_edge_pad",
            match_source_aspect=False,
        )

    monkeypatch.setattr("gen_automation.i2v_worker.app.download_input", download)
    monkeypatch.setattr("gen_automation.i2v_worker.app.prepare_input_image", prepare)
    monkeypatch.setattr("gen_automation.i2v_worker.app.finalize_native_video", finalize)
    upload = AsyncMock(return_value=("version-1", 5, "a" * 64))
    monkeypatch.setattr("gen_automation.i2v_worker.app.upload_video", upload)
    execute = AsyncMock(return_value=(Path("final.mp4"), Path("base.mp4")))
    settings = SimpleNamespace(
        runtime_root=runtime,
        environment="test",
        network_timeout_seconds=5,
        network_attempts=1,
        profile="minimax_h3",
        model_objects=[
            SimpleNamespace(role=k, install_path=f"models/any/{v}") for k, v in MODEL_PATHS.items()
        ],
    )
    result = await _run_job(
        job,
        settings=settings,
        supervisor=SimpleNamespace(comfy_client=SimpleNamespace(execute=execute)),
        workflow=load_workflow_template(
            ROOT / "workflows/dasiwa-minimax-h3-i2v-v1.api.json", profile="minimax_h3"
        ),
    )
    assert execute.await_count == 1 and upload.await_count == 2
    assert calls == [("base.mp4", 768, 992, 12345), ("final.mp4", 1152, 1504, 12345)]
    assert (
        result.output.metadata["h3_base_video"]["metadata"]["seed"]
        == result.output.metadata["seed"]
        == 12345
    )
    assert not (runtime / "jobs" / str(job.attempt_id)).exists()
    missing = copy.deepcopy(raw)
    missing.pop("base_video_grant")
    with pytest.raises(ValidationError, match="diagnostic selection"):
        I2VJob.model_validate(missing)


def test_dashboard_comparison_routes_keep_owner_auth_and_private_delivery(client, monkeypatch):
    client.app.state.settings = client.app.state.settings.model_copy(
        update={
            "i2v_profile": "minimax_h3",
            "i2v_h3_source_resolution_enabled": True,
            "i2v_h3_diagnostics_enabled": True,
            "i2v_require_private_delivery": True,
            "storage_delivery_domain": "private.cloudfront.net",
        }
    )
    dashboard = client.get("/dashboard/animations")
    assert dashboard.status_code == 200
    assert 'data-h3-diagnostics-enabled="true"' in dashboard.text
    assert 'name="h3_save_base_video" disabled' in dashboard.text
    assert "Diagnostic · keep video before upscaling" in dashboard.text
    raw = dict(
        storage_backend="s3",
        storage_bucket="private",
        object_key="i2v/outputs/test.mp4",
        object_version_id="final-version",
        sha256="a" * 64,
        content_type="video/mp4",
        width=1152,
        height=1504,
        frame_count=124,
        fps=24,
        duration_ms=5167,
        byte_size=100,
        metadata={"seed": 12345},
    )
    raw["metadata"]["h3_base_video"] = {
        **raw,
        "object_key": "i2v/outputs/test.base.mp4",
        "object_version_id": "base-version",
        "width": 768,
        "height": 992,
        "metadata": {"stage": "pre_upscale", "seed": 12345},
    }
    output = I2VOutputSnapshot(
        **raw, output_id=uuid4(), job_id=uuid4(), attempt_id=uuid4(), created_at=datetime.now(UTC)
    )
    listing = AsyncMock(return_value=(output,))
    monkeypatch.setattr("gen_automation.api.routes.i2v.list_recent_i2v_outputs", listing)
    store = MemoryObjectStore(bucket="private")
    store.backend = "s3"
    store.presign_download = AsyncMock(return_value="https://private.cloudfront.net/video")
    client.app.state.object_store = store
    response = client.get("/api/v1/i2v/outputs/recent")
    assert response.status_code == 200
    item = response.json()[0]
    assert item["base_video"]["width"] == 768
    assert listing.await_args.kwargs["actor_user_id"] == UUID(int=0)
    for variant, expected in (
        (item["playback_url"], raw),
        (item["base_video"]["playback_url"], raw["metadata"]["h3_base_video"]),
    ):
        response = client.get(variant, follow_redirects=False)
        assert response.status_code == 307
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["location"].startswith("https://private.cloudfront.net/")
        assert store.presign_download.await_args.kwargs["key"] == expected["object_key"]
        assert (
            store.presign_download.await_args.kwargs["version_id"] == expected["object_version_id"]
        )
    listing.return_value = ()
    assert client.get(item["base_video"]["playback_url"]).status_code == 404
    listing.return_value = (output.model_copy(update={"metadata": {"seed": 12345}}),)
    assert client.get(item["base_video"]["playback_url"]).status_code == 404
    assert client.get(item["playback_url"] + "?variant=unknown").status_code == 422
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"auth_development_bypass_enabled": False, "auth_enabled": True}
    )
    client.app.state.authentication_service = SimpleNamespace(
        resolve_session=AsyncMock(side_effect=SessionUnauthorizedError("no session"))
    )
    listing.reset_mock()
    store.presign_download.reset_mock()
    assert client.get(item["base_video"]["playback_url"]).status_code == 401
    listing.assert_not_awaited()
    store.presign_download.assert_not_awaited()
