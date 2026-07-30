from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from gen_automation.app import create_app
from gen_automation.config import Environment, Settings
from gen_automation.controller.runtime import (
    ControllerRuntimeSnapshot,
    RuntimeStatus,
)
from gen_automation.domain.runtime_bindings import (
    WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING,
    WORKER_ARTIFACT_SESSION_TOKEN_BINDING,
)
from gen_automation.domain.signing import encode_base64url
from gen_automation.services.runtime_secrets import (
    AwsAssumeRoleRuntimeSecretResolver,
    ConfiguredRuntimeSecretResolver,
    RuntimeSecretResolutionError,
    build_runtime_secret_resolver,
    configured_runtime_binding_references,
)

SESSION_SECRET = encode_base64url(bytes(range(32)))
TOTP_ENCRYPTION_KEY = encode_base64url(bytes(range(32, 64)))
WORKER_SIGNING_PRIVATE_KEY = encode_base64url(bytes(range(1, 33)))
RUNTIME_ACCESS_KEY = "runtime-test-access-key"
RUNTIME_SECRET_KEY = "runtime-test-secret-key"  # noqa: S105
RUNTIME_SESSION_TOKEN = "runtime-test-session-token"  # noqa: S105
RUNTIME_MANIFEST = '{"artifacts":[],"manifest_sha256":"' + ("0" * 64) + '"}'
RUNTIME_ROLE_ARN = "arn:aws:iam::123456789012:role/gen-automation-staging-salad-artifact-reader"


def _protected_gpu_values() -> dict[str, object]:
    return {
        "environment": Environment.STAGING,
        "database_url": "postgresql+psycopg://user:pass@db/example",
        "public_base_url": "https://studio.example.test",
        "session_secret": SESSION_SECRET,
        "auth_enabled": True,
        "auth_totp_active_key_id": "totp-key-1",
        "auth_totp_encryption_keys": {
            "totp-key-1": TOTP_ENCRYPTION_KEY,
        },
        "trusted_proxy_cidrs": ("10.20.30.0/24",),
        "ingress_rate_limit_configured": True,
        "ingress_request_guards_configured": True,
        "background_runtime_enabled": True,
        "gpu_allocation_enabled": True,
        "storage_enabled": True,
        "storage_bucket": "private-assets",
        "salad_enabled": True,
        "salad_api_key": "salad-test-key",
        "salad_organization": "organization",
        "salad_project": "project",
        "salad_queue_name": "generation-queue",
        "salad_container_group_name": "generation-workers",
        "salad_gpu_class_ids": ("3c90c3cc-0d44-4b50-8888-8dd25736052a",),
        "salad_webhook_secret": "whsec_test",
        "salad_worker_image": ("registry.example.test/worker@sha256:" + ("a" * 64)),
        "worker_signing_key_id": "worker-key-1",
        "worker_signing_private_key": WORKER_SIGNING_PRIVATE_KEY,
        "salad_worker_allowed_upload_origin": "https://uploads.example.test",
        "salad_worker_model_manifest_json": RUNTIME_MANIFEST,
        "salad_worker_model_manifest_sha256": "0" * 64,
        "salad_worker_artifact_bucket": "model-artifacts",
        "salad_worker_artifact_region": "us-east-1",
        "salad_worker_artifact_role_arn": RUNTIME_ROLE_ARN,
    }


@pytest.mark.parametrize(
    "environment",
    [Environment.STAGING, Environment.PRODUCTION],
)
def test_protected_gpu_configuration_requires_complete_runtime_values(
    environment: Environment,
) -> None:
    values = _protected_gpu_values()
    values["environment"] = environment
    values["salad_worker_artifact_role_arn"] = None

    with pytest.raises(
        ValidationError,
        match="requires a Salad worker artifact reader role ARN",
    ):
        Settings(**values)  # type: ignore[arg-type]


def test_local_and_test_configuration_remain_worker_credential_free() -> None:
    local = Settings()
    test = Settings(environment=Environment.TEST)

    assert local.salad_worker_artifact_access_key_id is None
    assert test.salad_worker_artifact_secret_access_key is None
    assert build_runtime_secret_resolver(local) is None
    assert build_runtime_secret_resolver(test) is None


