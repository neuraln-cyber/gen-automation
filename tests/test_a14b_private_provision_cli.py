from __future__ import annotations

import asyncio
import io
import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from gen_automation import a14b_private_provision_cli as cli
from gen_automation.config import Settings
from gen_automation.db.models import ProviderBudgetGuard, SaladDeployment
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    BudgetState,
    DesiredDeploymentState,
    SaladDeploymentPurpose,
    SaladDeploymentState,
)
from gen_automation.domain.video import VideoContentRating
from gen_automation.integrations.salad.errors import SaladAPIError
from gen_automation.integrations.salad.models import (
    SaladContainerGroup,
    SaladContainerGroupState,
    SaladQueue,
)
from gen_automation.services import salad_deployments as deployment_service
from gen_automation.services import videos as video_service
from gen_automation.services.salad_deployments import DeploymentAction, DeploymentResult
from gen_automation.services.videos import CreateVideoSubmission, VideoQualityProfile

DEPLOYMENT_ID = UUID("d32be515-170f-416a-a356-3c70ef30db52")
GROUP_ID = UUID("f959d7cd-05d5-45fb-9a2a-2fca464c889d")
QUEUE_ID = UUID("aa834e62-4bc7-43c9-875a-20c62da68a37")
IMAGE = f"{cli.PRIVATE_IMAGE_REPOSITORY}@sha256:{'a' * 64}"
OTHER_IMAGE = f"{cli.PRIVATE_IMAGE_REPOSITORY}@sha256:{'b' * 64}"
USERNAME = "request-scoped-reader"
TOKEN = "ghp_request_scoped_secret_that_must_never_be_rendered"  # noqa: S105
NOW = datetime(2026, 8, 11, 14, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        salad_organization="organization",
        salad_project="project",
    )


def _deployment(
    *,
    state: SaladDeploymentState = SaladDeploymentState.PROVISIONING,
    provider_group_id: str | None = None,
) -> SaladDeployment:
    config_sha256 = "c" * 64
    return SaladDeployment(
        id=DEPLOYMENT_ID,
        version_no=44,
        config_sha256=config_sha256,
        runtime_artifact_manifest_sha256=None,
        runtime_managed_lora_sha256s=None,
        provider_configuration={
            "container": {
                "resources": {
                    "cpu": 4,
                    "memory": cli.EXPECTED_MEMORY_MB,
                    "storage_amount": cli.EXPECTED_STORAGE_BYTES,
                    "gpu_classes": [cli.RTX_5090_GPU_CLASS_ID],
                },
                "priority": "high",
                "image_caching": True,
            },
            "queue_connection": {},
            "queue_autoscaler": {"polling_period": 15},
            "runtime_bindings": [],
            "runtime_binding_contract_sha256": "f" * 64,
            "image_pull_mode": "ephemeral_basic",
        },
        worker_image_digest=IMAGE,
        organization_name="organization",
        project_name="project",
        queue_name=deployment_service.deterministic_provider_name(
            "video-queue",
            version_no=44,
            config_sha256=config_sha256,
        ),
        provider_queue_id=str(QUEUE_ID),
        container_group_name=deployment_service.deterministic_provider_name(
            "video-group",
            version_no=44,
            config_sha256=config_sha256,
        ),
        provider_container_group_id=provider_group_id,
        purpose=SaladDeploymentPurpose.VIDEO,
        state=state,
        desired_state=DesiredDeploymentState.ACTIVE,
        is_current=True,
        min_replicas=0,
        max_replicas=1,
        desired_queue_length=1,
        max_hourly_cost_microusd=cli.EXPECTED_MAX_HOURLY_COST_MICROUSD,
        created_at=NOW,
        updated_at=NOW,
        last_error_code=(
            "registry_authentication_required"
            if state == SaladDeploymentState.PROVISIONING
            else "group_create_transport_unknown"
        ),
    )


