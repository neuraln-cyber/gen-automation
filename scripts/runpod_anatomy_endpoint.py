#!/usr/bin/env python3
"""Safely plan, provision, recover, or remove the RunPod anatomy endpoint."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

RUNPOD_API_ROOT = "https://rest.runpod.io/v1"
MODEL = "Qwen/Qwen3-VL-8B-Instruct"
MODEL_REVISION = "60595ebc30ec8e3b1d3b9e65d4943ca011c0006a"
WORKER_IMAGE = (
    "runpod/worker-v1-vllm@sha256:cda7e80957d736e82b8c040eee69e0b6a7fd9d0fa6c7c74ef247d79b89bf9cab"
)
TEMPLATE_NAME = "gen-automation-anatomy-qwen3-vl-8b-v1"
ENDPOINT_NAME = "gen-automation-anatomy-staging"
SPEND_SWITCH = "GEN_AUTOMATION_RUNPOD_SPEND_ALLOWED"
STATE_SCHEMA = "gen-automation/runpod-anatomy-state/v2"


class RunPodHTTPError(RuntimeError):
    def __init__(self, method: str, path: str, status_code: int, detail: str) -> None:
        super().__init__(
            f"RunPod API rejected {method} {path}: HTTP {status_code}: {detail}"
        )
        self.status_code = status_code


def template_payload() -> dict[str, Any]:
    return {
        "imageName": WORKER_IMAGE,
        "name": TEMPLATE_NAME,
        "category": "NVIDIA",
        "containerDiskInGb": 60,
        "dockerEntrypoint": [],
        "dockerStartCmd": [],
        "env": {
            "MODEL_NAME": MODEL,
            "REVISION": MODEL_REVISION,
            "OPENAI_SERVED_MODEL_NAME_OVERRIDE": MODEL,
            "MAX_MODEL_LEN": "4096",
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


def _desired_plan() -> dict[str, Any]:
    return {
        "template": template_payload(),
        "endpoint": endpoint_payload("<template-id>"),
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
        "recovery": {
            "journal_schema": STATE_SCHEMA,
            "resume_command": "apply",
            "read_only_reconciliation_command": "recover",
            "cleanup_command": "destroy --acknowledge-destroy",
            "explicit_duplicate_risk_override": "apply --retry-create",
        },
        "template": template_payload(),
        "endpoint": endpoint_payload("<created-template-id>"),
        "gateway_upstream_url": (
            "https://api.runpod.ai/v2/<created-endpoint-id>/openai/v1/chat/completions"
        ),
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _request(
    api_key: str,
    path: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
    allow_not_found: bool = False,
) -> Any:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(  # noqa: S310 - fixed official API root
        f"{RUNPOD_API_ROOT}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            body = response.read(1024 * 1024)
    except urllib.error.HTTPError as error:
        if allow_not_found and error.code == 404:
            return None
        detail = error.read(32 * 1024).decode(errors="replace")
        raise RunPodHTTPError(method, path, error.code, detail) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"RunPod API request failed for {method} {path}: {error}") from error
    if not body:
        return None
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"RunPod API returned invalid JSON for {method} {path}") from error


def _post(api_key: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = _request(api_key, path, method="POST", payload=payload)
    if not isinstance(result, dict):
        raise RuntimeError(f"RunPod API returned a non-object for POST {path}")
    return result


def _get(
    api_key: str, path: str, *, allow_not_found: bool = False
) -> dict[str, Any] | None:
    result = _request(api_key, path, method="GET", allow_not_found=allow_not_found)
    if result is None and allow_not_found:
        return None
    if not isinstance(result, dict):
        raise RuntimeError(f"RunPod API returned a non-object for GET {path}")
    return result


def _list(api_key: str, path: str) -> list[dict[str, Any]]:
    result = _request(api_key, path, method="GET")
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise RuntimeError(f"RunPod API returned a non-list for GET {path}")
    return result


def _delete(api_key: str, path: str) -> None:
    _request(api_key, path, method="DELETE", allow_not_found=True)


def _id_path(kind: str, resource_id: str) -> str:
    plural = {"template": "templates", "endpoint": "endpoints"}[kind]
    return f"/{plural}/{urllib.parse.quote(resource_id, safe='')}"


def _required_id(value: dict[str, Any], kind: str) -> str:
    resource_id = value.get("id")
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise RuntimeError(f"RunPod did not return an ID for the {kind}")
    return resource_id


def _strings(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return None


def _endpoint_template_id(actual: dict[str, Any]) -> str | None:
    value = actual.get("templateId")
    if isinstance(value, str) and value.strip():
        return value
    nested = actual.get("template")
    if isinstance(nested, dict):
        nested_id = nested.get("id")
        if isinstance(nested_id, str):
            return nested_id
    return None


def _mismatches(kind: str, actual: dict[str, Any], template_id: str | None) -> list[str]:
    if kind == "template":
        expected = template_payload()
        fields = (
            "imageName",
            "name",
            "category",
            "containerDiskInGb",
            "dockerEntrypoint",
            "dockerStartCmd",
            "env",
            "isPublic",
            "isServerless",
            "ports",
            "readme",
            "volumeInGb",
            "volumeMountPath",
        )
        return [field for field in fields if actual.get(field) != expected[field]]
    if not template_id:
        raise RuntimeError("template ID is required to verify an endpoint")
    expected = endpoint_payload(template_id)
    required_fields = (
        "name",
        "computeType",
        "gpuCount",
        "workersMin",
        "workersMax",
        "idleTimeout",
        "executionTimeoutMs",
        "scalerType",
        "scalerValue",
    )
    mismatches = [
        field for field in required_fields if actual.get(field) != expected[field]
    ]
    if "minCudaVersion" in actual and actual["minCudaVersion"] != expected["minCudaVersion"]:
        mismatches.append("minCudaVersion")
    if _endpoint_template_id(actual) != template_id:
        mismatches.append("templateId")
    if _strings(actual.get("gpuTypeIds")) != expected["gpuTypeIds"]:
        mismatches.append("gpuTypeIds")
    for field in ("allowedCudaVersions", "dataCenterIds"):
        if set(_strings(actual.get(field)) or []) != set(expected[field]):
            mismatches.append(field)
    # RunPod's v1 endpoint read response currently omits flashboot.
    if "flashboot" in actual and actual["flashboot"] != expected["flashboot"]:
        mismatches.append("flashboot")
    return mismatches


def _verify(kind: str, actual: dict[str, Any], template_id: str | None = None) -> None:
    mismatches = _mismatches(kind, actual, template_id)
    if mismatches:
        raise RuntimeError(
            f"RunPod {kind} does not match the pinned plan: {', '.join(mismatches)}"
        )


def _verify_identity(
    kind: str,
    actual: dict[str, Any],
    template_id: str | None = None,
    desired: dict[str, Any] | None = None,
) -> None:
    """Verify stable ownership fields before deletion, allowing configuration drift."""

    if kind == "template":
        expected = desired or template_payload()
        expected_env = expected.get("env")
        env = actual.get("env")
        if (
            actual.get("name") != expected.get("name")
            or actual.get("imageName") != expected.get("imageName")
            or not isinstance(env, dict)
            or not isinstance(expected_env, dict)
            or env.get("REVISION") != expected_env.get("REVISION")
        ):
            raise RuntimeError("RunPod template identity does not match this journal")
        return
    # The endpoint ID is durably journaled and queried directly. Its bound
    # template is the stable ownership relationship; display-name drift is not.
    if _endpoint_template_id(actual) != template_id:
        raise RuntimeError("RunPod endpoint identity does not match this journal")


def _read(
    api_key: str, kind: str, resource_id: str
) -> dict[str, Any] | None:
    path = _id_path(kind, resource_id)
    if kind == "template":
        path += "?includeEndpointBoundTemplates=true"
    return _get(api_key, path, allow_not_found=True)


def _reconcile(
    api_key: str, kind: str, template_id: str | None = None
) -> dict[str, Any] | None:
    if kind == "template":
        items = _list(api_key, "/templates?includeEndpointBoundTemplates=true")
        candidates = [item for item in items if item.get("name") == TEMPLATE_NAME]
    else:
        if not template_id:
            raise RuntimeError("template ID is required to reconcile an endpoint")
        items = _list(api_key, "/endpoints")
        candidates = [
            item
            for item in items
            if item.get("name") == ENDPOINT_NAME
            and _endpoint_template_id(item) == template_id
        ]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise RuntimeError(f"cannot safely reconcile {kind}: found {len(candidates)} matches")
    _verify(kind, candidates[0], template_id)
    _required_id(candidates[0], kind)
    return candidates[0]


def _new_journal() -> dict[str, Any]:
    resource = {"id": None, "phase": "planned", "origin": None, "create_attempted": False}
    return {
        "schema": STATE_SCHEMA,
        "journal_revision": 0,
        "status": "applying",
        "created_at": _now(),
        "updated_at": _now(),
        "desired": _desired_plan(),
        "resources": {"template": dict(resource), "endpoint": dict(resource)},
        "last_error": None,
    }


def _lock_handle(handle: BinaryIO, *, unlock: bool) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        mode = msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK
        msvcrt.locking(handle.fileno(), mode, 1)
    else:
        import fcntl

        operation = (
            fcntl.LOCK_UN  # type: ignore[attr-defined]
            if unlock
            else fcntl.LOCK_EX | fcntl.LOCK_NB  # type: ignore[attr-defined]
        )
        fcntl.flock(handle.fileno(), operation)  # type: ignore[attr-defined]


@contextmanager
def _state_lock(state_file: Path) -> Iterator[None]:
    """Hold a process-released, non-blocking lock for the whole command."""

    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = state_file.with_name(f".{state_file.name}.lock")
    with lock_file.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        try:
            _lock_handle(handle, unlock=False)
        except OSError as error:
            raise RuntimeError(
                f"another RunPod state operation is already using {state_file}"
            ) from error
        try:
            yield
        finally:
            _lock_handle(handle, unlock=True)


def _write_journal(path: Path, journal: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    journal["journal_revision"] = int(journal.get("journal_revision", 0)) + 1
    journal["updated_at"] = _now()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(journal, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_journal(path: Path, *, require_current_plan: bool = True) -> dict[str, Any]:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise RuntimeError(f"RunPod state file is unexpectedly large: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"could not read RunPod state file {path}: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise RuntimeError(f"unsupported or invalid RunPod state file schema in {path}")
    if require_current_plan and value.get("desired") != _desired_plan():
        raise RuntimeError("journal plan differs; recover it with the matching script revision")
    if not isinstance(value.get("desired"), dict):
        raise RuntimeError(f"invalid desired plan in {path}")
    resources = value.get("resources")
    if not isinstance(resources, dict) or not all(
        isinstance(resources.get(name), dict) for name in ("template", "endpoint")
    ):
        raise RuntimeError(f"invalid resource journal in {path}")
    return value


def _resource(journal: dict[str, Any], kind: str) -> dict[str, Any]:
    value = journal["resources"][kind]
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {kind} journal")
    return value


def _checkpoint_resource(
    journal: dict[str, Any],
    state_file: Path,
    kind: str,
    resource_id: str,
    origin: str,
) -> None:
    _resource(journal, kind).update(
        {"id": resource_id, "phase": "created", "origin": origin}
    )
    _write_journal(state_file, journal)


def _record_error(
    journal: dict[str, Any], state_file: Path, error: Exception, status: str
) -> None:
    journal["status"] = status
    journal["last_error"] = {
        "at": _now(),
        "type": type(error).__name__,
        "message": str(error)[:4000],
    }
    _write_journal(state_file, journal)


def _ensure(
    api_key: str,
    journal: dict[str, Any],
    state_file: Path,
    kind: str,
    template_id: str | None = None,
    *,
    retry_create: bool = False,
) -> str:
    resource = _resource(journal, kind)
    resource_id = resource.get("id")
    if not isinstance(resource_id, str) or not resource_id.strip():
        previous_phase = resource.get("phase")
        had_prior_ambiguous_attempt = bool(resource.get("create_attempted"))
        resource["phase"] = "reconciling"
        _write_journal(state_file, journal)
        actual = _reconcile(api_key, kind, template_id)
        if actual is not None and not resource.get("create_attempted"):
            raise RuntimeError(
                f"matching {kind} predates this journal; refusing to adopt an unowned resource"
            )
        if actual is None:
            if resource.get("create_attempted") and not retry_create:
                resource["phase"] = "create_outcome_unknown"
                _write_journal(state_file, journal)
                raise RuntimeError(
                    f"{kind} create outcome is unresolved; no duplicate POST was sent. "
                    "Wait and rerun recover, or explicitly apply with --retry-create."
                )
            if previous_phase == "create_rejected" and not retry_create:
                resource["phase"] = "create_rejected"
                _write_journal(state_file, journal)
                raise RuntimeError(
                    f"{kind} create was rejected; explicitly apply with --retry-create"
                )
            resource.update(
                {
                    "phase": "creating",
                    "create_attempted": True,
                    "create_attempts": int(resource.get("create_attempts", 0)) + 1,
                }
            )
            _write_journal(state_file, journal)  # durable before POST
            payload = (
                template_payload()
                if kind == "template"
                else endpoint_payload(template_id or "")
            )
            path = "/templates" if kind == "template" else "/endpoints"
            try:
                actual = _post(api_key, path, payload)
            except Exception as create_error:
                # A timeout/5xx can occur after RunPod accepted POST.
                try:
                    actual = _reconcile(api_key, kind, template_id)
                except Exception as reconcile_error:
                    raise RuntimeError(
                        f"{kind} create and reconciliation failed: "
                        f"create={create_error}; reconcile={reconcile_error}"
                    ) from create_error
                if (
                    isinstance(create_error, RunPodHTTPError)
                    and 400 <= create_error.status_code < 500
                ):
                    if had_prior_ambiguous_attempt:
                        if actual is None:
                            resource.update(
                                {
                                    "phase": "create_outcome_unknown",
                                    "create_attempted": True,
                                }
                            )
                            _write_journal(state_file, journal)
                            raise create_error
                        origin = "reconciled-after-retry-rejection"
                    else:
                        resource.update(
                            {"phase": "create_rejected", "create_attempted": False}
                        )
                        _write_journal(state_file, journal)
                        raise create_error
                if actual is None:
                    resource["phase"] = "create_outcome_unknown"
                    _write_journal(state_file, journal)
                    raise
                if not (
                    isinstance(create_error, RunPodHTTPError)
                    and 400 <= create_error.status_code < 500
                ):
                    origin = "reconciled-after-create-error"
            else:
                origin = "created"
        else:
            origin = "reconciled"
        resource_id = _required_id(actual, kind)
        _checkpoint_resource(journal, state_file, kind, resource_id, origin)

    actual = _read(api_key, kind, resource_id)
    if actual is None:
        raise RuntimeError(f"journaled RunPod {kind} {resource_id!r} is missing")
    _verify(kind, actual, template_id)
    resource.update({"phase": "verified", "readback_verified_at": _now()})
    if kind == "endpoint":
        resource["readback_unavailable_fields"] = [] if "flashboot" in actual else ["flashboot"]
    _write_journal(state_file, journal)
    return resource_id


def _discover(
    api_key: str,
    journal: dict[str, Any],
    state_file: Path,
    kind: str,
    template_id: str | None = None,
) -> str | None:
    resource = _resource(journal, kind)
    resource_id = resource.get("id")
    if isinstance(resource_id, str) and resource_id.strip():
        return resource_id
    if not resource.get("create_attempted"):
        return None
    actual = _reconcile(api_key, kind, template_id)
    if actual is None:
        return None
    resource_id = _required_id(actual, kind)
    _checkpoint_resource(journal, state_file, kind, resource_id, "reconciled")
    return resource_id


def _mark_ready(
    journal: dict[str, Any], state_file: Path, template_id: str, endpoint_id: str
) -> dict[str, Any]:
    journal.update(
        {
            "status": "ready",
            "last_error": None,
            "ready_at": _now(),
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
    )
    _write_journal(state_file, journal)
    return journal


def _api_key() -> str:
    value = os.environ.get("RUNPOD_API_KEY", "")
    if not value.strip():
        raise RuntimeError("RUNPOD_API_KEY is required")
    return value


def apply_plan(
    *, state_file: Path, acknowledge_spend: bool, retry_create: bool = False
) -> dict[str, Any]:
    if not acknowledge_spend or os.environ.get(SPEND_SWITCH, "").lower() != "true":
        raise RuntimeError(
            "paid provisioning is locked; explicit spend release and "
            "--acknowledge-spend are required"
        )
    api_key = _api_key()
    with _state_lock(state_file):
        return _apply_plan_locked(
            api_key=api_key, state_file=state_file, retry_create=retry_create
        )


def _apply_plan_locked(
    *, api_key: str, state_file: Path, retry_create: bool
) -> dict[str, Any]:
    if state_file.exists():
        journal = _load_journal(state_file)
        status = str(journal.get("status", ""))
        if status == "destroyed":
            raise RuntimeError("this journal is destroyed; provision with a new state file")
        if "destroy" in status:
            raise RuntimeError("cleanup is incomplete; rerun destroy")
        journal.update({"status": "applying", "last_error": None})
    else:
        journal = _new_journal()
    _write_journal(state_file, journal)  # first durable checkpoint before any API call
    try:
        template_id = _ensure(
            api_key, journal, state_file, "template", retry_create=retry_create
        )
        endpoint_id = _ensure(
            api_key,
            journal,
            state_file,
            "endpoint",
            template_id,
            retry_create=retry_create,
        )
        return _mark_ready(journal, state_file, template_id, endpoint_id)
    except Exception as error:
        _record_error(journal, state_file, error, "recovery_required")
        raise RuntimeError(
            f"provisioning incomplete; journal preserved at {state_file}. Rerun apply, "
            f"recover, or destroy --acknowledge-destroy. Cause: {error}"
        ) from error


def recover_plan(*, state_file: Path) -> dict[str, Any]:
    """Reconcile and verify without creating or deleting RunPod resources."""

    with _state_lock(state_file):
        return _recover_plan_locked(state_file)


def _recover_plan_locked(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        raise RuntimeError(f"RunPod state file does not exist: {state_file}")
    journal = _load_journal(state_file)
    if journal.get("status") == "destroyed":
        return journal
    if "destroy" in str(journal.get("status", "")):
        raise RuntimeError("cleanup is incomplete; rerun destroy")
    api_key = _api_key()
    try:
        template_id = _discover(api_key, journal, state_file, "template")
        if template_id:
            actual = _read(api_key, "template", template_id)
            if actual is None:
                raise RuntimeError(f"journaled RunPod template {template_id!r} is missing")
            _verify("template", actual)
            _resource(journal, "template")["phase"] = "verified"
            _write_journal(state_file, journal)
        endpoint_id = _discover(api_key, journal, state_file, "endpoint", template_id)
        if endpoint_id:
            actual = _read(api_key, "endpoint", endpoint_id)
            if actual is None:
                raise RuntimeError(f"journaled RunPod endpoint {endpoint_id!r} is missing")
            _verify("endpoint", actual, template_id)
            _resource(journal, "endpoint")["phase"] = "verified"
            _write_journal(state_file, journal)
        if template_id and endpoint_id:
            return _mark_ready(journal, state_file, template_id, endpoint_id)
        error = RuntimeError("one or more attempted resources are not visible")
        _record_error(journal, state_file, error, "recovery_required")
        return journal
    except Exception as error:
        _record_error(journal, state_file, error, "recovery_required")
        raise RuntimeError(f"reconciliation failed; journal preserved: {error}") from error


def _destroy_one(
    api_key: str,
    journal: dict[str, Any],
    state_file: Path,
    kind: str,
    template_id: str | None = None,
) -> None:
    resource = _resource(journal, kind)
    resource_id = _discover(api_key, journal, state_file, kind, template_id)
    if not resource_id:
        if resource.get("create_attempted"):
            raise RuntimeError(
                f"{kind} create outcome is unresolved; retry destroy after RunPod list propagation"
            )
        resource["phase"] = "deleted"
        _write_journal(state_file, journal)
        return
    actual = _read(api_key, kind, resource_id)
    if actual is None:
        resource.update({"phase": "deleted", "deleted_at": _now()})
        _write_journal(state_file, journal)
        return
    desired = journal["desired"].get(kind)
    if not isinstance(desired, dict):
        raise RuntimeError(f"journal has no desired {kind} plan")
    _verify_identity(
        kind, actual, template_id, desired
    )  # never delete an unrelated target
    resource["phase"] = "delete_pending"
    _write_journal(state_file, journal)  # durable before DELETE
    try:
        _delete(api_key, _id_path(kind, resource_id))
    except Exception:
        if _read(api_key, kind, resource_id) is not None:
            raise
    if _read(api_key, kind, resource_id) is not None:
        raise RuntimeError(f"RunPod {kind} {resource_id!r} still exists after deletion")
    resource.update({"phase": "deleted", "deleted_at": _now()})
    _write_journal(state_file, journal)


def destroy_plan(*, state_file: Path, acknowledge_destroy: bool) -> dict[str, Any]:
    with _state_lock(state_file):
        return _destroy_plan_locked(
            state_file=state_file, acknowledge_destroy=acknowledge_destroy
        )


def _destroy_plan_locked(
    *, state_file: Path, acknowledge_destroy: bool
) -> dict[str, Any]:
    if not state_file.exists():
        raise RuntimeError(f"RunPod state file does not exist: {state_file}")
    journal = _load_journal(state_file, require_current_plan=False)
    if journal.get("status") == "destroyed":
        return journal
    if not acknowledge_destroy:
        raise RuntimeError("destroy requires --acknowledge-destroy")
    api_key = _api_key()
    journal.update({"status": "destroying", "last_error": None})
    _write_journal(state_file, journal)
    try:
        template_id = _discover(api_key, journal, state_file, "template")
        _destroy_one(api_key, journal, state_file, "endpoint", template_id)
        _destroy_one(api_key, journal, state_file, "template")
        journal.update({"status": "destroyed", "last_error": None, "destroyed_at": _now()})
        _write_journal(state_file, journal)
        return journal
    except Exception as error:
        _record_error(journal, state_file, error, "destroy_recovery_required")
        raise RuntimeError(
            f"cleanup incomplete; journal preserved at {state_file}. Rerun destroy. Cause: {error}"
        ) from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", nargs="?", choices=("plan", "apply", "recover", "destroy"), default="plan"
    )
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--acknowledge-spend", action="store_true")
    parser.add_argument("--acknowledge-destroy", action="store_true")
    parser.add_argument("--retry-create", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plan":
        print(json.dumps(public_plan(), indent=2, sort_keys=True))
        return 0
    if args.state_file is None:
        print(f"{args.command} requires --state-file", file=sys.stderr)
        return 2
    try:
        if args.command == "apply":
            state = apply_plan(
                state_file=args.state_file,
                acknowledge_spend=args.acknowledge_spend,
                retry_create=args.retry_create,
            )
        elif args.command == "recover":
            state = recover_plan(state_file=args.state_file)
        else:
            state = destroy_plan(
                state_file=args.state_file, acknowledge_destroy=args.acknowledge_destroy
            )
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
