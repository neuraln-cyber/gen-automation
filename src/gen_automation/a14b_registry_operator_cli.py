"""Local one-use transport for an A14B private GHCR pull credential."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, Protocol, cast
from uuid import UUID

import boto3
import httpx2
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import SecretStr

from gen_automation.a14b_private_provision_cli import (
    A14B_REGISTRY_PARAMETER_NAME,
    A14B_REGISTRY_PARAMETER_SCHEMA,
    A14B_REGISTRY_USERNAME,
    PRIVATE_IMAGE_PATTERN,
    PRIVATE_IMAGE_REPOSITORY,
)

AWS_ACCOUNT_ID = "861912887470"
AWS_PROFILE = "gen-automation-staging"
AWS_REGION = "eu-central-1"
GITHUB_API_URL = "https://api.github.com/user"
GHCR_MANIFEST_PREFIX = (
    "https://ghcr.io/v2/neuraln-cyber/gen-automation/video-worker-a14b-private/manifests/"
)
GHCR_TOKEN_ENDPOINT = "https://ghcr.io/token"  # noqa: S105 - public endpoint
GHCR_REPOSITORY_SCOPE = "repository:neuraln-cyber/gen-automation/video-worker-a14b-private:pull"
_AWS_OPERATOR_ARN_PATTERN = re.compile(
    r"arn:aws:sts::861912887470:assumed-role/"
    r"AWSReservedSSO_GenAutomationStagingDeployer_[A-Fa-f0-9]{16}/"
    r"[A-Za-z0-9+=,.@_-]{2,64}"
)
_INSTANCE_ID_PATTERN = re.compile(r"i-[0-9a-f]{8,17}")
_COMMAND_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TOKEN_MAX_BYTES = 1_024
_MANIFEST_MAX_BYTES = 2 * 1024 * 1024
_PARAMETER_MAX_BYTES = 2_048
_PARAMETER_TTL = timedelta(minutes=10)
_POLL_DEADLINE_SECONDS = 12 * 60
_POLL_INTERVAL_SECONDS = 3
_SUCCESS_OUTPUT = "Exact private A14B VIDEO group is provisioned."
_MANIFEST_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
}
_MANIFEST_ACCEPT = ", ".join(sorted(_MANIFEST_MEDIA_TYPES))
_GHCR_CHALLENGE = (
    f'Bearer realm="{GHCR_TOKEN_ENDPOINT}",service="ghcr.io",scope="{GHCR_REPOSITORY_SCOPE}"'
)


class RegistryOperatorError(RuntimeError):
    """The bounded local authorization contract failed."""


class RegistryHandoffRetainedError(RegistryOperatorError):
    """The temporary parameter may still be needed by an unobserved command."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        raise RegistryOperatorError("invalid command arguments")


class _STSClient(Protocol):
    def get_caller_identity(self) -> dict[str, Any]: ...


class _SSMClient(Protocol):
    def put_parameter(self, **kwargs: object) -> dict[str, Any]: ...

    def send_command(self, **kwargs: object) -> dict[str, Any]: ...

    def get_command_invocation(self, **kwargs: object) -> dict[str, Any]: ...

    def delete_parameter(self, **kwargs: object) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _Command:
    image: str
    deployment_id: UUID
    instance_id: str


@dataclass(frozen=True)
class _AwsClients:
    sts: _STSClient
    ssm: _SSMClient