def _group(deployment: SaladDeployment) -> SaladContainerGroup:
    return SaladContainerGroup(
        id=GROUP_ID,
        name=deployment.container_group_name,
        display_name=deployment.container_group_name,
        replicas=0,
        pending_change=False,
        version=1,
        current_state=SaladContainerGroupState(
            status="stopped",
            description="stopped",
            allocating_count=0,
            creating_count=0,
            running_count=0,
            stopping_count=0,
            start_time=NOW,
            finish_time=NOW,
        ),
        create_time=NOW,
        update_time=NOW,
        raw={
            "container": {"image": deployment.worker_image_digest},
            "queue_connection": {"queue_name": deployment.queue_name},
        },
    )


class _Session:
    def __init__(self, deployment: SaladDeployment) -> None:
        self.deployment = deployment
        self.commits = 0

    async def scalar(self, _query: object) -> SaladDeployment:
        return self.deployment

    async def commit(self) -> None:
        self.commits += 1


class _AdvisoryGateSession(_Session):
    def __init__(
        self,
        deployment: SaladDeployment,
        *,
        gate: asyncio.Lock,
        owner: str,
        events: list[str],
    ) -> None:
        super().__init__(deployment)
        self._gate = gate
        self._owner = owner
        self._events = events
        self.lock_waiting = asyncio.Event()
        self.holds_advisory_gate = False

    def get_bind(self) -> object:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def execute(
        self,
        statement: object,
        _parameters: object | None = None,
    ) -> object:
        assert "pg_advisory_xact_lock" in str(statement)
        self._events.append(f"{self._owner}:gate_wait")
        self.lock_waiting.set()
        await self._gate.acquire()
        self.holds_advisory_gate = True
        self._events.append(f"{self._owner}:gate_acquired")
        return SimpleNamespace()

    async def commit(self) -> None:
        self.commits += 1
        self._events.append(f"{self._owner}:commit")
        self.release_advisory_gate()

    def release_advisory_gate(self) -> None:
        if self.holds_advisory_gate:
            self.holds_advisory_gate = False
            self._gate.release()


class _Client:
    def __init__(
        self,
        group: SaladContainerGroup | None,
    ) -> None:
        self.group = group
        self.get_calls: list[str] = []

    async def get_container_group(self, name: str) -> SaladContainerGroup:
        self.get_calls.append(name)
        if self.group is None:
            raise SaladAPIError(
                status_code=404,
                message="not found",
                response_body="",
                request_id=None,
            )
        return self.group


class _ProvisioningClient:
    def __init__(self, deployment: SaladDeployment) -> None:
        self.deployment = deployment
        self.queue = SaladQueue(
            id=QUEUE_ID,
            name=deployment.queue_name,
            display_name=deployment.queue_name,
            description=None,
            current_queue_length=0,
            container_groups=(),
            create_time=NOW,
            update_time=NOW,
        )
        self.group: SaladContainerGroup | None = None
        self.created_payloads: list[dict[str, Any]] = []

    async def get_queue(self, _name: str) -> SaladQueue:
        return self.queue

    async def get_container_group(self, _name: str) -> SaladContainerGroup:
        if self.group is None:
            raise SaladAPIError(
                status_code=404,
                message="not found",
                response_body="",
                request_id=None,
            )
        return self.group

    async def create_container_group(self, configuration: dict[str, Any]) -> SaladContainerGroup:
        self.created_payloads.append(configuration)
        raw = deepcopy(configuration)
        container = raw.get("container")
        if isinstance(container, dict):
            container.pop("registry_authentication", None)
        self.group = SaladContainerGroup(
            id=GROUP_ID,
            name=self.deployment.container_group_name,
            display_name=self.deployment.container_group_name,
            replicas=0,
            pending_change=False,
            version=1,
            current_state=SaladContainerGroupState(
                status="stopped",
                description="stopped",
                allocating_count=0,
                creating_count=0,
                running_count=0,
                stopping_count=0,
                start_time=None,
                finish_time=NOW,
            ),
            create_time=NOW,
            update_time=NOW,
            raw=raw,
        )
        return self.group


