import hashlib
import json
import re
from pathlib import Path

import pytest

from gen_automation.video_worker.model_integrity import (
    A14B_MODEL_ARTIFACTS,
    A14B_MODEL_MANIFEST,
)
from gen_automation.video_worker.profiles import (
    A14B_ADULT_VIDEO_EXECUTION_CONTRACT_SHA256,
    A14B_ADULT_VIDEO_PROFILE_SHA256,
    A14B_ADULT_VIDEO_WORKFLOW_SHA256,
    A14B_VIDEO_EXECUTION_CONTRACT_SHA256,
    A14B_VIDEO_PROFILE_REGISTRY_SHA256,
    A14B_VIDEO_PROFILE_SHA256,
    A14B_VIDEO_WORKFLOW_SHA256,
)
from scripts.require_private_ghcr_package import (
    PackageCheckPhase,
    PackageStateError,
    require_private_ghcr_package,
)

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile.video-worker-a14b"
MANIFEST = ROOT / "video-models" / "wan2.2-smoothmix-i2v-a14b-q3-private.json"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish-video-worker-a14b.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
ANIMATION_STUDIO_DOC = ROOT / "docs" / "animation-studio.md"
ACCESS_DOC = ROOT / "docs" / "access-and-secrets.md"
MAIN = ROOT / "src" / "gen_automation" / "video_worker" / "main.py"
SMOOTHMIX_MIRROR_REVISION = "3521de1624df15f248b28920858db043c71cc76e"
WAN_GENERAL_MIRROR_REVISION = "a49adf1cc929aff7388a840931e9934db4c3cbef"
COMFYUI_GGUF_REVISION = "6ea2651e7df66d7585f6ffee804b20e92fb38b8a"
COMFYUI_NAG_REVISION = "c6f27116a8259f5b501d498a09e51c82fa72e35f"


def test_a14b_manifest_and_docker_pin_every_private_model_artifact() -> None:
    dockerfile = re.sub(r"\\\r?\n\s*", " ", DOCKERFILE.read_text(encoding="utf-8"))
    manifest_bytes = MANIFEST.read_bytes()
    manifest = json.loads(manifest_bytes)

    assert len(manifest_bytes) == A14B_MODEL_MANIFEST.size_bytes
    assert hashlib.sha256(manifest_bytes).hexdigest() == A14B_MODEL_MANIFEST.sha256
    assert A14B_MODEL_MANIFEST.sha256 in dockerfile
    assert manifest["total_bytes"] == 24_196_862_685
    assert manifest["total_bytes"] == sum(item.size_bytes for item in A14B_MODEL_ARTIFACTS)
    assert manifest["distribution"]["visibility"] == "private"
    assert manifest["distribution"]["runtime_model_download"] is False
    assert manifest["distribution"]["runtime_fit_verified"] is False
    assert "provider-managed secret only" in manifest["distribution"]["provider_registry_auth"]
    assert any(
        "authenticated exact-digest pull succeeds" in requirement
        for requirement in manifest["predeploy_validation"]["must_prove"]
    )
    assert manifest["distribution"]["maximum_compressed_image_bytes"] == 33_000_000_000
    assert manifest["distribution"]["minimum_storage_bytes"] == 53_687_091_200
    assert dockerfile.count("ADD --checksum=sha256:") == len(A14B_MODEL_ARTIFACTS)
    for artifact in manifest["artifacts"]:
        assert f"ADD --checksum=sha256:{artifact['sha256']} --chmod=0444" in dockerfile
        assert artifact["source"] in dockerfile
        assert str(artifact["byte_size"]) in dockerfile
        assert Path(artifact["relative_path"]).name in dockerfile
    smoothmix_sources = [
        artifact["source"]
        for artifact in manifest["artifacts"]
        if artifact["role"] in {"high_noise_diffusion_model", "low_noise_diffusion_model"}
    ]
    assert len(smoothmix_sources) == 2
    assert all(
        source.startswith(
            "https://huggingface.co/Animhaven/SmoothMix-Wan2.2-I2V-GGUF/"
            f"resolve/{SMOOTHMIX_MIRROR_REVISION}/"
        )
        for source in smoothmix_sources
    )
    assert "civitai.com/api/download/models/2587054" not in dockerfile
    assert "civitai.com/api/download/models/2587073" not in dockerfile
    adult_lora_sources = [
        artifact["source"]
        for artifact in manifest["artifacts"]
        if artifact["role"] in {"high_noise_adult_lora", "low_noise_adult_lora"}
    ]
    assert len(adult_lora_sources) == 2
    assert all(
        source.startswith(
            f"https://huggingface.co/rahul7star/wan2.2Lora/resolve/{WAN_GENERAL_MIRROR_REVISION}/"
        )
        for source in adult_lora_sources
    )
    assert "civitai.com/api/download/models/2073605" not in dockerfile
    assert "civitai.com/api/download/models/2083303" not in dockerfile


