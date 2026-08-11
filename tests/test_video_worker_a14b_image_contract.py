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
ANIMATION_STUDIO_DOC = ROOT / "docs" / "animation-studio.md"
ACCESS_DOC = ROOT / "docs" / "access-and-secrets.md"
MAIN = ROOT / "src" / "gen_automation" / "video_worker" / "main.py"
SMOOTHMIX_MIRROR_REVISION = "3521de1624df15f248b28920858db043c71cc76e"


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


def test_a14b_publish_workflow_bootstraps_only_a_private_or_exactly_absent_package() -> None:
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "ref: ${{ inputs.source_sha }}" in workflow
    assert 'git rev-parse HEAD)" = "$SOURCE_SHA"' in workflow
    assert "publish-video-worker-a14b-private" in workflow
    assert 'gh api "/repos/${GITHUB_REPOSITORY}"' in workflow
    # The source repository is public. GHCR still defaults a first-published
    # container package to private, and the workflow verifies that postcondition.
    assert "jq -r .private" not in workflow
    assert 'package_scope="orgs"' in workflow
    assert 'package_scope="users"' in workflow
    assert "private-or-absent package" in workflow
    assert "scripts/require_private_ghcr_package.py" in workflow
    assert "--phase pre-push" in workflow
    assert "--phase post-push" in workflow
    assert "Require the model-bearing package to be private after push" in workflow
    assert workflow.index("Build and push the immutable unverified candidate") < workflow.index(
        "Require the model-bearing package to be private after push"
    )
    assert "candidate-sha-${{ env.SOURCE_SHA }}" in workflow
    assert "candidate workflow accepts only an unverified image" in workflow
    assert "runtime_fit_report_sha256" in workflow
    assert "license_confirmation_sha256" not in workflow
    assert "operator_license_confirmation" not in workflow
    assert 'MAX_COMPRESSED_IMAGE_BYTES: "33000000000"' in workflow
    assert "70000000000" in workflow
    assert "moby/buildkit:v0.32.2@sha256:" in workflow
    assert "docker/buildkit-syft-scanner:stable-1@sha256:" in workflow
    assert "${IMAGE_NAME}@${{ steps.verify.outputs.digest }}" in workflow
    assert "a14b-candidate.spdx.json" in workflow
    assert "anchore/scan-action@e1165082" in workflow
    assert "grype-version: v0.110.0" in workflow
    assert "severity-cutoff: critical" in workflow
    assert "candidate_digest: ${{ steps.verify.outputs.digest }}" in workflow
    assert 'imagetools inspect "${IMAGE_NAME}@${digest}"' in workflow
    assert "actions/upload-artifact@043fb46d" in workflow
    assert "Dockerfile.video-worker-a14b" in workflow
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
