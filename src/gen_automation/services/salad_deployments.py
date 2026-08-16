from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    ProviderBudgetGuard,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.domain.enums import (
    BudgetState,
    DesiredDeploymentState,
    GenerationAttemptState,
    GenerationState,
    ReleasePhase,
    SaladDeploymentPurpose,
    SaladDeploymentState,
    SpendEntryType,
)
from gen_automation.domain.runtime_bindings import (
    SALAD_WORKER_ALLOWED_RUNTIME_BINDINGS,
    SALAD_WORKER_RUNTIME_BINDING_REFERENCES,
    WORKER_MODEL_MANIFEST_JSON_BINDING,
    WORKER_MODEL_MANIFEST_SHA256_BINDING,
)
from gen_automation.integrations.salad.errors import (
    SaladAPIError,
    SaladProtocolError,
    SaladRateLimitError,
    SaladTransportError,
)
from gen_automation.integrations.salad.models import (
    JSONObject,
    JSONValue,
    SaladContainerGroup,
    SaladContainerGroupInstance,
    SaladContainerGroupInstancePage,
    SaladContainerGroupInstanceState,
    SaladQueue,
)
from gen_automation.services.budgets import (
    BudgetError,
    record_spend_entry,
    reevaluate_budget_guard,
)
from gen_automation.services.experiment_warm_leases import (
    effective_experiment_min_replicas,
)
from gen_automation.services.runtime_secrets import (
    RuntimeSecretResolutionError,
    RuntimeSecretResolver,
)

_PROVIDER = "salad"
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
_IMAGE_DIGEST_PATTERN = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_PROVIDER_PROGRESS_PATTERN = re.compile(r"(?<!\d)(100|[0-9]{1,2})(?:\.[0-9]+)?\s*%")
_PROVIDER_PENDING_STALL_AFTER = timedelta(minutes=30)
_SAFE_PROVIDER_GROUP_STATUSES = frozenset(
    {
        "allocating",
        "creating",
        "deploying",
        "failed",
        "pending",
        "preparing",
        "running",
        "stopped",
        "stopping",
    }
)
_SAFE_PROVIDER_PREPARATION_PHASES = frozenset({"allocation", "image_pull", "preparing", "unknown"})
_RECONCILE_DELAY = timedelta(seconds=30)
_MAX_PROVIDER_NAME_LENGTH = 63
_MICROSECONDS_PER_HOUR = 3_600_000_000
_RUNTIME_BINDINGS_KEY = "runtime_bindings"
_WORKER_QUEUE_PATH = "/jobs/generate"
_WORKER_PORT = 8000
_WORKER_RESTART_POLICY = "on_failure"
_SALAD_DEFAULT_SHM_SIZE = 64
_RUNTIME_REFRESH_CONVERGENCE_TIMEOUT_SECONDS = 60.0
_RUNTIME_REFRESH_POLL_SECONDS = 1.0
_QUEUE_ADMISSION_CONVERGENCE_TIMEOUT_SECONDS = 120.0
_QUEUE_ADMISSION_POLL_SECONDS = 1.0
_WORKER_STARTUP_PROBE: JSONObject = {
    "http": {
        "headers": [],
        "path": "/health",
        "port": _WORKER_PORT,
        "scheme": "http",
    },
    "initial_delay_seconds": 0,
    "period_seconds": 5,
    "timeout_seconds": 5,
    "success_threshold": 1,
    # A bootstrap-only HTTP responder serves /health while model artifacts are
    # materialized. Poll quickly so a healthy allocation is observed promptly;
    # the threshold remains inside Salad's live API limits and now only bounds
    # failure to start the Python responder.
    "failure_threshold": 20,
}
_WORKER_READINESS_PROBE: JSONObject = {
    "http": {
        "headers": [],
        "path": "/ready",
        "port": _WORKER_PORT,
        "scheme": "http",
    },
    "initial_delay_seconds": 0,
    "period_seconds": 5,
    "timeout_seconds": 3,
    "success_threshold": 1,
    "failure_threshold": 3,
}
_WORKER_LIVENESS_PROBE: JSONObject = {
    "http": {
        "headers": [],
        "path": "/health",
        "port": _WORKER_PORT,
        "scheme": "http",
    },
    "initial_delay_seconds": 0,
    "period_seconds": 30,
    "timeout_seconds": 5,
    "success_threshold": 1,
    "failure_threshold": 3,
}
_WORKER_PROBE_CONTRACTS: Mapping[str, JSONObject] = {
    "startup_probe": _WORKER_STARTUP_PROBE,
    "readiness_probe": _WORKER_READINESS_PROBE,
    "liveness_probe": _WORKER_LIVENESS_PROBE,
}
_SENSITIVE_CONFIGURATION_MARKERS = frozenset(
    {
        "environment",
        "environmentvariables",
        "secrets",
        "credentials",
        "apikey",
        "accesskey",
        "privatekey",
        "password",
        "token",
    }
)
_GENERATION_ACTIVE_RELEASE_PHASES = (
    ReleasePhase.READY,
    ReleasePhase.GENERATING,
)
_GENERATION_ACTIVE_GPU_ATTEMPT_STATES = (
    GenerationAttemptState.SUBMITTED,
    GenerationAttemptState.RUNNING,
    GenerationAttemptState.UNKNOWN,
)
_GENERATION_ACTIVE_GPU_JOB_STATES = (
    GenerationState.RUNNING,
    GenerationState.UNKNOWN,
)
_GENERATION_STOP_REQUESTED_ACTION = "release.generation_stop_requested"


class SaladDeploymentError(Exception):
    """Base class for fail-closed deployment-controller errors."""


class SaladDeploymentValidationError(SaladDeploymentError):
    """The durable deployment configuration cannot safely be provisioned."""


class SaladDeploymentNotFoundError(SaladDeploymentError):
    """The requested deployment does not exist."""


class DeploymentAction(StrEnum):
    QUEUE_CREATED = "queue_created"
    QUEUE_ADOPTED = "queue_adopted"
    GROUP_CREATED = "group_created"
    GROUP_ADOPTED = "group_adopted"
    AUTOSCALER_REPAIR_REQUESTED = "autoscaler_repair_requested"
    START_REQUESTED = "start_requested"
    RECONCILED = "reconciled"
    STOP_REQUESTED = "stop_requested"
    STOPPED = "stopped"
    DEFERRED = "deferred"
    FAILED = "failed"
    BUDGET_BLOCKED = "budget_blocked"


@dataclass(frozen=True)
class DeploymentResult:
    deployment_id: UUID
    action: DeploymentAction
    state: SaladDeploymentState
    provider_queue_id: str | None
    provider_container_group_id: str | None
    metered_microusd: int = 0
    error_code: str | None = None


class SaladDeploymentClient(Protocol):
    async def create_queue(
        self,
        name: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
    ) -> SaladQueue: ...

    async def get_queue(self, queue_name: str) -> SaladQueue: ...

    async def create_container_group(
        self,
        configuration: Mapping[str, JSONValue],
    ) -> SaladContainerGroup: ...

    async def get_container_group(
        self,
        container_group_name: str,
    ) -> SaladContainerGroup: ...

    async def list_container_group_instances(
        self,
        container_group_name: str,
    ) -> SaladContainerGroupInstancePage: ...

    async def update_container_group(
        self,
        container_group_name: str,
        patch: Mapping[str, JSONValue],
    ) -> SaladContainerGroup: ...

    async def start_container_group(self, container_group_name: str) -> None: ...

    async def stop_container_group(self, container_group_name: str) -> None: ...


@dataclass(frozen=True)
class _RuntimeBinding:
    name: str
    reference: str