def test_a14b_image_labels_match_all_worker_contracts() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    contracts = manifest["contracts"]

    assert contracts["base"] == {
        "profile_sha256": A14B_VIDEO_PROFILE_SHA256,
        "workflow_sha256": A14B_VIDEO_WORKFLOW_SHA256,
        "execution_contract_sha256": A14B_VIDEO_EXECUTION_CONTRACT_SHA256,
    }
    assert contracts["adult"] == {
        "profile_sha256": A14B_ADULT_VIDEO_PROFILE_SHA256,
        "workflow_sha256": A14B_ADULT_VIDEO_WORKFLOW_SHA256,
        "execution_contract_sha256": A14B_ADULT_VIDEO_EXECUTION_CONTRACT_SHA256,
    }
    assert contracts["profile_registry_sha256"] == A14B_VIDEO_PROFILE_REGISTRY_SHA256
    for digest in (
        A14B_VIDEO_PROFILE_SHA256,
        A14B_VIDEO_WORKFLOW_SHA256,
        A14B_VIDEO_EXECUTION_CONTRACT_SHA256,
        A14B_ADULT_VIDEO_PROFILE_SHA256,
        A14B_ADULT_VIDEO_WORKFLOW_SHA256,
        A14B_ADULT_VIDEO_EXECUTION_CONTRACT_SHA256,
        A14B_VIDEO_PROFILE_REGISTRY_SHA256,
        contracts["workflow_registry_sha256"],
    ):
        assert digest in dockerfile
    assert "VIDEO_WORKER_MAX_SIGNATURE_TTL_SECONDS" not in dockerfile
    main = MAIN.read_text(encoding="utf-8")
    assert "--disable-all-custom-nodes" in main
    assert "--whitelist-custom-nodes" in main
    assert '"ComfyUI-GGUF"' in main
    assert '"ComfyUI-NAG"' in main


def test_a14b_model_free_foundation_is_built_and_smoked_in_ci() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    ci = CI_WORKFLOW.read_text(encoding="utf-8")

    for node, variable, revision in (
        ("ComfyUI-GGUF", "COMFYUI_GGUF_COMMIT", COMFYUI_GGUF_REVISION),
        ("ComfyUI-NAG", "COMFYUI_NAG_COMMIT", COMFYUI_NAG_REVISION),
    ):
        node_path = f"/opt/comfyui/custom_nodes/{node}"
        assert dockerfile.index(node_path) < dockerfile.index(f"git -C {node_path} init")
        assert f"{variable}={revision}" in dockerfile
        assert f'test "$(git -C {node_path} rev-parse HEAD)" = "${{{variable}}}"' in dockerfile

    build_step = ci.split("- name: Build A14B model-free runtime foundation", 1)[1].split(
        "- name:", 1
    )[0]
    assert "--file Dockerfile.video-worker-a14b" in build_step
    assert "--target runtime-foundation" in build_step
    assert "--tag gen-automation-video-worker-a14b-foundation:test" in build_step
    assert "--target production" not in build_step

    smoke_step = ci.split("- name: Smoke-test A14B custom node revisions", 1)[1].split(
        "- name:", 1
    )[0]
    assert "docker run --rm" in smoke_step
    assert "gen-automation-video-worker-a14b-foundation:test" in smoke_step
    assert COMFYUI_GGUF_REVISION in smoke_step
    assert COMFYUI_NAG_REVISION in smoke_step
    assert "UnetLoaderGGUF" in smoke_step
    assert "KSamplerWithNAG (Advanced)" in smoke_step


def test_public_repository_cannot_publish_the_model_bearing_a14b_image() -> None:
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "A14B model publication is restricted to the private packaging repository" in workflow
    assert "packages: write" not in workflow
    assert "docker/login-action" not in workflow
    assert "docker/build-push-action" not in workflow
    assert "Dockerfile.video-worker-a14b" not in workflow
    assert "ghcr.io" not in workflow
    assert "schedule:" not in workflow


def test_a14b_ghcr_package_gate_accepts_only_the_safe_status_matrix(tmp_path: Path) -> None:
    private_response = tmp_path / "private.json"
    private_response.write_text('{"visibility":"private"}', encoding="utf-8")
    public_response = tmp_path / "public.json"
    public_response.write_text('{"visibility":"public"}', encoding="utf-8")

    require_private_ghcr_package(
        phase=PackageCheckPhase.PRE_PUSH,
        http_status="404",
        response_body=tmp_path / "absent.json",
    )
    require_private_ghcr_package(
        phase=PackageCheckPhase.PRE_PUSH,
        http_status="200",
        response_body=private_response,
    )
    require_private_ghcr_package(
        phase=PackageCheckPhase.POST_PUSH,
        http_status="200",
        response_body=private_response,
    )

    for status in ("000", "400", "401", "403", "409", "429", "500", "503"):
        with pytest.raises(PackageStateError, match="unexpected GHCR package"):
            require_private_ghcr_package(
                phase=PackageCheckPhase.PRE_PUSH,
                http_status=status,
                response_body=private_response,
            )
    for phase, status, response in (
        (PackageCheckPhase.PRE_PUSH, "200", public_response),
        (PackageCheckPhase.POST_PUSH, "200", public_response),
        (PackageCheckPhase.POST_PUSH, "404", private_response),
    ):
        with pytest.raises(PackageStateError):
            require_private_ghcr_package(
                phase=phase,
                http_status=status,
                response_body=response,
            )


def test_a14b_operator_docs_bind_the_private_model_bearing_lane() -> None:
    animation_doc = ANIMATION_STUDIO_DOC.read_text(encoding="utf-8")
    access_doc = ACCESS_DOC.read_text(encoding="utf-8")

    for text in (animation_doc, access_doc):
        assert "20260811_0036" in text
        assert "provider-managed registry" in text
        assert "exact-digest" in text
    assert "81 native frames" in animation_doc
    assert "17,100" in animation_doc
    assert "critical-vulnerability" in animation_doc
    assert "RTX 5090 with at least 32 GiB" in animation_doc
    assert "storage allocation is at least 50 GiB" in animation_doc
    assert "absent/identity EXIF orientation" in animation_doc
    assert "Keep migration `20260811_0036`" in animation_doc
    assert "model-bearing A14B" in access_doc