async def _no_database_check(_session: object) -> None:
    return None


@pytest.fixture(autouse=True)
def _isolated_operator_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    async def admission(_session: object) -> bool:
        return True

    async def budget(_session: object) -> bool:
        return True

    async def deployment(session: _Session, _deployment_id: UUID) -> SaladDeployment:
        return session.deployment

    monkeypatch.setattr(cli, "_group_configuration_drift", lambda _deployment, _group: None)
    monkeypatch.setattr(cli, "acquire_a14b_submission_lock", admission)
    monkeypatch.setattr(cli, "_lock_budget_guard", budget)
    monkeypatch.setattr(cli, "_load_deployment_locked", deployment)


def _credentials() -> cli._Credentials:
    return cli._Credentials(username=USERNAME, password=SecretStr(TOKEN))


def test_cli_never_echoes_stdin_or_accidental_argv_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail_with_secret(_command: object, _credentials: object) -> str:
        raise RuntimeError(f"backend echoed {USERNAME}:{TOKEN}")

    monkeypatch.setattr(cli, "_execute", fail_with_secret)
    status = cli.a14b_private_provision_main(
        [
            "provision",
            "--deployment-id",
            str(DEPLOYMENT_ID),
            "--image",
            IMAGE,
            "--credential-stdin",
        ],
        input_stream=io.StringIO(f"{USERNAME}\n{TOKEN}\n"),
    )
    output = capsys.readouterr()

    assert status == 1
    assert USERNAME not in output.out + output.err
    assert TOKEN not in output.out + output.err

    status = cli.a14b_private_provision_main(
        [
            "provision",
            "--deployment-id",
            str(DEPLOYMENT_ID),
            "--image",
            IMAGE,
            "--credential-stdin",
            "--token",
            TOKEN,
        ],
        input_stream=io.StringIO(""),
    )
    output = capsys.readouterr()
    assert status == 2
    assert TOKEN not in output.out + output.err


class _ParameterClient:
    def __init__(self, parameter: dict[str, object]) -> None:
        self.parameter = parameter
        self.calls: list[dict[str, object]] = []

    def get_parameter(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"Parameter": self.parameter}