def deterministic_provider_name(
    base_name: str,
    *,
    version_no: int,
    config_sha256: str,
) -> str:
    """Derive a stable DNS-safe Salad name from immutable deployment identity."""

    if version_no <= 0:
        raise SaladDeploymentValidationError("deployment version must be positive")
    if re.fullmatch(r"[0-9a-f]{64}", config_sha256) is None:
        raise SaladDeploymentValidationError("deployment configuration hash is invalid")

    normalized = re.sub(r"[^a-z0-9-]+", "-", base_name.strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized or not normalized[0].isalpha():
        normalized = f"resource-{normalized}" if normalized else "resource"
    suffix = f"-v{version_no}-{config_sha256[:10]}"
    if normalized.endswith(suffix):
        result = normalized
    else:
        prefix_length = _MAX_PROVIDER_NAME_LENGTH - len(suffix)
        normalized = normalized[:prefix_length].rstrip("-")
        result = f"{normalized}{suffix}"
    if _NAME_PATTERN.fullmatch(result) is None:
        raise SaladDeploymentValidationError("derived Salad resource name is invalid")
    return result


async def provision_deployment_step(
    session: AsyncSession,
    *,
    deployment_id: UUID,
    client: SaladDeploymentClient,
    secret_resolver: RuntimeSecretResolver | None = None,
    now: datetime | None = None,
    billing_observation_clock: Callable[[], datetime] | None = None,
) -> DeploymentResult:
    """Advance provisioning by at most one remote mutation.

    The caller must commit each returned step before invoking this function again.
    This ensures a queue ID is durable before a container-group POST can occur.
    Ambiguous mutations enter ``UNKNOWN`` and are only reconciled by deterministic
    name; they are never automatically repeated.
    """

    observed_at = _as_utc(now or datetime.now(UTC))
    billing_observation_clock = _resolve_billing_observation_clock(
        explicit_now=now,
        observed_at=observed_at,
        clock=billing_observation_clock,
    )
    budget_guard_available = await _lock_budget_guard(session)
    deployment = await _load_deployment_locked(session, deployment_id)
    _validate_local_deployment(deployment)
    _retire_legacy_video_deployment(session, deployment, observed_at)
    was_unknown = deployment.state == SaladDeploymentState.UNKNOWN
    _assign_deterministic_names(deployment)

    if not await _budget_allows_provider_work(
        session,
        deployment,
        observed_at,
        guard_available=budget_guard_available,
    ):
        return await _budget_blocked_result(
            session,
            deployment,
            client,
            observed_at,
            billing_observation_clock=billing_observation_clock,
        )

    if _stop_is_desired(deployment):
        return await _request_stop(
            session,
            deployment,
            client,
            observed_at,
            billing_observation_clock=billing_observation_clock,
        )
    if deployment.is_current and await _has_unstopped_superseded_group(
        session,
        deployment.id,
        deployment.purpose,
    ):
        return await _defer_rollout_overlap(session, deployment, observed_at)

    if deployment.provider_queue_id is None:
        deployment.state = SaladDeploymentState.PROVISIONING
        await session.flush()
        try:
            queue = await _get_queue_or_none(client, deployment.queue_name)
        except (SaladAPIError, SaladProtocolError, SaladTransportError) as error:
            return await _handle_read_error(
                session,
                deployment,
                error,
                code_prefix="queue_reconcile",
                observed_at=observed_at,
            )
        if queue is not None:
            return await _adopt_queue(session, deployment, queue, observed_at)
        if was_unknown:
            return await _defer_unknown(
                session,
                deployment,
                error_code="queue_create_outcome_unknown",
                observed_at=observed_at,
            )
        try:
            queue = await client.create_queue(
                deployment.queue_name,
                display_name=deployment.queue_name,
                description=(f"Generation queue for immutable deployment v{deployment.version_no}"),
            )
        except (SaladAPIError, SaladProtocolError, SaladTransportError) as error:
            return await _handle_mutation_error(
                session,
                deployment,
                error,
                code_prefix="queue_create",
                observed_at=observed_at,
            )
        if queue.name != deployment.queue_name:
            return await _mark_unknown(
                session,
                deployment,
                error_code="queue_create_response_mismatch",
                observed_at=observed_at,
            )
        deployment.provider_queue_id = str(queue.id)
        deployment.state = SaladDeploymentState.PROVISIONING
        deployment.unknown_since = None
        _clear_error(deployment)
        _touch(deployment)
        _audit(
            session,
            deployment,
            action="salad_deployment.queue_created",
            detail={"queue_id": str(queue.id), "queue_name": queue.name},
            occurred_at=observed_at,
        )
        await session.flush()
        return _result(deployment, DeploymentAction.QUEUE_CREATED)

    if deployment.provider_container_group_id is None:
        deployment.state = SaladDeploymentState.PROVISIONING
        await session.flush()
        queue_result = await _verify_persisted_queue(
            session,
            deployment,
            client,
            observed_at,
        )
        if queue_result is not None:
            return queue_result
        try:
            group = await _get_group_or_none(client, deployment.container_group_name)
        except (SaladAPIError, SaladProtocolError, SaladTransportError) as error:
            return await _handle_read_error(
                session,
                deployment,
                error,
                code_prefix="group_reconcile",
                observed_at=observed_at,
            )
        if group is not None:
            return await _adopt_group(session, deployment, group, observed_at)
        if was_unknown:
            return await _defer_unknown(
                session,
                deployment,
                error_code="group_create_outcome_unknown",
                observed_at=observed_at,
            )

        try:
            payload = await _container_group_payload(
                deployment,
                secret_resolver,
            )
        except SaladDeploymentValidationError:
            return await _mark_failed(
                session,
                deployment,
                error_code="invalid_container_group_configuration",
                observed_at=observed_at,
            )
        except Exception:
            # A secret backend is an untrusted external dependency. Its exception
            # text can contain secret material, so only a fixed code is persisted.
            return await _mark_failed(
                session,
                deployment,
                error_code="runtime_binding_resolution_failed",
                observed_at=observed_at,
            )

        try:
            group = await client.create_container_group(payload)
        except (SaladAPIError, SaladProtocolError, SaladTransportError) as error:
            return await _handle_mutation_error(
                session,
                deployment,
                error,
                code_prefix="group_create",
                observed_at=observed_at,
            )
        if group.name != deployment.container_group_name:
            return await _mark_unknown(
                session,
                deployment,
                error_code="group_create_response_mismatch",
                observed_at=observed_at,
            )

        deployment.provider_container_group_id = str(group.id)
        deployment.state = SaladDeploymentState.PROVISIONING
        deployment.unknown_since = None
        _clear_error(deployment)
        _touch(deployment)
        _audit(
            session,
            deployment,
            action="salad_deployment.container_group_created",
            detail={
                "container_group_id": str(group.id),
                "container_group_name": group.name,
            },
            occurred_at=observed_at,
        )
        await session.flush()
        return _result(deployment, DeploymentAction.GROUP_CREATED)

    return await _reconcile_locked(
        session,
        deployment,
        client,
        observed_at,
        guard_available=budget_guard_available,
        billing_observation_clock=billing_observation_clock,
    )


async def reconcile_deployment(
    session: AsyncSession,
    *,
    deployment_id: UUID,
    client: SaladDeploymentClient,
    now: datetime | None = None,
    billing_observation_clock: Callable[[], datetime] | None = None,
) -> DeploymentResult:
    """Reconcile durable IDs, provider state, runtime cost, and the kill switch."""

    observed_at = _as_utc(now or datetime.now(UTC))
    billing_observation_clock = _resolve_billing_observation_clock(
        explicit_now=now,
        observed_at=observed_at,
        clock=billing_observation_clock,
    )
    budget_guard_available = await _lock_budget_guard(session)
    deployment = await _load_deployment_locked(session, deployment_id)
    _validate_local_deployment(deployment)
    _retire_legacy_video_deployment(session, deployment, observed_at)
    _assign_deterministic_names(deployment)
    if deployment.provider_queue_id is None or deployment.provider_container_group_id is None:
        return await _defer_unknown(
            session,
            deployment,
            error_code="provider_resources_incomplete",
            observed_at=observed_at,
        )
    return await _reconcile_locked(
        session,
        deployment,
        client,
        observed_at,
        guard_available=budget_guard_available,
        billing_observation_clock=billing_observation_clock,
    )


async def _reconcile_locked(
    session: AsyncSession,
    deployment: SaladDeployment,
    client: SaladDeploymentClient,
    observed_at: datetime,
    *,
    guard_available: bool,
    billing_observation_clock: Callable[[], datetime],
) -> DeploymentResult:
    budget_open = await _budget_allows_provider_work(
        session,
        deployment,
        observed_at,
        guard_available=guard_available,
    )
    if not budget_open or _stop_is_desired(deployment):
        return await _request_stop(
            session,
            deployment,
            client,
            observed_at,
            billing_observation_clock=billing_observation_clock,
        )

    try:
        queue = await client.get_queue(deployment.queue_name)
        group = await client.get_container_group(deployment.container_group_name)
    except (SaladAPIError, SaladProtocolError, SaladTransportError) as error:
        # Queue/group read failure makes instance billing unobservable too.
        # Surface that ambiguity immediately, including before the first exact
        # running-instance observation.
        deployment.billing_observation_stale = True
        return await _handle_read_error(
            session,
            deployment,
            error,
            code_prefix="deployment_reconcile",
            observed_at=observed_at,
        )

    effective_min_replicas = await effective_worker_min_replicas(
        session,
        salad_deployment_id=deployment.id,
        now=observed_at,
    )
    billing_observation = await _refresh_billing_observation(
        session,
        deployment,
        client,
        group,
        observed_at=observed_at,
        end_session=_is_true_scale_to_zero(
            group,
            effective_min_replicas=effective_min_replicas,
        ),
        billing_observation_clock=billing_observation_clock,
    )
    drift_code = _remote_drift_code(
        deployment,
        queue,
        group,
        effective_min_replicas=effective_min_replicas,
    )
    current_replicas = _observed_replicas(group)
    try:
        metered = await _meter_runtime_interval(
            session,
            deployment,
            current_replicas=current_replicas,
            group=group,
            observed_at=observed_at,
            running_instances=(
                billing_observation.running_instances if billing_observation is not None else None
            ),
        )
    except BudgetError:
        deployment.desired_state = DesiredDeploymentState.STOPPED
        deployment.last_error_code = "runtime_metering_failed_closed"
        deployment.last_error_detail = "Runtime metering failed; provider execution was disabled."
        deployment.reconcile_after = observed_at
        _touch(deployment)
        _audit(
            session,
            deployment,
            action="salad_deployment.runtime_metering_failed_closed",
            detail={"error_code": "runtime_metering_failed_closed"},
            occurred_at=observed_at,
        )
        await session.flush()
        return await _request_stop(
            session,
            deployment,
            client,
            observed_at,
            billing_observation_clock=billing_observation_clock,
        )

    previous_provider_status = deployment.provider_status
    deployment.observed_replicas = current_replicas
    deployment.ready_replicas = group.current_state.running_count
    deployment.last_observed_at = observed_at
    deployment.provider_status = _safe_provider_status(queue, group)
    deployment.reconcile_after = observed_at + _RECONCILE_DELAY

    # Recording an interval can engage the budget service's durable kill switch.
    if _stop_is_desired(deployment):
        await session.flush()
        stop_result = await _request_stop(
            session,
            deployment,
            client,
            observed_at,
            billing_observation_clock=billing_observation_clock,
        )
        return DeploymentResult(
            deployment_id=stop_result.deployment_id,
            action=stop_result.action,
            state=stop_result.state,
            provider_queue_id=stop_result.provider_queue_id,
            provider_container_group_id=stop_result.provider_container_group_id,
            metered_microusd=metered,
            error_code=stop_result.error_code,
        )

    if group.pending_change:
        stalled = _record_pending_preparation(
            deployment,
            previous_provider_status=previous_provider_status,
            observed_at=observed_at,
        )
        deployment.state = (
            SaladDeploymentState.DEGRADED if stalled else SaladDeploymentState.PROVISIONING
        )
        deployment.last_error_code = (
            "provider_image_preparation_stalled"
            if stalled
            else "provider_image_preparation_pending"
        )
        deployment.last_error_detail = (
            "Provider image preparation stopped reporting progress; reconciliation remains "
            "read-only."
            if stalled
            else "Provider image preparation is pending; reconciliation remains read-only."
        )
    else:
        deployment.unknown_since = None
        repair_result = await _request_active_contract_repair(
            session,
            deployment,
            client,
            group,
            drift_code=drift_code,
            effective_min_replicas=effective_min_replicas,
            observed_at=observed_at,
            metered_microusd=metered,
        )
        if repair_result is not None:
            return repair_result

    if not group.pending_change:
        provider_status = group.status.strip().lower()
        # A manually or provider-stopped group is a valid idle state while the
        # durable demand calculation remains zero. Starting it here would fight
        # the operator stop and allocate an otherwise unneeded GPU.
        unhealthy_status = provider_status == "failed" or (
            provider_status == "stopped" and effective_min_replicas == 1
        )
        if drift_code is not None or unhealthy_status:
            deployment.state = SaladDeploymentState.DEGRADED
            deployment.last_error_code = drift_code or "provider_group_unhealthy"
            deployment.last_error_detail = "Provider resources do not match the active deployment."
        else:
            deployment.state = SaladDeploymentState.ACTIVE
            deployment.activated_at = deployment.activated_at or observed_at
            deployment.unknown_since = None
            _clear_error(deployment)
    _touch(deployment)
    _audit(
        session,
        deployment,
        action="salad_deployment.reconciled",
        detail={
            "state": deployment.state.value,
            "observed_replicas": current_replicas,
            "ready_replicas": group.current_state.running_count,
            "metered_microusd": metered,
        },
        occurred_at=observed_at,
    )
    await session.flush()
    return _result(
        deployment,
        DeploymentAction.RECONCILED,
        metered_microusd=metered,
        error_code=deployment.last_error_code,
    )


async def effective_worker_min_replicas(
    session: AsyncSession,
    *,
    salad_deployment_id: UUID,
    now: datetime,
) -> int:
    """Keep one worker only while experiments or generation genuinely need it."""

    purpose = await session.scalar(
        select(SaladDeployment.purpose).where(SaladDeployment.id == salad_deployment_id)
    )
    if purpose is None or purpose == SaladDeploymentPurpose.VIDEO:
        # Retired video deployment rows remain readable for migration history,
        # but image jobs and Experiment warm leases must never reactivate them.
        return 0

    experiment_minimum = await effective_experiment_min_replicas(
        session,
        salad_deployment_id=salad_deployment_id,
        now=now,
    )
    if experiment_minimum == 1:
        return 1
    stop_marker = exists(
        select(AuditEvent.id).where(
            AuditEvent.resource_type == "release",
            AuditEvent.resource_id == Release.id,
            AuditEvent.action == _GENERATION_STOP_REQUESTED_ACTION,
        )
    )
    # A local queued/retryable job has not crossed the provider boundary yet.
    # Raising the minimum here can start a scaled-to-zero group before the
    # runtime refresh replaces its short-lived artifact credentials. Keep the
    # group at zero until either a warm lease records the refreshed provider
    # version above or submission records durable provider work below. Salad's
    # queue autoscaler then supplies the same demand signal without booting a
    # stale container.
    active_attempt_id = await session.scalar(
        select(GenerationAttempt.id)
        .join(GenerationJob, GenerationJob.id == GenerationAttempt.job_id)
        .join(ReleaseVersion, ReleaseVersion.id == GenerationJob.release_version_id)
        .join(Release, Release.id == ReleaseVersion.release_id)
        .where(
            GenerationAttempt.salad_deployment_id == salad_deployment_id,
            GenerationAttempt.provider == _PROVIDER,
            GenerationJob.provider == _PROVIDER,
            GenerationAttempt.state.in_(_GENERATION_ACTIVE_GPU_ATTEMPT_STATES),
            GenerationJob.state.in_(_GENERATION_ACTIVE_GPU_JOB_STATES),
            Release.phase.in_(_GENERATION_ACTIVE_RELEASE_PHASES),
            Release.current_version_no == ReleaseVersion.version_no,
            ~stop_marker,
        )
        .order_by(GenerationAttempt.created_at, GenerationAttempt.id)
        .limit(1)
    )
    return 1 if active_attempt_id is not None else 0


async def _request_active_contract_repair(
    session: AsyncSession,
    deployment: SaladDeployment,
    client: SaladDeploymentClient,
    group: SaladContainerGroup,
    *,
    drift_code: str | None,
    effective_min_replicas: int,
    observed_at: datetime,
    metered_microusd: int,
) -> DeploymentResult | None:
    """Repair only provider fields with documented, unambiguous mutations.

    A missing autoscaler can be restored directly. A stopped group whose
    configuration otherwise matches can be started through its dedicated
    action only when durable demand requires one worker. All other drift
    remains degraded and blocks dispatch.
    """

    if drift_code == "provider_autoscaler_drift":
        patch: JSONObject = {
            "queue_autoscaler": _desired_queue_autoscaler(
                deployment,
                min_replicas=effective_min_replicas,
            )
        }
        try:
            updated = await client.update_container_group(
                deployment.container_group_name,
                patch,
            )
        except (SaladAPIError, SaladProtocolError, SaladTransportError) as error:
            return await _handle_mutation_error(
                session,
                deployment,
                error,
                code_prefix="autoscaler_repair",
                observed_at=observed_at,
            )
        if (
            updated.name != deployment.container_group_name
            or str(updated.id) != deployment.provider_container_group_id
        ):
            return await _mark_unknown(
                session,
                deployment,
                error_code="autoscaler_repair_response_mismatch",
                observed_at=observed_at,
            )
        deployment.state = SaladDeploymentState.PROVISIONING
        deployment.last_error_code = "provider_autoscaler_repair_pending"
        deployment.last_error_detail = "Provider queue autoscaler repair is awaiting read-back."
        deployment.reconcile_after = observed_at + _RECONCILE_DELAY
        _touch(deployment)
        _audit(
            session,
            deployment,
            action="salad_deployment.autoscaler_repair_requested",
            detail={"container_group_name": deployment.container_group_name},
            occurred_at=observed_at,
        )
        await session.flush()
        return _result(
            deployment,
            DeploymentAction.AUTOSCALER_REPAIR_REQUESTED,
            metered_microusd=metered_microusd,
            error_code=deployment.last_error_code,
        )

    if (
        drift_code is None
        and effective_min_replicas == 1
        and group.status.strip().lower() == "stopped"
    ):
        try:
            await client.start_container_group(deployment.container_group_name)
        except (SaladAPIError, SaladProtocolError, SaladTransportError) as error:
            return await _handle_mutation_error(
                session,
                deployment,
                error,
                code_prefix="group_start",
                observed_at=observed_at,
            )
        deployment.state = SaladDeploymentState.PROVISIONING
        deployment.last_error_code = "provider_start_pending"
        deployment.last_error_detail = "Provider container group start is awaiting read-back."
        deployment.reconcile_after = observed_at + _RECONCILE_DELAY
        _touch(deployment)
        _audit(
            session,
            deployment,
            action="salad_deployment.start_requested",
            detail={"container_group_name": deployment.container_group_name},
            occurred_at=observed_at,
        )
        await session.flush()
        return _result(
            deployment,
            DeploymentAction.START_REQUESTED,
            metered_microusd=metered_microusd,
            error_code=deployment.last_error_code,
        )

    return None


async def _budget_allows_provider_work(
    session: AsyncSession,
    deployment: SaladDeployment,
    observed_at: datetime,
    *,
    guard_available: bool,
) -> bool:
    if not guard_available:
        _engage_local_kill_switch(
            session,
            deployment,
            error_code="budget_guard_unavailable",
            observed_at=observed_at,
        )
        return False
    try:
        snapshot = await reevaluate_budget_guard(
            session,
            provider=_PROVIDER,
            now=observed_at,
        )
    except BudgetError:
        _engage_local_kill_switch(
            session,
            deployment,
            error_code="budget_guard_unavailable",
            observed_at=observed_at,
        )
        return False
    if snapshot.state == BudgetState.BLOCKED:
        _engage_local_kill_switch(
            session,
            deployment,
            error_code="budget_limit_exceeded",
            observed_at=observed_at,
        )
        return False
    return True


async def _lock_budget_guard(session: AsyncSession) -> bool:
    """Establish the global provider lock before any deployment row lock."""

    guard_id = await session.scalar(
        select(ProviderBudgetGuard.id)
        .where(ProviderBudgetGuard.provider == _PROVIDER)
        .with_for_update()
    )
    return guard_id is not None


async def _budget_blocked_result(
    session: AsyncSession,
    deployment: SaladDeployment,
    client: SaladDeploymentClient,
    observed_at: datetime,
    *,
    billing_observation_clock: Callable[[], datetime],
) -> DeploymentResult:
    return await _request_stop(
        session,
        deployment,
        client,
        observed_at,
        billing_observation_clock=billing_observation_clock,
    )


async def _request_stop(
    session: AsyncSession,
    deployment: SaladDeployment,
    client: SaladDeploymentClient,
    observed_at: datetime,
    *,
    billing_observation_clock: Callable[[], datetime],
) -> DeploymentResult:
    group_read_failed = False
    try:
        group = await _get_group_or_none(client, deployment.container_group_name)
    except (SaladAPIError, SaladProtocolError, SaladTransportError):
        group = None
        group_read_failed = True
        deployment.billing_observation_stale = True
    if group is not None:
        if group.name != deployment.container_group_name:
            return await _mark_failed(
                session,
                deployment,
                error_code="stop_container_group_name_collision",
                observed_at=observed_at,
            )
        if deployment.provider_container_group_id != str(group.id):
            deployment.provider_container_group_id = str(group.id)
            _touch(deployment)
            _audit(
                session,
                deployment,
                action="salad_deployment.stop_container_group_recovered",
                detail={
                    "container_group_id": str(group.id),
                    "container_group_name": group.name,
                },
                occurred_at=observed_at,
            )
    stop_converged = False
    if group is not None:
        state = group.current_state
        stop_converged = (
            deployment.state == SaladDeploymentState.DRAINING
            and not group.status.strip()
            and not group.pending_change
            and group.replicas == 0
            and state.allocating_count == 0
            and state.creating_count == 0
            and state.running_count == 0
            and state.stopping_count == 0
        )
    terminal_group = bool(
        group is not None and (group.status.strip().lower() == "stopped" or stop_converged)
    )

    # A live-group stop cycle is capped at exactly three provider requests:
    # group read, instance read, and stop mutation. Queue identity is not needed
    # for the mutation, so recover it only once the group is absent or terminal
    # and no stop mutation will be made in the same cycle.
    if deployment.provider_queue_id is None and (
        (group is None and not group_read_failed) or terminal_group
    ):
        try:
            queue = await _get_queue_or_none(client, deployment.queue_name)
        except (SaladAPIError, SaladProtocolError, SaladTransportError):
            queue = None
        if queue is not None:
            if queue.name != deployment.queue_name:
                return await _mark_failed(
                    session,
                    deployment,
                    error_code="stop_queue_name_collision",
                    observed_at=observed_at,
                )
            deployment.provider_queue_id = str(queue.id)
            _touch(deployment)
            _audit(
                session,
                deployment,
                action="salad_deployment.stop_queue_recovered",
                detail={"queue_id": str(queue.id), "queue_name": queue.name},
                occurred_at=observed_at,
            )

    if group is None and not group_read_failed:
        return await _confirm_provider_group_stopped(
            session,
            deployment,
            observed_at=observed_at,
            status="absent",
            metered_microusd=0,
        )

    metered = 0
    billing_observation: _BillingObservation | None = None
    if group is not None:
        billing_observation = await _refresh_billing_observation(
            session,
            deployment,
            client,
            group,
            observed_at=observed_at,
            end_session=False,
            billing_observation_clock=billing_observation_clock,
        )
        current_replicas = _observed_replicas(group)
        try:
            metered = await _meter_runtime_interval(
                session,
                deployment,
                current_replicas=current_replicas,
                group=group,
                observed_at=observed_at,
                running_instances=(
                    billing_observation.running_instances
                    if billing_observation is not None
                    else None
                ),
            )
        except BudgetError:
            deployment.last_error_code = "final_runtime_metering_failed"
            deployment.last_error_detail = (
                "Final runtime metering failed; the provider stop remains mandatory."
            )
            _touch(deployment)
            _audit(
                session,
                deployment,
                action="salad_deployment.final_runtime_metering_failed",
                detail={"error_code": "final_runtime_metering_failed"},
                occurred_at=observed_at,
            )
        deployment.observed_replicas = current_replicas
        deployment.ready_replicas = group.current_state.running_count
        deployment.last_observed_at = observed_at
    if group is not None and terminal_group:
        if billing_observation is not None and billing_observation.running_instances == 0:
            return await _confirm_provider_group_stopped(
                session,
                deployment,
                observed_at=(group.current_state.finish_time or observed_at),
                status="stopped",
                metered_microusd=metered,
            )
        # Salad documents that a group can report stopped while an individual
        # instance is still running. Keep reconciling until the instance list
        # authoritatively shows zero billable instances. A failed instance read
        # is likewise unresolved and must never stop the billing clock.
        deployment.state = SaladDeploymentState.DRAINING
        deployment.reconcile_after = observed_at + _RECONCILE_DELAY
        deployment.last_error_code = "billing_instance_stop_unconfirmed"
        deployment.last_error_detail = (
            "Provider group stop is visible, but running-instance shutdown is unconfirmed."
        )
        _touch(deployment)
        await session.flush()
        return _result(
            deployment,
            DeploymentAction.DEFERRED,
            metered_microusd=metered,
            error_code=deployment.last_error_code,
        )

    try:
        await client.stop_container_group(deployment.container_group_name)
    except (SaladAPIError, SaladProtocolError, SaladTransportError) as error:
        return await _handle_mutation_error(
            session,
            deployment,
            error,
            code_prefix="group_stop",
            observed_at=observed_at,
        )

    if (
        deployment.provider_queue_id is not None
        and deployment.provider_container_group_id is not None
    ):
        deployment.state = SaladDeploymentState.DRAINING
    else:
        deployment.state = SaladDeploymentState.UNKNOWN
        deployment.unknown_since = deployment.unknown_since or observed_at
    deployment.reconcile_after = observed_at + _RECONCILE_DELAY
    deployment.last_error_code = deployment.last_error_code or "stop_requested"
    deployment.last_error_detail = "Provider stop was accepted and awaits reconciliation."
    _touch(deployment)
    _audit(
        session,
        deployment,
        action="salad_deployment.stop_requested",
        detail={"container_group_name": deployment.container_group_name},
        occurred_at=observed_at,
    )
    await session.flush()
    return _result(
        deployment,
        DeploymentAction.STOP_REQUESTED,
        metered_microusd=metered,
        error_code=deployment.last_error_code,
    )


async def _confirm_provider_group_stopped(
    session: AsyncSession,
    deployment: SaladDeployment,
    *,
    observed_at: datetime,
    status: str,
    metered_microusd: int,
) -> DeploymentResult:
    billing_ended_at = _as_utc(observed_at)
    if deployment.billing_observed_at is not None:
        billing_ended_at = max(
            billing_ended_at,
            _stored_as_utc(deployment.billing_observed_at),
        )
    _end_billing_session(deployment, ended_at=billing_ended_at)
    deployment.billing_observed_at = billing_ended_at
    deployment.billing_observation_stale = False
    has_complete_identity = (
        deployment.provider_queue_id is not None
        and deployment.provider_container_group_id is not None
    )
    deployment.state = (
        SaladDeploymentState.STOPPED if has_complete_identity else SaladDeploymentState.FAILED
    )
    deployment.observed_replicas = 0
    deployment.ready_replicas = 0
    deployment.last_observed_at = observed_at
    deployment.stopped_at = observed_at
    deployment.provider_status = f"group={status}"
    deployment.unknown_since = None
    deployment.reconcile_after = None
    if has_complete_identity:
        deployment.last_error_code = None
        deployment.last_error_detail = None
    elif not has_complete_identity:
        deployment.last_error_code = (
            deployment.last_error_code or "deployment_stopped_before_provisioning"
        )
        deployment.last_error_detail = (
            deployment.last_error_detail
            or "No provider container group exists under the deterministic deployment name."
        )
    _touch(deployment)
    audit_detail: dict[str, str | int] = {"provider_status": status}
    if deployment.provider_container_group_id is not None:
        audit_detail["container_group_id"] = deployment.provider_container_group_id
    _audit(
        session,
        deployment,
        action="salad_deployment.stop_confirmed",
        detail=audit_detail,
        occurred_at=observed_at,
    )
    await session.flush()
    return _result(
        deployment,
        DeploymentAction.STOPPED,
        metered_microusd=metered_microusd,
        error_code=deployment.last_error_code,
    )


async def _has_unstopped_superseded_group(
    session: AsyncSession,
    current_deployment_id: UUID,
    purpose: SaladDeploymentPurpose,
) -> bool:
    blocker = await session.scalar(
        select(SaladDeployment.id)
        .where(
            SaladDeployment.id != current_deployment_id,
            SaladDeployment.purpose == purpose,
            SaladDeployment.is_current.is_(False),
            SaladDeployment.provider_container_group_id.is_not(None),
            SaladDeployment.state != SaladDeploymentState.STOPPED,
            SaladDeployment.stopped_at.is_(None),
        )
        .order_by(SaladDeployment.version_no)
        .limit(1)
        .with_for_update()
    )
    return blocker is not None


async def _defer_rollout_overlap(
    session: AsyncSession,
    deployment: SaladDeployment,
    observed_at: datetime,
) -> DeploymentResult:
    deployment.reconcile_after = observed_at + _RECONCILE_DELAY
    deployment.last_error_code = "superseded_deployment_not_stopped"
    deployment.last_error_detail = (
        "Provisioning waits until every superseded provider group is confirmed stopped."
    )
    _touch(deployment)
    _audit(
        session,
        deployment,
        action="salad_deployment.rollout_deferred",
        detail={"error_code": "superseded_deployment_not_stopped"},
        occurred_at=observed_at,
    )
    await session.flush()
    return _result(
        deployment,
        DeploymentAction.DEFERRED,
        error_code=deployment.last_error_code,
    )


def _matches_worker_contract(
    value: object,
    expected: JSONValue,
    *,
    allow_null_extensions: bool = False,
) -> bool:
    if isinstance(expected, Mapping):
        if not isinstance(value, Mapping):
            return False
        if any(
            key not in value
            or not _matches_worker_contract(
                value[key],
                expected_value,
                allow_null_extensions=allow_null_extensions,
            )
            for key, expected_value in expected.items()
        ):
            return False
        extra_keys = set(value) - set(expected)
        return (
            all(value[key] is None for key in extra_keys)
            if allow_null_extensions
            else not extra_keys
        )
    if isinstance(expected, list):
        return (
            isinstance(value, list)
            and len(value) == len(expected)
            and all(
                _matches_worker_contract(
                    item,
                    expected_item,
                    allow_null_extensions=allow_null_extensions,
                )
                for item, expected_item in zip(value, expected, strict=True)
            )
        )
    return type(value) is type(expected) and value == expected


async def _container_group_payload(
    deployment: SaladDeployment,
    resolver: RuntimeSecretResolver | None,
    *,
    environment_overrides: Mapping[str, str] | None = None,
) -> JSONObject:
    configuration = deepcopy(deployment.provider_configuration)
    if not isinstance(configuration, dict) or not all(
        isinstance(key, str) for key in configuration
    ):
        raise SaladDeploymentValidationError("provider configuration must be an object")
    bindings_value = configuration.pop(_RUNTIME_BINDINGS_KEY, [])
    bindings = _parse_runtime_bindings(bindings_value)
    if configuration.pop("runtime_binding_contract_sha256", None) is not None:
        raise SaladDeploymentValidationError("retired runtime binding contract is not supported")
    if configuration.pop("image_pull_mode", "public") != "public":
        raise SaladDeploymentValidationError("private image pull mode is not supported")

    if _contains_sensitive_configuration(configuration):
        raise SaladDeploymentValidationError(
            "live environment values must not be stored in provider configuration"
        )
    configured_name = configuration.get("name")
    if configured_name is not None and configured_name != deployment.container_group_name:
        raise SaladDeploymentValidationError("container group name conflicts with deployment")
    configured_replicas = configuration.get("replicas", 0)
    if (
        not isinstance(configured_replicas, int)
        or isinstance(configured_replicas, bool)
        or configured_replicas != 0
    ):
        raise SaladDeploymentValidationError("initial Salad replicas must be zero")

    container_value = configuration.get("container")
    if not isinstance(container_value, dict) or not all(
        isinstance(key, str) for key in container_value
    ):
        raise SaladDeploymentValidationError("provider configuration.container is required")
    container = cast(JSONObject, deepcopy(container_value))
    # Salad's write contract places priority inside ``container`` even though
    # the read representation exposes the effective value at group level.
    # Normalize legacy persisted configurations without sending an ignored
    # top-level field back to the provider.
    legacy_priority = configuration.pop("priority", None)
    configured_priority = container.get("priority")
    if legacy_priority is not None and configured_priority is not None:
        if legacy_priority != configured_priority:
            raise SaladDeploymentValidationError("container priority conflicts")
    elif legacy_priority is not None:
        configured_priority = legacy_priority
        container["priority"] = legacy_priority
    configured_image = container.get("image")
    if configured_image is not None and configured_image != deployment.worker_image_digest:
        raise SaladDeploymentValidationError("container image must match the immutable digest")
    if configured_priority not in {"high", "medium", "low", "batch"}:
        raise SaladDeploymentValidationError("container priority is invalid")

    queue_connection_value = configuration.get("queue_connection")
    if not isinstance(queue_connection_value, dict):
        raise SaladDeploymentValidationError("queue_connection is required")
    queue_connection = cast(JSONObject, deepcopy(queue_connection_value))
    if "path" in queue_connection and queue_connection["path"] != _WORKER_QUEUE_PATH:
        raise SaladDeploymentValidationError(f"queue_connection.path must be {_WORKER_QUEUE_PATH}")
    configured_port = queue_connection.get("port")
    if "port" in queue_connection and (
        not isinstance(configured_port, int)
        or isinstance(configured_port, bool)
        or configured_port != _WORKER_PORT
    ):
        raise SaladDeploymentValidationError(f"queue_connection.port must be {_WORKER_PORT}")
    configured_queue = queue_connection.get("queue_name")
    if "queue_name" in queue_connection and configured_queue != deployment.queue_name:
        raise SaladDeploymentValidationError("queue connection conflicts with deployment queue")
    queue_connection["path"] = _WORKER_QUEUE_PATH
    queue_connection["port"] = _WORKER_PORT

    configured_restart_policy = configuration.get("restart_policy")
    if "restart_policy" in configuration and configured_restart_policy != _WORKER_RESTART_POLICY:
        raise SaladDeploymentValidationError(f"restart_policy must be {_WORKER_RESTART_POLICY}")
    for probe_name, expected_probe in _WORKER_PROBE_CONTRACTS.items():
        if probe_name in configuration and not _matches_worker_contract(
            configuration[probe_name],
            expected_probe,
        ):
            raise SaladDeploymentValidationError(
                f"{probe_name} conflicts with the production worker contract"
            )

    autoscaler_value = configuration.get("queue_autoscaler", {})
    if not isinstance(autoscaler_value, dict):
        raise SaladDeploymentValidationError("queue_autoscaler must be an object")
    autoscaler = cast(JSONObject, deepcopy(autoscaler_value))
    expected_autoscaler = {
        "min_replicas": deployment.min_replicas,
        "max_replicas": deployment.max_replicas,
        "desired_queue_length": deployment.desired_queue_length,
    }
    for key, expected in expected_autoscaler.items():
        configured = autoscaler.get(key)
        if configured is not None and (
            not isinstance(configured, int)
            or isinstance(configured, bool)
            or configured != expected
        ):
            raise SaladDeploymentValidationError(f"queue_autoscaler.{key} conflicts")
        autoscaler[key] = expected

    binding_references = {binding.name: binding.reference for binding in bindings}
    environment: dict[str, JSONValue] = {}
    if resolver is None:
        if bindings:
            raise SaladDeploymentValidationError("runtime bindings require a secret resolver")
    else:
        try:
            resolved = await resolver.resolve_many(binding_references)
        except RuntimeSecretResolutionError:
            raise SaladDeploymentValidationError("runtime binding could not be resolved") from None
        if set(resolved) != set(binding_references):
            raise SaladDeploymentValidationError("runtime binding could not be resolved")
        for name, value in resolved.items():
            if not isinstance(value, str) or not value:
                raise SaladDeploymentValidationError("runtime binding could not be resolved")
            environment[name] = value

    overrides = dict(environment_overrides or {})
    allowed_overrides = {
        WORKER_MODEL_MANIFEST_JSON_BINDING,
        WORKER_MODEL_MANIFEST_SHA256_BINDING,
    }
    if not set(overrides).issubset(allowed_overrides) or any(
        not isinstance(value, str) or not value for value in overrides.values()
    ):
        raise SaladDeploymentValidationError("runtime environment override is invalid")
    if bool(overrides) and set(overrides) != allowed_overrides:
        raise SaladDeploymentValidationError("runtime manifest override is incomplete")
    environment.update(overrides)

    container["image"] = deployment.worker_image_digest
    if environment:
        container["environment_variables"] = environment
    queue_connection["queue_name"] = deployment.queue_name
    configuration["name"] = deployment.container_group_name
    configuration["display_name"] = deployment.container_group_name
    configuration["autostart_policy"] = True
    configuration["replicas"] = 0
    configuration["restart_policy"] = _WORKER_RESTART_POLICY
    for probe_name, expected_probe in _WORKER_PROBE_CONTRACTS.items():
        configuration[probe_name] = deepcopy(expected_probe)
    configuration["container"] = container
    configuration["queue_connection"] = queue_connection
    configuration["queue_autoscaler"] = autoscaler
    return cast(JSONObject, configuration)


async def refresh_container_group_runtime(
    deployment: SaladDeployment,
    client: SaladDeploymentClient,
    resolver: RuntimeSecretResolver | None,
    *,
    environment_overrides: Mapping[str, str] | None = None,
    convergence_timeout_seconds: float = _RUNTIME_REFRESH_CONVERGENCE_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _RUNTIME_REFRESH_POLL_SECONDS,
) -> SaladContainerGroup:
    """Replace an existing group's ephemeral bindings immediately before submission."""

    if deployment.provider_container_group_id is None:
        raise SaladDeploymentValidationError("container group is not provisioned")
    if (
        not deployment.is_current
        or deployment.state != SaladDeploymentState.ACTIVE
        or deployment.desired_state != DesiredDeploymentState.ACTIVE
    ):
        raise SaladDeploymentValidationError("deployment is not active")
    try:
        preflight = await client.get_container_group(deployment.container_group_name)
    except Exception:
        raise SaladDeploymentValidationError(
            "container group preflight could not be verified"
        ) from None
    _validate_runtime_group(deployment, preflight)
    if preflight.pending_change:
        raise SaladDeploymentValidationError("container group has a pending change")

    payload = await _container_group_payload(
        deployment,
        resolver,
        environment_overrides=environment_overrides,
    )
    container = payload.get("container")
    if not isinstance(container, dict):
        raise SaladDeploymentValidationError("provider configuration.container is required")
    environment = container.get("environment_variables")
    if not isinstance(environment, dict) or not environment:
        raise SaladDeploymentValidationError("runtime binding could not be resolved")
    try:
        updated = await client.update_container_group(
            deployment.container_group_name,
            {"container": container},
        )
        _validate_runtime_group(deployment, updated)
        if updated.version < preflight.version:
            raise SaladDeploymentValidationError("container group runtime update was not accepted")
        target_version = max(preflight.version + 1, updated.version)
        async with asyncio.timeout(convergence_timeout_seconds):
            while True:
                observed = await client.get_container_group(deployment.container_group_name)
                _validate_runtime_group(deployment, observed)
                if observed.version >= target_version and not observed.pending_change:
                    return observed
                await asyncio.sleep(poll_interval_seconds)
    except SaladDeploymentValidationError:
        raise
    except TimeoutError:
        raise SaladDeploymentValidationError(
            "container group runtime update did not converge"
        ) from None
    except Exception:
        raise SaladDeploymentValidationError("container group runtime update failed") from None


async def ensure_container_group_queue_admission(
    deployment: SaladDeployment,
    client: SaladDeploymentClient,
    *,
    convergence_timeout_seconds: float = _QUEUE_ADMISSION_CONVERGENCE_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _QUEUE_ADMISSION_POLL_SECONDS,
) -> SaladContainerGroup:
    """Prove the exact image group is attached to its queue before a job POST."""

    if (
        deployment.provider_queue_id is None
        or deployment.provider_container_group_id is None
        or not deployment.is_current
        or deployment.state != SaladDeploymentState.ACTIVE
        or deployment.desired_state != DesiredDeploymentState.ACTIVE
    ):
        raise SaladDeploymentValidationError("deployment is not ready for queue admission")
    try:
        initial = await client.get_container_group(deployment.container_group_name)
        _validate_runtime_group(deployment, initial)
        if initial.pending_change:
            raise SaladDeploymentValidationError("container group has a pending change")
        if _is_authoritatively_stopped(initial):
            await client.start_container_group(deployment.container_group_name)

        async with asyncio.timeout(convergence_timeout_seconds):
            while True:
                group = await client.get_container_group(deployment.container_group_name)
                _validate_runtime_group(deployment, group)
                if group.status.strip().lower() == "failed":
                    raise SaladDeploymentValidationError("container group failed before admission")
                queue = await client.get_queue(deployment.queue_name)
                if (
                    str(queue.id) != deployment.provider_queue_id
                    or queue.name != deployment.queue_name
                ):
                    raise SaladDeploymentValidationError("queue identity does not match deployment")
                if (
                    not group.pending_change
                    and not _is_authoritatively_stopped(group)
                    and _queue_contains_exact_group(queue, deployment)
                ):
                    return group
                await asyncio.sleep(poll_interval_seconds)
    except SaladDeploymentValidationError:
        raise
    except TimeoutError:
        raise SaladDeploymentValidationError(
            "container group did not become eligible for queue admission"
        ) from None
    except Exception:
        raise SaladDeploymentValidationError(
            "container group queue admission could not be verified"
        ) from None


def _queue_contains_exact_group(
    queue: SaladQueue,
    deployment: SaladDeployment,
) -> bool:
    return any(
        item.get("name") == deployment.container_group_name
        and str(item.get("id")) == deployment.provider_container_group_id
        for item in queue.container_groups
    )


def _validate_runtime_group(
    deployment: SaladDeployment,
    group: SaladContainerGroup,
) -> None:
    if (
        group.name != deployment.container_group_name
        or str(group.id) != deployment.provider_container_group_id
    ):
        raise SaladDeploymentValidationError("container group identity does not match deployment")
    if _group_configuration_drift(deployment, group) is not None:
        raise SaladDeploymentValidationError(
            "container group configuration does not match deployment"
        )


def _parse_runtime_bindings(value: object) -> tuple[_RuntimeBinding, ...]:
    if not isinstance(value, list):
        raise SaladDeploymentValidationError("runtime_bindings must be an array")
    result: list[_RuntimeBinding] = []
    names: set[str] = set()
    references: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise SaladDeploymentValidationError("runtime binding must be an object")
        if set(item) != {"name", "reference"}:
            raise SaladDeploymentValidationError("runtime binding fields are invalid")
        name = item.get("name")
        reference = item.get("reference")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,126}", name) is None
            or not isinstance(reference, str)
            or not reference
            or len(reference) > 500
        ):
            raise SaladDeploymentValidationError("runtime binding is invalid")
        if name not in SALAD_WORKER_ALLOWED_RUNTIME_BINDINGS:
            raise SaladDeploymentValidationError("runtime binding name is not allowed")
        if reference != SALAD_WORKER_RUNTIME_BINDING_REFERENCES[name]:
            raise SaladDeploymentValidationError("runtime binding reference is invalid")
        if name in names or reference in references:
            raise SaladDeploymentValidationError("runtime bindings must be unique")
        names.add(name)
        references.add(reference)
        result.append(_RuntimeBinding(name=name, reference=reference))
    return tuple(result)


