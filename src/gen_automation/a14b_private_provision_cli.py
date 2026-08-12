"""Bounded staging cutover checks and one-shot private A14B group provisioning."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, NoReturn, Protocol, TextIO, cast
from uuid import UUID

import boto3
import httpx2
from botocore.config import Config
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.config import Environment, Settings
from gen_automation.db.models import (
    ExperimentWarmLease,
    SaladDeployment,
    VideoGenerationAttempt,
    VideoGenerationJob,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    DesiredDeploymentState,
    ExperimentWarmLeaseState,
    SaladDeploymentPurpose,
    SaladDeploymentState,
)
from gen_automation.domain.video import VideoGenerationAttemptState, VideoGenerationState
from gen_automation.integrations.salad.client import SaladClient
from gen_automation.integrations.salad.errors import SaladAPIError
from gen_automation.integrations.salad.models import SaladContainerGroup
from gen_automation.services.runtime_secrets import (
    RuntimeSecretResolver,
    build_runtime_secret_resolver,
)
from gen_automation.services.salad_deployments import (
    DeploymentAction,
    DeploymentResult,
    EphemeralRegistryBasicAuth,
    _group_configuration_drift,
    _load_deployment_locked,
    _lock_budget_guard,
    provision_deployment_step,
)
from gen_automation.services.videos import acquire_a14b_submission_lock

MINIMUM_CONTROL_PLANE_REVISION = "d585214403c2b8090dc468b5045db1cf7b06b3ac"
REQUIRED_MIGRATION_HEAD = "20260811_0036"
PRIVATE_IMAGE_REPOSITORY = (
    "ghcr.io/neuraln-cyber/gen-automation-a14b-registry/video-worker-a14b-private"
)
PRIVATE_IMAGE_PATTERN = re.compile(rf"{re.escape(PRIVATE_IMAGE_REPOSITORY)}@sha256:[0-9a-f]{{64}}")
IMMUTABLE_VIDEO_IMAGE_PATTERN = re.compile(
    r"(?:ghcr[.]io/neuraln-cyber/gen-automation/video-worker|"
    r"ghcr[.]io/neuraln-cyber/gen-automation-a14b-registry/"
    r"video-worker-a14b-private)@sha256:[0-9a-f]{64}"
)
RTX_5090_GPU_CLASS_ID = "851399fb-7329-4195-a042-d6514b28cf33"
EXPECTED_STORAGE_BYTES = 50 * 1024 * 1024 * 1024
EXPECTED_MEMORY_MB = 32 * 1024
EXPECTED_MAX_HOURLY_COST_MICROUSD = 500_000
A14B_REGISTRY_PARAMETER_NAME = "/gen-automation-staging/a14b/ghcr-pull-once"
A14B_REGISTRY_PARAMETER_ARN = (
    "arn:aws:ssm:eu-central-1:861912887470:parameter/gen-automation-staging/a14b/ghcr-pull-once"
)
A14B_REGISTRY_PARAMETER_SCHEMA = "gen-automation.a14b-ghcr-pull/v1"
A14B_REGISTRY_USERNAME = "neuraln-cyber"
_A14B_REGISTRY_PARAMETER_MAX_CHARACTERS = 2_048
_A14B_REGISTRY_PARAMETER_MAX_AGE = timedelta(minutes=10)
_A14B_REGISTRY_PARAMETER_CLOCK_SKEW = timedelta(seconds=30)
_A14B_REGISTRY_PARAMETER_EXPIRES_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
_MAX_STDIN_CREDENTIAL_CHARACTERS = 1_128


class OperatorInputError(ValueError):
    """The bounded operator contract was not satisfied."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        raise OperatorInputError("invalid command arguments")


class ProvisionStatus(StrEnum):
    CREATED = "created"
    ADOPTED = "adopted"
    ALREADY_PROVISIONED = "already_provisioned"
    AMBIGUOUS_FOUND = "ambiguous_found"
    AMBIGUOUS_NOT_FOUND = "ambiguous_not_found"


