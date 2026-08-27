import ast
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE_PATH = ROOT / "Dockerfile.worker"
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
COMFY_INPUT_PATH = ROOT / "requirements-comfy.in"
COMFY_LOCK_PATH = ROOT / "requirements-comfy.lock"
WORKER_BASE_PATH = ROOT / "requirements-worker-base.txt"
SALAD_QUEUE_WORKER_PATCH_PATH = ROOT / "patches" / "salad-queue-worker" / "strict-http-status.patch"
SALAD_QUEUE_WORKER_HEARTBEAT_PATCH_PATH = (
    ROOT / "patches" / "salad-queue-worker" / "job-stream-heartbeat-watchdog.patch"
)

DOCKERFILE_FRONTEND = (
    "docker/dockerfile:1.7.1@"
    "sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e"
)
GO_IMAGE = (
    "golang:1.26.2-alpine@sha256:f85330846cde1e57ca9ec309382da3b8e6ae3ab943d2739500e08c86393a21b1"
)
PYTORCH_IMAGE = (
    "pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime@"
    "sha256:eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be"
)
UBUNTU_IMAGE = (
    "ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517"
)
COMFYUI_COMMIT = "700821e1364eaab0e8f21c538a2131719fec57bf"
IMPACT_PACK_COMMIT = "429d0159ad429e64d2b3916e6e7be9c22d025c3c"
IMPACT_SUBPACK_COMMIT = "50c7b71a6a224734cc9b21963c6d1926816a97f1"
SALAD_QUEUE_WORKER_COMMIT = "73d7a3c80a73f26339194e024cb47c8501c67f75"
SALAD_QUEUE_WORKER_PATCH_SHA256 = "a25fa6ca196554eb1e4b6acaabfb22730db69515d07a8e82585015df4213c0ae"
SALAD_QUEUE_WORKER_HEARTBEAT_PATCH_SHA256 = (
    "e19fcdf5b5f828e3435f027133017dbd68d46c4c0ab0321c2de7198719b06d18"
)


def _dockerfile() -> str:
    return DOCKERFILE_PATH.read_text(encoding="utf-8")


def _logical_lines(value: str) -> str:
    joined = re.sub(r"\\\r?\n\s*", " ", value)
    return re.sub(r"[ \t]+", " ", joined)


def test_builder_and_gpu_runtime_bases_are_exact_digest_pins() -> None:
    dockerfile = _dockerfile()
    from_lines = [
        line.strip() for line in dockerfile.splitlines() if line.strip().upper().startswith("FROM ")
    ]

    assert dockerfile.splitlines()[0] == f"# syntax={DOCKERFILE_FRONTEND}"
    assert from_lines == [
        f"FROM {GO_IMAGE} AS salad-queue-worker-builder",
        f"FROM {PYTORCH_IMAGE} AS pytorch-runtime",
        "FROM pytorch-runtime AS pytorch-runtime-split",
        f"FROM {UBUNTU_IMAGE}",
    ]
    assert all(
        re.fullmatch(r"FROM \S+@sha256:[0-9a-f]{64}(?: AS [a-z0-9-]+)?", line)
        for line in (from_lines[0], from_lines[1], from_lines[3])
    )
    assert not re.search(r"^ARG\s+\w*(?:BASE|IMAGE)", dockerfile, flags=re.MULTILINE)


def test_comfyui_source_is_the_exact_immutable_release_commit() -> None:
    dockerfile = _logical_lines(_dockerfile())

    assert f"COMFYUI_COMMIT={COMFYUI_COMMIT}" in dockerfile
    assert "ARG COMFYUI_COMMIT" not in dockerfile
    assert "https://github.com/Comfy-Org/ComfyUI.git" in dockerfile
    assert 'git -C /opt/comfyui fetch --depth 1 origin "${COMFYUI_COMMIT}"' in dockerfile
    assert 'test "$(git -C /opt/comfyui rev-parse HEAD)" = "${COMFYUI_COMMIT}"' in dockerfile
    assert f'org.opencontainers.image.comfyui.revision="{COMFYUI_COMMIT}"' in dockerfile

    comfy_input = COMFY_INPUT_PATH.read_text(encoding="utf-8")
    assert COMFYUI_COMMIT in comfy_input


