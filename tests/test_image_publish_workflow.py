import hashlib
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "publish-images.yml"
I2V_INPUTS = (
    ".dockerignore",
    ".gitattributes",
    "Dockerfile.i2v-worker",
    "THIRD_PARTY_LICENSES.md",
    "requirements-i2v-face.lock",
    "requirements-i2v-worker.lock",
    "patches/comfyui-nag/chroma-stream-blocks.patch",
    "src/gen_automation/__init__.py",
    "src/gen_automation/i2v_worker",
    "workflows/dasiwa-wan22-i2v-v1.api.json",
)


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


def test_model_free_i2v_worker_is_keyed_only_by_its_exact_inputs() -> None:
    workflow = _workflow()
    generic_matrix = workflow.split("  publish-worker:\n", maxsplit=1)[0]
    i2v_job = workflow.split("  publish-i2v-worker:\n", maxsplit=1)[1].split(
        "  record-staging-source:\n", maxsplit=1
    )[0]
    metadata_job = workflow.split("  record-staging-source:\n", maxsplit=1)[1]

    assert "image_suffix: i2v-worker" not in generic_matrix
    assert "dockerfile: Dockerfile.i2v-worker" not in generic_matrix
    inputs_match = re.search(r"          i2v_inputs=\(\n(.*?)\n          \)", i2v_job, re.DOTALL)
    assert inputs_match is not None
    assert tuple(line.strip() for line in inputs_match.group(1).splitlines()) == I2V_INPUTS
    assert "fetch-depth: 0" in i2v_job
    assert 'git log --first-parent -1 --format=%H "$SOURCE_SHA" -- "${i2v_inputs[@]}"' in i2v_job
    assert 'git ls-tree -r "$1" -- "${i2v_inputs[@]}"' in i2v_job
    assert "source_revision: ${{ steps.i2v_source.outputs.revision }}" in i2v_job
    assert "image_digest: ${{ steps.verify_i2v.outputs.digest }}" in i2v_job
    assert "ref: ${{ steps.i2v_source.outputs.revision }}" in i2v_job
    assert "Reuse an existing immutable I2V worker image when available" in i2v_job
    assert "group: publish-i2v-worker" in i2v_job
    assert "cancel-in-progress: false" in i2v_job
    assert 'image_tag="${IMAGE_NAME}:sha-${I2V_SOURCE_REVISION}"' in i2v_job
    assert "steps.existing_i2v.outputs.exists == 'false'" in i2v_job
    assert "file: Dockerfile.i2v-worker" in i2v_job
    assert "org.opencontainers.image.revision=${{ steps.i2v_source.outputs.revision }}" in i2v_job
    assert "provenance: mode=max" in i2v_job
    assert "sbom: true" in i2v_job
    assert "Attest the new I2V worker registry digest" in i2v_job
    assert "subject-digest: ${{ steps.push_i2v.outputs.digest }}" in i2v_job
    assert "Verify the exact immutable I2V worker image" in i2v_job
    assert '[ "$digest" = "${{ steps.push_i2v.outputs.digest }}" ]' in i2v_job
    assert 'printf \'digest=%s\\n\' "$digest" >>"$GITHUB_OUTPUT"' in i2v_job
    assert "needs: [publish, publish-worker, publish-i2v-worker]" in metadata_job
    assert "I2V_SOURCE_REVISION: ${{ needs.publish-i2v-worker.outputs.source_revision }}" in (
        metadata_job
    )
    assert "I2V_IMAGE_DIGEST: ${{ needs.publish-i2v-worker.outputs.image_digest }}" in metadata_job
    assert "staging-i2v-worker-source-revision" in metadata_job
    assert "staging-i2v-worker-image-digest" in metadata_job


def test_i2v_source_resolver_stays_on_the_last_main_input_tree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    git_executable = shutil.which("git")
    assert git_executable is not None

    def git(*arguments: str) -> str:
        return subprocess.run(  # noqa: S603 - isolated test repository only.
            [git_executable, *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def input_file(path: str) -> Path:
        candidate = repository / path
        if path == "src/gen_automation/i2v_worker":
            candidate /= "main.py"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    def commit(message: str) -> str:
        git("add", "--all")
        git("commit", "--message", message)
        return git("rev-parse", "HEAD")

    def resolve(source: str) -> str:
        candidate = git(
            "log",
            "--first-parent",
            "-1",
            "--format=%H",
            source,
            "--",
            *I2V_INPUTS,
        )

        def fingerprint(revision: str) -> str:
            tree = subprocess.run(  # noqa: S603 - isolated test repository only.
                [git_executable, "ls-tree", "-r", revision, "--", *I2V_INPUTS],
                cwd=repository,
                check=True,
                capture_output=True,
            ).stdout
            return hashlib.sha256(tree).hexdigest()

        return candidate if fingerprint(candidate) == fingerprint(source) else source

    git("init", "--initial-branch", "main")
    git("config", "user.email", "tests@example.invalid")
    git("config", "user.name", "Workflow tests")
    for path in I2V_INPUTS:
        input_file(path).write_text(f"initial {path}\n", encoding="utf-8")
    commit("initial I2V tree")

    git("checkout", "-b", "feature")
    input_file("Dockerfile.i2v-worker").write_text("feature worker\n", encoding="utf-8")
    input_file("src/gen_automation/i2v_worker").write_text("feature runtime\n", encoding="utf-8")
    feature_revision = commit("change I2V inputs")

    git("checkout", "main")
    (repository / "README.md").write_text("unrelated\n", encoding="utf-8")
    commit("diverge main")
    git("merge", "--no-ff", "feature", "--message", "merge I2V worker")
    published_revision = git("rev-parse", "HEAD")
    (repository / "README.md").write_text("still unrelated\n", encoding="utf-8")
    unrelated_revision = commit("control-plane-only change")

    assert resolve(unrelated_revision) == published_revision
    assert published_revision != feature_revision
    for index, path in enumerate(I2V_INPUTS, start=1):
        input_file(path).write_text(f"change {index}\n", encoding="utf-8")
        input_revision = commit(f"change {path}")
        assert resolve(input_revision) == input_revision


def test_worker_publication_is_keyed_only_by_the_exact_worker_inputs() -> None:
    workflow = _workflow()
    generic_matrix = workflow.split("  publish-worker:\n", maxsplit=1)[0]
    worker_job = workflow.split("  publish-worker:\n", maxsplit=1)[1].split(
        "  publish-i2v-worker:\n", maxsplit=1
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
        "src/gen_automation/domain/controlled_duo.py",
        "src/gen_automation/domain/deliverability.py",
        "src/gen_automation/domain/generation_limits.py",
        "src/gen_automation/domain/signing.py",
    ):
        assert worker_input in worker_job
    assert 'git ls-tree -r "$1" -- "${worker_inputs[@]}"' in worker_job


def test_worker_is_reused_or_built_attested_and_verified_by_immutable_revision() -> None:
    workflow = _workflow()
    worker_job = workflow.split("  publish-worker:\n", maxsplit=1)[1].split(
        "  publish-i2v-worker:\n", maxsplit=1
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

    assert "needs: [publish, publish-worker, publish-i2v-worker]" in metadata_job
    assert "WORKER_SOURCE_REVISION: ${{ needs.publish-worker.outputs.source_revision }}" in (
        metadata_job
    )
    assert "WORKER_IMAGE_DIGEST: ${{ needs.publish-worker.outputs.image_digest }}" in metadata_job
    assert "staging-worker-source-revision" in metadata_job
    assert "staging-worker-image-digest" in metadata_job