def _contains_sensitive_configuration(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if any(marker in normalized for marker in _SENSITIVE_CONFIGURATION_MARKERS):
                return True
            if _contains_sensitive_configuration(item):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_configuration(item) for item in value)
    return False


@dataclass(frozen=True)
class _BillingObservation:
    running_instances: int
    observed_at: datetime


async def _refresh_billing_observation(
    session: AsyncSession,
    deployment: SaladDeployment,
    client: SaladDeploymentClient,
    group: SaladContainerGroup,
    *,
    observed_at: datetime,
    end_session: bool,
    billing_observation_clock: Callable[[], datetime],
) -> _BillingObservation | None:
    """Persist provider-running time without treating free preparation as billable.

    Salad bills an instance only in its documented ``running`` state. The
    instance ``update_time`` is the provider timestamp for that state
    transition. A failed instance read deliberately leaves the previous open
    segment untouched so network ambiguity can never make a live cost clock
    disappear.
    """

    try:
        page = await client.list_container_group_instances(deployment.container_group_name)
    except (SaladAPIError, SaladProtocolError, SaladTransportError):
        # An open segment is always unresolved. Before a first observation or
        # after a completed session, aggregate activity proves a new lifecycle
        # may be charging, but still cannot provide an exact instance start.
        deployment.billing_observation_stale = bool(
            deployment.billing_observation_stale
            or (
                deployment.billing_session_started_at is not None
                and deployment.billing_session_ended_at is None
            )
            or _group_has_instance_activity(group)
        )
        await session.flush()
        return None

    received_at = max(
        _as_utc(observed_at),
        _as_utc(billing_observation_clock()),
    )
    instances = page.instances
    running = tuple(
        instance
        for instance in instances
        if instance.state == SaladContainerGroupInstanceState.RUNNING
    )
    _apply_billing_observation(
        deployment,
        instances=instances,
        running=running,
        observed_at=received_at,
        end_session=end_session,
    )
    await session.flush()
    return _BillingObservation(running_instances=len(running), observed_at=received_at)


