from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from pydantic import SecretStr

from gen_automation import i2v_lora_rollout_cli as cli
from gen_automation.config import Settings
from gen_automation.services import i2v_lora_rollout as rollout
from gen_automation.services.i2v_lora_rollout import (
    I2VLoraRolloutError,
    I2VLoraRolloutResult,
)

WORKER_DIGEST = "a" * 64
WORKER_IMAGE = "ghcr.io/neuraln-cyber/gen-automation/i2v-worker@sha256:" + WORKER_DIGEST
SOURCE_REVISION = "b" * 40


def _settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "salad_enabled": True,
        "salad_api_key": SecretStr("salad-secret"),
        "salad_organization": "organization",
        "salad_project": "project",
        "salad_api_base_url": "https://provider.invalid/api/",
        "salad_request_timeout_seconds": 30,
        "salad_worker_artifact_region": SecretStr("eu-central-1"),
        "database_url": "postgresql+asyncpg://database.invalid/test",
    }
    values.update(updates)
    return cast(Settings, SimpleNamespace(**values))


def _worker_identity_arguments() -> list[str]:
    return [
        "--expected-worker-image",
        WORKER_IMAGE,
        "--expected-worker-source-revision",
        SOURCE_REVISION,
    ]


def _manifest_arguments(tmp_path: Path) -> list[str]:
    return [
        "--expected-private-manifest-bucket",
        rollout.REVIEWED_MANIFEST_BUCKET,
        "--expected-private-manifest-key",
        rollout.REVIEWED_MANIFEST_KEY,
        "--expected-private-manifest-version",
        rollout.REVIEWED_MANIFEST_VERSION,
        "--expected-private-manifest-source-sha256",
        rollout.REVIEWED_SOURCE_SHA256,
        "--prepared-host-env-output",
        str(tmp_path / "prepared.env"),
    ]


class _FakeDatabase:
    instances: ClassVar[list[_FakeDatabase]] = []

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.sessions = object()
        self.closed = False
        self.instances.append(self)

    async def dispose(self) -> None:
        self.closed = True


class _FakeHttpClient:
    instances: ClassVar[list[_FakeHttpClient]] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.instances.append(self)

    async def aclose(self) -> None:
        self.closed = True


class _FakeResolver:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeManifestClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _install_runtime_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FakeResolver, list[tuple[str, dict[str, object]]]]:
    _FakeDatabase.instances = []
    _FakeHttpClient.instances = []
    resolver = _FakeResolver()
    boto_calls: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(cli, "Database", _FakeDatabase)
    monkeypatch.setattr(cli.httpx2, "AsyncClient", _FakeHttpClient)
    monkeypatch.setattr(cli, "build_runtime_secret_resolver", lambda _settings: resolver)
    monkeypatch.setattr(cli, "SaladClient", lambda **_kwargs: object())

    def build_boto_client(service: str, **kwargs: object) -> _FakeManifestClient:
        boto_calls.append((service, kwargs))
        return _FakeManifestClient()

    monkeypatch.setattr(cli.boto3, "client", build_boto_client)
    return resolver, boto_calls


def _assert_runtime_closed(resolver: _FakeResolver) -> None:
    assert resolver.closed is True
    assert len(_FakeDatabase.instances) == 1
    assert _FakeDatabase.instances[0].closed is True
    assert len(_FakeHttpClient.instances) == 1
    assert _FakeHttpClient.instances[0].closed is True