def test_salad_queue_worker_is_built_from_the_exact_commit_and_copied() -> None:
    dockerfile = _logical_lines(_dockerfile())

    assert f"SALAD_QUEUE_WORKER_COMMIT={SALAD_QUEUE_WORKER_COMMIT}" in dockerfile
    assert "ARG SALAD_QUEUE_WORKER_COMMIT" not in dockerfile
    assert "https://github.com/SaladTechnologies/salad-cloud-job-queue-worker.git" in dockerfile
    assert 'git -C /src fetch --depth 1 origin "${SALAD_QUEUE_WORKER_COMMIT}"' in dockerfile
    assert 'test "$(git -C /src rev-parse HEAD)" = "${SALAD_QUEUE_WORKER_COMMIT}"' in dockerfile
    assert "go mod download" in dockerfile
    assert "go mod verify" in dockerfile
    assert "-mod=readonly" in dockerfile
    assert "CGO_ENABLED=0 GOOS=linux GOARCH=amd64" in dockerfile
    assert "./cmd/salad-http-job-queue-worker" in dockerfile
    assert (
        "COPY --from=salad-queue-worker-builder --chmod=0555 "
        "/out/salad-http-job-queue-worker "
        "/usr/local/bin/salad-http-job-queue-worker"
    ) in dockerfile
    assert (
        f'org.opencontainers.image.salad-queue-worker.revision="{SALAD_QUEUE_WORKER_COMMIT}"'
        in dockerfile
    )


def test_salad_queue_worker_accepts_only_2xx_job_responses() -> None:
    dockerfile = _logical_lines(_dockerfile())
    patch = SALAD_QUEUE_WORKER_PATCH_PATH.read_bytes()
    patch_text = patch.decode("utf-8")

    assert hashlib.sha256(patch).hexdigest() == SALAD_QUEUE_WORKER_PATCH_SHA256
    assert f"SALAD_QUEUE_WORKER_PATCH_SHA256={SALAD_QUEUE_WORKER_PATCH_SHA256}" in dockerfile
    assert (
        "COPY patches/salad-queue-worker/strict-http-status.patch "
        "/tmp/strict-http-status.patch" in dockerfile
    )
    assert "sha256sum -c -" in dockerfile
    assert "git apply --unidiff-zero --check /tmp/strict-http-status.patch" in dockerfile
    assert "git apply --unidiff-zero /tmp/strict-http-status.patch" in dockerfile
    assert "go test -mod=readonly ./cmd/salad-http-job-queue-worker" in dockerfile
    assert (
        f'org.opencontainers.image.salad-queue-worker.patch-sha256="'
        f'{SALAD_QUEUE_WORKER_PATCH_SHA256}"' in dockerfile
    )
    assert (
        "+\tif resp.StatusCode < http.StatusOK || "
        "resp.StatusCode >= http.StatusMultipleChoices {" in patch_text
    )
    assert 'name: "unauthorized", statusCode: http.StatusUnauthorized, wantError: true' in (
        patch_text
    )


def test_salad_queue_worker_reconnects_a_silent_job_stream() -> None:
    dockerfile = _logical_lines(_dockerfile())
    patch = SALAD_QUEUE_WORKER_HEARTBEAT_PATCH_PATH.read_bytes()
    patch_text = patch.decode("utf-8")

    assert hashlib.sha256(patch).hexdigest() == SALAD_QUEUE_WORKER_HEARTBEAT_PATCH_SHA256
    assert (
        f"SALAD_QUEUE_WORKER_HEARTBEAT_PATCH_SHA256="
        f"{SALAD_QUEUE_WORKER_HEARTBEAT_PATCH_SHA256}" in dockerfile
    )
    assert (
        "COPY patches/salad-queue-worker/job-stream-heartbeat-watchdog.patch "
        "/tmp/job-stream-heartbeat-watchdog.patch" in dockerfile
    )
    assert "git apply --check /tmp/job-stream-heartbeat-watchdog.patch" in dockerfile
    assert "git apply /tmp/job-stream-heartbeat-watchdog.patch" in dockerfile
    assert (
        f'org.opencontainers.image.salad-queue-worker.heartbeat-patch-sha256="'
        f'{SALAD_QUEUE_WORKER_HEARTBEAT_PATCH_SHA256}"' in dockerfile
    )
    assert "+const jobStreamSilenceTimeout = 2 * time.Minute" in patch_text
    assert "+\t\t\tstreamCtx, cancelStream := context.WithCancel(authCtx)" in patch_text
    assert '+\t\t\t\t\t\tlogger.Warn("job stream heartbeat timed out")' in patch_text
    assert "+\t\t\t\t\t\tcancelStream()" in patch_text
    assert "CurrentJobId: currentJobId" in patch_text