def _apply_billing_observation(
    deployment: SaladDeployment,
    *,
    instances: tuple[SaladContainerGroupInstance, ...],
    running: tuple[SaladContainerGroupInstance, ...],
    observed_at: datetime,
    end_session: bool,
) -> None:
    observed_at = _as_utc(observed_at)
    deployment.billing_observed_at = observed_at
    deployment.billing_observation_stale = False

    preparing = any(
        instance.state
        in {
            SaladContainerGroupInstanceState.ALLOCATING,
            SaladContainerGroupInstanceState.DOWNLOADING,
            SaladContainerGroupInstanceState.CREATING,
        }
        for instance in instances
    )
    if deployment.billing_session_ended_at is not None and preparing:
        # The same deployment is reused after true scale-to-zero. A new free
        # preparation lifecycle is a new session, but billing has not started.
        deployment.billing_session_started_at = None
        deployment.billing_session_ended_at = None
        deployment.billing_accumulated_microseconds = 0
        deployment.billing_active_instance_id = None
        deployment.billing_active_started_at = None
        deployment.billing_estimated = False

    untracked_stopping = any(
        instance.state == SaladContainerGroupInstanceState.STOPPING
        and str(instance.id) != deployment.billing_active_instance_id
        for instance in instances
    )
    if untracked_stopping:
        # A billable interval may have started and stopped wholly between
        # controller polls. Without the matching persisted running segment its
        # duration cannot be reconstructed, so never present this as exact zero.
        deployment.billing_observation_stale = True
        deployment.billing_estimated = True

    if running:
        # The database and deployment contract cap this creator worker at one
        # replica. Retain a conservative, visibly estimated clock if provider
        # drift ever reports more than one instead of silently hiding cost.
        selected = min(running, key=lambda item: (_stored_as_utc(item.update_time), item.id))
        selected_started_at = _stored_as_utc(selected.update_time)
        start_was_estimated = len(running) != 1
        if len(running) != 1:
            # Multiple creator instances violate the durable replica ceiling.
            # Freeze the public clock as stale/estimated; the conservative
            # budget meter still charges every observed group replica while
            # ordinary reconciliation converges the provider drift.
            deployment.billing_observation_stale = True
        if selected_started_at > observed_at:
            selected_started_at = observed_at
            start_was_estimated = True

        active_id = deployment.billing_active_instance_id
        active_started_at = deployment.billing_active_started_at
        same_segment = (
            active_id == str(selected.id)
            and active_started_at is not None
            and _stored_as_utc(active_started_at) == selected_started_at
        )
        if not same_segment and active_started_at is not None:
            _close_active_billing_segment(
                deployment,
                instances=instances,
                observed_at=observed_at,
                fallback_end=min(selected_started_at, observed_at),
            )

        if deployment.billing_session_started_at is None or (
            deployment.billing_session_ended_at is not None
        ):
            deployment.billing_session_started_at = selected_started_at
            deployment.billing_session_ended_at = None
            deployment.billing_accumulated_microseconds = 0
            deployment.billing_estimated = start_was_estimated
        elif selected_started_at < _stored_as_utc(deployment.billing_session_started_at):
            selected_started_at = _stored_as_utc(deployment.billing_session_started_at)
            deployment.billing_estimated = True

        if not same_segment:
            deployment.billing_active_instance_id = str(selected.id)
            deployment.billing_active_started_at = selected_started_at
        deployment.billing_estimated = deployment.billing_estimated or start_was_estimated
        return

    if deployment.billing_active_started_at is not None:
        _close_active_billing_segment(
            deployment,
            instances=instances,
            observed_at=observed_at,
            fallback_end=observed_at,
        )
    if end_session:
        _end_billing_session(deployment, ended_at=observed_at)