def test_deployment_environment_values_are_loaded_as_secret_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_ACCESS_KEY_ID",
        RUNTIME_ACCESS_KEY,
    )
    monkeypatch.setenv(
        "GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_SECRET_ACCESS_KEY",
        RUNTIME_SECRET_KEY,
    )

    settings = Settings(_env_file=None)

    assert isinstance(settings.salad_worker_artifact_access_key_id, SecretStr)
    assert settings.salad_worker_artifact_access_key_id.get_secret_value() == RUNTIME_ACCESS_KEY
    assert RUNTIME_ACCESS_KEY not in repr(settings)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "salad_worker_allowed_upload_origin",
            "https://uploads.example.test/path",
            "exact HTTPS origin",
        ),
        (
            "salad_worker_model_manifest_sha256",
            "A" * 64,
            "lowercase SHA-256",
        ),
        (
            "salad_worker_artifact_bucket",
            "invalid bucket name",
            "artifact bucket is invalid",
        ),
        (
            "salad_worker_artifact_region",
            "r" * 129,
            "artifact region is invalid",
        ),
        (
            "salad_worker_artifact_endpoint_url",
            "http://artifacts.example.test",
            "endpoint must use HTTPS",
        ),
    ],
)
def test_runtime_value_validation_fails_closed(
    field: str,
    value: str,
    message: str,
) -> None:
    values = _protected_gpu_values()
    values[field] = value

    with pytest.raises(ValidationError, match=message):
        Settings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    [
        "salad_worker_artifact_access_key_id",
        "salad_worker_artifact_secret_access_key",
        "salad_worker_artifact_session_token",
        "salad_worker_artifact_endpoint_url",
    ],
)
def test_artifact_reader_role_rejects_static_credentials_and_custom_endpoints(
    field: str,
) -> None:
    values = _protected_gpu_values()
    values[field] = "https://artifacts.example.test" if field.endswith("url") else "configured"

    with pytest.raises(ValidationError, match="reader role cannot be combined"):
        Settings(**values)  # type: ignore[arg-type]


class FakeStsClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def assume_role(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(dict(kwargs))
        call_no = len(self.calls)
        return {
            "Credentials": {
                "AccessKeyId": f"{RUNTIME_ACCESS_KEY}-{call_no}",
                "SecretAccessKey": f"{RUNTIME_SECRET_KEY}-{call_no}",
                "SessionToken": f"{RUNTIME_SESSION_TOKEN}-{call_no}",
                "Expiration": datetime.now(UTC) + timedelta(hours=1),
            }
        }

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "environment",
    [Environment.STAGING, Environment.PRODUCTION],
)
async def test_configured_resolver_resolves_only_the_complete_allowlisted_set(
    environment: Environment,
) -> None:
    values = _protected_gpu_values()
    values["environment"] = environment
    settings = Settings(**values)  # type: ignore[arg-type]
    sts_client = FakeStsClient()
    resolver = build_runtime_secret_resolver(settings, sts_client=sts_client)
    assert isinstance(resolver, AwsAssumeRoleRuntimeSecretResolver)
    bindings = configured_runtime_binding_references(settings)

    first = await resolver.resolve_many(bindings)
    second = await resolver.resolve_many(bindings)

    assert set(first) == set(bindings)
    assert WORKER_ARTIFACT_SESSION_TOKEN_BINDING in first
    assert first[WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING] == f"{RUNTIME_ACCESS_KEY}-1"
    assert second[WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING] == f"{RUNTIME_ACCESS_KEY}-2"
    assert len(sts_client.calls) == 2
    assert all(call["RoleArn"] == RUNTIME_ROLE_ARN for call in sts_client.calls)
    assert all(call["DurationSeconds"] == 3600 for call in sts_client.calls)
    assert RUNTIME_ACCESS_KEY not in repr(resolver)
    assert RUNTIME_SECRET_KEY not in repr(resolver)


@pytest.mark.asyncio
async def test_resolver_rejects_unknown_mismatched_and_incomplete_bindings() -> None:
    settings = Settings(**_protected_gpu_values())  # type: ignore[arg-type]
    resolver = build_runtime_secret_resolver(settings, sts_client=FakeStsClient())
    assert resolver is not None
    bindings = configured_runtime_binding_references(settings)

    with pytest.raises(RuntimeSecretResolutionError, match="not allowed"):
        await resolver.resolve_many(
            {"GEN_WORKER_UNREVIEWED_SECRET": "deployment-config://unreviewed"}
        )

    mismatched = dict(bindings)
    mismatched[WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING] = "deployment-config://salad-worker/different"
    with pytest.raises(RuntimeSecretResolutionError, match="reference is invalid"):
        await resolver.resolve_many(mismatched)

    incomplete = dict(bindings)
    incomplete.pop(WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING)
    with pytest.raises(RuntimeSecretResolutionError, match="set is incomplete"):
        await resolver.resolve_many(incomplete)


