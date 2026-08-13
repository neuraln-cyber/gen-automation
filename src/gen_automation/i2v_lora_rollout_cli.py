"""Explicit operator CLI for the reviewed I2V LoRA worker rollout.

The serving control plane must first be restarted in its queue-frozen maintenance
profile. This one-off command then receives the preserved prior environment so it
can validate and roll back either a baseline or already-capable worker. It never
creates, cancels, retries, or reorders a queue job.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import boto3
import httpx2
from botocore.config import Config
from pydantic import ValidationError

from gen_automation.config import Settings
from gen_automation.db.session import Database
from gen_automation.integrations.salad import SaladClient
from gen_automation.services.i2v_lora_rollout import (
    I2VLoraRolloutError,
    I2VLoraRolloutResult,
    ReviewedManifestCoordinates,
    dry_run_reviewed_worker_rollout,
    profile_preflight,
    promote_reviewed_worker,
    read_provider_rollback_state,
    recycle_promote_reviewed_worker,
    rollback_reviewed_worker,
    rollout_status,
)
from gen_automation.services.runtime_secrets import build_runtime_secret_resolver


def i2v_lora_rollout_main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        parsed = parser.parse_args(list(arguments) if arguments is not None else None)
        settings = Settings()
        result = asyncio.run(_run(parsed, settings=settings))
    except (I2VLoraRolloutError, ValidationError):
        _safe_failure("rollout preconditions were not satisfied")
        return 2
    except (OSError, RuntimeError, ValueError):
        _safe_failure("rollout operation failed")
        return 1
    except Exception:
        _safe_failure("rollout operation failed without exposing secret details")
        return 1
    print(
        json.dumps(
            {key: value for key, value in asdict(result).items() if value is not None},
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


async def _run(arguments: argparse.Namespace, *, settings: Settings) -> I2VLoraRolloutResult:
    _require_control_plane_integrations(settings)
    if arguments.operation in {"promote", "rollback", "recycle-promote"}:
        _require_phase_one_maintenance(settings)
    database = Database(settings.database_url)
    http_client = httpx2.AsyncClient(follow_redirects=False, trust_env=False)
    resolver = build_runtime_secret_resolver(settings)
    if resolver is None:
        raise I2VLoraRolloutError("runtime secret resolver is unavailable")
    api_key = settings.salad_api_key
    organization = settings.salad_organization
    project = settings.salad_project
    assert api_key is not None and organization is not None and project is not None
    salad_client = SaladClient(
        http_client=http_client,
        api_key=api_key.get_secret_value(),
        organization=organization,
        project=project,
        base_url=str(settings.salad_api_base_url),
        timeout=settings.salad_request_timeout_seconds,
    )
    manifest_client: Any | None = None
    try:
        if arguments.operation in {"dry-run", "promote", "recycle-promote"}:
            manifest_client = boto3.client(
                "s3",
                region_name=settings.salad_worker_artifact_region.get_secret_value()
                if settings.salad_worker_artifact_region is not None
                else None,
                config=Config(
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"mode": "standard", "max_attempts": 3},
                    max_pool_connections=2,
                    user_agent_extra="gen-automation-i2v-lora-rollout/1",
                ),
            )
        if arguments.operation == "status":
            return await rollout_status(
                settings=settings,
                sessions=database.sessions,
                salad_client=salad_client,
            )
        if arguments.operation == "profile-preflight":
            return await profile_preflight(
                settings=settings,
                sessions=database.sessions,
                salad_client=salad_client,
                resolver=resolver,
                expected_worker_image=arguments.expected_worker_image,
                expected_worker_source_revision=arguments.expected_worker_source_revision,
                expected_public_profile=arguments.expected_public_profile,
            )
        if arguments.operation == "rollback":
            return await rollback_reviewed_worker(
                settings=settings,
                sessions=database.sessions,
                salad_client=salad_client,
                resolver=resolver,
                state=read_provider_rollback_state(arguments.rollback_state_input),
                provider_mutation_marker_output=arguments.provider_mutation_marker_output,
            )
        coordinates = ReviewedManifestCoordinates(
            bucket=arguments.expected_private_manifest_bucket,
            key=arguments.expected_private_manifest_key,
            version_id=arguments.expected_private_manifest_version,
            source_sha256=arguments.expected_private_manifest_source_sha256,
        )
        common = {
            "settings": settings,
            "sessions": database.sessions,
            "salad_client": salad_client,
            "resolver": resolver,
            "manifest_client": manifest_client,
            "artifact_client_factory": _artifact_client_factory(settings),
            "coordinates": coordinates,
            "worker_image": arguments.expected_worker_image,
            "worker_source_revision": arguments.expected_worker_source_revision,
            "prepared_host_env_output": arguments.prepared_host_env_output,
        }
        if arguments.operation == "dry-run":
            return await dry_run_reviewed_worker_rollout(
                **common,
                diagnostic_output=arguments.diagnostic_output,
            )
        if arguments.operation == "promote":
            return await promote_reviewed_worker(
                **common,
                rollback_state_output=arguments.rollback_state_output,
                provider_mutation_marker_output=arguments.provider_mutation_marker_output,
            )
        if arguments.operation == "recycle-promote":
            return await recycle_promote_reviewed_worker(
                **common,
                rollback_state_output=arguments.rollback_state_output,
                provider_mutation_marker_output=arguments.provider_mutation_marker_output,
                diagnostic_output=arguments.diagnostic_output,
            )
        raise I2VLoraRolloutError("unknown rollout operation")
    finally:
        if manifest_client is not None and callable(getattr(manifest_client, "close", None)):
            manifest_client.close()
        try:
            await resolver.aclose()
        finally:
            try:
                await http_client.aclose()
            finally:
                await database.dispose()


def _require_control_plane_integrations(settings: Settings) -> None:
    if (
        not settings.salad_enabled
        or settings.salad_api_key is None
        or settings.salad_organization is None
        or settings.salad_project is None
    ):
        raise I2VLoraRolloutError("Salad integration is unavailable")


def _require_phase_one_maintenance(_settings: Settings) -> None:
    # The wrapper first loads a serving maintenance profile and then invokes this
    # one-off command with the exact pre-rollout settings.
    # Keeping those settings intact is required to validate and roll back both a
    # baseline worker and a future already-capable worker upgrade.
    if os.environ.get("GEN_AUTOMATION_I2V_ROLLOUT_MAINTENANCE_CONFIRMED") != "true":
        raise I2VLoraRolloutError("the control-plane service-stop proof is unavailable")


def _artifact_client_factory(settings: Settings) -> Any:
    region = (
        settings.salad_worker_artifact_region.get_secret_value()
        if settings.salad_worker_artifact_region is not None
        else None
    )

    def build(environment: dict[str, str] | Any) -> Any:
        return boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=environment["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=environment["AWS_SECRET_ACCESS_KEY"],
            aws_session_token=environment["AWS_SESSION_TOKEN"],
            config=Config(
                connect_timeout=5,
                read_timeout=30,
                retries={"mode": "standard", "max_attempts": 3},
                max_pool_connections=2,
                user_agent_extra="gen-automation-i2v-lora-artifact-preflight/1",
            ),
        )

    return build


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gen_automation.i2v_lora_rollout_cli",
        description="Bounded reviewed I2V LoRA provider rollout (no queue mutations).",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("status", help="Read current durable/provider state only.")

    profile = subparsers.add_parser(
        "profile-preflight",
        help="Read-only exact provider/capability check before changing public exposure.",
    )
    _add_worker_identity_arguments(profile)
    profile.add_argument(
        "--expected-public-profile",
        required=True,
        type=_boolean,
        metavar="true|false",
    )

    for name in ("dry-run", "promote", "recycle-promote"):
        command = subparsers.add_parser(name)
        _add_worker_identity_arguments(command)
        command.add_argument("--expected-private-manifest-bucket", required=True)
        command.add_argument("--expected-private-manifest-key", required=True)
        command.add_argument("--expected-private-manifest-version", required=True)
        command.add_argument("--expected-private-manifest-source-sha256", required=True)
        command.add_argument("--prepared-host-env-output", required=True, type=Path)
        if name in {"dry-run", "recycle-promote"}:
            command.add_argument("--diagnostic-output", required=True, type=Path)
        if name in {"promote", "recycle-promote"}:
            command.add_argument("--rollback-state-output", required=True, type=Path)
            command.add_argument(
                "--provider-mutation-marker-output",
                required=True,
                type=Path,
            )

    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--rollback-state-input", required=True, type=Path)
    rollback.add_argument(
        "--provider-mutation-marker-output",
        required=True,
        type=Path,
    )
    return parser


def _add_worker_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--expected-worker-image", required=True, type=_worker_image)
    parser.add_argument("--expected-worker-source-revision", required=True, type=_source_revision)


def _boolean(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def _worker_image(value: str) -> str:
    if re.fullmatch(
        r"ghcr\.io/neuraln-cyber/gen-automation/i2v-worker@sha256:[0-9a-f]{64}",
        value,
    ):
        return value
    raise argparse.ArgumentTypeError("expected the exact immutable I2V worker image")


def _source_revision(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value):
        return value
    raise argparse.ArgumentTypeError("expected a 40-hex worker source revision")


def _safe_failure(message: str) -> None:
    print(f"I2V LoRA rollout failed: {message}.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(i2v_lora_rollout_main())