def _close_active_billing_segment(
    deployment: SaladDeployment,
    *,
    instances: tuple[SaladContainerGroupInstance, ...],
    observed_at: datetime,
    fallback_end: datetime,
) -> None:
    active_id = deployment.billing_active_instance_id
    active_started = deployment.billing_active_started_at
    if active_id is None or active_started is None:
        deployment.billing_active_instance_id = None
        deployment.billing_active_started_at = None
        return

    active_started = _stored_as_utc(active_started)
    exact_transition = next(
        (
            instance
            for instance in instances
            if str(instance.id) == active_id
            and instance.state != SaladContainerGroupInstanceState.RUNNING
        ),
        None,
    )
    ended_at = (
        _stored_as_utc(exact_transition.update_time)
        if exact_transition is not None
        else _as_utc(fallback_end)
    )
    if ended_at < active_started or ended_at > observed_at:
        ended_at = observed_at
        deployment.billing_estimated = True
    elif exact_transition is None:
        deployment.billing_estimated = True
    deployment.billing_accumulated_microseconds += _timedelta_microseconds(
        ended_at - active_started
    )
    deployment.billing_active_instance_id = None
    deployment.billing_active_started_at = None


def _end_billing_session(deployment: SaladDeployment, *, ended_at: datetime) -> None:
    if (
        deployment.billing_session_started_at is None
        or deployment.billing_session_ended_at is not None
    ):
        return
    ended_at = _as_utc(ended_at)
    if deployment.billing_active_started_at is not None:
        _close_active_billing_segment(
            deployment,
            instances=(),
            observed_at=ended_at,
            fallback_end=ended_at,
        )
    started_at = _stored_as_utc(deployment.billing_session_started_at)
    deployment.billing_session_ended_at = max(started_at, ended_at)


