import hashlib
import json
import re
from pathlib import Path

from gen_automation.video_worker.model_integrity import MODEL_MANIFEST as PINNED_MODEL_MANIFEST

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile.video-worker"
MODEL_MANIFEST = ROOT / "video-models" / "wan2.2-ti2v-5b-comfy.json"
VIDEO_REQUIREMENTS = ROOT / "requirements-video-worker.lock"
MAIN_PATH = ROOT / "src" / "gen_automation" / "video_worker" / "main.py"

MODEL_REVISION = "fb1388adc906ab39ffc26ee40e96b22886b56bc4"
ARTIFACTS = {
    "wan2.2_ti2v_5B_fp16.safetensors": (
        9_999_658_848,
        "456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e",
    ),
    "umt5_xxl_fp8_e4m3fn_scaled.safetensors": (
        6_735_906_897,
        "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
    ),
    "wan2.2_vae.safetensors": (
        1_409_400_960,
        "e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156",
    ),
}


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_model_layer_uses_exact_revision_checksums_and_sizes() -> None:
    dockerfile = re.sub(r"\\\r?\n\s*", " ", _dockerfile())
    manifest = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))

    assert manifest["upstream"]["revision"] == MODEL_REVISION
    assert manifest["profile_id"] == "wan2.2-ti2v-5b-comfy-v1"
    assert manifest["profile_ids"] == [
        "wan2.2-ti2v-5b-comfy-v1",
        "wan2.2-ti2v-5b-comfy-hq-v1",
    ]
    manifest_bytes = MODEL_MANIFEST.read_bytes()
    assert len(manifest_bytes) == PINNED_MODEL_MANIFEST.size_bytes == 1472
    assert hashlib.sha256(manifest_bytes).hexdigest() == PINNED_MODEL_MANIFEST.sha256
    assert PINNED_MODEL_MANIFEST.sha256 in dockerfile
    assert manifest["distribution"]["runtime_model_download"] is False
    assert manifest["total_bytes"] == sum(size for size, _digest in ARTIFACTS.values())
    for filename, (size, digest) in ARTIFACTS.items():
        assert f"resolve/{MODEL_REVISION}/" in dockerfile
        assert f"ADD --checksum=sha256:{digest} --chmod=0444" in dockerfile
        assert filename in dockerfile
        assert f')" = {size}' in dockerfile


def test_expensive_model_layer_precedes_application_source_and_has_no_runtime_fetch() -> None:
    dockerfile = _dockerfile()
    contract = dockerfile.split("FROM runtime-foundation AS runtime-contract", 1)[1].split(
        "FROM runtime-foundation AS model-runtime",
        1,
    )[0]
    model_runtime = dockerfile.split("FROM runtime-foundation AS model-runtime", 1)[1].split(
        "FROM model-runtime AS production",
        1,
    )[0]
    production = dockerfile.split("FROM model-runtime AS production", 1)[1]

    assert "ADD --checksum=sha256:" not in contract
    assert "COPY src/gen_automation/video_worker" in contract
    assert model_runtime.count("ADD --checksum=sha256:") == 3
    assert model_runtime.count("--chmod=0444") == 3
    assert "&& chmod 0444" not in model_runtime
    assert "COPY src/gen_automation/" not in model_runtime
    assert "COPY --from=runtime-contract /opt/video-worker/src ./src" in production
    assert "ARG " not in model_runtime
    assert "huggingface-cli" not in dockerfile
    assert "wget " not in dockerfile
    assert "curl " not in dockerfile
    assert "runtime_model_download" not in dockerfile