def _parameter_payload(
    *,
    image: str = IMAGE,
    username: str = cli.A14B_REGISTRY_USERNAME,
    expires: str = "2026-08-11T14:09:00Z",
) -> str:
    return json.dumps(
        {
            "schema": cli.A14B_REGISTRY_PARAMETER_SCHEMA,
            "image": image,
            "user": username,
            "pass": TOKEN,
            "expires": expires,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _parameter(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "Name": cli.A14B_REGISTRY_PARAMETER_NAME,
        "ARN": cli.A14B_REGISTRY_PARAMETER_ARN,
        "Type": "SecureString",
        "DataType": "text",
        "Version": 7,
        "Selector": ":7",
        "LastModifiedDate": NOW,
        "Value": _parameter_payload(),
    }
    value.update(updates)
    return value


def test_provision_cli_requires_exactly_one_explicit_credential_source() -> None:
    common = ["provision", "--deployment-id", str(DEPLOYMENT_ID), "--image", IMAGE]
    with pytest.raises(cli.OperatorInputError):
        cli._parse_command(common)
    with pytest.raises(cli.OperatorInputError):
        cli._parse_command(
            [
                *common,
                "--credential-stdin",
                "--ssm-parameter-name",
                cli.A14B_REGISTRY_PARAMETER_NAME,
                "--ssm-parameter-version",
                "7",
            ]
        )
    command = cli._parse_command(
        [
            *common,
            "--ssm-parameter-name",
            cli.A14B_REGISTRY_PARAMETER_NAME,
            "--ssm-parameter-version",
            "7",
        ]
    )
    assert command.credential_source == cli._CredentialSource.SSM_PARAMETER
    assert command.ssm_parameter_version == 7
    with pytest.raises(cli.OperatorInputError):
        cli._parse_command(
            [
                *common,
                "--ssm-parameter-name",
                f"{cli.A14B_REGISTRY_PARAMETER_NAME}-other",
                "--ssm-parameter-version",
                "7",
            ]
        )


def test_ssm_loader_binds_metadata_version_payload_ttl_and_secret() -> None:
    client = _ParameterClient(_parameter())
    credentials = cli._load_ssm_credentials(
        client=client,
        parameter_name=cli.A14B_REGISTRY_PARAMETER_NAME,
        parameter_version=7,
        image=IMAGE,
        now=NOW,
    )
    assert credentials.username == cli.A14B_REGISTRY_USERNAME
    assert credentials.password.get_secret_value() == TOKEN
    assert client.calls == [
        {"Name": f"{cli.A14B_REGISTRY_PARAMETER_NAME}:7", "WithDecryption": True}
    ]


@pytest.mark.parametrize(
    "parameter",
    [
        _parameter(ARN="arn:aws:ssm:eu-central-1:861912887470:parameter/other"),
        _parameter(Version=8),
        _parameter(Type="String"),
        _parameter(LastModifiedDate=datetime(2026, 8, 11, 13, 49, tzinfo=UTC)),
        _parameter(Value=_parameter_payload(image=OTHER_IMAGE)),
        _parameter(Value=_parameter_payload(username="different-user")),
        _parameter(Value=_parameter_payload(expires="2026-08-11T14:11:00Z")),
        _parameter(Value=_parameter_payload(expires="2026-08-11T13:59:59Z")),
        _parameter(
            LastModifiedDate=datetime(2026, 8, 11, 13, 55, tzinfo=UTC),
            Value=_parameter_payload(expires="2026-08-11T14:09:00Z"),
        ),
        _parameter(
            Value=(
                '{"expires":"2026-08-11T14:09:00Z","image":"'
                + IMAGE
                + '","pass":"first","pass":"second","schema":"'
                + cli.A14B_REGISTRY_PARAMETER_SCHEMA
                + '","user":"neuraln-cyber"}'
            )
        ),
    ],
)
def test_ssm_loader_rejects_stale_mismatched_or_ambiguous_values(
    parameter: dict[str, object],
) -> None:
    with pytest.raises(cli.OperatorInputError):
        cli._load_ssm_credentials(
            client=_ParameterClient(parameter),
            parameter_name=cli.A14B_REGISTRY_PARAMETER_NAME,
            parameter_version=7,
            image=IMAGE,
            now=NOW,
        )


def test_ssm_cli_does_not_read_stdin_and_redacts_dependency_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _Unreadable(io.StringIO):
        def read(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("SSM source must not read stdin")

    async def fail_with_secret(_command: object, credentials: object) -> str:
        assert isinstance(credentials, cli._Credentials)
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(cli, "_bounded_ssm_client", lambda: _ParameterClient(_parameter()))
    monkeypatch.setattr(
        cli,
        "_load_ssm_credentials",
        lambda **_kwargs: cli._Credentials(
            username=cli.A14B_REGISTRY_USERNAME,
            password=SecretStr(TOKEN),
        ),
    )
    monkeypatch.setattr(cli, "_execute", fail_with_secret)
    status = cli.a14b_private_provision_main(
        [
            "provision",
            "--deployment-id",
            str(DEPLOYMENT_ID),
            "--image",
            IMAGE,
            "--ssm-parameter-name",
            cli.A14B_REGISTRY_PARAMETER_NAME,
            "--ssm-parameter-version",
            "7",
        ],
        input_stream=_Unreadable(),
    )
    output = capsys.readouterr()
    assert status == 1
    assert TOKEN not in output.out + output.err


@pytest.mark.asyncio
async def test_one_shot_provision_is_exact_deployment_digest_and_auth_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment()
    session = _Session(deployment)
    client = _Client(_group(deployment))
    calls: list[dict[str, Any]] = []
    order: list[str] = []

    async def admission(_session: object) -> bool:
        order.append("admission")
        return True

    async def budget(_session: object) -> bool:
        order.append("budget")
        return True

    async def load(_session: object, deployment_id: UUID) -> SaladDeployment:
        order.append("deployment")
        assert deployment_id == DEPLOYMENT_ID
        return deployment

    async def drain(_session: object) -> None:
        order.append("drain")

    async def provision(_session: object, **kwargs: Any) -> DeploymentResult:
        order.append("provision")
        calls.append(kwargs)
        auth = kwargs["registry_basic_auth"]
        assert auth.image_digest == IMAGE
        assert auth.username == USERNAME
        assert auth.password.get_secret_value() == TOKEN
        return DeploymentResult(
            deployment_id=DEPLOYMENT_ID,
            action=DeploymentAction.GROUP_CREATED,
            state=SaladDeploymentState.PROVISIONING,
            provider_queue_id=str(QUEUE_ID),
            provider_container_group_id=str(GROUP_ID),
        )

    monkeypatch.setattr(cli, "_migration_head", _no_database_check)
    monkeypatch.setattr(cli, "acquire_a14b_submission_lock", admission)
    monkeypatch.setattr(cli, "_lock_budget_guard", budget)
    monkeypatch.setattr(cli, "_load_deployment_locked", load)
    monkeypatch.setattr(cli, "_assert_database_drained", drain)
    monkeypatch.setattr(cli, "provision_deployment_step", provision)

    result = await cli.provision_private_group_once(
        session,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        _settings(),
        deployment_id=DEPLOYMENT_ID,
        image=IMAGE,
        credentials=_credentials(),
        secret_resolver=None,
    )

    assert result == cli.ProvisionStatus.CREATED
    assert len(calls) == 1
    assert calls[0]["deployment_id"] == DEPLOYMENT_ID
    assert session.commits == 1
    assert client.get_calls == [deployment.container_group_name]
    assert order == ["admission", "budget", "deployment", "drain", "provision"]


@pytest.mark.asyncio
async def test_real_service_integration_reuses_canonical_lock_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'operator-locks.db').as_posix()}")
    await database.create_schema()
    deployment = _deployment()
    provider = _ProvisioningClient(deployment)
    order: list[str] = []
    real_admission = video_service.acquire_a14b_submission_lock
    real_budget = deployment_service._lock_budget_guard
    real_deployment = deployment_service._load_deployment_locked
    real_provision = cli.provision_deployment_step
    service_results: list[DeploymentResult] = []

    async def admission(session: Any) -> bool:
        order.append("admission")
        await real_admission(session)
        # SQLite cannot emulate a PostgreSQL transaction advisory lock. The
        # real-service portion of this test exercises the canonical budget and
        # deployment row locks; fail-closed dialect behavior is covered below.
        return True

    async def budget(session: Any) -> bool:
        order.append("budget")
        return await real_budget(session)

    async def load(session: Any, deployment_id: UUID) -> SaladDeployment:
        order.append("deployment")
        return await real_deployment(session, deployment_id)

    async def provision(session: Any, **kwargs: Any) -> DeploymentResult:
        result = await real_provision(session, **kwargs)
        service_results.append(result)
        return result

    monkeypatch.setattr(cli, "_migration_head", _no_database_check)
    monkeypatch.setattr(cli, "acquire_a14b_submission_lock", admission)
    monkeypatch.setattr(cli, "_lock_budget_guard", budget)
    monkeypatch.setattr(cli, "_load_deployment_locked", load)
    monkeypatch.setattr(cli, "provision_deployment_step", provision)
    monkeypatch.setattr(deployment_service, "_lock_budget_guard", budget)
    monkeypatch.setattr(deployment_service, "_load_deployment_locked", load)
    try:
        async with database.sessions() as session:
            session.add(
                ProviderBudgetGuard(
                    id=uuid4(),
                    provider="salad",
                    currency="USD",
                    daily_limit_microusd=5_000_000,
                    monthly_limit_microusd=25_000_000,
                    state=BudgetState.OPEN,
                    lock_version=1,
                    updated_at=NOW,
                )
            )
            session.add(deployment)
            await session.commit()

            result = await cli.provision_private_group_once(
                session,
                provider,  # type: ignore[arg-type]
                _settings(),
                deployment_id=DEPLOYMENT_ID,
                image=IMAGE,
                credentials=_credentials(),
                secret_resolver=None,
            )
            await session.refresh(deployment)
    finally:
        await database.dispose()

    assert result == cli.ProvisionStatus.CREATED
    assert [item.action for item in service_results] == [DeploymentAction.GROUP_CREATED]
    assert order == ["admission", "budget", "deployment", "budget", "deployment"]
    assert deployment.provider_container_group_id == str(GROUP_ID)
    assert len(provider.created_payloads) == 1
    assert TOKEN not in repr(deployment.provider_configuration)


@pytest.mark.asyncio
async def test_private_operator_fails_closed_without_transaction_advisory_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'operator-dialect.db').as_posix()}")
    deployment = _deployment()
    budget_called = False

    async def budget(_session: object) -> bool:
        nonlocal budget_called
        budget_called = True
        return True

    monkeypatch.setattr(
        cli, "acquire_a14b_submission_lock", video_service.acquire_a14b_submission_lock
    )
    monkeypatch.setattr(cli, "_lock_budget_guard", budget)
    try:
        async with database.sessions() as session:
            with pytest.raises(cli.OperatorInputError, match="admission lock"):
                await cli.provision_private_group_once(
                    session,
                    _Client(_group(deployment)),  # type: ignore[arg-type]
                    _settings(),
                    deployment_id=DEPLOYMENT_ID,
                    image=IMAGE,
                    credentials=_credentials(),
                    secret_resolver=None,
                )
    finally:
        await database.dispose()

    assert budget_called is False