@dataclass(frozen=True)
class _TerminalInvocation:
    status: str
    response_code: object
    standard_output: object
    standard_error: object


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser(
        prog="python -m gen_automation.a14b_registry_operator_cli",
        description="Authorize exactly one private A14B group creation through staging SSM.",
    )
    parser.add_argument("--image", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--instance-id", required=True)
    return parser


def _parse_command(arguments: Sequence[str]) -> _Command:
    namespace, unknown = _parser().parse_known_args(list(arguments))
    if unknown:
        raise RegistryOperatorError("unknown command arguments")
    image = str(namespace.image)
    if PRIVATE_IMAGE_PATTERN.fullmatch(image) is None:
        raise RegistryOperatorError("private image binding is invalid")
    try:
        deployment_id = UUID(str(namespace.deployment_id))
    except ValueError as error:
        raise RegistryOperatorError("deployment binding is invalid") from error
    if str(deployment_id) != str(namespace.deployment_id):
        raise RegistryOperatorError("deployment binding is not canonical")
    instance_id = str(namespace.instance_id)
    if _INSTANCE_ID_PATTERN.fullmatch(instance_id) is None:
        raise RegistryOperatorError("instance binding is invalid")
    return _Command(image=image, deployment_id=deployment_id, instance_id=instance_id)


def _capture_gh_token() -> SecretStr:
    executable = shutil.which("gh")
    if executable is None or not Path(executable).is_file():
        raise RegistryOperatorError("GitHub CLI is unavailable")
    try:
        child_environment = os.environ.copy()
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
            child_environment.pop(name, None)
        result = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments
            [executable, "auth", "token", "--hostname", "github.com"],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_environment,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RegistryOperatorError("GitHub credential capture failed") from error
    if result.returncode != 0 or not result.stdout or len(result.stdout) > _TOKEN_MAX_BYTES + 2:
        raise RegistryOperatorError("GitHub credential capture failed")
    try:
        value = result.stdout.decode("ascii")
    except UnicodeDecodeError as error:
        raise RegistryOperatorError("GitHub credential is invalid") from error
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    else:
        raise RegistryOperatorError("GitHub credential is invalid")
    if (
        not value
        or len(value) > _TOKEN_MAX_BYTES
        or any(not 0x21 <= ord(item) <= 0x7E for item in value)
    ):
        raise RegistryOperatorError("GitHub credential is invalid")
    return SecretStr(value)


def _verify_github_identity(client: httpx2.Client, token: SecretStr) -> None:
    response = client.get(
        GITHUB_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token.get_secret_value()}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if (
        response.status_code != 200
        or response.history
        or str(response.url) != GITHUB_API_URL
        or len(response.content) > 32_768
    ):
        raise RegistryOperatorError("GitHub identity verification failed")
    scopes = {
        item.strip()
        for item in response.headers.get("x-oauth-scopes", "").split(",")
        if item.strip()
    }
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise RegistryOperatorError("GitHub identity verification failed") from error
    if (
        not isinstance(payload, dict)
        or payload.get("login") != A14B_REGISTRY_USERNAME
        or "read:packages" not in scopes
    ):
        raise RegistryOperatorError("GitHub identity or package scope is not approved")


def _ghcr_pull_token(
    client: httpx2.Client,
    token: SecretStr,
    *,
    manifest_url: str,
) -> SecretStr:
    challenge = client.get(
        manifest_url,
        headers={"Accept": _MANIFEST_ACCEPT, "Accept-Encoding": "identity"},
    )
    if (
        challenge.status_code != 401
        or challenge.history
        or str(challenge.url) != manifest_url
        or challenge.headers.get("www-authenticate") != _GHCR_CHALLENGE
        or len(challenge.content) > 32_768
    ):
        raise RegistryOperatorError("private GHCR authentication challenge is invalid")
    basic_value = base64.b64encode(
        f"{A14B_REGISTRY_USERNAME}:{token.get_secret_value()}".encode("ascii")
    ).decode("ascii")
    response = client.get(
        GHCR_TOKEN_ENDPOINT,
        params={"service": "ghcr.io", "scope": GHCR_REPOSITORY_SCOPE},
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {basic_value}",
        },
    )
    basic_value = "[cleared]"
    if (
        response.status_code != 200
        or response.history
        or response.url.scheme != "https"
        or response.url.host != "ghcr.io"
        or response.url.path != "/token"
        or response.url.params.get("service") != "ghcr.io"
        or response.url.params.get("scope") != GHCR_REPOSITORY_SCOPE
        or len(response.url.params) != 2
        or len(response.content) > 32_768
    ):
        raise RegistryOperatorError("private GHCR token exchange failed")
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise RegistryOperatorError("private GHCR token exchange failed") from error
    if not isinstance(payload, dict):
        raise RegistryOperatorError("private GHCR token exchange failed")
    registry_value = payload.get("token")
    alternate_value = payload.get("access_token")
    if registry_value is None:
        registry_value = alternate_value
    if alternate_value is not None and alternate_value != registry_value:
        raise RegistryOperatorError("private GHCR token exchange returned ambiguous credentials")
    if (
        not isinstance(registry_value, str)
        or not registry_value
        or len(registry_value) > 8_192
        or any(not 0x21 <= ord(item) <= 0x7E for item in registry_value)
    ):
        raise RegistryOperatorError("private GHCR token exchange returned invalid credentials")
    return SecretStr(registry_value)


def _verify_exact_manifest(client: httpx2.Client, token: SecretStr, image: str) -> None:
    digest = image.removeprefix(f"{PRIVATE_IMAGE_REPOSITORY}@")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise RegistryOperatorError("private manifest digest is invalid")
    manifest_url = f"{GHCR_MANIFEST_PREFIX}{digest}"
    registry_token = _ghcr_pull_token(client, token, manifest_url=manifest_url)
    response = client.get(
        manifest_url,
        headers={
            "Accept": _MANIFEST_ACCEPT,
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {registry_token.get_secret_value()}",
        },
    )
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    content_encoding = response.headers.get("content-encoding", "").strip().lower()
    if (
        response.status_code != 200
        or response.history
        or str(response.url) != manifest_url
        or response.headers.get("docker-content-digest") != digest
        or content_type not in _MANIFEST_MEDIA_TYPES
        or content_encoding not in ("", "identity")
        or not response.content
        or len(response.content) > _MANIFEST_MAX_BYTES
        or f"sha256:{hashlib.sha256(response.content).hexdigest()}" != digest
    ):
        raise RegistryOperatorError("exact private GHCR manifest verification failed")


