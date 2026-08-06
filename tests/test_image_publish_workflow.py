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


def test_control_plane_images_are_digest_attested_without_a_mutable_tag() -> None:
    workflow = _workflow()

    assert "image_suffix: control-plane" in workflow
    assert "image_suffix: semantic-gateway" in workflow
    assert "dockerfile: Dockerfile.semantic-gateway" in workflow
    assert "image_suffix: control-plane-mega" in workflow
    assert "dockerfile: Dockerfile.mega" in workflow
    assert "platforms: linux/amd64" in workflow
    assert "tags: ${{ env.IMAGE_NAME }}:sha-${{ env.SOURCE_SHA }}" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert "subject-digest: ${{ steps.push.outputs.digest }}" in workflow
    assert "push-to-registry: true" in workflow
    assert "create-storage-record: false" in workflow
    assert ":latest" not in workflow.casefold()


def test_worker_publication_is_keyed_only_by_the_exact_worker_inputs() -> None:
    workflow = _workflow()
    generic_matrix = workflow.split("  publish-worker:\n", maxsplit=1)[0]
    worker_job = workflow.split("  publish-worker:\n", maxsplit=1)[1].split(
        "  record-staging-source:\n", maxsplit=1
    )[0]

    assert "image_suffix: gpu-worker" not in generic_matrix
    assert "dockerfile: Dockerfile.worker" not in generic_matrix
    assert "fetch-depth: 0" in worker_job
    assert 'git log -1 --format=%H "$SOURCE_SHA" -- "${worker_inputs[@]}"' in worker_job
    for worker_input in (
        ".dockerignore",
        ".gitattributes",
        "Dockerfile.worker",
        "requirements.lock",
        "requirements-comfy.lock",
        "requirements-worker-base.txt",
        "patches/salad-queue-worker/strict-http-status.patch",
        "src/gen_automation/__init__.py",
        "src/gen_automation/gpu_worker",
        "src/gen_automation/domain/__init__.py",
        "src/gen_automation/domain/deliverability.py",
        "src/gen_automation/domain/generation_limits.py",
        "src/gen_automation/domain/signing.py",
    ):
        assert worker_input in worker_job
    assert 'git ls-tree -r "$1" -- "${worker_inputs[@]}"' in worker_job


def test_worker_is_reused_or_built_attested_and_verified_by_immutable_revision() -> None:
    workflow = _workflow()
    worker_job = workflow.split("  publish-worker:\n", maxsplit=1)[1].split(
        "  record-staging-source:\n", maxsplit=1
    )[0]

    assert "Reuse an existing immutable worker image when available" in worker_job
    assert "group: publish-gpu-worker" in worker_job
    assert "cancel-in-progress: false" in worker_job
    assert 'image_tag="${IMAGE_NAME}:sha-${WORKER_SOURCE_REVISION}"' in worker_job
    assert "steps.existing_worker.outputs.exists == 'false'" in worker_job
    assert "file: Dockerfile.worker" in worker_job
    assert "provenance: mode=max" in worker_job
    assert "sbom: true" in worker_job
    assert "Attest the new worker registry digest" in worker_job
    assert "subject-digest: ${{ steps.push_worker.outputs.digest }}" in worker_job
    assert "Verify the exact immutable worker image" in worker_job
    assert 'printf \'digest=%s\\n\' "$digest" >>"$GITHUB_OUTPUT"' in worker_job


def test_publication_carries_stable_worker_source_and_digest_to_deployment() -> None:
    workflow = _workflow()
    metadata_job = workflow.split("  record-staging-source:\n", maxsplit=1)[1]

    assert "needs: [publish, publish-worker]" in metadata_job
    assert "WORKER_SOURCE_REVISION: ${{ needs.publish-worker.outputs.source_revision }}" in (
        metadata_job
    )
    assert "WORKER_IMAGE_DIGEST: ${{ needs.publish-worker.outputs.image_digest }}" in metadata_job
    assert "staging-worker-source-revision" in metadata_job
    assert "staging-worker-image-digest" in metadata_job
