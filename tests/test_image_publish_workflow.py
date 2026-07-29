from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "publish-images.yml"


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_registry_publish_is_gated_on_the_exact_successful_main_ci_commit() -> None:
    workflow = _workflow()

    assert "workflow_run:" in workflow
    assert 'workflows: ["CI"]' in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
    assert "SOURCE_SHA: ${{ github.event.workflow_run.head_sha }}" in workflow
    assert "ref: ${{ env.SOURCE_SHA }}" in workflow
    assert "persist-credentials: false" in workflow


def test_registry_actions_are_full_commit_pins_and_permissions_are_narrow() -> None:
    workflow = _workflow()

    assert "docker/setup-buildx-action@4d04d5d9486b7bd6fa91e7baf45bbb4f8b9deedd" in workflow
    assert "docker/login-action@b45d80f862d83dbcd57f89517bcf500b2ab88fb2" in workflow
    assert "docker/build-push-action@f9f3042f7e2789586610d6e8b85c8f03e5195baf" in workflow
    assert "actions/attest@59d89421af93a897026c735860bf21b6eb4f7b26" in workflow
    assert "contents: read" in workflow
    assert "packages: write" in workflow
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
    assert "artifact-metadata: write" not in workflow


def test_all_deployable_images_are_digest_attested_without_a_mutable_tag() -> None:
    workflow = _workflow()

    assert "image_suffix: control-plane" in workflow
    assert "image_suffix: gpu-worker" in workflow
    assert "dockerfile: Dockerfile.worker" in workflow
    assert "image_suffix: semantic-gateway" in workflow
    assert "dockerfile: Dockerfile.semantic-gateway" in workflow
    assert "platforms: linux/amd64" in workflow
    assert "tags: ${{ env.IMAGE_NAME }}:sha-${{ env.SOURCE_SHA }}" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert "subject-digest: ${{ steps.push.outputs.digest }}" in workflow
    assert "push-to-registry: true" in workflow
    assert "create-storage-record: false" in workflow
    assert ":latest" not in workflow.casefold()
