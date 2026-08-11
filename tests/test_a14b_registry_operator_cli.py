from __future__ import annotations

import base64
import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from gen_automation import a14b_registry_operator_cli as operator

TOKEN = "ghp_one_use_registry_secret_never_render"  # noqa: S105
REGISTRY_TOKEN = "short_lived_registry_bearer"  # noqa: S105
MANIFEST = b'{"schemaVersion":2,"manifests":[]}'
DIGEST = "sha256:" + __import__("hashlib").sha256(MANIFEST).hexdigest()
IMAGE = f"{operator.PRIVATE_IMAGE_REPOSITORY}@{DIGEST}"
DEPLOYMENT_ID = UUID("d32be515-170f-416a-a356-3c70ef30db52")
INSTANCE_ID = "i-0123456789abcdef0"
COMMAND_ID = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
OPERATOR_ARN = (
    "arn:aws:sts::861912887470:assumed-role/"
    "AWSReservedSSO_GenAutomationStagingDeployer_0123456789ABCDEF/operator"
)


class _STS:
    def __init__(self, *, account: str = operator.AWS_ACCOUNT_ID, arn: str = OPERATOR_ARN) -> None:
        self.account = account
        self.arn = arn

    def get_caller_identity(self) -> dict[str, object]:
        return {"Account": self.account, "Arn": self.arn, "UserId": "public-user-id"}


class _SSM:
    def __init__(self, statuses: list[str] | None = None) -> None:
        self.statuses = list(statuses or ["Pending", "Success"])
        self.events: list[str] = []
        self.put: dict[str, object] | None = None
        self.sent: dict[str, object] | None = None
        self.deleted: dict[str, object] | None = None

    def put_parameter(self, **kwargs: object) -> dict[str, object]:
        self.events.append("put")
        self.put = kwargs
        return {"Version": 4, "Tier": "Standard"}

    def send_command(self, **kwargs: object) -> dict[str, object]:
        self.events.append("send")
        self.sent = kwargs
        return {"Command": {"CommandId": COMMAND_ID}}

    def get_command_invocation(self, **kwargs: object) -> dict[str, object]:
        self.events.append("poll")
        status = self.statuses.pop(0)
        return {
            "CommandId": kwargs["CommandId"],
            "InstanceId": kwargs["InstanceId"],
            "Status": status,
            "ResponseCode": 0 if status == "Success" else -1,
            "StandardOutputContent": operator._SUCCESS_OUTPUT if status == "Success" else "",
            "StandardErrorContent": "",
        }

    def delete_parameter(self, **kwargs: object) -> dict[str, object]:
        self.events.append("delete")
        self.deleted = kwargs
        return {}


def _command() -> operator._Command:
    return operator._Command(
        image=IMAGE,
        deployment_id=DEPLOYMENT_ID,
        instance_id=INSTANCE_ID,
    )


def _transport(
    *,
    login: str = operator.A14B_REGISTRY_USERNAME,
    scopes: str = "repo, read:packages",
    manifest_digest: str = DIGEST,
) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == operator.GITHUB_API_URL:
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            return httpx2.Response(
                200,
                request=request,
                headers={"X-OAuth-Scopes": scopes},
                json={"login": login},
            )
        if request.url.path == "/token":
            assert request.url.host == "ghcr.io"
            assert request.url.params.get("service") == "ghcr.io"
            assert request.url.params.get("scope") == operator.GHCR_REPOSITORY_SCOPE
            expected_basic = base64.b64encode(
                f"{operator.A14B_REGISTRY_USERNAME}:{TOKEN}".encode("ascii")
            ).decode("ascii")
            assert request.headers["authorization"] == f"Basic {expected_basic}"
            return httpx2.Response(
                200,
                request=request,
                headers={"Content-Type": "application/json"},
                json={"token": REGISTRY_TOKEN},
            )
        assert str(request.url) == f"{operator.GHCR_MANIFEST_PREFIX}{DIGEST}"
        assert request.headers["accept-encoding"] == "identity"
        if "authorization" not in request.headers:
            return httpx2.Response(
                401,
                request=request,
                headers={"WWW-Authenticate": operator._GHCR_CHALLENGE},
            )
        assert request.headers["authorization"] == f"Bearer {REGISTRY_TOKEN}"
        return httpx2.Response(
            200,
            request=request,
            headers={
                "Content-Type": "application/vnd.oci.image.index.v1+json",
                "Docker-Content-Digest": manifest_digest,
            },
            content=MANIFEST,
        )

    return httpx2.MockTransport(handler)