def _is_true_scale_to_zero(
    group: SaladContainerGroup,
    *,
    effective_min_replicas: int,
) -> bool:
    state = group.current_state
    return bool(
        effective_min_replicas == 0
        and group.replicas == 0
        and state.allocating_count == 0
        and state.creating_count == 0
        and state.running_count == 0
        and state.stopping_count == 0
    )


def _group_has_instance_activity(group: SaladContainerGroup) -> bool:
    state = group.current_state
    return bool(
        group.replicas > 0
        or state.allocating_count > 0
        or state.creating_count > 0
        or state.running_count > 0
        or state.stopping_count > 0
    )


async def _meter_runtime_interval(
    session: AsyncSession,
    deployment: SaladDeployment,
    *,
    current_replicas: int,
    group: SaladContainerGroup,
    observed_at: datetime,
    running_instances: int | None,
) -> int:
    previous_replicas = deployment.observed_replicas or 0
    instance_floor = group.replicas if running_instances is None else running_instances
    replicas = max(previous_replicas, current_replicas, instance_floor)
    if replicas <= 0:
        return 0

    if deployment.last_observed_at is not None:
        interval_start = _stored_as_utc(deployment.last_observed_at)
    elif group.current_state.start_time is not None:
        interval_start = _stored_as_utc(group.current_state.start_time)
        if deployment.created_at is not None:
            interval_start = max(interval_start, _stored_as_utc(deployment.created_at))
    else:
        return 0
    interval_end = observed_at
    if _is_authoritatively_stopped(group) and running_instances == 0:
        finish_time = group.current_state.finish_time
        if finish_time is None:
            # A definitive zero-instance STOPPED observation disproves an
            # ongoing synthetic interval even when Salad omits its end time.
            return 0
        interval_end = min(interval_end, _stored_as_utc(finish_time))
    if interval_start >= interval_end:
        return 0

    newly_metered = 0
    for segment_start, segment_end in _daily_segments(interval_start, interval_end):
        duration_microseconds = _timedelta_microseconds(segment_end - segment_start)
        amount = (
            deployment.max_hourly_cost_microusd * replicas * duration_microseconds
            + _MICROSECONDS_PER_HOUR
            - 1
        ) // _MICROSECONDS_PER_HOUR
        if amount <= 0:
            continue
        result = await record_spend_entry(
            session,
            provider=_PROVIDER,
            dedupe_key=(
                f"salad-runtime:{deployment.id}:"
                f"{_epoch_microseconds(segment_start)}:"
                f"{_epoch_microseconds(segment_end)}:r{replicas}"
            ),
            entry_type=SpendEntryType.USAGE,
            amount_microusd=amount,
            effective_at=segment_start,
            salad_deployment_id=deployment.id,
            now=observed_at,
        )
        if not result.replayed:
            newly_metered += amount
    return newly_metered


def _daily_segments(start: datetime, end: datetime) -> tuple[tuple[datetime, datetime], ...]:
    segments: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        next_day = cursor.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        segment_end = min(next_day, end)
        segments.append((cursor, segment_end))
        cursor = segment_end
    return tuple(segments)