def test_final_runtime_is_non_root_with_a_writable_non_root_home() -> None:
    dockerfile = _dockerfile()
    user_directives = re.findall(r"^USER\s+(.+)$", dockerfile, flags=re.MULTILINE)

    assert user_directives == ["10001:10001"]
    assert "useradd" in dockerfile
    assert "--uid 10001" in dockerfile
    assert "--gid worker" in dockerfile
    assert "--home-dir /home/worker" in dockerfile
    assert "--create-home" in dockerfile
    assert "HOME=/home/worker" in dockerfile
    assert dockerfile.index("USER 10001:10001") < dockerfile.index("ENTRYPOINT")


def test_image_has_only_exactly_pinned_custom_node_sources() -> None:
    dockerfile = _logical_lines(_dockerfile())
    normalized = _dockerfile().casefold()

    assert "comfyui-manager" not in normalized
    assert "custom-node-manager" not in normalized
    assert ":latest" not in normalized
    assert "@latest" not in normalized
    assert "git clone" not in normalized
    assert f"IMPACT_PACK_COMMIT={IMPACT_PACK_COMMIT}" in dockerfile
    assert f"IMPACT_SUBPACK_COMMIT={IMPACT_SUBPACK_COMMIT}" in dockerfile
    assert "https://github.com/ltdrdata/ComfyUI-Impact-Pack.git" in dockerfile
    assert "https://github.com/ltdrdata/ComfyUI-Impact-Subpack.git" in dockerfile
    assert (
        'test "$(git -C /opt/comfyui/custom_nodes/ComfyUI-Impact-Pack rev-parse HEAD)" '
        '= "${IMPACT_PACK_COMMIT}"' in dockerfile
    )
    assert (
        'test "$(git -C /opt/comfyui/custom_nodes/ComfyUI-Impact-Subpack rev-parse HEAD)" '
        '= "${IMPACT_SUBPACK_COMMIT}"' in dockerfile
    )
    assert f'org.opencontainers.image.impact-pack.revision="{IMPACT_PACK_COMMIT}"' in dockerfile
    assert (
        f'org.opencontainers.image.impact-subpack.revision="{IMPACT_SUBPACK_COMMIT}"' in dockerfile
    )


def test_expected_worker_entrypoint_is_the_only_runtime_entrypoint() -> None:
    dockerfile = _dockerfile()
    entrypoints = re.findall(r"^ENTRYPOINT\s+(.+)$", dockerfile, flags=re.MULTILINE)

    assert entrypoints == [
        '["/opt/worker-venv/bin/python", "-m", "gen_automation.gpu_worker.main"]'
    ]
    assert not re.search(r"^CMD\s+", dockerfile, flags=re.MULTILINE)


def test_worker_image_copies_only_its_runtime_package_and_direct_domain_imports() -> None:
    dockerfile = _dockerfile()
    source_copies = [
        line.strip() for line in dockerfile.splitlines() if line.strip().startswith("COPY src/")
    ]

    assert source_copies == [
        "COPY src/gen_automation/__init__.py ./src/gen_automation/__init__.py",
        "COPY src/gen_automation/gpu_worker ./src/gen_automation/gpu_worker",
        "COPY src/gen_automation/domain/__init__.py ./src/gen_automation/domain/__init__.py",
        (
            "COPY src/gen_automation/domain/controlled_duo.py "
            "./src/gen_automation/domain/controlled_duo.py"
        ),
        (
            "COPY src/gen_automation/domain/deliverability.py "
            "./src/gen_automation/domain/deliverability.py"
        ),
        (
            "COPY src/gen_automation/domain/generation_limits.py "
            "./src/gen_automation/domain/generation_limits.py"
        ),
        "COPY src/gen_automation/domain/signing.py ./src/gen_automation/domain/signing.py",
    ]
    assert "COPY src ./src" not in dockerfile