def _authorize(
    ssm: _SSM,
    *,
    transport: httpx2.MockTransport | None = None,
    sts: _STS | None = None,
    monotonic: Callable[[], float] | None = None,
) -> None:
    with httpx2.Client(
        transport=transport or _transport(),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        operator.authorize_private_registry_once(
            _command(),
            token=SecretStr(TOKEN),
            http_client=client,
            aws_clients=operator._AwsClients(sts=sts or _STS(), ssm=ssm),
            now=NOW,
            sleep=lambda _seconds: None,
            monotonic=monotonic or (lambda: 0.0),
        )


def test_gh_token_capture_uses_fixed_non_shell_pipe_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "gh.exe"
    executable.write_bytes(b"reviewed executable fixture")
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", TOKEN)

    def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert arguments == [str(executable), "auth", "token", "--hostname", "github.com"]
        child_environment = kwargs.pop("env")
        assert isinstance(child_environment, dict)
        assert "GH_TOKEN" not in child_environment
        assert "GITHUB_TOKEN" not in child_environment
        assert "GH_ENTERPRISE_TOKEN" not in child_environment
        assert kwargs == {
            "shell": False,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "check": False,
            "timeout": 10,
        }
        assert TOKEN not in arguments
        return subprocess.CompletedProcess(arguments, 0, f"{TOKEN}\r\n".encode("ascii"), b"")

    monkeypatch.setattr(operator.shutil, "which", lambda _name: str(executable))
    monkeypatch.setattr(operator.subprocess, "run", run)
    token = operator._capture_gh_token()
    assert token.get_secret_value() == TOKEN
    assert TOKEN not in repr(token)


def test_one_use_handoff_binds_scope_manifest_payload_command_and_cleanup_order() -> None:
    ssm = _SSM()
    _authorize(ssm)
    assert ssm.events == ["put", "send", "poll", "poll", "delete"]
    assert ssm.put is not None
    assert ssm.put["Name"] == operator.A14B_REGISTRY_PARAMETER_NAME
    assert ssm.put["Type"] == "SecureString"
    assert ssm.put["Overwrite"] is False
    assert ssm.put["Tier"] == "Standard"
    payload = json.loads(str(ssm.put["Value"]))
    assert set(payload) == {"schema", "image", "user", "pass", "expires"}
    assert payload == {
        "schema": operator.A14B_REGISTRY_PARAMETER_SCHEMA,
        "image": IMAGE,
        "user": operator.A14B_REGISTRY_USERNAME,
        "pass": TOKEN,
        "expires": "2026-08-12T12:10:00Z",
    }
    assert ssm.sent is not None
    assert ssm.sent["InstanceIds"] == [INSTANCE_ID]
    parameters = ssm.sent["Parameters"]
    assert isinstance(parameters, dict)
    remote = str(parameters["commands"][0])
    assert operator.A14B_REGISTRY_PARAMETER_NAME in remote
    assert "--ssm-parameter-version 4" in remote
    assert str(DEPLOYMENT_ID) in remote
    assert IMAGE in remote
    assert TOKEN not in remote
    assert "--credential-stdin" not in remote
    assert "Authorization" not in remote
    assert ssm.deleted == {"Name": operator.A14B_REGISTRY_PARAMETER_NAME}


@pytest.mark.parametrize(
    ("transport", "sts"),
    [
        (_transport(login="other-user"), _STS()),
        (_transport(scopes="repo"), _STS()),
        (_transport(manifest_digest="sha256:" + "0" * 64), _STS()),
        (_transport(), _STS(account="000000000000")),
    ],
)
def test_identity_scope_manifest_and_aws_mismatches_fail_before_parameter_write(
    transport: httpx2.MockTransport,
    sts: _STS,
) -> None:
    ssm = _SSM()
    with pytest.raises(operator.RegistryOperatorError):
        _authorize(ssm, transport=transport, sts=sts)
    assert ssm.events == []


def test_stale_existing_parameter_is_not_overwritten_deleted_or_sent() -> None:
    class _Existing(_SSM):
        def put_parameter(self, **kwargs: object) -> dict[str, object]:
            self.events.append("put")
            self.put = kwargs
            raise ClientError(
                {"Error": {"Code": "ParameterAlreadyExists", "Message": TOKEN}},
                "PutParameter",
            )

    ssm = _Existing()
    with pytest.raises(ClientError):
        _authorize(ssm)
    assert ssm.events == ["put"]
    assert ssm.put is not None and ssm.put["Overwrite"] is False
    assert ssm.deleted is None
    assert ssm.sent is None


@pytest.mark.parametrize("status", ["Failed", "Cancelled", "TimedOut"])
def test_non_success_terminal_status_retains_parameter(status: str) -> None:
    ssm = _SSM(statuses=[status])
    with pytest.raises(operator.RegistryHandoffRetainedError):
        _authorize(ssm)
    assert ssm.events == ["put", "send", "poll"]
    assert ssm.deleted is None


def test_accepted_but_lost_or_malformed_send_response_retains_parameter() -> None:
    class _AcceptedButLost(_SSM):
        def send_command(self, **kwargs: object) -> dict[str, object]:
            self.events.append("send")
            self.sent = kwargs
            raise ConnectionError(TOKEN)

    class _MalformedResponse(_SSM):
        def send_command(self, **kwargs: object) -> dict[str, object]:
            self.events.append("send")
            self.sent = kwargs
            return {"Command": {"CommandId": "malformed"}}

    for ssm in (_AcceptedButLost(), _MalformedResponse()):
        with pytest.raises(operator.RegistryHandoffRetainedError) as captured:
            _authorize(ssm)
        assert TOKEN not in str(captured.value)
        assert ssm.events == ["put", "send"]
        assert ssm.deleted is None


def test_transient_poll_error_retains_parameter_without_retry_or_delete() -> None:
    class _TransientPollFailure(_SSM):
        def get_command_invocation(self, **_kwargs: object) -> dict[str, object]:
            self.events.append("poll")
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": TOKEN}},
                "GetCommandInvocation",
            )

    ssm = _TransientPollFailure()
    with pytest.raises(operator.RegistryHandoffRetainedError) as captured:
        _authorize(ssm)
    assert TOKEN not in str(captured.value)
    assert ssm.events == ["put", "send", "poll"]
    assert ssm.deleted is None


