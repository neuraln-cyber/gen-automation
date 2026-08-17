#!/usr/bin/env python3
"""Plan or apply the single-worker RunPod I2V endpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

RUNPOD_API_ROOT = "https://rest.runpod.io/v1"
ENDPOINT_NAME = "gen-automation-i2v-staging"
VOLUME_NAME_PREFIX = "gen-automation-i2v-models-staging"
VOLUME_SIZE_GB = 100
DATA_CENTER_ID = "US-IL-1"
# RunPod canonicalizes the compatible 32/48 GB Serverless pools to these IDs.
GPU_TYPES = (
    "NVIDIA GeForce RTX 5090",
    "NVIDIA A40",
    "NVIDIA RTX A6000",
    "NVIDIA L40S",
)
SPEND_SWITCH = "GEN_AUTOMATION_RUNPOD_I2V_SPEND_ALLOWED"
STATE_SCHEMA = "gen-automation/runpod-i2v-state/v1"
IMAGE_PATTERN = re.compile(
    r"^ghcr[.]io/neuraln-cyber/gen-automation/i2v-worker@sha256:([0-9a-f]{64})$"
)
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RunPodHTTPError(RuntimeError):
    def __init__(self, method: str, path: str, status_code: int) -> None:
        super().__init__(f"RunPod API rejected {method} {path}: HTTP {status_code}")
        self.status_code = status_code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _api_key() -> str:
    value = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not value:
        raise RuntimeError("RUNPOD_API_KEY is required")
    return value


def _request(
    api_key: str,
    path: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
    allow_not_found: bool = False,
) -> Any:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed official API root
        f"{RUNPOD_API_ROOT}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            response_body = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        if allow_not_found and error.code == 404:
            return None
        raise RunPodHTTPError(method, path, error.code) from None
    except urllib.error.URLError:
        raise RuntimeError(f"RunPod API request failed for {method} {path}") from None
    if not response_body:
        return None
    try:
        return json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError(f"RunPod API returned invalid JSON for {method} {path}") from None


def _list(api_key: str, path: str) -> list[dict[str, Any]]:
    value = _request(api_key, path, method="GET")
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise RuntimeError(f"RunPod API returned an invalid list for {path}")
    return value


def _get(api_key: str, path: str) -> dict[str, Any] | None:
    value = _request(api_key, path, method="GET", allow_not_found=True)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError(f"RunPod API returned an invalid object for {path}")
    return value


def _mutate(
    api_key: str,
    path: str,
    *,
    method: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    value = _request(api_key, path, method=method, payload=payload)
    if not isinstance(value, dict):
        raise RuntimeError(f"RunPod API returned an invalid mutation response for {path}")
    return value


def _required_id(value: dict[str, Any], kind: str) -> str:
    resource_id = value.get("id")
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise RuntimeError(f"RunPod did not return a valid {kind} ID")
    return resource_id


def _find_unique(
    api_key: str,
    *,
    path: str,
    name: str,
    kind: str,
) -> dict[str, Any] | None:
    candidates = [item for item in _list(api_key, path) if item.get("name") == name]
    if len(candidates) > 1:
        raise RuntimeError(f"cannot safely reconcile {kind}: duplicate name {name!r}")
    return candidates[0] if candidates else None


def _read_model_objects(path: Path) -> tuple[str, str]:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not path.is_file() or metadata.st_size > 64 * 1024:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raise RuntimeError("model objects file is invalid") from None
    if not isinstance(value, list) or len(value) not in (4, 14):
        raise RuntimeError("model objects file must contain exactly 4 or 14 objects")
    roles: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError("model objects file is invalid")
        role = item.get("role")
        if not isinstance(role, str) or not role or role in roles:
            raise RuntimeError("model objects file contains invalid roles")
        roles.add(role)
        if (
            not isinstance(item.get("bucket"), str)
            or not isinstance(item.get("key"), str)
            or not isinstance(item.get("version_id"), str)
            or not isinstance(item.get("byte_size"), int)
            or not SHA256_PATTERN.fullmatch(str(item.get("sha256", "")))
            or not isinstance(item.get("install_path"), str)
        ):
            raise RuntimeError("model objects file is invalid")
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _spec(
    *,
    image: str,
    source_revision: str,
    manifest_source_sha256: str,
    model_objects_json: str,
    model_objects_sha256: str,
    workers_min: int = 0,
    data_center_id: str = DATA_CENTER_ID,
) -> dict[str, dict[str, Any]]:
    match = IMAGE_PATTERN.fullmatch(image)
    if match is None:
        raise RuntimeError("worker image must be the exact official GHCR digest reference")
    if REVISION_PATTERN.fullmatch(source_revision) is None:
        raise RuntimeError("worker source revision is invalid")
    if SHA256_PATTERN.fullmatch(manifest_source_sha256) is None:
        raise RuntimeError("private manifest source identity is invalid")
    if workers_min not in (0, 1):
        raise RuntimeError("minimum workers must be zero or one")
    if re.fullmatch(r"[A-Z]{2,4}-[A-Z]{2,4}-[0-9]", data_center_id) is None:
        raise RuntimeError("RunPod datacenter is invalid")
    template_name = f"gen-automation-i2v-{match.group(1)[:16]}"
    volume_name = f"{VOLUME_NAME_PREFIX}-{data_center_id.casefold()}"
    volume = {
        "name": volume_name,
        "size": VOLUME_SIZE_GB,
        "dataCenterId": data_center_id,
    }
    template = {
        "imageName": image,
        "name": template_name,
        "category": "NVIDIA",
        "containerDiskInGb": 50,
        "dockerEntrypoint": [],
        "dockerStartCmd": [],
        "env": {
            "GEN_I2V_WORKER_ALLOWED_GPU_NAMES_CSV": ",".join(GPU_TYPES),
            "GEN_I2V_WORKER_MODEL_OBJECTS_JSON": model_objects_json,
            "GEN_I2V_WORKER_ENVIRONMENT": "production",
            "GEN_I2V_WORKER_LORA_WORKER_ENABLED": "true",
            "GEN_I2V_WORKER_SOURCE_REVISION": source_revision,
            "GEN_I2V_WORKER_PRIVATE_MANIFEST_SOURCE_SHA256": (manifest_source_sha256),
            "GEN_I2V_WORKER_VOLUME_ROOT": "/runpod-volume",
        },
        "isPublic": False,
        "isServerless": True,
        "ports": [],
        "readme": (
            "Pinned model-free WAN 2.2 I2V worker for gen-automation staging; "
            f"model objects {model_objects_sha256}."
        ),
        "volumeInGb": 0,
        "volumeMountPath": "/runpod-volume",
    }
    endpoint = {
        "name": ENDPOINT_NAME,
        "computeType": "GPU",
        "gpuTypeIds": list(GPU_TYPES),
        "gpuCount": 1,
        "allowedCudaVersions": ["12.8", "12.9", "13.0"],
        "minCudaVersion": "12.8",
        "dataCenterIds": [data_center_id],
        "flashboot": True,
        "workersMin": workers_min,
        "workersMax": 1,
        "idleTimeout": 60,
        "executionTimeoutMs": 6 * 60 * 60 * 1000,
        "scalerType": "QUEUE_DELAY",
        "scalerValue": 1,
    }
    return {"volume": volume, "template": template, "endpoint": endpoint}


def _template_id(endpoint: dict[str, Any]) -> str | None:
    direct = endpoint.get("templateId")
    if isinstance(direct, str) and direct:
        return direct
    nested = endpoint.get("template")
    if isinstance(nested, dict):
        value = nested.get("id")
        return value if isinstance(value, str) and value else None
    return None


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return []


def _verify_volume(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    fields = ("name", "size", "dataCenterId")
    mismatches = [field for field in fields if actual.get(field) != expected[field]]
    if mismatches:
        raise RuntimeError(f"RunPod network volume drift: {', '.join(mismatches)}")


def _verify_template(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    fields = (
        "imageName",
        "name",
        "category",
        "containerDiskInGb",
        "env",
        "isServerless",
        "readme",
    )
    mismatches = [field for field in fields if actual.get(field) != expected[field]]
    if actual.get("dockerEntrypoint") not in (None, []):
        mismatches.append("dockerEntrypoint")
    if actual.get("dockerStartCmd") not in (None, []):
        mismatches.append("dockerStartCmd")
    if actual.get("ports") not in (None, []):
        mismatches.append("ports")
    if mismatches:
        raise RuntimeError(f"RunPod template drift: {', '.join(mismatches)}")


def _verify_endpoint(
    actual: dict[str, Any],
    expected: dict[str, Any],
    *,
    template_id: str,
    volume_id: str,
) -> None:
    fields = (
        "name",
        "gpuCount",
        "workersMin",
        "workersMax",
        "idleTimeout",
        "executionTimeoutMs",
        "scalerType",
        "scalerValue",
    )
    mismatches = [field for field in fields if actual.get(field) != expected[field]]
    if _template_id(actual) != template_id:
        mismatches.append("templateId")
    if _strings(actual.get("gpuTypeIds")) != expected["gpuTypeIds"]:
        mismatches.append("gpuTypeIds")
    if actual.get("networkVolumeId") != volume_id:
        volume_ids = _strings(actual.get("networkVolumeIds"))
        if volume_ids != [volume_id]:
            mismatches.append("networkVolumeId")
    if actual.get("minCudaVersion") not in (None, expected["minCudaVersion"]):
        mismatches.append("minCudaVersion")
    if mismatches:
        raise RuntimeError(f"RunPod endpoint drift: {', '.join(mismatches)}")


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(
        json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _state(spec: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "status": "planned",
        "created_at": _now(),
        "updated_at": _now(),
        "worker_image": spec["template"]["imageName"],
        "resources": {
            name: {"id": None, "create_attempted": False}
            for name in ("volume", "template", "endpoint")
        },
    }


def _resource_state(state: dict[str, Any], name: str) -> dict[str, Any]:
    resources = state.get("resources")
    if not isinstance(resources, dict) or not isinstance(resources.get(name), dict):
        raise RuntimeError("RunPod I2V state file is invalid")
    return cast(dict[str, Any], resources[name])


def _ensure_created(
    *,
    api_key: str,
    state: dict[str, Any],
    state_file: Path,
    kind: str,
    list_path: str,
    create_path: str,
    expected: dict[str, Any],
    verify: Any,
) -> dict[str, Any]:
    resource = _resource_state(state, kind)
    actual = _find_unique(
        api_key,
        path=list_path,
        name=str(expected["name"]),
        kind=kind,
    )
    if actual is None:
        if resource.get("create_attempted"):
            raise RuntimeError(
                f"RunPod {kind} create outcome is unresolved; no duplicate POST was sent"
            )
        resource["create_attempted"] = True
        state["status"] = f"creating_{kind}"
        state["updated_at"] = _now()
        _write_state(state_file, state)
        try:
            actual = _mutate(api_key, create_path, method="POST", payload=expected)
        except Exception:
            actual = _find_unique(
                api_key,
                path=list_path,
                name=str(expected["name"]),
                kind=kind,
            )
            if actual is None:
                raise
    verify(actual, expected)
    resource["id"] = _required_id(actual, kind)
    resource["verified_at"] = _now()
    state["updated_at"] = _now()
    _write_state(state_file, state)
    return actual


def _load_or_create_state(path: Path, spec: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not path.exists():
        return _state(spec)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RuntimeError("RunPod I2V state file is invalid") from None
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise RuntimeError("RunPod I2V state file is invalid")
    if value.get("worker_image") != spec["template"]["imageName"]:
        raise RuntimeError("RunPod I2V state belongs to another worker image")
    return value


def apply(spec: dict[str, dict[str, Any]], *, state_file: Path) -> dict[str, Any]:
    if os.environ.get(SPEND_SWITCH, "").casefold() != "true":
        raise RuntimeError(f"paid provisioning requires {SPEND_SWITCH}=true")
    api_key = _api_key()
    state = _load_or_create_state(state_file, spec)
    _write_state(state_file, state)
    volume = _ensure_created(
        api_key=api_key,
        state=state,
        state_file=state_file,
        kind="volume",
        list_path="/networkvolumes",
        create_path="/networkvolumes",
        expected=spec["volume"],
        verify=_verify_volume,
    )
    template = _find_unique(
        api_key,
        path="/templates?includeEndpointBoundTemplates=true",
        name=str(spec["template"]["name"]),
        kind="template",
    )
    if template is None:
        template = _ensure_created(
            api_key=api_key,
            state=state,
            state_file=state_file,
            kind="template",
            list_path="/templates?includeEndpointBoundTemplates=true",
            create_path="/templates",
            expected=spec["template"],
            verify=_verify_template,
        )
    else:
        try:
            _verify_template(template, spec["template"])
        except RuntimeError:
            template_id = _required_id(template, "template")
            update_fields = (
                "containerDiskInGb",
                "dockerEntrypoint",
                "dockerStartCmd",
                "env",
                "imageName",
                "isPublic",
                "name",
                "ports",
                "readme",
                "volumeInGb",
                "volumeMountPath",
            )
            template = _mutate(
                api_key,
                f"/templates/{urllib.parse.quote(template_id, safe='')}",
                method="PATCH",
                payload={field: spec["template"][field] for field in update_fields},
            )
            _verify_template(template, spec["template"])
        template_state = _resource_state(state, "template")
        template_state.update({"id": _required_id(template, "template"), "verified_at": _now()})
        state["updated_at"] = _now()
        _write_state(state_file, state)
    volume_id = _required_id(volume, "volume")
    template_id = _required_id(template, "template")
    endpoint_payload = {
        **spec["endpoint"],
        "templateId": template_id,
        "networkVolumeId": volume_id,
    }
    endpoint_patch_payload = {
        key: value for key, value in endpoint_payload.items() if key != "computeType"
    }
    endpoint = _find_unique(
        api_key,
        path="/endpoints",
        name=ENDPOINT_NAME,
        kind="endpoint",
    )
    if endpoint is None:
        endpoint = _ensure_created(
            api_key=api_key,
            state=state,
            state_file=state_file,
            kind="endpoint",
            list_path="/endpoints",
            create_path="/endpoints",
            expected=endpoint_payload,
            verify=lambda actual, expected: _verify_endpoint(
                actual,
                expected,
                template_id=template_id,
                volume_id=volume_id,
            ),
        )
    elif _template_id(endpoint) != template_id:
        endpoint_id = _required_id(endpoint, "endpoint")
        endpoint = _mutate(
            api_key,
            f"/endpoints/{urllib.parse.quote(endpoint_id, safe='')}",
            method="PATCH",
            payload=endpoint_patch_payload,
        )
    else:
        try:
            _verify_endpoint(
                endpoint,
                endpoint_payload,
                template_id=template_id,
                volume_id=volume_id,
            )
        except RuntimeError:
            endpoint_id = _required_id(endpoint, "endpoint")
            endpoint = _mutate(
                api_key,
                f"/endpoints/{urllib.parse.quote(endpoint_id, safe='')}",
                method="PATCH",
                payload=endpoint_patch_payload,
            )
    _verify_endpoint(
        endpoint,
        endpoint_payload,
        template_id=template_id,
        volume_id=volume_id,
    )
    endpoint_id = _required_id(endpoint, "endpoint")
    endpoint_state = _resource_state(state, "endpoint")
    endpoint_state.update({"id": endpoint_id, "verified_at": _now()})
    state.update(
        {
            "status": "ready",
            "updated_at": _now(),
            "endpoint_id": endpoint_id,
            "template_id": template_id,
            "network_volume_id": volume_id,
        }
    )
    _write_state(state_file, state)
    return state


def status(spec: dict[str, dict[str, Any]]) -> dict[str, Any]:
    api_key = _api_key()
    volume = _find_unique(
        api_key,
        path="/networkvolumes",
        name=str(spec["volume"]["name"]),
        kind="volume",
    )
    template = _find_unique(
        api_key,
        path="/templates?includeEndpointBoundTemplates=true",
        name=str(spec["template"]["name"]),
        kind="template",
    )
    endpoint = _find_unique(
        api_key,
        path="/endpoints",
        name=ENDPOINT_NAME,
        kind="endpoint",
    )
    if volume is None or template is None or endpoint is None:
        return {"ready": False, "reason": "resource_missing"}
    volume_id = _required_id(volume, "volume")
    template_id = _required_id(template, "template")
    _verify_volume(volume, spec["volume"])
    _verify_template(template, spec["template"])
    _verify_endpoint(
        endpoint,
        spec["endpoint"],
        template_id=template_id,
        volume_id=volume_id,
    )
    return {
        "ready": True,
        "endpoint_id": _required_id(endpoint, "endpoint"),
        "template_id": template_id,
        "network_volume_id": volume_id,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "apply", "status"))
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--manifest-source-sha256", required=True)
    parser.add_argument("--model-objects-file", type=Path, required=True)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--workers-min", type=int, choices=(0, 1), default=0)
    parser.add_argument("--data-center-id", default=DATA_CENTER_ID)
    parser.add_argument("--acknowledge-spend", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    model_objects_json, model_objects_sha256 = _read_model_objects(args.model_objects_file)
    spec = _spec(
        image=args.image,
        source_revision=args.source_revision,
        manifest_source_sha256=args.manifest_source_sha256,
        model_objects_json=model_objects_json,
        model_objects_sha256=model_objects_sha256,
        workers_min=args.workers_min,
        data_center_id=args.data_center_id,
    )
    if args.action == "plan":
        result: dict[str, Any] = {
            "mutates_runpod": False,
            "spec": spec,
            "model_objects_sha256": model_objects_sha256,
        }
    elif args.action == "apply":
        if not args.acknowledge_spend or args.state_file is None:
            raise RuntimeError("apply requires --acknowledge-spend and --state-file")
        result = apply(spec, state_file=args.state_file.resolve())
    else:
        result = status(spec)
    json.dump(result, sys.stdout, ensure_ascii=True, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