def test_worker_domain_copy_contract_matches_the_transitive_runtime_import_graph() -> None:
    worker_package = ROOT / "src" / "gen_automation" / "gpu_worker"
    pending_domain_imports: set[str] = set()
    for source_path in worker_package.glob("*.py"):
        module = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module.startswith("gen_automation.domain."):
                    pending_domain_imports.add(node.module)

    transitive_domain_imports: set[str] = set()
    while pending_domain_imports:
        module_name = pending_domain_imports.pop()
        if module_name in transitive_domain_imports:
            continue
        transitive_domain_imports.add(module_name)
        source_path = ROOT / "src" / Path(*module_name.split(".")).with_suffix(".py")
        module = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module.startswith("gen_automation.domain."):
                    pending_domain_imports.add(node.module)

    assert transitive_domain_imports == {
        "gen_automation.domain.controlled_duo",
        "gen_automation.domain.deliverability",
        "gen_automation.domain.generation_limits",
        "gen_automation.domain.signing",
    }
    dockerfile = _dockerfile()
    for module_name in transitive_domain_imports:
        source_path = module_name.replace(".", "/") + ".py"
        assert f"COPY src/{source_path} ./src/{source_path}" in dockerfile


def test_python_dependencies_are_hash_locked_and_cuda_stack_matches_base() -> None:
    dockerfile = _logical_lines(_dockerfile())
    comfy_lock = COMFY_LOCK_PATH.read_text(encoding="utf-8")
    worker_base = WORKER_BASE_PATH.read_text(encoding="utf-8")

    assert "VIRTUAL_ENV=/opt/worker-venv" in dockerfile
    assert (
        "python -m venv "
        "--copies "
        "--without-pip "
        "--system-site-packages "
        "/opt/worker-venv" in dockerfile
    )
    assert "test -x /opt/worker-venv/bin/python" in dockerfile
    assert "sys.prefix == '/opt/worker-venv'" in dockerfile
    assert "sys.prefix != sys.base_prefix" in dockerfile
    assert (
        "/opt/worker-venv/bin/python -m pip install --only-binary=:all: "
        "--require-hashes --no-deps -r requirements.lock" in dockerfile
    )
    assert (
        "/opt/worker-venv/bin/python -m pip install --only-binary=:all: "
        "--require-hashes --no-deps -r requirements-comfy.lock" in dockerfile
    )
    assert "pip check" not in dockerfile
    assert "--break-system-packages" not in dockerfile
    assert "EXTERNALLY-MANAGED" not in dockerfile
    assert "assert sys.version_info[:2] == (3, 12)" in dockerfile
    assert "--generate-hashes" in comfy_lock.splitlines()[4]
    assert not re.search(r"(?im)^\s*(?:-e\s+|https?://|git\+)", comfy_lock)
    assert worker_base.splitlines() == [
        "# Packages supplied by the immutable PyTorch CUDA base image.",
        "torch==2.11.0",
        "torchvision==0.26.0",
        "torchaudio==2.11.0",
    ]
    assert "assert torch.__version__.split('+')[0] == '2.11.0'" in dockerfile
    assert "assert torchvision.__version__.split('+')[0] == '0.26.0'" in dockerfile
    assert "assert torchaudio.__version__.split('+')[0] == '2.11.0'" in dockerfile
    assert (
        "all('/opt/worker-venv/' not in module.__file__ "
        "for module in (torch, torchaudio, torchvision))" in dockerfile
    )
    for dependency in (
        "dill==0.4.1",
        "matplotlib==3.11.1",
        "opencv-python-headless==4.14.0.94",
        "piexif==1.1.3",
        "scikit-image==0.26.0",
        "segment-anything==1.0",
        "ultralytics==8.4.110",
    ):
        assert dependency in comfy_lock


def test_ci_builds_the_contract_dockerfile_without_pin_overrides() -> None:
    ci = CI_PATH.read_text(encoding="utf-8")
    command = "docker build --file Dockerfile.worker --tag gen-automation-gpu-worker:test ."

    assert command in ci
    assert "Smoke-test GPU worker Python runtime" in ci
    assert "import gen_automation.gpu_worker.main" in ci
    assert "build-arg" not in ci.casefold()
