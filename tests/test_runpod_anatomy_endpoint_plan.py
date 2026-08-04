from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "runpod_anatomy_endpoint.py"


def _module():
    spec = importlib.util.spec_from_file_location("runpod_anatomy_endpoint", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plan_is_pinned_and_scales_to_zero() -> None:
    module = _module()
    plan = module.public_plan()

    assert plan["mutates_runpod"] is False
    assert plan["template"]["imageName"].startswith("runpod/worker-v1-vllm@sha256:")
    assert plan["template"]["env"]["REVISION"] == module.MODEL_REVISION
    assert "MODEL_REVISION" not in plan["template"]["env"]
    assert plan["endpoint"]["workersMin"] == 0
    assert plan["endpoint"]["workersMax"] == 1
    assert plan["endpoint"]["gpuTypeIds"] == ["NVIDIA A40", "NVIDIA RTX A6000"]
    assert plan["template"]["containerDiskInGb"] == 60
    assert plan["template"]["env"]["MAX_MODEL_LEN"] == "4096"


def test_apply_is_locked_while_spending_is_not_released(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    monkeypatch.delenv(module.SPEND_SWITCH, raising=False)
    monkeypatch.setenv("RUNPOD_API_KEY", "not-sent-because-guard-fires-first")

    with pytest.raises(RuntimeError, match="paid provisioning is locked"):
        module.apply_plan(state_file=tmp_path / "state.json", acknowledge_spend=True)


def _remote_template(module: Any, resource_id: str = "template-1") -> dict[str, Any]:
    return {**module.template_payload(), "id": resource_id}


def _remote_endpoint(
    module: Any,
    template_id: str = "template-1",
    resource_id: str = "endpoint-1",
) -> dict[str, Any]:
    return {**module.endpoint_payload(template_id), "id": resource_id}


def _release_spending(module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(module.SPEND_SWITCH, "true")
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-never-sent")


def test_apply_journals_before_api_calls_and_verifies_readback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    _release_spending(module, monkeypatch)
    state_file = tmp_path / "runpod-state.json"
    template = _remote_template(module)
    endpoint = _remote_endpoint(module)
    calls: list[tuple[str, str]] = []

    def fake_list(_api_key: str, path: str) -> list[dict[str, Any]]:
        # The durable journal must already identify which mutation is about to
        # happen before even the pre-create reconciliation request is made.
        persisted = json.loads(state_file.read_text(encoding="utf-8"))
        resource_name = "template" if path.startswith("/templates") else "endpoint"
        assert persisted["resources"][resource_name]["phase"] == "reconciling"
        calls.append(("list", path))
        return []

    def fake_post(_api_key: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
        persisted = json.loads(state_file.read_text(encoding="utf-8"))
        resource_name = "template" if path == "/templates" else "endpoint"
        assert persisted["resources"][resource_name]["phase"] == "creating"
        assert persisted["resources"][resource_name]["create_attempted"] is True
        calls.append(("post", path))
        return template if path == "/templates" else endpoint

    def fake_get(
        _api_key: str, path: str, *, allow_not_found: bool = False
    ) -> dict[str, Any] | None:
        del allow_not_found
        calls.append(("get", path))
        return template if path.startswith("/templates/") else endpoint

    monkeypatch.setattr(module, "_list", fake_list)
    monkeypatch.setattr(module, "_post", fake_post)
    monkeypatch.setattr(module, "_get", fake_get)

    state = module.apply_plan(state_file=state_file, acknowledge_spend=True)

    assert state["status"] == "ready"
    assert state["template_id"] == "template-1"
    assert state["endpoint_id"] == "endpoint-1"
    assert state["resources"]["template"]["phase"] == "verified"
    assert state["resources"]["endpoint"]["phase"] == "verified"
    assert "test-key-never-sent" not in state_file.read_text(encoding="utf-8")
    assert calls == [
        ("list", "/templates?includeEndpointBoundTemplates=true"),
        ("post", "/templates"),
        ("get", "/templates/template-1?includeEndpointBoundTemplates=true"),
        ("list", "/endpoints"),
        ("post", "/endpoints"),
        ("get", "/endpoints/endpoint-1"),
    ]


def test_apply_resumes_ambiguous_endpoint_creation_without_duplicate_post(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    _release_spending(module, monkeypatch)
    state_file = tmp_path / "runpod-state.json"
    template = _remote_template(module)
    endpoint = _remote_endpoint(module)
    endpoint_visible = False
    endpoint_posts = 0

    def fake_list(_api_key: str, path: str) -> list[dict[str, Any]]:
        if path.startswith("/templates"):
            return []
        return [endpoint] if endpoint_visible else []

    def fake_post(_api_key: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal endpoint_posts
        if path == "/templates":
            return template
        endpoint_posts += 1
        raise RuntimeError("simulated timeout after server accepted POST")

    def fake_get(
        _api_key: str, path: str, *, allow_not_found: bool = False
    ) -> dict[str, Any] | None:
        del allow_not_found
        return template if path.startswith("/templates/") else endpoint

    monkeypatch.setattr(module, "_list", fake_list)
    monkeypatch.setattr(module, "_post", fake_post)
    monkeypatch.setattr(module, "_get", fake_get)

    with pytest.raises(RuntimeError, match="journal preserved"):
        module.apply_plan(state_file=state_file, acknowledge_spend=True)

    interrupted = json.loads(state_file.read_text(encoding="utf-8"))
    assert interrupted["status"] == "recovery_required"
    assert interrupted["resources"]["template"]["phase"] == "verified"
    assert interrupted["resources"]["endpoint"]["phase"] == "create_outcome_unknown"
    assert interrupted["resources"]["endpoint"]["id"] is None

    # A second apply while RunPod still has not exposed the result must not
    # issue a duplicate POST (endpoint names are not unique).
    with pytest.raises(RuntimeError, match="no duplicate POST was sent"):
        module.apply_plan(state_file=state_file, acknowledge_spend=True)
    assert endpoint_posts == 1

    endpoint_visible = True
    state = module.apply_plan(state_file=state_file, acknowledge_spend=True)

    assert state["status"] == "ready"
    assert state["resources"]["endpoint"]["origin"] == "reconciled"
    assert endpoint_posts == 1


def test_apply_preserves_recovery_journal_on_readback_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    _release_spending(module, monkeypatch)
    state_file = tmp_path / "runpod-state.json"
    created = _remote_template(module)
    mismatched = {**created, "containerDiskInGb": 40}

    monkeypatch.setattr(module, "_list", lambda _key, _path: [])
    monkeypatch.setattr(module, "_post", lambda _key, _path, _payload: created)
    monkeypatch.setattr(
        module,
        "_get",
        lambda _key, _path, *, allow_not_found=False: mismatched,
    )

    with pytest.raises(RuntimeError, match="containerDiskInGb"):
        module.apply_plan(state_file=state_file, acknowledge_spend=True)

    journal = json.loads(state_file.read_text(encoding="utf-8"))
    assert journal["status"] == "recovery_required"
    assert journal["resources"]["template"]["id"] == "template-1"
    assert journal["resources"]["template"]["phase"] == "created"
    assert journal["resources"]["endpoint"]["phase"] == "planned"


def test_fresh_journal_refuses_to_adopt_or_delete_preexisting_resource(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    _release_spending(module, monkeypatch)
    state_file = tmp_path / "runpod-state.json"
    template = _remote_template(module)
    post_calls: list[str] = []
    delete_calls: list[str] = []

    monkeypatch.setattr(
        module,
        "_list",
        lambda _key, path: [template] if path.startswith("/templates") else [],
    )
    monkeypatch.setattr(
        module,
        "_post",
        lambda _key, path, _payload: post_calls.append(path),
    )
    monkeypatch.setattr(module, "_delete", lambda _key, path: delete_calls.append(path))

    with pytest.raises(RuntimeError, match="predates this journal"):
        module.apply_plan(state_file=state_file, acknowledge_spend=True)

    journal = json.loads(state_file.read_text(encoding="utf-8"))
    assert journal["resources"]["template"]["create_attempted"] is False
    assert post_calls == []

    destroyed = module.destroy_plan(state_file=state_file, acknowledge_destroy=True)
    assert destroyed["status"] == "destroyed"
    assert delete_calls == []


def test_definitive_create_rejection_does_not_grant_resource_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    _release_spending(module, monkeypatch)
    state_file = tmp_path / "runpod-state.json"

    monkeypatch.setattr(module, "_list", lambda _key, _path: [])

    def reject_create(_api_key: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
        raise module.RunPodHTTPError("POST", path, 400, "invalid request")

    monkeypatch.setattr(module, "_post", reject_create)

    with pytest.raises(RuntimeError, match="HTTP 400"):
        module.apply_plan(state_file=state_file, acknowledge_spend=True)

    journal = json.loads(state_file.read_text(encoding="utf-8"))
    assert journal["resources"]["template"]["phase"] == "create_rejected"
    assert journal["resources"]["template"]["create_attempted"] is False


def test_retry_rejection_preserves_and_reconciles_prior_ambiguous_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    _release_spending(module, monkeypatch)
    state_file = tmp_path / "runpod-state.json"
    template = _remote_template(module)
    endpoint = _remote_endpoint(module)
    endpoint_visible = False
    endpoint_posts = 0

    def fake_list(_api_key: str, path: str) -> list[dict[str, Any]]:
        if path.startswith("/templates"):
            return []
        return [endpoint] if endpoint_visible else []

    def fake_post(_api_key: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal endpoint_posts, endpoint_visible
        if path == "/templates":
            return template
        endpoint_posts += 1
        if endpoint_posts == 1:
            raise RuntimeError("first endpoint response was lost")
        endpoint_visible = True
        raise module.RunPodHTTPError("POST", path, 409, "endpoint already exists")

    monkeypatch.setattr(module, "_list", fake_list)
    monkeypatch.setattr(module, "_post", fake_post)
    monkeypatch.setattr(
        module,
        "_get",
        lambda _key, path, *, allow_not_found=False: (
            template if path.startswith("/templates/") else endpoint
        ),
    )

    with pytest.raises(RuntimeError, match="journal preserved"):
        module.apply_plan(state_file=state_file, acknowledge_spend=True)

    state = module.apply_plan(
        state_file=state_file,
        acknowledge_spend=True,
        retry_create=True,
    )

    assert state["status"] == "ready"
    assert endpoint_posts == 2
    assert state["resources"]["endpoint"]["create_attempted"] is True
    assert state["resources"]["endpoint"]["origin"] == "reconciled-after-retry-rejection"


def test_documented_endpoint_readback_normalization_is_accepted() -> None:
    module = _module()
    endpoint = _remote_endpoint(module)
    endpoint["dataCenterIds"] = ",".join(endpoint["dataCenterIds"])
    endpoint.pop("flashboot")
    endpoint.pop("minCudaVersion")

    module._verify("endpoint", endpoint, "template-1")


def test_documented_template_readback_normalization_is_accepted() -> None:
    module = _module()
    template = _remote_template(module)
    template.update(
        {
            "dockerEntrypoint": None,
            "dockerStartCmd": None,
            "ports": None,
            "isPublic": None,
            "volumeInGb": None,
            "volumeMountPath": "/workspace",
        }
    )

    module._verify("template", template)


def test_template_readback_normalization_does_not_hide_effective_drift() -> None:
    module = _module()
    template = _remote_template(module)
    template.update(
        {
            "dockerEntrypoint": ["/bin/custom-entrypoint"],
            "ports": ["8080/http"],
            "isPublic": True,
            "volumeInGb": 10,
            "volumeMountPath": "/workspace",
        }
    )

    mismatches = module._mismatches("template", template, None)

    assert "dockerEntrypoint" in mismatches
    assert "ports" in mismatches
    assert "isPublic" in mismatches
    assert "volumeInGb" in mismatches
    assert "volumeMountPath" in mismatches


def test_state_lock_prevents_concurrent_apply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    _release_spending(module, monkeypatch)
    state_file = tmp_path / "runpod-state.json"

    with module._state_lock(state_file):
        with pytest.raises(RuntimeError, match="another RunPod state operation"):
            module.apply_plan(state_file=state_file, acknowledge_spend=True)


def test_destroy_verifies_targets_deletes_endpoint_first_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    _release_spending(module, monkeypatch)
    state_file = tmp_path / "runpod-state.json"
    remote: dict[str, dict[str, Any] | None] = {
        "template": None,
        "endpoint": None,
    }

    def fake_list(_api_key: str, path: str) -> list[dict[str, Any]]:
        resource = remote["template" if path.startswith("/templates") else "endpoint"]
        return [resource] if resource is not None else []

    def fake_post(_api_key: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
        name = "template" if path == "/templates" else "endpoint"
        resource = _remote_template(module) if name == "template" else _remote_endpoint(module)
        remote[name] = resource
        return resource

    def fake_get(
        _api_key: str, path: str, *, allow_not_found: bool = False
    ) -> dict[str, Any] | None:
        del allow_not_found
        return remote["template" if path.startswith("/templates/") else "endpoint"]

    delete_order: list[str] = []

    def fake_delete(_api_key: str, path: str) -> None:
        name = "template" if path.startswith("/templates/") else "endpoint"
        delete_order.append(name)
        remote[name] = None
        if name == "endpoint":
            # A transport failure after successful deletion is reconciled by
            # the subsequent read instead of turning into a duplicate action.
            raise RuntimeError("simulated lost DELETE response")

    monkeypatch.setattr(module, "_list", fake_list)
    monkeypatch.setattr(module, "_post", fake_post)
    monkeypatch.setattr(module, "_get", fake_get)
    monkeypatch.setattr(module, "_delete", fake_delete)

    state = module.apply_plan(state_file=state_file, acknowledge_spend=True)
    # Cleanup uses the journaled identity even if a future script revision
    # changes a tunable desired field.
    state["desired"]["template"]["containerDiskInGb"] = 55
    module._write_journal(state_file, state)
    destroyed = module.destroy_plan(state_file=state_file, acknowledge_destroy=True)

    assert destroyed["status"] == "destroyed"
    assert destroyed["resources"]["endpoint"]["phase"] == "deleted"
    assert destroyed["resources"]["template"]["phase"] == "deleted"
    assert delete_order == ["endpoint", "template"]

    # Rerunning cleanup on its completed journal is a no-op.
    assert module.destroy_plan(state_file=state_file, acknowledge_destroy=True) == destroyed
    assert delete_order == ["endpoint", "template"]


def test_destroy_refuses_to_delete_a_resource_that_no_longer_matches_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    _release_spending(module, monkeypatch)
    state_file = tmp_path / "runpod-state.json"
    journal = module._new_journal()
    journal["status"] = "ready"
    journal["resources"]["template"].update(
        {"id": "template-1", "phase": "verified", "origin": "created"}
    )
    journal["resources"]["endpoint"].update(
        {"id": "endpoint-1", "phase": "verified", "origin": "created"}
    )
    module._write_journal(state_file, journal)
    mismatched_endpoint = {**_remote_endpoint(module), "templateId": "unrelated-template"}
    delete_calls: list[str] = []

    monkeypatch.setattr(
        module,
        "_get",
        lambda _key, path, *, allow_not_found=False: (
            _remote_template(module) if path.startswith("/templates/") else mismatched_endpoint
        ),
    )
    monkeypatch.setattr(module, "_delete", lambda _key, path: delete_calls.append(path))

    with pytest.raises(RuntimeError, match="identity"):
        module.destroy_plan(state_file=state_file, acknowledge_destroy=True)

    assert delete_calls == []
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["status"] == "destroy_recovery_required"