def test_poll_deadline_retains_parameter_until_manual_resolution() -> None:
    class _Pending(_SSM):
        def get_command_invocation(self, **kwargs: object) -> dict[str, object]:
            self.events.append("poll")
            return {
                "CommandId": kwargs["CommandId"],
                "InstanceId": kwargs["InstanceId"],
                "Status": "Pending",
            }

    ticks = iter((0.0, 0.0, float(operator._POLL_DEADLINE_SECONDS + 1)))
    ssm = _Pending()
    with pytest.raises(operator.RegistryHandoffRetainedError):
        _authorize(ssm, monotonic=lambda: next(ticks))
    assert ssm.events == ["put", "send", "poll"]
    assert ssm.deleted is None


def test_bound_success_with_invalid_output_retains_parameter() -> None:
    class _InvalidSuccess(_SSM):
        def get_command_invocation(self, **kwargs: object) -> dict[str, object]:
            self.events.append("poll")
            return {
                "CommandId": kwargs["CommandId"],
                "InstanceId": kwargs["InstanceId"],
                "Status": "Success",
                "ResponseCode": 0,
                "StandardOutputContent": "unexpected output",
                "StandardErrorContent": "",
            }

    ssm = _InvalidSuccess()
    with pytest.raises(operator.RegistryHandoffRetainedError):
        _authorize(ssm)
    assert ssm.events == ["put", "send", "poll"]
    assert ssm.deleted is None