@pytest.mark.asyncio
async def test_resolver_errors_repr_and_config_serialization_redact_values() -> None:
    settings = Settings(**_protected_gpu_values())  # type: ignore[arg-type]
    resolver = build_runtime_secret_resolver(settings, sts_client=FakeStsClient())
    assert resolver is not None

    with pytest.raises(RuntimeSecretResolutionError) as captured:
        await resolver.resolve_many(
            {WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING: ("deployment-config://salad-worker/different")}
        )

    rendered = "\n".join(
        (
            str(captured.value),
            repr(resolver),
            repr(settings),
            settings.model_dump_json(),
        )
    )
    assert RUNTIME_ACCESS_KEY not in rendered
    assert RUNTIME_SECRET_KEY not in rendered
    assert RUNTIME_MANIFEST not in rendered


@pytest.mark.asyncio
async def test_assume_role_response_requires_all_three_fresh_credentials() -> None:
    class IncompleteStsClient(FakeStsClient):
        def assume_role(self, **kwargs: object) -> Mapping[str, object]:
            del kwargs
            return {
                "Credentials": {
                    "AccessKeyId": RUNTIME_ACCESS_KEY,
                    "SecretAccessKey": RUNTIME_SECRET_KEY,
                    "Expiration": datetime.now(UTC) + timedelta(hours=1),
                }
            }

    settings = Settings(**_protected_gpu_values())  # type: ignore[arg-type]
    resolver = build_runtime_secret_resolver(settings, sts_client=IncompleteStsClient())
    assert resolver is not None

    with pytest.raises(RuntimeSecretResolutionError, match="could not be resolved"):
        await resolver.resolve_many(configured_runtime_binding_references(settings))


@pytest.mark.asyncio
async def test_resolver_close_drops_values_and_fails_closed() -> None:
    resolver = ConfiguredRuntimeSecretResolver(
        {WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING: SecretStr(RUNTIME_ACCESS_KEY)},
        require_complete=False,
    )

    await resolver.aclose()

    assert resolver.closed is True
    with pytest.raises(RuntimeSecretResolutionError, match="unavailable"):
        await resolver.resolve_many({})


class _LifecycleResolver:
    def __init__(self, events: list[str]) -> None:
        self.closed = False
        self.events = events

    async def resolve_many(
        self,
        bindings: Mapping[str, str],
    ) -> Mapping[str, str]:
        return dict(bindings)

    async def aclose(self) -> None:
        self.events.append("resolver-close")
        self.closed = True


class _LifecycleRuntime:
    def __init__(self, events: list[str]) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.events = events

    async def start(self) -> None:
        self.events.append("runtime-start")
        self.start_calls += 1

    async def stop(self) -> None:
        self.events.append("runtime-stop")
        self.stop_calls += 1

    def snapshot(self) -> ControllerRuntimeSnapshot:
        return ControllerRuntimeSnapshot(
            instance_id="runtime-secret-lifecycle",
            status=RuntimeStatus.HEALTHY,
            ready=True,
            started_at=None,
            loops=(),
        )


def test_app_passes_resolver_to_controller_and_closes_after_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    resolver = _LifecycleResolver(events)
    runtime = _LifecycleRuntime(events)
    captured_resolver: object | None = None

    monkeypatch.setattr(
        "gen_automation.app.build_runtime_secret_resolver",
        lambda _settings: resolver,
    )

    def fake_build_controller_runtime(**kwargs: object) -> _LifecycleRuntime:
        nonlocal captured_resolver
        captured_resolver = kwargs["secret_resolver"]
        return runtime

    monkeypatch.setattr(
        "gen_automation.app.build_controller_runtime",
        fake_build_controller_runtime,
    )
    settings = Settings(
        environment=Environment.TEST,
        database_url=(f"sqlite+aiosqlite:///{(tmp_path / 'runtime-secrets.db').as_posix()}"),
        auto_create_schema=True,
        background_runtime_enabled=True,
    )

    with TestClient(create_app(settings)) as client:
        assert runtime.start_calls == 1
        assert captured_resolver is resolver
        assert not hasattr(client.app.state, "runtime_secret_resolver")
        assert resolver.closed is False

    assert runtime.stop_calls == 1
    assert resolver.closed is True
    assert events == ["runtime-start", "runtime-stop", "resolver-close"]