def _build_aws_clients() -> _AwsClients:
    configuration = Config(
        connect_timeout=5,
        read_timeout=10,
        proxies={},
        retries={"mode": "standard", "total_max_attempts": 1},
    )
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    sts = session.client(
        "sts",
        region_name=AWS_REGION,
        endpoint_url=f"https://sts.{AWS_REGION}.amazonaws.com",
        config=configuration,
    )
    ssm = session.client(
        "ssm",
        region_name=AWS_REGION,
        endpoint_url=f"https://ssm.{AWS_REGION}.amazonaws.com",
        config=configuration,
    )
    return _AwsClients(sts=cast(_STSClient, sts), ssm=cast(_SSMClient, ssm))


def _verify_aws_identity(client: _STSClient) -> None:
    identity = client.get_caller_identity()
    account = identity.get("Account")
    arn = identity.get("Arn")
    if (
        account != AWS_ACCOUNT_ID
        or not isinstance(arn, str)
        or _AWS_OPERATOR_ARN_PATTERN.fullmatch(arn) is None
    ):
        raise RegistryOperatorError("active AWS identity is not the approved staging deployer")


def _credential_payload(*, image: str, token: SecretStr, now: datetime) -> str:
    if now.tzinfo is None:
        raise RegistryOperatorError("authorization clock is invalid")
    expires_at = (now.astimezone(UTC) + _PARAMETER_TTL).replace(microsecond=0)
    payload = json.dumps(
        {
            "schema": A14B_REGISTRY_PARAMETER_SCHEMA,
            "image": image,
            "user": A14B_REGISTRY_USERNAME,
            "pass": token.get_secret_value(),
            "expires": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(payload.encode("utf-8")) > _PARAMETER_MAX_BYTES:
        raise RegistryOperatorError("temporary registry authorization is too large")
    return payload


def _put_parameter_once(client: _SSMClient, *, payload: str) -> dict[str, Any]:
    return client.put_parameter(
        Name=A14B_REGISTRY_PARAMETER_NAME,
        Description="One-use A14B private GHCR pull authorization",
        Value=payload,
        Type="SecureString",
        Overwrite=False,
        Tier="Standard",
        DataType="text",
    )


def _parameter_version(response: dict[str, Any]) -> int:
    version = response.get("Version")
    if not isinstance(version, int) or not 1 <= version <= 999_999:
        raise RegistryOperatorError("temporary parameter creation returned invalid metadata")
    if response.get("Tier") not in (None, "Standard"):
        raise RegistryOperatorError("temporary parameter creation returned invalid metadata")
    return version


def _remote_command(command: _Command, *, parameter_version: int) -> str:
    # Every interpolated field is validated public identity. The registry secret
    # is fetched inside the container by fixed SSM name and exact version.
    return (
        "set -eu; "
        'container_id="$(/usr/bin/docker compose '
        "--env-file /etc/gen-automation/deploy.env "
        "--file /opt/gen-automation/deploy/compose.yaml "
        'ps --status running --quiet control-plane-mega 2>/dev/null)"; '
        '[ -n "$container_id" ]; '
        "[ \"$(/usr/bin/printf '%s\\n' \"$container_id\" | /usr/bin/sed '/^$/d' | "
        '/usr/bin/wc -l)" -eq 1 ]; '
        '/usr/bin/docker exec "$container_id" python3.12 -m '
        "gen_automation.a14b_private_provision_cli provision "
        f"--deployment-id {command.deployment_id} "
        f"--image {command.image} "
        f"--ssm-parameter-name {A14B_REGISTRY_PARAMETER_NAME} "
        f"--ssm-parameter-version {parameter_version}"
    )


def _send_command(client: _SSMClient, command: _Command, *, parameter_version: int) -> str:
    response = client.send_command(
        InstanceIds=[command.instance_id],
        DocumentName="AWS-RunShellScript",
        Comment="One-use private A14B registry authorization",
        TimeoutSeconds=60,
        MaxConcurrency="1",
        MaxErrors="0",
        Parameters={
            "commands": [_remote_command(command, parameter_version=parameter_version)],
            "executionTimeout": ["420"],
        },
    )
    command_value = response.get("Command")
    command_id = command_value.get("CommandId") if isinstance(command_value, dict) else None
    if not isinstance(command_id, str) or _COMMAND_ID_PATTERN.fullmatch(command_id) is None:
        raise RegistryOperatorError("AWS returned an invalid command identifier")
    return command_id


def _invocation_error_code(error: ClientError) -> str | None:
    payload = error.response.get("Error")
    value = payload.get("Code") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


def _wait_for_terminal(
    client: _SSMClient,
    command: _Command,
    *,
    command_id: str,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> _TerminalInvocation:
    deadline = monotonic() + _POLL_DEADLINE_SECONDS
    while monotonic() < deadline:
        try:
            invocation = client.get_command_invocation(
                CommandId=command_id,
                InstanceId=command.instance_id,
            )
        except ClientError as error:
            if _invocation_error_code(error) != "InvocationDoesNotExist":
                raise RegistryOperatorError("remote command status could not be read") from error
            sleep(_POLL_INTERVAL_SECONDS)
            continue
        if (
            invocation.get("CommandId") != command_id
            or invocation.get("InstanceId") != command.instance_id
        ):
            raise RegistryOperatorError("remote command status binding is invalid")
        status = invocation.get("Status")
        if status in {"Pending", "InProgress", "Delayed", "Cancelling"}:
            sleep(_POLL_INTERVAL_SECONDS)
            continue
        if status in {"Success", "Cancelled", "TimedOut", "Failed"}:
            return _TerminalInvocation(
                status=status,
                response_code=invocation.get("ResponseCode"),
                standard_output=invocation.get("StandardOutputContent"),
                standard_error=invocation.get("StandardErrorContent"),
            )
        raise RegistryOperatorError("remote command returned an unknown status")
    raise RegistryOperatorError("remote command did not finish before the bounded deadline")


def _require_successful_terminal_invocation(invocation: _TerminalInvocation) -> None:
    if invocation.status != "Success":
        raise RegistryOperatorError("remote command failed safely")
    if (
        invocation.response_code != 0
        or not isinstance(invocation.standard_output, str)
        or invocation.standard_output.strip() != _SUCCESS_OUTPUT
        or not isinstance(invocation.standard_error, str)
        or invocation.standard_error.strip()
    ):
        raise RegistryOperatorError("remote command success contract is invalid")


def authorize_private_registry_once(
    command: _Command,
    *,
    token: SecretStr,
    http_client: httpx2.Client,
    aws_clients: _AwsClients,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Create one SecureString and delete it only after a validated successful command."""

    _verify_aws_identity(aws_clients.sts)
    _verify_github_identity(http_client, token)
    _verify_exact_manifest(http_client, token, command.image)
    payload = _credential_payload(image=command.image, token=token, now=now or datetime.now(UTC))
    parameter_created = False
    cleanup_safe = False
    try:
        creation = _put_parameter_once(aws_clients.ssm, payload=payload)
        parameter_created = True
        version = _parameter_version(creation)
        command_id = _send_command(aws_clients.ssm, command, parameter_version=version)
        terminal = _wait_for_terminal(
            aws_clients.ssm,
            command,
            command_id=command_id,
            sleep=sleep,
            monotonic=monotonic,
        )
        _require_successful_terminal_invocation(terminal)
        cleanup_safe = True
    except Exception as error:
        if parameter_created and not cleanup_safe:
            raise RegistryHandoffRetainedError(
                "temporary parameter retained because command state is unverified"
            ) from error
        raise
    finally:
        payload = "[cleared]"
        if parameter_created and cleanup_safe:
            try:
                aws_clients.ssm.delete_parameter(Name=A14B_REGISTRY_PARAMETER_NAME)
            except Exception as error:
                raise RegistryHandoffRetainedError(
                    "temporary parameter cleanup result is unverified"
                ) from error


def a14b_registry_operator_main(arguments: Sequence[str] | None = None) -> int:
    """Run the local one-use handoff without rendering any dependency detail."""

    try:
        command = _parse_command(sys.argv[1:] if arguments is None else arguments)
        aws_clients = _build_aws_clients()
        _verify_aws_identity(aws_clients.sts)
        token = _capture_gh_token()
        with httpx2.Client(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx2.Timeout(10.0, connect=5.0),
        ) as http_client:
            authorize_private_registry_once(
                command,
                token=token,
                http_client=http_client,
                aws_clients=aws_clients,
            )
    except RegistryHandoffRetainedError:
        print(
            "A14B registry handoff or cleanup state is unverified. Inspect the fixed temporary "
            "parameter and command/provider state before manual cleanup or retry.",
            file=sys.stderr,
        )
        return 3
    except RegistryOperatorError:
        print("A14B registry authorization failed safely.", file=sys.stderr)
        return 2
    except Exception:
        print(
            "A14B registry authorization failed without exposing credentials or provider details.",
            file=sys.stderr,
        )
        return 1
    print("Exact private A14B registry authorization was consumed and deleted.")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the module contract
    raise SystemExit(a14b_registry_operator_main())