class _CredentialSource(StrEnum):
    STDIN = "stdin"
    SSM_PARAMETER = "ssm_parameter"


class _SSMClient(Protocol):
    def get_parameter(self, **kwargs: object) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _Credentials:
    username: str
    password: SecretStr


@dataclass(frozen=True)
class _Command:
    operation: str
    deployment_id: UUID | None = None
    image: str | None = None
    current_image: str | None = None
    minimum_revision: str | None = None
    expected_image_lane_sha256: str | None = None
    credential_source: _CredentialSource | None = None
    ssm_parameter_name: str | None = None
    ssm_parameter_version: int | None = None


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser(
        prog="python -m gen_automation.a14b_private_provision_cli",
        description=(
            "Validate a staging VIDEO cutover or provision one exact private A14B group. "
            "Registry credentials come from exactly one bounded stdin or SSM source."
        ),
    )
    subcommands = parser.add_subparsers(
        dest="operation",
        required=True,
        parser_class=_SafeArgumentParser,
    )

    provision = subcommands.add_parser("provision")
    provision.add_argument("--deployment-id", required=True)
    provision.add_argument("--image", required=True)
    credential_source = provision.add_mutually_exclusive_group(required=True)
    credential_source.add_argument("--credential-stdin", action="store_true")
    credential_source.add_argument("--ssm-parameter-name")
    provision.add_argument("--ssm-parameter-version")

    safe = subcommands.add_parser("assert-cutover-safe")
    safe.add_argument("--expected-current-image", required=True)
    safe.add_argument("--minimum-control-plane-revision", required=True)

    applied = subcommands.add_parser("assert-cutover-applied")
    applied.add_argument("--image", required=True)
    applied.add_argument("--minimum-control-plane-revision", required=True)
    applied.add_argument("--expected-image-lane-sha256", required=True)
    return parser


def _parse_command(arguments: Sequence[str]) -> _Command:
    namespace, unknown = _parser().parse_known_args(list(arguments))
    if unknown:
        raise OperatorInputError("unknown command arguments")
    if namespace.operation == "provision":
        try:
            deployment_id = UUID(namespace.deployment_id)
        except (AttributeError, ValueError) as error:
            raise OperatorInputError("deployment ID is invalid") from error
        image = str(namespace.image)
        _require_private_image(image)
        if namespace.credential_stdin:
            if namespace.ssm_parameter_version is not None:
                raise OperatorInputError("stdin credentials cannot select an SSM version")
            return _Command(
                operation="provision",
                deployment_id=deployment_id,
                image=image,
                credential_source=_CredentialSource.STDIN,
            )
        parameter_name = str(namespace.ssm_parameter_name)
        if parameter_name != A14B_REGISTRY_PARAMETER_NAME:
            raise OperatorInputError("SSM credential parameter name is invalid")
        version_value = namespace.ssm_parameter_version
        if (
            not isinstance(version_value, str)
            or re.fullmatch(r"[1-9][0-9]{0,5}", version_value) is None
        ):
            raise OperatorInputError("SSM credential parameter version is invalid")
        return _Command(
            operation="provision",
            deployment_id=deployment_id,
            image=image,
            credential_source=_CredentialSource.SSM_PARAMETER,
            ssm_parameter_name=parameter_name,
            ssm_parameter_version=int(version_value),
        )
    if namespace.operation == "assert-cutover-safe":
        current_image = str(namespace.expected_current_image)
        if IMMUTABLE_VIDEO_IMAGE_PATTERN.fullmatch(current_image) is None:
            raise OperatorInputError("current VIDEO image is not an approved immutable reference")
        minimum_revision = str(namespace.minimum_control_plane_revision)
        _require_minimum_revision(minimum_revision)
        return _Command(
            operation="assert-cutover-safe",
            current_image=current_image,
            minimum_revision=minimum_revision,
        )
    if namespace.operation == "assert-cutover-applied":
        image = str(namespace.image)
        _require_private_image(image)
        minimum_revision = str(namespace.minimum_control_plane_revision)
        _require_minimum_revision(minimum_revision)
        lane_sha256 = str(namespace.expected_image_lane_sha256)
        if re.fullmatch(r"[0-9a-f]{64}", lane_sha256) is None:
            raise OperatorInputError("IMAGE lane binding is invalid")
        return _Command(
            operation="assert-cutover-applied",
            image=image,
            minimum_revision=minimum_revision,
            expected_image_lane_sha256=lane_sha256,
        )
    raise OperatorInputError("unsupported operation")