def test_video_image_is_non_root_and_contains_pinned_ffprobe_contract() -> None:
    dockerfile = _dockerfile()
    assert re.findall(r"^USER\s+(.+)$", dockerfile, flags=re.MULTILINE) == [
        "10002:10002",
        "10002:10002",
    ]
    assert "ffmpeg" in dockerfile
    assert "/usr/bin/ffprobe -version" in dockerfile
    assert (
        'ENTRYPOINT ["/opt/video-worker-venv/bin/python", "-m", '
        '"gen_automation.video_worker.main"]' in dockerfile
    )
    assert "gen_automation.gpu_worker" not in dockerfile
    assert 'org.opencontainers.image.video-profile-sha256="a83c946f9a61' in dockerfile
    assert 'org.opencontainers.image.video-workflow-sha256="ecba3eef1c14' in dockerfile
    assert 'org.opencontainers.image.video-hq-profile-sha256="d690fb7b0fc8' in dockerfile
    assert 'org.opencontainers.image.video-hq-workflow-sha256="01c5b5350cab' in dockerfile
    assert 'org.opencontainers.image.video-hq-execution-contract-sha256="00fb341e491f' in dockerfile
    assert 'org.opencontainers.image.video-profile-registry-sha256="8f536e93c5e0' in dockerfile
    assert 'org.opencontainers.image.video-workflow-registry-sha256="19e4426429de' in dockerfile
    assert "VIDEO_WORKER_PROFILE_ID=wan2.2-ti2v-5b-comfy-v1" in dockerfile
    assert "VIDEO_WORKER_PROFILE_IDS_JSON=" in dockerfile


def test_pinned_salad_sidecar_is_built_patched_and_copied() -> None:
    dockerfile = _dockerfile()
    assert "73d7a3c80a73f26339194e024cb47c8501c67f75" in dockerfile
    assert "a25fa6ca196554eb1e4b6acaabfb22730db69515d07a8e82585015df4213c0ae" in dockerfile
    assert "git apply --unidiff-zero --check /tmp/strict-http-status.patch" in dockerfile
    assert "go test -mod=readonly ./cmd/salad-http-job-queue-worker" in dockerfile
    assert "COPY --from=salad-queue-worker-builder --chmod=0555" in dockerfile
    assert "/usr/local/bin/salad-http-job-queue-worker" in dockerfile


def test_production_starts_real_comfy_adapter_and_bootstrap_server() -> None:
    main = MAIN_PATH.read_text(encoding="utf-8")
    assert "verify_model_runtime,\n            profile_ids=settings.allowed_profile_ids" in main
    assert "start_comfy(profile_ids=settings.allowed_profile_ids)" in main
    assert "executor = NativeComfyWanExecutor(" in main
    assert "UnconfiguredWanComfyExecutor" not in main
    assert "await _wait_for_server_start(server, server_task)" in main
    assert "queue_process = start_queue_worker()" in main
    assert "SwitchableVideoWorkerApplication()" in main
    assert "timeout_graceful_shutdown=SERVER_GRACEFUL_SHUTDOWN_SECONDS" in main
    assert "port=8000" in main


def test_default_production_target_cannot_bypass_model_layer() -> None:
    dockerfile = _dockerfile().rstrip()
    assert "FROM runtime-foundation AS runtime-contract" in dockerfile
    assert "FROM runtime-foundation AS model-runtime" in dockerfile
    assert "FROM model-runtime AS production" in dockerfile
    assert dockerfile.rindex("FROM model-runtime AS production") > dockerfile.rindex(
        "ADD --checksum=sha256:"
    )
    assert dockerfile.endswith(
        'ENTRYPOINT ["/opt/video-worker-venv/bin/python", "-m", "gen_automation.video_worker.main"]'
    )


def test_video_python_dependency_is_exactly_hash_locked() -> None:
    requirements = VIDEO_REQUIREMENTS.read_text(encoding="utf-8")
    assert "av==18.0.0" in requirements
    assert "--only-binary :all:" in requirements
    assert len(re.findall(r"--hash=sha256:[0-9a-f]{64}", requirements)) == 18
    assert not re.search(r"(?im)^\s*(?:-e\s+|https?://|git\+)", requirements)