@pytest.mark.asyncio
async def test_operator_gate_blocks_concurrent_a14b_submission_through_post_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment()
    gate = asyncio.Lock()
    events: list[str] = []
    operator_session = _AdvisoryGateSession(
        deployment,
        gate=gate,
        owner="operator",
        events=events,
    )
    submission_session = _AdvisoryGateSession(
        deployment,
        gate=gate,
        owner="submission",
        events=events,
    )
    post_entered = asyncio.Event()
    allow_post_to_finish = asyncio.Event()
    submission_passed_gate = asyncio.Event()

    class SubmissionReachedDurableWorkError(RuntimeError):
        pass

    async def budget(_session: object) -> bool:
        assert operator_session.holds_advisory_gate
        events.append("operator:budget")
        return True

    async def load(_session: object, deployment_id: UUID) -> SaladDeployment:
        assert deployment_id == DEPLOYMENT_ID
        assert operator_session.holds_advisory_gate
        events.append("operator:deployment")
        return deployment

    async def drain(_session: object) -> None:
        assert operator_session.holds_advisory_gate
        events.append("operator:final_drain")

    async def provision(_session: object, **_kwargs: object) -> DeploymentResult:
        assert operator_session.holds_advisory_gate
        events.append("operator:provider_post")
        post_entered.set()
        await allow_post_to_finish.wait()
        assert operator_session.holds_advisory_gate
        events.append("operator:provider_post_returned")
        return DeploymentResult(
            deployment_id=DEPLOYMENT_ID,
            action=DeploymentAction.GROUP_CREATED,
            state=SaladDeploymentState.PROVISIONING,
            provider_queue_id=str(QUEUE_ID),
            provider_container_group_id=str(GROUP_ID),
        )

    async def require_actor(*_args: object, **_kwargs: object) -> object:
        events.append("submission:durable_work")
        submission_passed_gate.set()
        raise SubmissionReachedDurableWorkError

    monkeypatch.setattr(cli, "_migration_head", _no_database_check)
    monkeypatch.setattr(
        cli, "acquire_a14b_submission_lock", video_service.acquire_a14b_submission_lock
    )
    monkeypatch.setattr(cli, "_lock_budget_guard", budget)
    monkeypatch.setattr(cli, "_load_deployment_locked", load)
    monkeypatch.setattr(cli, "_assert_database_drained", drain)
    monkeypatch.setattr(cli, "provision_deployment_step", provision)
    monkeypatch.setattr(video_service, "_require_actor", require_actor)

    operator_task = asyncio.create_task(
        cli.provision_private_group_once(
            operator_session,  # type: ignore[arg-type]
            _Client(_group(deployment)),  # type: ignore[arg-type]
            _settings(),
            deployment_id=DEPLOYMENT_ID,
            image=IMAGE,
            credentials=_credentials(),
            secret_resolver=None,
        )
    )
    await asyncio.wait_for(post_entered.wait(), timeout=1)
    assert operator_session.holds_advisory_gate

    command = CreateVideoSubmission(
        submission_id=uuid4(),
        source_asset_id=uuid4(),
        prompt="subtle natural motion",
        content_rating=VideoContentRating.SFW,
        duration_seconds=5,
        variant_count=1,
        source_rights_confirmed=True,
        lawful_use_confirmed=True,
        quality_profile=VideoQualityProfile.SMOOTHMIX_A14B_Q3,
    )
    submission_task = asyncio.create_task(
        video_service.create_video_submission(
            submission_session,  # type: ignore[arg-type]
            command=command,
            actor_user_id=uuid4(),
            max_hourly_cost_usd=Decimal("0.50"),
            runtime_worker_image_digest=IMAGE,
        )
    )
    await asyncio.wait_for(submission_session.lock_waiting.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not submission_task.done()
    assert not submission_passed_gate.is_set()
    assert events[:6] == [
        "operator:gate_wait",
        "operator:gate_acquired",
        "operator:budget",
        "operator:deployment",
        "operator:final_drain",
        "operator:provider_post",
    ]

    allow_post_to_finish.set()
    assert await operator_task == cli.ProvisionStatus.CREATED
    await asyncio.wait_for(submission_passed_gate.wait(), timeout=1)
    with pytest.raises(SubmissionReachedDurableWorkError):
        await submission_task
    submission_session.release_advisory_gate()

    assert operator_session.commits == 1
    assert events.index("operator:provider_post_returned") < events.index("operator:commit")
    assert events.index("operator:commit") < events.index("submission:gate_acquired")
    assert events.index("submission:gate_acquired") < events.index("submission:durable_work")


@pytest.mark.asyncio
async def test_wrong_digest_fails_before_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment()
    session = _Session(deployment)
    client = _Client(_group(deployment))
    called = False

    async def provision(*_args: object, **_kwargs: object) -> DeploymentResult:
        nonlocal called
        called = True
        raise AssertionError("must not provision")

    monkeypatch.setattr(cli, "_migration_head", _no_database_check)
    monkeypatch.setattr(cli, "_assert_database_drained", _no_database_check)
    monkeypatch.setattr(cli, "provision_deployment_step", provision)

    with pytest.raises(cli.OperatorInputError, match="binding"):
        await cli.provision_private_group_once(
            session,  # type: ignore[arg-type]
            client,  # type: ignore[arg-type]
            _settings(),
            deployment_id=DEPLOYMENT_ID,
            image=OTHER_IMAGE,
            credentials=_credentials(),
            secret_resolver=None,
        )

    assert called is False
    assert session.commits == 0
    assert client.get_calls == []


@pytest.mark.asyncio
async def test_unknown_replay_uses_deterministic_readback_without_second_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(state=SaladDeploymentState.UNKNOWN)
    session = _Session(deployment)
    client = _Client(_group(deployment))

    async def forbidden(*_args: object, **_kwargs: object) -> DeploymentResult:
        raise AssertionError("ambiguous POST must never be repeated")

    monkeypatch.setattr(cli, "_migration_head", _no_database_check)
    monkeypatch.setattr(cli, "_assert_database_drained", _no_database_check)
    monkeypatch.setattr(cli, "provision_deployment_step", forbidden)

    result = await cli.provision_private_group_once(
        session,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        _settings(),
        deployment_id=DEPLOYMENT_ID,
        image=IMAGE,
        credentials=_credentials(),
        secret_resolver=None,
    )

    assert result == cli.ProvisionStatus.AMBIGUOUS_FOUND
    assert session.commits == 0
    assert client.get_calls == [deployment.container_group_name]


@pytest.mark.asyncio
async def test_ambiguous_readback_rejects_same_name_with_configuration_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(state=SaladDeploymentState.UNKNOWN)
    session = _Session(deployment)
    client = _Client(_group(deployment))
    monkeypatch.setattr(cli, "_migration_head", _no_database_check)
    monkeypatch.setattr(cli, "_assert_database_drained", _no_database_check)
    monkeypatch.setattr(
        cli,
        "_group_configuration_drift",
        lambda _deployment, _group: "provider_priority_drift",
    )

    with pytest.raises(cli.OperatorInputError, match="exact target"):
        await cli.provision_private_group_once(
            session,  # type: ignore[arg-type]
            client,  # type: ignore[arg-type]
            _settings(),
            deployment_id=DEPLOYMENT_ID,
            image=IMAGE,
            credentials=_credentials(),
            secret_resolver=None,
        )

    assert session.commits == 0
    assert client.get_calls == [deployment.container_group_name]


@pytest.mark.asyncio
async def test_ambiguous_post_is_committed_then_read_once_and_not_repeated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment()
    session = _Session(deployment)
    client = _Client(None)
    call_count = 0

    async def ambiguous(_session: object, **_kwargs: object) -> DeploymentResult:
        nonlocal call_count
        call_count += 1
        return DeploymentResult(
            deployment_id=DEPLOYMENT_ID,
            action=DeploymentAction.DEFERRED,
            state=SaladDeploymentState.UNKNOWN,
            provider_queue_id=str(QUEUE_ID),
            provider_container_group_id=None,
            error_code="group_create_transport_unknown",
        )

    monkeypatch.setattr(cli, "_migration_head", _no_database_check)
    monkeypatch.setattr(cli, "_assert_database_drained", _no_database_check)
    monkeypatch.setattr(cli, "provision_deployment_step", ambiguous)

    result = await cli.provision_private_group_once(
        session,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        _settings(),
        deployment_id=DEPLOYMENT_ID,
        image=IMAGE,
        credentials=_credentials(),
        secret_resolver=None,
    )

    assert result == cli.ProvisionStatus.AMBIGUOUS_NOT_FOUND
    assert call_count == 1
    assert session.commits == 1
    assert client.get_calls == [deployment.container_group_name]


def test_credentials_are_stdin_only_and_bounded() -> None:
    credentials = cli._read_credentials(io.StringIO(f"{USERNAME}\r\n{TOKEN}\r\n"))
    assert credentials.username == USERNAME
    assert credentials.password.get_secret_value() == TOKEN
    with pytest.raises(cli.OperatorInputError):
        cli._read_credentials(io.StringIO(f"{USERNAME}\n{TOKEN}\nextra\n"))
    with pytest.raises(cli.OperatorInputError):
        cli._read_credentials(io.StringIO(f"{USERNAME}\n{'x' * 1025}\n"))


@pytest.mark.asyncio
async def test_existing_exact_group_is_a_read_only_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(provider_group_id=str(GROUP_ID))
    group = _group(deployment)
    monkeypatch.setattr(cli, "_migration_head", _no_database_check)
    monkeypatch.setattr(cli, "_assert_database_drained", _no_database_check)

    result = await cli.provision_private_group_once(
        _Session(deployment),  # type: ignore[arg-type]
        _Client(group),  # type: ignore[arg-type]
        _settings(),
        deployment_id=DEPLOYMENT_ID,
        image=IMAGE,
        credentials=_credentials(),
        secret_resolver=None,
    )

    assert result == cli.ProvisionStatus.ALREADY_PROVISIONED