def _require_private_image(image: str) -> None:
    if PRIVATE_IMAGE_PATTERN.fullmatch(image) is None:
        raise OperatorInputError("image is not the exact private A14B immutable repository")


def _require_minimum_revision(value: str) -> None:
    # A Git SHA is not ordered. Presence of this module is the capability proof;
    # this exact contract anchor prevents an operator from claiming another base.
    if value != MINIMUM_CONTROL_PLANE_REVISION:
        raise OperatorInputError("minimum control-plane contract is invalid")


def _read_credentials(stream: TextIO) -> _Credentials:
    if stream.isatty():
        raise OperatorInputError("registry credentials require non-interactive stdin")
    payload = stream.read(_MAX_STDIN_CREDENTIAL_CHARACTERS + 1)
    if not payload or len(payload) > _MAX_STDIN_CREDENTIAL_CHARACTERS:
        raise OperatorInputError("registry credential input is invalid")
    if "\x00" in payload or not payload.endswith("\n"):
        raise OperatorInputError("registry credential input is invalid")
    lines = payload.splitlines()
    if len(lines) != 2:
        raise OperatorInputError("registry credential input is invalid")
    username, password_value = lines
    return _validated_credentials(username=username, password_value=password_value)


def _validated_credentials(*, username: str, password_value: str) -> _Credentials:
    password = SecretStr(password_value)
    # EphemeralRegistryBasicAuth performs the canonical character/length checks.
    try:
        EphemeralRegistryBasicAuth(
            image_digest=f"{PRIVATE_IMAGE_REPOSITORY}@sha256:{'0' * 64}",
            username=username,
            password=password,
        )
    except Exception as error:
        raise OperatorInputError("registry credential input is invalid") from error
    return _Credentials(username=username, password=password)