def _timedelta_microseconds(value: timedelta) -> int:
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


def _epoch_microseconds(value: datetime) -> int:
    return _timedelta_microseconds(value - datetime(1970, 1, 1, tzinfo=UTC))


def _observed_replicas(group: SaladContainerGroup) -> int:
    state = group.current_state
    lifecycle_count = (
        state.allocating_count + state.creating_count + state.running_count + state.stopping_count
    )
    if _is_authoritatively_stopped(group):
        # Salad can retain the configured replica target after every actual
        # instance has stopped. The target is not billable observation data.
        return 0
    return max(group.replicas, lifecycle_count)


def _is_authoritatively_stopped(group: SaladContainerGroup) -> bool:
    state = group.current_state
    return bool(
        state.status.strip().lower() == "stopped"
        and state.allocating_count == 0
        and state.creating_count == 0
        and state.running_count == 0
        and state.stopping_count == 0
    )


def _safe_provider_status(queue: SaladQueue, group: SaladContainerGroup) -> str:
    queue_length = max(0, min(queue.current_queue_length, 999_999_999))
    queue_value = (
        f"{queue_length}+" if queue.current_queue_length > queue_length else str(queue_length)
    )
    raw_status = group.status.strip().lower()
    group_status = raw_status if raw_status in _SAFE_PROVIDER_GROUP_STATUSES else "unknown"
    fields = [
        f"queue={queue_value}",
        f"group={group_status}",
        f"pending={int(group.pending_change)}",
    ]
    if group.pending_change:
        phase, progress = _safe_preparation_snapshot(group.current_state.description)
        fields.extend(
            (
                f"phase={phase}",
                f"progress={progress if progress is not None else 'unknown'}",
            )
        )
    # Every field is selected from a fixed vocabulary or a bounded integer, so this
    # representation is guaranteed to fit the 100-character database boundary.
    return ";".join(fields)


def _safe_preparation_snapshot(description: str) -> tuple[str, int | None]:
    normalized = description.casefold()
    if any(marker in normalized for marker in ("download", "image", "pull")):
        phase = "image_pull"
    elif any(marker in normalized for marker in ("allocat", "capacity", "node")):
        phase = "allocation"
    elif any(marker in normalized for marker in ("creat", "prepar", "provision", "start")):
        phase = "preparing"
    else:
        phase = "unknown"
    progress_match = _PROVIDER_PROGRESS_PATTERN.search(normalized)
    progress = int(progress_match.group(1)) if progress_match is not None else None
    return phase, progress


def _parse_safe_preparation_snapshot(status: str | None) -> tuple[str, int | None] | None:
    if status is None:
        return None
    fields: dict[str, str] = {}
    for item in status.split(";"):
        key, separator, value = item.partition("=")
        if separator:
            fields[key] = value
    phase = fields.get("phase")
    progress_value = fields.get("progress")
    if phase not in _SAFE_PROVIDER_PREPARATION_PHASES or progress_value is None:
        return None
    if progress_value == "unknown":
        return phase, None
    if not progress_value.isascii() or not progress_value.isdecimal():
        return None
    progress = int(progress_value)
    if not 0 <= progress <= 100:
        return None
    return phase, progress


def _record_pending_preparation(
    deployment: SaladDeployment,
    *,
    previous_provider_status: str | None,
    observed_at: datetime,
) -> bool:
    current_snapshot = _parse_safe_preparation_snapshot(deployment.provider_status)
    previous_snapshot = _parse_safe_preparation_snapshot(previous_provider_status)
    if current_snapshot is None or current_snapshot != previous_snapshot:
        deployment.unknown_since = observed_at
        return False
    if deployment.unknown_since is None:
        deployment.unknown_since = observed_at
        return False
    unchanged_since = _stored_as_utc(deployment.unknown_since)
    return observed_at - unchanged_since >= _PROVIDER_PENDING_STALL_AFTER


async def _verify_persisted_queue(
    session: AsyncSession,
    deployment: SaladDeployment,
    client: SaladDeploymentClient,
    observed_at: datetime,
) -> DeploymentResult | None:
    try:
        queue = await client.get_queue(deployment.queue_name)
    except SaladAPIError as error:
        if error.status_code == 404:
            return await _mark_failed(
                session,
                deployment,
                error_code="persisted_queue_missing",
                observed_at=observed_at,
            )
        return await _handle_read_error(
            session,
            deployment,
            error,
            code_prefix="queue_verify",
            observed_at=observed_at,
        )
    except (SaladProtocolError, SaladTransportError) as error:
        return await _handle_read_error(
            session,
            deployment,
            error,
            code_prefix="queue_verify",
            observed_at=observed_at,
        )
    if queue.name != deployment.queue_name or str(queue.id) != deployment.provider_queue_id:
        return await _mark_failed(
            session,
            deployment,
            error_code="persisted_queue_identity_mismatch",
            observed_at=observed_at,
        )
    return None


async def _adopt_queue(
    session: AsyncSession,
    deployment: SaladDeployment,
    queue: SaladQueue,
    observed_at: datetime,
) -> DeploymentResult:
    if queue.name != deployment.queue_name:
        return await _mark_failed(
            session,
            deployment,
            error_code="queue_name_collision",
            observed_at=observed_at,
        )
    deployment.provider_queue_id = str(queue.id)
    deployment.state = SaladDeploymentState.PROVISIONING
    deployment.unknown_since = None
    _clear_error(deployment)
    _touch(deployment)
    _audit(
        session,
        deployment,
        action="salad_deployment.queue_adopted",
        detail={"queue_id": str(queue.id), "queue_name": queue.name},
        occurred_at=observed_at,
    )
    await session.flush()
    return _result(deployment, DeploymentAction.QUEUE_ADOPTED)


async def _adopt_group(
    session: AsyncSession,
    deployment: SaladDeployment,
    group: SaladContainerGroup,
    observed_at: datetime,
) -> DeploymentResult:
    if group.name != deployment.container_group_name:
        return await _mark_failed(
            session,
            deployment,
            error_code="container_group_name_collision",
            observed_at=observed_at,
        )
    deployment.provider_container_group_id = str(group.id)
    drift_code = _group_configuration_drift(deployment, group)
    deployment.state = (
        SaladDeploymentState.PROVISIONING if drift_code is None else SaladDeploymentState.DEGRADED
    )
    deployment.unknown_since = None
    deployment.last_error_code = drift_code
    deployment.last_error_detail = (
        None if drift_code is None else "Existing provider group does not match the deployment."
    )
    _touch(deployment)
    _audit(
        session,
        deployment,
        action="salad_deployment.container_group_adopted",
        detail={
            "container_group_id": str(group.id),
            "container_group_name": group.name,
            "configuration_matches": drift_code is None,
        },
        occurred_at=observed_at,
    )
    await session.flush()
    return _result(
        deployment,
        DeploymentAction.GROUP_ADOPTED,
        error_code=drift_code,
    )


async def _get_queue_or_none(
    client: SaladDeploymentClient,
    name: str,
) -> SaladQueue | None:
    try:
        return await client.get_queue(name)
    except SaladAPIError as error:
        if error.status_code == 404:
            return None
        raise


async def _get_group_or_none(
    client: SaladDeploymentClient,
    name: str,
) -> SaladContainerGroup | None:
    try:
        return await client.get_container_group(name)
    except SaladAPIError as error:
        if error.status_code == 404:
            return None
        raise


def _remote_drift_code(
    deployment: SaladDeployment,
    queue: SaladQueue,
    group: SaladContainerGroup,
    *,
    effective_min_replicas: int | None = None,
) -> str | None:
    if queue.name != deployment.queue_name or str(queue.id) != deployment.provider_queue_id:
        return "provider_queue_identity_drift"
    if (
        group.name != deployment.container_group_name
        or str(group.id) != deployment.provider_container_group_id
    ):
        return "provider_group_identity_drift"
    return _group_configuration_drift(
        deployment,
        group,
        effective_min_replicas=effective_min_replicas,
    )


def _group_configuration_drift(
    deployment: SaladDeployment,
    group: SaladContainerGroup,
    *,
    effective_min_replicas: int | None = None,
) -> str | None:
    raw = group.raw
    container = raw.get("container")
    if not isinstance(container, dict) or container.get("image") != deployment.worker_image_digest:
        return "provider_image_drift"
    desired_container = deployment.provider_configuration.get("container")
    if not isinstance(desired_container, dict):
        return "provider_container_contract_drift"
    for key, expected_value in desired_container.items():
        if key in {"image", "priority"}:
            continue
        observed_value = container.get(key)
        if (
            key == "resources"
            and isinstance(observed_value, dict)
            and observed_value.get("shm_size") == _SALAD_DEFAULT_SHM_SIZE
        ):
            observed_value = {
                resource_key: resource_value
                for resource_key, resource_value in observed_value.items()
                if resource_key != "shm_size"
            }
        if not _matches_worker_contract(
            observed_value,
            expected_value,
            allow_null_extensions=True,
        ):
            return "provider_container_contract_drift"
    queue_connection = raw.get("queue_connection")
    if (
        not isinstance(queue_connection, dict)
        or queue_connection.get("queue_name") != deployment.queue_name
        or queue_connection.get("path") != _WORKER_QUEUE_PATH
        or not isinstance(queue_connection.get("port"), int)
        or isinstance(queue_connection.get("port"), bool)
        or queue_connection.get("port") != _WORKER_PORT
    ):
        return "provider_queue_connection_drift"
    if raw.get("restart_policy") != _WORKER_RESTART_POLICY:
        return "provider_restart_policy_drift"
    for probe_name, expected_probe in _WORKER_PROBE_CONTRACTS.items():
        if not _matches_worker_contract(
            raw.get(probe_name),
            expected_probe,
            allow_null_extensions=True,
        ):
            return f"provider_{probe_name}_drift"
    autoscaler = raw.get("queue_autoscaler")
    expected_autoscaler = _desired_queue_autoscaler(
        deployment,
        min_replicas=effective_min_replicas,
    )
    if not isinstance(autoscaler, dict) or not _matches_worker_contract(
        autoscaler,
        expected_autoscaler,
        allow_null_extensions=True,
    ):
        return "provider_autoscaler_drift"
    desired_priority = desired_container.get("priority")
    if desired_priority is None:
        # Support drift checks for deployment rows written before priority was
        # moved into the provider's nested container contract.
        desired_priority = deployment.provider_configuration.get("priority")
    if raw.get("priority") != desired_priority:
        return "provider_priority_drift"
    if group.replicas > deployment.max_replicas:
        return "provider_replica_limit_drift"
    return None