def test_main_gives_generic_manual_direction_for_retained_handoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def retained(*_args: object, **_kwargs: object) -> None:
        raise operator.RegistryHandoffRetainedError(TOKEN)

    monkeypatch.setattr(operator, "_capture_gh_token", lambda: SecretStr(TOKEN))
    monkeypatch.setattr(
        operator, "_build_aws_clients", lambda: operator._AwsClients(_STS(), _SSM())
    )
    monkeypatch.setattr(operator, "authorize_private_registry_once", retained)
    status = operator.a14b_registry_operator_main(
        [
            "--image",
            IMAGE,
            "--deployment-id",
            str(DEPLOYMENT_ID),
            "--instance-id",
            INSTANCE_ID,
        ]
    )
    output = capsys.readouterr()
    assert status == 3
    assert "Inspect the fixed temporary parameter" in output.err
    assert "manual cleanup or retry" in output.err
    assert TOKEN not in output.out + output.err
    assert IMAGE not in output.out + output.err


def test_main_rejects_wrong_aws_identity_before_reading_github_credential(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential_read = False

    def capture() -> SecretStr:
        nonlocal credential_read
        credential_read = True
        return SecretStr(TOKEN)

    monkeypatch.setattr(operator, "_capture_gh_token", capture)
    monkeypatch.setattr(
        operator,
        "_build_aws_clients",
        lambda: operator._AwsClients(_STS(account="000000000000"), _SSM()),
    )
    status = operator.a14b_registry_operator_main(
        [
            "--image",
            IMAGE,
            "--deployment-id",
            str(DEPLOYMENT_ID),
            "--instance-id",
            INSTANCE_ID,
        ]
    )
    output = capsys.readouterr()
    assert status == 2
    assert credential_read is False
    assert TOKEN not in output.out + output.err


def test_main_redacts_token_from_dependency_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(operator, "_capture_gh_token", lambda: SecretStr(TOKEN))
    monkeypatch.setattr(
        operator, "_build_aws_clients", lambda: (_ for _ in ()).throw(RuntimeError(TOKEN))
    )
    status = operator.a14b_registry_operator_main(
        [
            "--image",
            IMAGE,
            "--deployment-id",
            str(DEPLOYMENT_ID),
            "--instance-id",
            INSTANCE_ID,
        ]
    )
    output = capsys.readouterr()
    assert status == 1
    assert TOKEN not in output.out + output.err
    assert IMAGE not in output.out + output.err


def test_public_command_parser_rejects_noncanonical_or_unknown_values() -> None:
    valid = [
        "--image",
        IMAGE,
        "--deployment-id",
        str(DEPLOYMENT_ID),
        "--instance-id",
        INSTANCE_ID,
    ]
    assert operator._parse_command(valid) == _command()
    with pytest.raises(operator.RegistryOperatorError):
        operator._parse_command([*valid, "--token", TOKEN])
    with pytest.raises(operator.RegistryOperatorError):
        operator._parse_command(
            [
                "--image",
                IMAGE.replace("video-worker-a14b-private", "video-worker"),
                "--deployment-id",
                str(DEPLOYMENT_ID),
                "--instance-id",
                INSTANCE_ID,
            ]
        )