def _bounded_ssm_client() -> _SSMClient:
    configuration = Config(
        connect_timeout=5,
        read_timeout=10,
        proxies={},
        retries={"mode": "standard", "total_max_attempts": 2},
    )
    session = boto3.Session(region_name="eu-central-1")
    return cast(
        _SSMClient,
        session.client(
            "ssm",
            region_name="eu-central-1",
            endpoint_url="https://ssm.eu-central-1.amazonaws.com",
            config=configuration,
        ),
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise OperatorInputError("SSM credential payload is invalid")
        value[key] = item
    return value


def _load_ssm_credentials(
    *,
    client: _SSMClient,
    parameter_name: str,
    parameter_version: int,
    image: str,
    now: datetime | None = None,
) -> _Credentials:
    if parameter_name != A14B_REGISTRY_PARAMETER_NAME or not 1 <= parameter_version <= 999_999:
        raise OperatorInputError("SSM credential selector is invalid")
    selected_at = now or datetime.now(UTC)
    if selected_at.tzinfo is None:
        raise OperatorInputError("SSM credential validation clock is invalid")
    selected_at = selected_at.astimezone(UTC)
    response = client.get_parameter(
        Name=f"{parameter_name}:{parameter_version}",
        WithDecryption=True,
    )
    parameter = response.get("Parameter")
    if not isinstance(parameter, dict):
        raise OperatorInputError("SSM credential metadata is invalid")
    selector = parameter.get("Selector")
    last_modified = parameter.get("LastModifiedDate")
    if (
        parameter.get("Name") != parameter_name
        or parameter.get("ARN") != A14B_REGISTRY_PARAMETER_ARN
        or parameter.get("Type") != "SecureString"
        or parameter.get("DataType") != "text"
        or parameter.get("Version") != parameter_version
        or selector not in (None, f":{parameter_version}", f"{parameter_name}:{parameter_version}")
        or not isinstance(last_modified, datetime)
        or last_modified.tzinfo is None
    ):
        raise OperatorInputError("SSM credential metadata is invalid")
    modified_at = last_modified.astimezone(UTC)
    if not (
        selected_at - _A14B_REGISTRY_PARAMETER_MAX_AGE
        <= modified_at
        <= selected_at + _A14B_REGISTRY_PARAMETER_CLOCK_SKEW
    ):
        raise OperatorInputError("SSM credential parameter is stale")
    raw_payload = parameter.get("Value")
    if not isinstance(raw_payload, str) or not raw_payload or "\x00" in raw_payload:
        raise OperatorInputError("SSM credential payload is invalid")
    try:
        payload_size = len(raw_payload.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise OperatorInputError("SSM credential payload is invalid") from error
    if payload_size > _A14B_REGISTRY_PARAMETER_MAX_CHARACTERS:
        raise OperatorInputError("SSM credential payload is invalid")
    try:
        payload = json.loads(raw_payload, object_pairs_hook=_reject_duplicate_json_keys)
    except OperatorInputError:
        raise
    except (TypeError, ValueError) as error:
        raise OperatorInputError("SSM credential payload is invalid") from error
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "image",
        "user",
        "pass",
        "expires",
    }:
        raise OperatorInputError("SSM credential payload is invalid")
    schema = payload.get("schema")
    payload_image = payload.get("image")
    username = payload.get("user")
    password_value = payload.get("pass")
    expires = payload.get("expires")
    if not all(
        isinstance(value, str)
        for value in (schema, payload_image, username, password_value, expires)
    ):
        raise OperatorInputError("SSM credential payload is invalid")
    assert isinstance(schema, str)
    assert isinstance(payload_image, str)
    assert isinstance(username, str)
    assert isinstance(password_value, str)
    assert isinstance(expires, str)
    if (
        not hmac.compare_digest(schema, A14B_REGISTRY_PARAMETER_SCHEMA)
        or not hmac.compare_digest(payload_image, image)
        or not hmac.compare_digest(username, A14B_REGISTRY_USERNAME)
        or _A14B_REGISTRY_PARAMETER_EXPIRES_PATTERN.fullmatch(expires) is None
    ):
        raise OperatorInputError("SSM credential payload binding is invalid")
    try:
        expires_at = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise OperatorInputError("SSM credential expiry is invalid") from error
    if not (
        selected_at < expires_at <= selected_at + _A14B_REGISTRY_PARAMETER_MAX_AGE
        and expires_at
        <= modified_at + _A14B_REGISTRY_PARAMETER_MAX_AGE + _A14B_REGISTRY_PARAMETER_CLOCK_SKEW
    ):
        raise OperatorInputError("SSM credential parameter is expired or overlong")
    return _validated_credentials(username=username, password_value=password_value)


def _validate_staging_settings(settings: Settings) -> None:
    if (
        settings.environment != Environment.STAGING
        or not settings.salad_enabled
        or not settings.video_generation_enabled
        or not settings.background_runtime_enabled
        or settings.salad_api_key is None
        or settings.salad_organization is None
        or settings.salad_project is None
    ):
        raise OperatorInputError("staging Salad VIDEO runtime is not fully enabled")


async def _migration_head(session: AsyncSession) -> None:
    value = await session.scalar(text("SELECT version_num FROM alembic_version"))
    if value != REQUIRED_MIGRATION_HEAD:
        raise OperatorInputError("database migration head is not approved")


async def _assert_database_drained(session: AsyncSession) -> None:
    active_jobs = await session.scalar(
        select(func.count())
        .select_from(VideoGenerationJob)
        .where(
            VideoGenerationJob.state.not_in(
                (
                    VideoGenerationState.SUCCEEDED,
                    VideoGenerationState.FAILED,
                    VideoGenerationState.CANCELLED,
                )
            )
        )
    )
    active_attempts = await session.scalar(
        select(func.count())
        .select_from(VideoGenerationAttempt)
        .where(
            VideoGenerationAttempt.state.not_in(
                (
                    VideoGenerationAttemptState.SUCCEEDED,
                    VideoGenerationAttemptState.FAILED,
                    VideoGenerationAttemptState.CANCELLED,
                )
            )
        )
    )
    live_leases = await session.scalar(
        select(func.count())
        .select_from(ExperimentWarmLease)
        .join(SaladDeployment, SaladDeployment.id == ExperimentWarmLease.salad_deployment_id)
        .where(
            SaladDeployment.purpose == SaladDeploymentPurpose.VIDEO,
            ExperimentWarmLease.state.in_(
                (
                    ExperimentWarmLeaseState.STARTING,
                    ExperimentWarmLeaseState.ACTIVE,
                    ExperimentWarmLeaseState.ENDING,
                )
            ),
        )
    )
    open_reservations = await session.scalar(
        select(func.count())
        .select_from(VideoGenerationJob)
        .where(VideoGenerationJob.reserved_cost_microusd > 0)
    )
    if any(value != 0 for value in (active_jobs, active_attempts, live_leases, open_reservations)):
        raise OperatorInputError("VIDEO lane is not drained")


async def _current_deployment(
    session: AsyncSession,
    purpose: SaladDeploymentPurpose,
    *,
    lock: bool = False,
) -> SaladDeployment:
    query = select(SaladDeployment).where(
        SaladDeployment.purpose == purpose,
        SaladDeployment.is_current.is_(True),
    )
    if lock:
        query = query.with_for_update()
    deployment = await session.scalar(query)
    if deployment is None:
        raise OperatorInputError("current deployment is missing")
    return deployment


def _image_lane_sha256(deployment: SaladDeployment) -> str:
    payload = json.dumps(
        {
            "config_sha256": deployment.config_sha256,
            "id": str(deployment.id),
            "version_no": deployment.version_no,
            "worker_image_digest": deployment.worker_image_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_private_deployment(
    deployment: SaladDeployment,
    *,
    image: str,
    settings: Settings,
) -> None:
    configuration = deployment.provider_configuration
    container = configuration.get("container")
    resources = container.get("resources") if isinstance(container, dict) else None
    gpu_classes = resources.get("gpu_classes") if isinstance(resources, dict) else None
    if (
        deployment.purpose != SaladDeploymentPurpose.VIDEO
        or not deployment.is_current
        or deployment.worker_image_digest != image
        or deployment.organization_name != settings.salad_organization
        or deployment.project_name != settings.salad_project
        or deployment.desired_state != DesiredDeploymentState.ACTIVE
        or configuration.get("image_pull_mode") != "ephemeral_basic"
        or not isinstance(container, dict)
        or container.get("priority") != "high"
        or container.get("image_caching") is not True
        or not isinstance(resources, dict)
        or resources.get("cpu") != 4
        or resources.get("memory") != EXPECTED_MEMORY_MB
        or resources.get("storage_amount") != EXPECTED_STORAGE_BYTES
        or gpu_classes != [RTX_5090_GPU_CLASS_ID]
        or deployment.min_replicas != 0
        or deployment.max_replicas != 1
        or deployment.desired_queue_length != 1
        or deployment.max_hourly_cost_microusd != EXPECTED_MAX_HOURLY_COST_MICROUSD
    ):
        raise OperatorInputError("private A14B deployment binding is invalid")


def _validate_group_readback(
    group: SaladContainerGroup,
    deployment: SaladDeployment,
    *,
    expected_id: str | None,
) -> None:
    container = group.raw.get("container")
    queue_connection = group.raw.get("queue_connection")
    if (
        group.name != deployment.container_group_name
        or (expected_id is not None and str(group.id) != expected_id)
        or not isinstance(container, dict)
        or container.get("image") != deployment.worker_image_digest
        or not isinstance(queue_connection, dict)
        or queue_connection.get("queue_name") != deployment.queue_name
        or _group_configuration_drift(deployment, group) is not None
    ):
        raise OperatorInputError("provider group readback does not match the exact target")


async def _read_group_or_none(
    client: SaladClient,
    deployment: SaladDeployment,
) -> SaladContainerGroup | None:
    try:
        return await client.get_container_group(deployment.container_group_name)
    except SaladAPIError as error:
        if error.status_code == 404:
            return None
        raise


async def _assert_provider_drained(
    client: SaladClient,
    deployment: SaladDeployment,
) -> None:
    if deployment.provider_queue_id is None or deployment.provider_container_group_id is None:
        raise OperatorInputError("current VIDEO provider identity is incomplete")
    queue = await client.get_queue(deployment.queue_name)
    group = await client.get_container_group(deployment.container_group_name)
    instances = await client.list_container_group_instances(deployment.container_group_name)
    state = group.current_state
    if (
        queue.name != deployment.queue_name
        or str(queue.id) != deployment.provider_queue_id
        or queue.current_queue_length != 0
        or group.name != deployment.container_group_name
        or str(group.id) != deployment.provider_container_group_id
        or group.replicas != 0
        or group.pending_change
        or state.allocating_count != 0
        or state.creating_count != 0
        or state.running_count != 0
        or state.stopping_count != 0
        or instances.instances
    ):
        raise OperatorInputError("VIDEO provider lane is not authoritatively drained")


async def _assert_cutover_safe(
    session: AsyncSession,
    client: SaladClient,
    settings: Settings,
    *,
    current_image: str,
) -> str:
    if settings.salad_video_worker_image != current_image:
        raise OperatorInputError("running VIDEO image does not match the host environment")
    await _migration_head(session)
    await _assert_database_drained(session)
    image_deployment = await _current_deployment(session, SaladDeploymentPurpose.IMAGE)
    video_deployment = await _current_deployment(session, SaladDeploymentPurpose.VIDEO)
    if (
        video_deployment.worker_image_digest != current_image
        or video_deployment.observed_replicas not in (None, 0)
        or video_deployment.ready_replicas not in (None, 0)
    ):
        raise OperatorInputError("current VIDEO deployment does not match the drained target")
    superseded_live = await session.scalar(
        select(func.count())
        .select_from(SaladDeployment)
        .where(
            SaladDeployment.purpose == SaladDeploymentPurpose.VIDEO,
            SaladDeployment.is_current.is_(False),
            SaladDeployment.provider_container_group_id.is_not(None),
            SaladDeployment.state != SaladDeploymentState.STOPPED,
        )
    )
    if superseded_live != 0:
        raise OperatorInputError("a superseded VIDEO deployment is not stopped")
    await _assert_provider_drained(client, video_deployment)
    return _image_lane_sha256(image_deployment)


async def _assert_cutover_applied(
    session: AsyncSession,
    client: SaladClient,
    settings: Settings,
    *,
    image: str,
    expected_image_lane_sha256: str,
) -> UUID:
    if settings.salad_video_worker_image != image:
        raise OperatorInputError("running VIDEO image does not match the requested cutover")
    await _migration_head(session)
    await _assert_database_drained(session)
    image_deployment = await _current_deployment(session, SaladDeploymentPurpose.IMAGE)
    if _image_lane_sha256(image_deployment) != expected_image_lane_sha256:
        raise OperatorInputError("IMAGE lane changed during VIDEO cutover")
    deployment = await _current_deployment(session, SaladDeploymentPurpose.VIDEO)
    _validate_private_deployment(deployment, image=image, settings=settings)
    if (
        deployment.provider_queue_id is None
        or deployment.provider_container_group_id is not None
        or deployment.state != SaladDeploymentState.PROVISIONING
        or deployment.last_error_code != "registry_authentication_required"
    ):
        raise OperatorInputError("private VIDEO deployment is not awaiting one-shot auth")
    queue = await client.get_queue(deployment.queue_name)
    if (
        queue.name != deployment.queue_name
        or str(queue.id) != deployment.provider_queue_id
        or queue.current_queue_length != 0
    ):
        raise OperatorInputError("private VIDEO queue readback is invalid")
    if await _read_group_or_none(client, deployment) is not None:
        raise OperatorInputError("private VIDEO group already exists before authorization")
    return UUID(str(deployment.id))


async def provision_private_group_once(
    session: AsyncSession,
    client: SaladClient,
    settings: Settings,
    *,
    deployment_id: UUID,
    image: str,
    credentials: _Credentials,
    secret_resolver: RuntimeSecretResolver | None,
) -> ProvisionStatus:
    """Issue at most one provisioning step and never repeat an ambiguous group POST."""

    # Global order: A14B admission -> provider budget -> deployment. The
    # provisioning service reacquires the latter two locks in the same order,
    # which is reentrant within this transaction and cannot invert the
    # controller's budget-before-deployment contract.
    if not await acquire_a14b_submission_lock(session):
        raise OperatorInputError("transaction-scoped A14B admission lock is unavailable")
    await _migration_head(session)
    if not await _lock_budget_guard(session):
        raise OperatorInputError("provider budget guard is unavailable")
    deployment = await _load_deployment_locked(session, deployment_id)
    _validate_private_deployment(deployment, image=image, settings=settings)

    auth = EphemeralRegistryBasicAuth(
        image_digest=image,
        username=credentials.username,
        password=credentials.password,
    )
    if deployment.provider_queue_id is None:
        raise OperatorInputError("private VIDEO queue must be durable before group provisioning")

    # This is the final drain observation. The shared transaction advisory
    # guard prevents create_video_submission from inserting an A14B job until
    # this transaction commits or rolls back after the provider POST.
    await _assert_database_drained(session)

    # Replays and prior ambiguous outcomes are read-only. In particular, a second
    # invocation cannot convert UNKNOWN into a repeated private registry POST.
    if (
        deployment.provider_container_group_id is not None
        or deployment.state == SaladDeploymentState.UNKNOWN
    ):
        group = await _read_group_or_none(client, deployment)
        if group is None:
            return ProvisionStatus.AMBIGUOUS_NOT_FOUND
        _validate_group_readback(
            group,
            deployment,
            expected_id=deployment.provider_container_group_id,
        )
        return (
            ProvisionStatus.ALREADY_PROVISIONED
            if deployment.provider_container_group_id is not None
            else ProvisionStatus.AMBIGUOUS_FOUND
        )

    if (
        deployment.state != SaladDeploymentState.PROVISIONING
        or deployment.last_error_code != "registry_authentication_required"
    ):
        raise OperatorInputError("private VIDEO deployment is not awaiting registry auth")

    result: DeploymentResult = await provision_deployment_step(
        session,
        deployment_id=deployment_id,
        client=client,
        secret_resolver=secret_resolver,
        registry_basic_auth=auth,
    )
    await session.commit()

    if result.action in {DeploymentAction.GROUP_CREATED, DeploymentAction.GROUP_ADOPTED}:
        group = await _read_group_or_none(client, deployment)
        if group is None:
            return ProvisionStatus.AMBIGUOUS_NOT_FOUND
        _validate_group_readback(
            group,
            deployment,
            expected_id=result.provider_container_group_id,
        )
        return (
            ProvisionStatus.CREATED
            if result.action == DeploymentAction.GROUP_CREATED
            else ProvisionStatus.ADOPTED
        )

    if result.state == SaladDeploymentState.UNKNOWN:
        group = await _read_group_or_none(client, deployment)
        if group is None:
            return ProvisionStatus.AMBIGUOUS_NOT_FOUND
        _validate_group_readback(group, deployment, expected_id=None)
        return ProvisionStatus.AMBIGUOUS_FOUND
    raise OperatorInputError("one-shot provisioning did not create or adopt the exact group")


async def _execute(command: _Command, credentials: _Credentials | None) -> str | ProvisionStatus:
    settings = Settings()
    _validate_staging_settings(settings)
    database = Database(settings.database_url)
    http_client = httpx2.AsyncClient(follow_redirects=False, trust_env=False)
    api_key = settings.salad_api_key
    organization = settings.salad_organization
    project = settings.salad_project
    assert api_key is not None and organization is not None and project is not None
    client = SaladClient(
        http_client=http_client,
        api_key=api_key.get_secret_value(),
        organization=organization,
        project=project,
        base_url=str(settings.salad_api_base_url),
        timeout=settings.salad_request_timeout_seconds,
    )
    try:
        async with database.sessions() as session:
            if command.operation == "assert-cutover-safe":
                assert command.current_image is not None
                return await _assert_cutover_safe(
                    session,
                    client,
                    settings,
                    current_image=command.current_image,
                )
            if command.operation == "assert-cutover-applied":
                assert command.image is not None
                assert command.expected_image_lane_sha256 is not None
                deployment_id = await _assert_cutover_applied(
                    session,
                    client,
                    settings,
                    image=command.image,
                    expected_image_lane_sha256=command.expected_image_lane_sha256,
                )
                return str(deployment_id)
            if credentials is None or command.deployment_id is None or command.image is None:
                raise OperatorInputError("provisioning inputs are incomplete")
            resolver = build_runtime_secret_resolver(settings)
            return await provision_private_group_once(
                session,
                client,
                settings,
                deployment_id=command.deployment_id,
                image=command.image,
                credentials=credentials,
                secret_resolver=resolver,
            )
    finally:
        try:
            await http_client.aclose()
        finally:
            await database.dispose()


def a14b_private_provision_main(
    arguments: Sequence[str] | None = None,
    *,
    input_stream: TextIO | None = None,
) -> int:
    """Run exactly one fixed operator action without exposing exception details."""

    try:
        command = _parse_command(sys.argv[1:] if arguments is None else arguments)
        credentials: _Credentials | None = None
        if command.operation == "provision":
            assert command.image is not None
            if command.credential_source == _CredentialSource.STDIN:
                credentials = _read_credentials(input_stream or sys.stdin)
            elif command.credential_source == _CredentialSource.SSM_PARAMETER:
                assert command.ssm_parameter_name is not None
                assert command.ssm_parameter_version is not None
                credentials = _load_ssm_credentials(
                    client=_bounded_ssm_client(),
                    parameter_name=command.ssm_parameter_name,
                    parameter_version=command.ssm_parameter_version,
                    image=command.image,
                )
            else:
                raise OperatorInputError("credential source is invalid")
        result = asyncio.run(_execute(command, credentials))
    except OperatorInputError:
        print("A14B operator input or target validation failed safely.", file=sys.stderr)
        return 2
    except Exception:
        print(
            "A14B operator action failed without exposing credentials or provider details.",
            file=sys.stderr,
        )
        return 1

    if command.operation == "assert-cutover-safe":
        assert isinstance(result, str) and re.fullmatch(r"[0-9a-f]{64}", result)
        print(result)
        return 0
    if command.operation == "assert-cutover-applied":
        assert isinstance(result, str)
        UUID(result)
        print(f"deployment_id={result}")
        return 0
    assert isinstance(result, ProvisionStatus)
    if result in {
        ProvisionStatus.CREATED,
        ProvisionStatus.ADOPTED,
        ProvisionStatus.ALREADY_PROVISIONED,
    }:
        print("Exact private A14B VIDEO group is provisioned.")
        return 0
    print(
        "Private group POST outcome remains ambiguous; deterministic readback was performed "
        "and the POST was not repeated.",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":  # pragma: no cover - exercised through the module contract
    raise SystemExit(a14b_private_provision_main())