def _desired_queue_autoscaler(
    deployment: SaladDeployment,
    *,
    min_replicas: int | None = None,
) -> JSONObject:
    resolved_min_replicas = deployment.min_replicas if min_replicas is None else min_replicas
    if resolved_min_replicas not in {0, 1}:
        raise SaladDeploymentValidationError(
            "effective queue autoscaler minimum must be zero or one"
        )
    if resolved_min_replicas > deployment.max_replicas:
        raise SaladDeploymentValidationError(
            "effective queue autoscaler minimum exceeds the replica cap"
        )
    configured = deployment.provider_configuration.get("queue_autoscaler")
    desired = deepcopy(configured) if isinstance(configured, dict) else {}
    desired["min_replicas"] = resolved_min_replicas
    desired["max_replicas"] = deployment.max_replicas
    desired["desired_queue_length"] = deployment.desired_queue_length
    return cast(JSONObject, desired)


async def _handle_read_error(
    session: AsyncSession,
    deployment: SaladDeployment,
    error: SaladAPIError | SaladProtocolError | SaladTransportError,
    *,
    code_prefix: str,
    observed_at: datetime,
) -> DeploymentResult:
    if isinstance(error, SaladAPIError) and error.status_code == 404:
        error_code = f"{code_prefix}_not_found"
    elif isinstance(error, SaladRateLimitError):
        error_code = f"{code_prefix}_rate_limited"
    elif isinstance(error, SaladAPIError):
        error_code = f"{code_prefix}_http_{error.status_code}"
    elif isinstance(error, SaladProtocolError):
        error_code = f"{code_prefix}_protocol_error"
    else:
        error_code = f"{code_prefix}_transport_error"
    return await _mark_unknown(
        session,
        deployment,
        error_code=error_code,
        observed_at=observed_at,
    )


async def _handle_mutation_error(
    session: AsyncSession,
    deployment: SaladDeployment,
    error: SaladAPIError | SaladProtocolError | SaladTransportError,
    *,
    code_prefix: str,
    observed_at: datetime,
) -> DeploymentResult:
    if isinstance(error, SaladRateLimitError):
        deployment.state = (
            SaladDeploymentState.DEGRADED
            if _has_provider_resources(deployment)
            else SaladDeploymentState.PROVISIONING
        )
        deployment.last_error_code = f"{code_prefix}_rate_limited"
        deployment.last_error_detail = "Provider rate limited the mutation before retry."
        deployment.reconcile_after = observed_at + _RECONCILE_DELAY
        _touch(deployment)
        await session.flush()
        return _result(
            deployment,
            DeploymentAction.DEFERRED,
            error_code=deployment.last_error_code,
        )
    if isinstance(error, SaladAPIError) and error.status_code < 500:
        if error.status_code == 409:
            return await _mark_unknown(
                session,
                deployment,
                error_code=f"{code_prefix}_conflict_requires_reconcile",
                observed_at=observed_at,
            )
        return await _mark_failed(
            session,
            deployment,
            error_code=f"{code_prefix}_rejected",
            observed_at=observed_at,
        )
    if isinstance(error, SaladProtocolError):
        error_code = f"{code_prefix}_response_unknown"
    elif isinstance(error, SaladAPIError):
        error_code = f"{code_prefix}_http_{error.status_code}_unknown"
    else:
        error_code = f"{code_prefix}_transport_unknown"
    return await _mark_unknown(
        session,
        deployment,
        error_code=error_code,
        observed_at=observed_at,
    )


async def _mark_unknown(
    session: AsyncSession,
    deployment: SaladDeployment,
    *,
    error_code: str,
    observed_at: datetime,
) -> DeploymentResult:
    deployment.state = SaladDeploymentState.UNKNOWN
    deployment.unknown_since = deployment.unknown_since or observed_at
    deployment.last_error_code = error_code
    deployment.last_error_detail = (
        "Provider outcome is ambiguous; read-only reconciliation is required."
    )
    deployment.reconcile_after = observed_at + _RECONCILE_DELAY
    _touch(deployment)
    _audit(
        session,
        deployment,
        action="salad_deployment.provider_outcome_unknown",
        detail={"error_code": error_code},
        occurred_at=observed_at,
    )
    await session.flush()
    return _result(
        deployment,
        DeploymentAction.DEFERRED,
        error_code=error_code,
    )


async def _defer_unknown(
    session: AsyncSession,
    deployment: SaladDeployment,
    *,
    error_code: str,
    observed_at: datetime,
) -> DeploymentResult:
    return await _mark_unknown(
        session,
        deployment,
        error_code=error_code,
        observed_at=observed_at,
    )


async def _mark_failed(
    session: AsyncSession,
    deployment: SaladDeployment,
    *,
    error_code: str,
    observed_at: datetime,
) -> DeploymentResult:
    deployment.state = (
        SaladDeploymentState.DEGRADED
        if _has_provider_resources(deployment)
        else SaladDeploymentState.FAILED
    )
    deployment.last_error_code = error_code
    deployment.last_error_detail = (
        "Deployment provisioning failed validation or provider rejection."
    )
    deployment.reconcile_after = None
    _touch(deployment)
    _audit(
        session,
        deployment,
        action="salad_deployment.provisioning_failed",
        detail={"error_code": error_code},
        occurred_at=observed_at,
    )
    await session.flush()
    return _result(
        deployment,
        DeploymentAction.FAILED,
        error_code=error_code,
    )


def _engage_local_kill_switch(
    session: AsyncSession,
    deployment: SaladDeployment,
    *,
    error_code: str,
    observed_at: datetime,
) -> None:
    changed = deployment.desired_state != DesiredDeploymentState.STOPPED
    deployment.desired_state = DesiredDeploymentState.STOPPED
    deployment.last_error_code = error_code
    deployment.last_error_detail = "Provider execution is disabled by the hard budget guard."
    deployment.reconcile_after = observed_at
    if changed:
        _touch(deployment)
        _audit(
            session,
            deployment,
            action="salad_deployment.kill_switch_engaged",
            detail={"error_code": error_code},
            occurred_at=observed_at,
        )


def _assign_deterministic_names(deployment: SaladDeployment) -> None:
    queue_name = deterministic_provider_name(
        deployment.queue_name,
        version_no=deployment.version_no,
        config_sha256=deployment.config_sha256,
    )
    group_name = deterministic_provider_name(
        deployment.container_group_name,
        version_no=deployment.version_no,
        config_sha256=deployment.config_sha256,
    )
    if deployment.queue_name != queue_name or deployment.container_group_name != group_name:
        deployment.queue_name = queue_name
        deployment.container_group_name = group_name
        _touch(deployment)


def _validate_local_deployment(deployment: SaladDeployment) -> None:
    if _IMAGE_DIGEST_PATTERN.fullmatch(deployment.worker_image_digest) is None:
        raise SaladDeploymentValidationError("worker image must use an immutable sha256 digest")
    if (
        deployment.min_replicas != 0
        or deployment.max_replicas != 1
        or deployment.desired_queue_length != 1
    ):
        raise SaladDeploymentValidationError(
            "v1 requires autoscaler min 0, max 1, and desired queue length 1"
        )
    if deployment.max_hourly_cost_microusd <= 0:
        raise SaladDeploymentValidationError("maximum hourly cost must be positive")


async def _load_deployment_locked(
    session: AsyncSession,
    deployment_id: UUID,
) -> SaladDeployment:
    deployment = await session.scalar(
        select(SaladDeployment).where(SaladDeployment.id == deployment_id).with_for_update()
    )
    if deployment is None:
        raise SaladDeploymentNotFoundError("Salad deployment was not found")
    return deployment


def _has_provider_resources(deployment: SaladDeployment) -> bool:
    return (
        deployment.provider_queue_id is not None
        and deployment.provider_container_group_id is not None
    )


def _stop_is_desired(deployment: SaladDeployment) -> bool:
    return deployment.desired_state == DesiredDeploymentState.STOPPED


def _retire_legacy_video_deployment(
    session: AsyncSession,
    deployment: SaladDeployment,
    observed_at: datetime,
) -> None:
    """Force historical video lanes toward STOPPED without reviving their runtime."""

    if deployment.purpose != SaladDeploymentPurpose.VIDEO:
        return
    previous_desired_state = deployment.desired_state
    deployment.desired_state = DesiredDeploymentState.STOPPED
    deployment.administrative_stop_reason = "video_generation_retired"
    deployment.reconcile_after = observed_at
    if previous_desired_state == DesiredDeploymentState.STOPPED:
        return
    _touch(deployment)
    _audit(
        session,
        deployment,
        action="salad_deployment.video_generation_retired",
        detail={"previous_desired_state": previous_desired_state.value},
        occurred_at=observed_at,
    )


def _touch(deployment: SaladDeployment) -> None:
    deployment.lock_version += 1


def _clear_error(deployment: SaladDeployment) -> None:
    deployment.last_error_code = None
    deployment.last_error_detail = None


def _result(
    deployment: SaladDeployment,
    action: DeploymentAction,
    *,
    metered_microusd: int = 0,
    error_code: str | None = None,
) -> DeploymentResult:
    return DeploymentResult(
        deployment_id=deployment.id,
        action=action,
        state=deployment.state,
        provider_queue_id=deployment.provider_queue_id,
        provider_container_group_id=deployment.provider_container_group_id,
        metered_microusd=metered_microusd,
        error_code=error_code,
    )


def _audit(
    session: AsyncSession,
    deployment: SaladDeployment,
    *,
    action: str,
    detail: Mapping[str, str | int | bool],
    occurred_at: datetime,
) -> None:
    safe_detail: dict[str, Any] = dict(detail)
    session.add(
        AuditEvent(
            actor="salad-deployment-controller",
            action=action,
            resource_type="salad_deployment",
            resource_id=deployment.id,
            correlation_id=f"salad-deployment:{deployment.id}",
            detail=safe_detail,
            occurred_at=occurred_at,
        )
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SaladDeploymentValidationError("deployment timestamps must include a timezone")
    return value.astimezone(UTC)


def _resolve_billing_observation_clock(
    *,
    explicit_now: datetime | None,
    observed_at: datetime,
    clock: Callable[[], datetime] | None,
) -> Callable[[], datetime]:
    if clock is not None:
        return clock
    if explicit_now is not None:
        # ``now`` is the service's deterministic test/replay boundary. Preserve
        # that behavior unless a post-response clock is supplied explicitly.
        return lambda: observed_at
    return _utc_now


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stored_as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