@pytest.mark.parametrize("operation", ("promote", "rollback"))
@pytest.mark.asyncio
async def test_mutating_operations_require_exact_service_stop_proof_before_clients(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("GEN_AUTOMATION_I2V_ROLLOUT_MAINTENANCE_CONFIRMED", raising=False)

    class UnexpectedDatabase:
        def __init__(self, _database_url: str) -> None:
            raise AssertionError("maintenance must be checked before clients are created")

    monkeypatch.setattr(cli, "Database", UnexpectedDatabase)
    if operation == "rollback":
        arguments = cli._parser().parse_args(
            [
                "rollback",
                "--rollback-state-input",
                str(tmp_path / "rollback.json"),
                "--provider-mutation-marker-output",
                str(tmp_path / "provider-mutation.json"),
            ]
        )
    else:
        arguments = cli._parser().parse_args(
            [
                "promote",
                *_worker_identity_arguments(),
                *_manifest_arguments(tmp_path),
                "--rollback-state-output",
                str(tmp_path / "rollback.json"),
                "--provider-mutation-marker-output",
                str(tmp_path / "provider-mutation.json"),
            ]
        )

    with pytest.raises(I2VLoraRolloutError, match="service-stop proof"):
        await cli._run(arguments, settings=_settings())


@pytest.mark.parametrize("value", ("TRUE", "1", "yes", " true", "true "))
def test_maintenance_proof_is_case_and_whitespace_strict(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEN_AUTOMATION_I2V_ROLLOUT_MAINTENANCE_CONFIRMED", value)

    with pytest.raises(I2VLoraRolloutError, match="service-stop proof"):
        cli._require_phase_one_maintenance(_settings())


def test_maintenance_proof_accepts_only_exact_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEN_AUTOMATION_I2V_ROLLOUT_MAINTENANCE_CONFIRMED", "true")

    cli._require_phase_one_maintenance(_settings())


@pytest.mark.asyncio
async def test_status_is_read_only_and_does_not_construct_an_s3_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEN_AUTOMATION_I2V_ROLLOUT_MAINTENANCE_CONFIRMED", raising=False)
    resolver, boto_calls = _install_runtime_fakes(monkeypatch)
    captured: dict[str, object] = {}
    expected = I2VLoraRolloutResult("status", WORKER_IMAGE, True, 0, 0, 0)

    async def status(**kwargs: object) -> I2VLoraRolloutResult:
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(cli, "rollout_status", status)

    result = await cli._run(cli._parser().parse_args(["status"]), settings=_settings())

    assert result == expected
    assert set(captured) == {"settings", "sessions", "salad_client"}
    assert captured["settings"] is not None
    assert captured["sessions"] is _FakeDatabase.instances[0].sessions
    assert boto_calls == []
    _assert_runtime_closed(resolver)


@pytest.mark.asyncio
async def test_profile_preflight_dispatches_exact_identity_without_s3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEN_AUTOMATION_I2V_ROLLOUT_MAINTENANCE_CONFIRMED", raising=False)
    resolver, boto_calls = _install_runtime_fakes(monkeypatch)
    captured: dict[str, object] = {}
    expected = I2VLoraRolloutResult("profile-preflight", WORKER_IMAGE, True, 0, 0, 0)

    async def preflight(**kwargs: object) -> I2VLoraRolloutResult:
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(cli, "profile_preflight", preflight)
    arguments = cli._parser().parse_args(
        [
            "profile-preflight",
            *_worker_identity_arguments(),
            "--expected-public-profile",
            "false",
        ]
    )

    result = await cli._run(arguments, settings=_settings())

    assert result == expected
    assert captured["expected_worker_image"] == WORKER_IMAGE
    assert captured["expected_worker_source_revision"] == SOURCE_REVISION
    assert captured["expected_public_profile"] is False
    assert captured["resolver"] is resolver
    assert boto_calls == []
    _assert_runtime_closed(resolver)


@pytest.mark.asyncio
async def test_dry_run_dispatches_exact_manifest_and_closes_s3(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("GEN_AUTOMATION_I2V_ROLLOUT_MAINTENANCE_CONFIRMED", raising=False)
    resolver, boto_calls = _install_runtime_fakes(monkeypatch)
    captured: dict[str, object] = {}
    expected = I2VLoraRolloutResult("dry-run", WORKER_IMAGE, True, 0, 0, 0)

    async def dry_run(**kwargs: object) -> I2VLoraRolloutResult:
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(cli, "dry_run_reviewed_worker_rollout", dry_run)
    arguments = cli._parser().parse_args(
        ["dry-run", *_worker_identity_arguments(), *_manifest_arguments(tmp_path)]
    )

    result = await cli._run(arguments, settings=_settings())

    assert result == expected
    coordinates = cast(rollout.ReviewedManifestCoordinates, captured["coordinates"])
    assert coordinates == rollout.ReviewedManifestCoordinates(
        bucket=rollout.REVIEWED_MANIFEST_BUCKET,
        key=rollout.REVIEWED_MANIFEST_KEY,
        version_id=rollout.REVIEWED_MANIFEST_VERSION,
        source_sha256=rollout.REVIEWED_SOURCE_SHA256,
    )
    assert captured["worker_image"] == WORKER_IMAGE
    assert captured["worker_source_revision"] == SOURCE_REVISION
    assert captured["prepared_host_env_output"] == tmp_path / "prepared.env"
    assert callable(captured["artifact_client_factory"])
    assert len(boto_calls) == 1
    assert boto_calls[0][0] == "s3"
    manifest_client = cast(_FakeManifestClient, captured["manifest_client"])
    assert manifest_client.closed is True
    _assert_runtime_closed(resolver)


def test_mutating_parsers_require_a_provider_mutation_marker(tmp_path: Path) -> None:
    promoted = cli._parser().parse_args(
        [
            "promote",
            *_worker_identity_arguments(),
            *_manifest_arguments(tmp_path),
            "--rollback-state-output",
            str(tmp_path / "rollback.json"),
            "--provider-mutation-marker-output",
            str(tmp_path / "provider-mutation.json"),
        ]
    )
    rolled_back = cli._parser().parse_args(
        [
            "rollback",
            "--rollback-state-input",
            str(tmp_path / "rollback.json"),
            "--provider-mutation-marker-output",
            str(tmp_path / "provider-mutation.json"),
        ]
    )

    assert promoted.provider_mutation_marker_output == tmp_path / "provider-mutation.json"
    assert rolled_back.provider_mutation_marker_output == tmp_path / "provider-mutation.json"


@pytest.mark.parametrize(
    ("image", "revision"),
    (
        ("ghcr.io/neuraln-cyber/gen-automation/i2v-worker:latest", SOURCE_REVISION),
        (
            "ghcr.io/neuraln-cyber/gen-automation/i2v-worker@sha256:" + "A" * 64,
            SOURCE_REVISION,
        ),
        (
            "ghcr.io/another-owner/gen-automation/i2v-worker@sha256:" + WORKER_DIGEST,
            SOURCE_REVISION,
        ),
        (WORKER_IMAGE, "b" * 39),
        (WORKER_IMAGE, "B" * 40),
    ),
)
def test_parser_rejects_non_exact_worker_identity(
    image: str,
    revision: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli._parser().parse_args(
            [
                "profile-preflight",
                "--expected-worker-image",
                image,
                "--expected-worker-source-revision",
                revision,
                "--expected-public-profile",
                "false",
            ]
        )

    assert raised.value.code == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("value", ("False", "TRUE", "0", "yes", ""))
def test_parser_rejects_non_exact_public_profile_boolean(
    value: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli._parser().parse_args(
            [
                "profile-preflight",
                *_worker_identity_arguments(),
                "--expected-public-profile",
                value,
            ]
        )

    assert raised.value.code == 2
    assert capsys.readouterr().out == ""


def test_parser_accepts_exact_worker_identity_and_boolean() -> None:
    parsed = cli._parser().parse_args(
        [
            "profile-preflight",
            *_worker_identity_arguments(),
            "--expected-public-profile",
            "true",
        ]
    )

    assert parsed.expected_worker_image == WORKER_IMAGE
    assert parsed.expected_worker_source_revision == SOURCE_REVISION
    assert parsed.expected_public_profile is True


@pytest.mark.parametrize(
    ("failure", "exit_code", "expected_error"),
    (
        (
            I2VLoraRolloutError("operator-secret-marker"),
            2,
            "I2V LoRA rollout failed: rollout preconditions were not satisfied.\n",
        ),
        (
            RuntimeError("runtime-secret-marker"),
            1,
            "I2V LoRA rollout failed: rollout operation failed.\n",
        ),
        (
            KeyError("unexpected-secret-marker"),
            1,
            "I2V LoRA rollout failed: rollout operation failed without exposing secret details.\n",
        ),
    ),
)
def test_main_redacts_expected_and_unexpected_errors_without_tracebacks(
    failure: Exception,
    exit_code: int,
    expected_error: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(_arguments: argparse.Namespace, *, settings: Settings) -> Any:
        del settings
        raise failure

    monkeypatch.setattr(cli, "Settings", _settings)
    monkeypatch.setattr(cli, "_run", fail)

    assert cli.i2v_lora_rollout_main(["status"]) == exit_code
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert captured.out == ""
    assert captured.err == expected_error
    assert "secret-marker" not in combined
    assert "Traceback" not in combined
    assert type(failure).__name__ not in combined


def test_main_emits_only_sorted_compact_result_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = I2VLoraRolloutResult(
        operation="status",
        provider_image=WORKER_IMAGE,
        provider_ready=True,
        durable_active_jobs=0,
        durable_active_attempts=0,
        provider_active_jobs=0,
    )

    async def succeed(_arguments: argparse.Namespace, *, settings: Settings) -> Any:
        del settings
        return expected

    monkeypatch.setattr(cli, "Settings", _settings)
    monkeypatch.setattr(cli, "_run", succeed)

    assert cli.i2v_lora_rollout_main(["status"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert (
        captured.out
        == json.dumps(
            {
                "durable_active_attempts": 0,
                "durable_active_jobs": 0,
                "operation": "status",
                "provider_active_jobs": 0,
                "provider_image": WORKER_IMAGE,
                "provider_ready": True,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
