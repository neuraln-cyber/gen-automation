#!/usr/bin/env python3
"""Plan or provision the cost-bounded RunPod anatomy VLM endpoint.

The default command is read-only and prints the exact immutable plan.  Applying
the plan requires both an explicit CLI acknowledgement and an environment
switch so a copied API key can never create billable resources accidentally.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

RUNPOD_API_ROOT = "https://rest.runpod.io/v1"
MODEL = "Qwen/Qwen3-VL-8B-Instruct"
MODEL_REVISION = "60595ebc30ec8e3b1d3b9e65d4943ca011c0006a"
WORKER_IMAGE = (
    "runpod/worker-v1-vllm@"
    "sha256:cda7e80957d736e82b8c040eee69e0b6a7fd9d0fa6c7c74ef247d79b89bf9cab"
)
TEMPLATE_NAME = "gen-automation-anatomy-qwen3-vl-8b-v1"
ENDPOINT_NAME = "gen-automation-anatomy-staging"
SPEND_SWITCH = "GEN_AUTOMATION_RUNPOD_SPEND_ALLOWED"


def template_payload() -> dict[str, Any]:
    return {
        "imageName": WORKER_IMAGE,
        "name": TEMPLATE_NAME,
        "category": "NVIDIA",
        "containerDiskInGb": 40,
        "dockerEntrypoint": [],
        "dockerStartCmd": [],
        "env": {
            "MODEL_NAME": MODEL,
            # worker-vllm maps upper-cased vLLM AsyncEngineArgs fields; the
            # immutable Hugging Face commit is the `revision` engine argument.
            "REVISION": MODEL_REVISION,
            "OPENAI_SERVED_MODEL_NAME_OVERRIDE": MODEL,
            "MAX_MODEL_LEN": "8192",
            "MAX_CONCURRENCY": "1",
            "MAX_NUM_SEQS": "1",
            "GPU_MEMORY_UTILIZATION": "0.92",
            "DISABLE_LOG_REQUESTS": "true",
            "DISABLE_LOG_STATS": "false",
        },
        "isPublic": False,
        "isServerless": True,
        "ports": [],
        "readme": "Pinned private VLM worker for gen-automation anatomy assessment.",
        "volumeInGb": 0,
        "volumeMountPath": "/runpod-volume",
    }


def endpoint_payload(template_id: str) -> dict[str, Any]:
    if not template_id.strip():
        raise ValueError("template_id is required")
    return {
        "name": ENDPOINT_NAME,
        "templateId": template_id,
        "computeType": "GPU",
        "gpuTypeIds": ["NVIDIA A40", "NVIDIA RTX A6000"],
        "gpuCount": 1,
        "allowedCudaVersions": ["13.0"],
        "minCudaVersion": "13.0",
        "dataCenterIds": ["EU-RO-1", "EU-CZ-1", "EU-SE-1", "EU-NL-1", "EU-FR-1"],
        "flashboot": True,
        "workersMin": 0,
        "workersMax": 1,
        "idleTimeout": 5,
        "executionTimeoutMs": 600_000,
        "scalerType": "QUEUE_DELAY",
        "scalerValue": 1,
    }


def public_plan() -> dict[str, Any]:
    return {
        "mutates_runpod": False,
        "billing_guard": {
            "workers_min": 0,
            "workers_max": 1,
            "required_apply_environment": f"{SPEND_SWITCH}=true",
            "required_apply_flag": "--acknowledge-spend",
        },
        "template": template_payload(),
        "endpoint": endpoint_payload("<created-template-id>"),
        "gateway_upstream_url": (
            "https://api.runpod.ai/v2/<created-endpoint-id>/openai/v1/chat/completions"
        ),
    }


def _post(api_key: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - fixed official API root
        f"{RUNPOD_API_ROOT}{path}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            body = response.read(1024 * 1024)
    except urllib.error.HTTPError as error:
        detail = error.read(32 * 1024).decode("utf-8", errors="replace")
        raise RuntimeError(f"RunPod API rejected {path}: HTTP {error.code}: {detail}") from error
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"RunPod API returned a non-object for {path}")
    return parsed


def _required_identifier(payload: dict[str, Any], *, resource: str) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"RunPod did not return an ID for the created {resource}")
    return value


def apply_plan(*, state_file: Path, acknowledge_spend: bool) -> dict[str, Any]:
    if not acknowledge_spend or os.environ.get(SPEND_SWITCH, "").lower() != "true":
        raise RuntimeError(
            "paid provisioning is locked; explicit spend release and "
            "--acknowledge-spend are required"
        )
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if not api_key.strip():
        raise RuntimeError("RUNPOD_API_KEY is required for apply")
    if state_file.exists():
        raise RuntimeError(f"refusing to replace existing state file: {state_file}")

    template = _post(api_key, "/templates", template_payload())
    template_id = _required_identifier(template, resource="template")
    endpoint = _post(api_key, "/endpoints", endpoint_payload(template_id))
    endpoint_id = _required_identifier(endpoint, resource="endpoint")
    state = {
        "schema": "gen-automation/runpod-anatomy-state/v1",
        "template_id": template_id,
        "endpoint_id": endpoint_id,
        "model": MODEL,
        "model_revision": MODEL_REVISION,
        "worker_image": WORKER_IMAGE,
        "workers_min": 0,
        "workers_max": 1,
        "gateway_upstream_url": (
            f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1/chat/completions"
        ),
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("plan", "apply"), default="plan")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--acknowledge-spend", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plan":
        print(json.dumps(public_plan(), indent=2, sort_keys=True))
        return 0
    if args.state_file is None:
        print("apply requires --state-file", file=sys.stderr)
        return 2
    try:
        state = apply_plan(
            state_file=args.state_file,
            acknowledge_spend=args.acknowledge_spend,
        )
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
