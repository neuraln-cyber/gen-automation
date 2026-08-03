from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from time import monotonic
from typing import cast
from uuid import UUID, uuid4

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.config import Settings
from gen_automation.db.models import (
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    ProviderBudgetGuard,
    SaladDeployment,
)
from gen_automation.domain.enums import (
    DesiredDeploymentState,
    GenerationAttemptState,
    GenerationState,
    SaladDeploymentState,
)
from gen_automation.gpu_worker.bootstrap import load_artifact_manifest
from gen_automation.integrations.mega import MegaCmdClient
from gen_automation.integrations.patreon import PatreonPublicationDriver
from gen_automation.integrations.salad.client import SaladClient
from gen_automation.integrations.salad.models import JSONValue
from gen_automation.integrations.semantic_vlm import SemanticVlmClient
from gen_automation.quality import DEFAULT_QUALITY_CONFIG
from gen_automation.semantic import SemanticAssessmentResult
from gen_automation.services.budgets import ensure_budget_guard
from gen_automation.services.collection import (
    ClaimedCollectionJob,
    claim_collection_jobs,
    collect_generation_job,
)
from gen_automation.services.derivative_isolation import DerivativeIsolationPolicy
from gen_automation.services.derivative_runtime import run_derivative_cycle
from gen_automation.services.mega_delivery import (
    MegaDeliveryClient,
    run_mega_delivery_cycle,
)
from gen_automation.services.outbox import (
    GENERATION_ATTEMPT_AGGREGATE,
    SALAD_JOB_SUBMIT_TOPIC,
    ClaimedOutboxEvent,
    ExternalEffect,
    OutboxError,
    claim_outbox_events,
    fail_outbox_event,
    succeed_outbox_event,
)
from gen_automation.services.publication_runtime import (
    XOAuthProvider,
    run_publication_cycle,
)
from gen_automation.services.quality_isolation import QualityIsolationPolicy
from gen_automation.services.quality_runtime import run_quality_cycle
from gen_automation.services.runtime_secrets import (
    RuntimeSecretResolver,
    configured_runtime_binding_references,
)
from gen_automation.services.salad import (
    SaladDeploymentConfig,
    SaladUploadIntentProvider,
    SubmissionDisposition,
    SubmissionResult,
    create_deployment_version,
    fail_definitely_unstarted_submission,
    reconcile_generation_attempt,
    submit_prepared_attempt,
)
from gen_automation.services.salad_deployments import (
    provision_deployment_step,
    reconcile_deployment,
    refresh_container_group_runtime,
)
from gen_automation.services.salad_inbox import (
    ClaimedSaladWebhook,
    SaladInboxError,
    claim_salad_webhook_receipts,
    fail_salad_webhook_receipt,
    process_salad_webhook_receipt,
)
from gen_automation.services.scheduling import dispatch_generation_jobs
from gen_automation.services.semantic_anatomy import (
    SemanticAssessmentProfile,
    run_semantic_assessment_cycle,
)
from gen_automation.services.worker_inputs import SaladWorkerJobInputProvider
from gen_automation.storage.base import ObjectStore

logger = structlog.get_logger(__name__)

type LoopCycle = Callable[[], Awaitable[bool]]
type StartupHook = Callable[[], Awaitable[None]]
type JitterSource = Callable[[], float]

_MAX_DELAY_JITTER_MULTIPLIER = 1.2
_RECONCILABLE_ATTEMPT_STATES = (
    GenerationAttemptState.UNKNOWN,
    GenerationAttemptState.SUBMITTED,
    GenerationAttemptState.RUNNING,
    GenerationAttemptState.CANCEL_REQUESTED,
)
_RUNTIME_REFRESH_BLOCKING_ATTEMPT_STATES = (
    GenerationAttemptState.SUBMITTING,
    GenerationAttemptState.SUBMITTED,
    GenerationAttemptState.RUNNING,
    GenerationAttemptState.UNKNOWN,
    GenerationAttemptState.CANCEL_REQUESTED,
)
_DEPLOYMENT_WORK_STATES = (
    SaladDeploymentState.PLANNED,
    SaladDeploymentState.PROVISIONING,
    SaladDeploymentState.ACTIVE,
    SaladDeploymentState.DEGRADED,
    SaladDeploymentState.DRAINING,
    SaladDeploymentState.UNKNOWN,
    SaladDeploymentState.FAILED,
)


def salad_deployment_config_from_settings(settings: Settings) -> SaladDeploymentConfig:
    """Build the immutable, secret-free Salad deployment intent."""

    required_names = (
        settings.salad_organization,
        settings.salad_project,
        settings.salad_queue_name,
        settings.salad_container_group_name,
        settings.salad_worker_image,
    )
    if (
        not settings.salad_enabled
        or not settings.gpu_allocation_enabled
        or any(value is None for value in required_names)
        or not settings.salad_gpu_class_ids
    ):
        raise ValueError("validated Salad GPU bootstrap settings are incomplete")

    organization, project, queue, container_group, worker_image = required_names
    assert organization is not None
    assert project is not None
    assert queue is not None
    assert container_group is not None
    assert worker_image is not None

    runtime_bindings = configured_runtime_binding_references(settings)
    provider_configuration: dict[str, JSONValue] = {
        "container": {
            "resources": {
                "cpu": settings.salad_container_cpu,
                "memory": settings.salad_container_memory_mb,
                "storage_amount": settings.salad_container_storage_bytes,
                "gpu_classes": [str(gpu_class_id) for gpu_class_id in settings.salad_gpu_class_ids],
            },
            "image_caching": True,
            "priority": "low",
        },
        "replicas": 0,
        "queue_connection": {},
        # SaladCloud supports a minimum 15-second queue polling period. Use it
        # so scale-to-zero workers notice the first queued image job as soon as
        # the provider allows, without keeping a GPU replica running while idle.
        "queue_autoscaler": {"polling_period": 15},
        "runtime_bindings": [
            {"name": name, "reference": reference} for name, reference in runtime_bindings.items()
        ],
    }
    return SaladDeploymentConfig(
        organization_name=organization,
        project_name=project,
        queue_name=queue,
        container_group_name=container_group,
        worker_image_digest=worker_image,
        max_hourly_cost_microusd=int(settings.salad_max_hourly_cost_usd * 1_000_000),
        provider_configuration=provider_configuration,
        min_replicas=0,
        max_replicas=settings.salad_max_replicas,
        # This is the provider's per-replica autoscaling target, not the
        # controller's bounded prefetch runway. One queued job should be enough
        # to keep the single allowed replica allocated.
        desired_queue_length=1,
    )


class RuntimeStatus(StrEnum):
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True)
class LoopSpec:
    name: str
    cycle: LoopCycle
    idle_interval_seconds: float
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 100:
            raise ValueError("controller loop name is invalid")
        if self.idle_interval_seconds <= 0:
            raise ValueError("controller loop interval must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("controller loop timeout must be positive")


@dataclass(frozen=True)
class LoopSnapshot:
    name: str
    task_alive: bool
    iterations: int
    consecutive_failures: int
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error_type: str | None
    stale: bool


@dataclass(frozen=True)
class ControllerRuntimeSnapshot:
    instance_id: str
    status: RuntimeStatus
    ready: bool
    started_at: datetime | None
    loops: tuple[LoopSnapshot, ...]
    live: bool

    @property
    def failed_loops(self) -> tuple[str, ...]:
        return tuple(
            loop.name
            for loop in self.loops
            if not loop.task_alive or loop.consecutive_failures > 0 or loop.stale
        )


@dataclass
class _LoopState:
    iterations: int = 0
    consecutive_failures: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error_type: str | None = None
    last_completion_monotonic: float | None = None


@dataclass
class _SubmissionProgress:
    provider_post_started: bool = False

    def mark_provider_post_started(self) -> None:
        self.provider_post_started = True


class ControllerRuntime:
    """Supervise bounded controller loops without allowing silent task death."""

    def __init__(
        self,
        *,
        instance_id: str,
        loops: Sequence[LoopSpec],
        startup: StartupHook | None = None,
        error_backoff_max_seconds: float = 60,
        shutdown_grace_seconds: float = 30,
        shutdown_cancel_seconds: float = 5,
        readiness_failure_threshold: int = 3,
        liveness_failure_threshold: int = 10,
        stale_after_seconds: float = 900,
        jitter: JitterSource | None = None,
    ) -> None:
        if not instance_id or len(instance_id) > 120:
            raise ValueError("controller instance ID is invalid")
        if error_backoff_max_seconds <= 0:
            raise ValueError("controller error backoff must be positive")
        if shutdown_grace_seconds <= 0:
            raise ValueError("controller shutdown grace must be positive")
        if shutdown_cancel_seconds <= 0:
            raise ValueError("controller shutdown cancellation timeout must be positive")
        if readiness_failure_threshold <= 0:
            raise ValueError("controller readiness failure threshold must be positive")
        if liveness_failure_threshold < readiness_failure_threshold:
            raise ValueError(
                "controller liveness failure threshold cannot be lower than readiness threshold"
            )
        if stale_after_seconds <= 0:
            raise ValueError("controller loop staleness threshold must be positive")
        maximum_completion_interval = (
            max(
                (
                    loop.timeout_seconds
                    + (
                        _MAX_DELAY_JITTER_MULTIPLIER
                        * max(loop.idle_interval_seconds, error_backoff_max_seconds)
                    )
                )
                for loop in loops
            )
            if loops
            else 0
        )
        if stale_after_seconds <= maximum_completion_interval:
            raise ValueError(
                "controller loop staleness threshold must exceed every loop timeout "
                "plus its maximum jittered delay"
            )
        names = [loop.name for loop in loops]
        if len(names) != len(set(names)):
            raise ValueError("controller loop names must be unique")

        self.instance_id = instance_id
        self._loop_specs = tuple(loops)
        self._startup = startup
        self._error_backoff_max_seconds = error_backoff_max_seconds
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._shutdown_cancel_seconds = shutdown_cancel_seconds
        self._readiness_failure_threshold = readiness_failure_threshold
        self._liveness_failure_threshold = liveness_failure_threshold
        self._stale_after_seconds = stale_after_seconds
        system_random = random.SystemRandom()
        self._jitter = jitter or system_random.random
        self._stop_event = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._states = {loop.name: _LoopState() for loop in self._loop_specs}
        self._started_at: datetime | None = None
        self._started_monotonic: float | None = None
        self._stopping = False
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        if any(not task.done() for task in self._tasks.values()):
            raise RuntimeError("controller runtime has loops left over from an incomplete shutdown")
        self._stop_event.clear()
        self._stopping = False
        if self._startup is not None:
            await self._startup()
        self._started_at = datetime.now(UTC)
        self._started_monotonic = monotonic()
        self._states = {loop.name: _LoopState() for loop in self._loop_specs}
        self._tasks = {}
        self._started = True
        for spec in self._loop_specs:
            task = asyncio.create_task(
                self._run_loop(spec),
                name=f"controller:{spec.name}:{self.instance_id}",
            )
            task.add_done_callback(partial(self._task_done, spec.name))
            self._tasks[spec.name] = task
        logger.info(
            "controller_runtime_started",
            controller_instance_id=self.instance_id,
            loop_count=len(self._loop_specs),
        )

    async def stop(self) -> None:
        if not self._started and not any(not task.done() for task in self._tasks.values()):
            return
        self._stopping = True
        self._stop_event.set()
        cleanup_task = asyncio.create_task(
            self._stop_tasks(),
            name=f"controller:shutdown:{self.instance_id}",
        )
        caller_cancelled = False
        lingering: tuple[str, ...] = ()
        try:
            while True:
                try:
                    lingering = await asyncio.shield(cleanup_task)
                    break
                except asyncio.CancelledError:
                    if cleanup_task.done():
                        # This is cancellation of the cleanup task itself rather
                        # than its caller; propagate it instead of spinning.
                        await cleanup_task
                        raise
                    current_task = asyncio.current_task()
                    if current_task is not None:
                        current_task.uncancel()
                    caller_cancelled = True
                    # The caller may be cancelled during ASGI teardown. The loop
                    # cleanup remains shielded and has its own hard deadlines.
        finally:
            self._started = False
            self._stopping = False
        if lingering:
            logger.critical(
                "controller_runtime_shutdown_incomplete",
                controller_instance_id=self.instance_id,
                lingering_loops=lingering,
                cancellation_timeout_seconds=self._shutdown_cancel_seconds,
            )
        logger.info(
            "controller_runtime_stopped",
            controller_instance_id=self.instance_id,
            lingering_loop_count=len(lingering),
        )
        if caller_cancelled:
            raise asyncio.CancelledError

    async def _stop_tasks(self) -> tuple[str, ...]:
        tasks = tuple(self._tasks.values())
        if not tasks:
            return ()
        completed, pending = await asyncio.wait(
            tasks,
            timeout=self._shutdown_grace_seconds,
        )
        for task in pending:
            task.cancel()
        if pending:
            cancelled, lingering = await asyncio.wait(
                pending,
                timeout=self._shutdown_cancel_seconds,
            )
            completed.update(cancelled)
        else:
            lingering = set()
        if completed:
            await asyncio.gather(*completed, return_exceptions=True)
        for task in lingering:
            # Reassert cancellation before detaching. asyncio cannot forcibly
            # terminate a coroutine that deliberately suppresses cancellation.
            task.cancel()
        return tuple(sorted(name for name, task in self._tasks.items() if task in lingering))

    def snapshot(self) -> ControllerRuntimeSnapshot:
        now_monotonic = monotonic()
        freshness_baseline = self._started_monotonic
        loops = tuple(
            LoopSnapshot(
                name=spec.name,
                task_alive=((task := self._tasks.get(spec.name)) is not None and not task.done()),
                iterations=self._states[spec.name].iterations,
                consecutive_failures=self._states[spec.name].consecutive_failures,
                last_success_at=self._states[spec.name].last_success_at,
                last_failure_at=self._states[spec.name].last_failure_at,
                last_error_type=self._states[spec.name].last_error_type,
                stale=self._is_stale(
                    self._states[spec.name],
                    now_monotonic=now_monotonic,
                    freshness_baseline=freshness_baseline,
                ),
            )
            for spec in self._loop_specs
        )
        all_alive = all(loop.task_alive for loop in loops)
        all_initialized = all(loop.last_success_at is not None for loop in loops)
        readiness_failed = any(
            loop.stale or loop.consecutive_failures >= self._readiness_failure_threshold
            for loop in loops
        )
        liveness_failed = any(
            loop.stale or loop.consecutive_failures >= self._liveness_failure_threshold
            for loop in loops
        )
        ready = (
            self._started
            and not self._stopping
            and all_alive
            and all_initialized
            and not readiness_failed
        )
        live = self._started and not self._stopping and all_alive and not liveness_failed
        if self._stopping:
            status = RuntimeStatus.STOPPING
        elif not self._started:
            status = RuntimeStatus.STOPPED
        elif any(
            not loop.task_alive or loop.consecutive_failures > 0 or loop.stale for loop in loops
        ):
            status = RuntimeStatus.DEGRADED
        elif not all_initialized:
            status = RuntimeStatus.STARTING
        else:
            status = RuntimeStatus.HEALTHY
        return ControllerRuntimeSnapshot(
            instance_id=self.instance_id,
            status=status,
            ready=ready,
            started_at=self._started_at,
            loops=loops,
            live=live,
        )

    async def _run_loop(self, spec: LoopSpec) -> None:
        state = self._states[spec.name]
        while not self._stop_event.is_set():
            worked = False
            try:
                async with asyncio.timeout(spec.timeout_seconds):
                    worked = await spec.cycle()
            except TimeoutError:
                self._record_failure(state, "TimeoutError")
                logger.error(
                    "controller_loop_timed_out",
                    controller_instance_id=self.instance_id,
                    loop=spec.name,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._record_failure(state, type(error).__name__)
                logger.exception(
                    "controller_loop_failed",
                    controller_instance_id=self.instance_id,
                    loop=spec.name,
                    error_type=type(error).__name__,
                )
            else:
                state.iterations += 1
                state.consecutive_failures = 0
                state.last_success_at = datetime.now(UTC)
                state.last_error_type = None
                state.last_completion_monotonic = monotonic()

            if self._stop_event.is_set():
                return
            if state.consecutive_failures:
                base_delay = min(
                    spec.idle_interval_seconds * (2 ** min(state.consecutive_failures - 1, 8)),
                    self._error_backoff_max_seconds,
                )
            elif worked:
                await asyncio.sleep(0)
                continue
            else:
                base_delay = spec.idle_interval_seconds
            delay = base_delay * (0.8 + (0.4 * self._bounded_jitter()))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass

    def _record_failure(self, state: _LoopState, error_type: str) -> None:
        state.iterations += 1
        state.consecutive_failures += 1
        state.last_failure_at = datetime.now(UTC)
        state.last_error_type = error_type
        state.last_completion_monotonic = monotonic()

    def _is_stale(
        self,
        state: _LoopState,
        *,
        now_monotonic: float,
        freshness_baseline: float | None,
    ) -> bool:
        if not self._started or freshness_baseline is None:
            return False
        last_completion = state.last_completion_monotonic or freshness_baseline
        return now_monotonic - last_completion > self._stale_after_seconds

    def _bounded_jitter(self) -> float:
        return min(max(self._jitter(), 0.0), 1.0)

    def _task_done(self, loop_name: str, task: asyncio.Task[None]) -> None:
        if task.cancelled() or self._stopping or self._stop_event.is_set():
            return
        error = task.exception()
        error_type = type(error).__name__ if error is not None else "UnexpectedExit"
        self._record_failure(self._states[loop_name], error_type)
        logger.error(
            "controller_loop_exited",
            controller_instance_id=self.instance_id,
            loop=loop_name,
            error_type=error_type,
        )


class ControllerWorkloads:
    """One bounded unit of durable work for each controller loop."""

    def __init__(
        self,
        *,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        instance_id: str,
        salad_client: SaladClient | None,
        object_store: ObjectStore | None,
        secret_resolver: RuntimeSecretResolver | None = None,
        x_oauth_provider: XOAuthProvider | None = None,
        mega_client: MegaDeliveryClient | None = None,
        semantic_vlm_client: SemanticVlmClient | None = None,
        patreon_driver: PatreonPublicationDriver | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.instance_id = instance_id
        self.salad_client = salad_client
        self.object_store = object_store
        self.secret_resolver = secret_resolver
        self.x_oauth_provider = x_oauth_provider
        self.mega_client = mega_client
        self.semantic_vlm_client = semantic_vlm_client
        self.patreon_driver = patreon_driver
        self._next_attempt_reconciliation: dict[UUID, float] = {}

    async def initialize(self) -> None:
        if self.salad_client is None:
            return
        async with self.sessions() as session:
            now = datetime.now(UTC)
            await ensure_budget_guard(
                session,
                provider="salad",
                daily_limit_usd=self.settings.salad_daily_budget_usd,
                monthly_limit_usd=self.settings.salad_monthly_budget_usd,
            )
            if self.settings.salad_enabled and self.settings.gpu_allocation_enabled:
                await create_deployment_version(
                    session,
                    salad_deployment_config_from_settings(self.settings),
                    actor=self._worker_id("bootstrap"),
                    now=now,
                )
            else:
                await self._disable_gpu_allocations(session, now=now)
            await session.commit()

    async def budget_once(self) -> bool:
        if self.salad_client is None:
            return False
        async with self.sessions() as session:
            await ensure_budget_guard(
                session,
                provider="salad",
                daily_limit_usd=self.settings.salad_daily_budget_usd,
                monthly_limit_usd=self.settings.salad_monthly_budget_usd,
            )
            await session.commit()
        return False

    async def deployment_once(self) -> bool:
        if self.salad_client is None:
            return False
        selected_id: UUID | None = None

        async def operation() -> bool:
            nonlocal selected_id
            now = datetime.now(UTC)
            remote_complete = and_(
                SaladDeployment.provider_queue_id.is_not(None),
                SaladDeployment.provider_container_group_id.is_not(None),
            )
            due = or_(
                SaladDeployment.reconcile_after.is_(None),
                SaladDeployment.reconcile_after <= now,
            )
            reconcilable = and_(
                SaladDeployment.state.in_(_DEPLOYMENT_WORK_STATES),
                remote_complete,
            )
            stoppable = and_(
                SaladDeployment.desired_state == DesiredDeploymentState.STOPPED,
                SaladDeployment.state.in_(
                    (
                        SaladDeploymentState.PLANNED,
                        SaladDeploymentState.PROVISIONING,
                        SaladDeploymentState.ACTIVE,
                        SaladDeploymentState.DEGRADED,
                        SaladDeploymentState.DRAINING,
                        SaladDeploymentState.UNKNOWN,
                    )
                ),
            )
            predicates = [reconcilable, stoppable]
            if self.settings.gpu_allocation_enabled:
                predicates.append(
                    and_(
                        SaladDeployment.state.in_(_DEPLOYMENT_WORK_STATES),
                        SaladDeployment.desired_state == DesiredDeploymentState.ACTIVE,
                        ~remote_complete,
                    )
                )

            async with self.sessions() as session:
                await self._lock_budget_guard(session)
                disabled_count = await self._disable_gpu_allocations(session, now=now)
                deployment = await session.scalar(
                    select(SaladDeployment)
                    .where(due, or_(*predicates))
                    .order_by(
                        SaladDeployment.reconcile_after,
                        SaladDeployment.version_no,
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if deployment is None:
                    if disabled_count:
                        await session.commit()
                        return True
                    await session.rollback()
                    return False
                selected_id = deployment.id
                if (
                    deployment.provider_queue_id is not None
                    and deployment.provider_container_group_id is not None
                ):
                    await reconcile_deployment(
                        session,
                        deployment_id=deployment.id,
                        client=self.salad_client,  # type: ignore[arg-type]
                        now=now,
                    )
                else:
                    await provision_deployment_step(
                        session,
                        deployment_id=deployment.id,
                        client=self.salad_client,  # type: ignore[arg-type]
                        secret_resolver=self.secret_resolver,
                        now=now,
                    )
                await session.commit()
                return True

        try:
            async with asyncio.timeout(self.settings.background_deployment_timeout_seconds):
                return await operation()
        except asyncio.CancelledError:
            if selected_id is not None:
                await self._mark_deployment_unknown(
                    selected_id,
                    error_code="controller_shutdown_during_provider_operation",
                )
            raise
        except TimeoutError:
            if selected_id is not None:
                await self._mark_deployment_unknown(
                    selected_id,
                    error_code="controller_provider_operation_timed_out",
                )
            raise
        except Exception:
            if selected_id is not None:
                await self._mark_deployment_unknown(
                    selected_id,
                    error_code="controller_provider_operation_failed",
                )
            raise

    async def dispatch_once(self) -> bool:
        if self.salad_client is None or not self.settings.gpu_allocation_enabled:
            return False
        async with self.sessions() as session:
            deployment_id = await session.scalar(
                select(SaladDeployment.id)
                .where(
                    SaladDeployment.is_current.is_(True),
                    SaladDeployment.state == SaladDeploymentState.ACTIVE,
                    SaladDeployment.desired_state == DesiredDeploymentState.ACTIVE,
                )
                .limit(1)
            )
            if deployment_id is None:
                await session.rollback()
                return False
            result = await dispatch_generation_jobs(
                session,
                salad_deployment_id=deployment_id,
                gpu_allocation_enabled=True,
                max_inflight=self.settings.salad_max_queued_jobs,
                limit=self.settings.salad_max_queued_jobs,
                actor=self._worker_id("dispatch"),
            )
            return bool(result.dispatched)

    async def submit_once(self) -> bool:
        if (
            self.salad_client is None
            or not self.settings.gpu_allocation_enabled
            or self.object_store is None
        ):
            return False
        worker_id = self._worker_id("submit")
        async with self.sessions() as session:
            events = await claim_outbox_events(
                session,
                worker_id=worker_id,
                limit=1,
                lease_seconds=self.settings.background_outbox_lease_seconds,
                topics=(SALAD_JOB_SUBMIT_TOPIC,),
            )
        if not events:
            return False
        event = events[0]
        if event.aggregate_type != GENERATION_ATTEMPT_AGGREGATE:
            await self._fail_submit_lease(
                event,
                worker_id=worker_id,
                error_code="invalid_submit_event_contract",
                external_effect=ExternalEffect.DEFINITELY_NOT_STARTED,
                retry_not_before=None,
            )
            return True

        progress = _SubmissionProgress()
        try:
            async with asyncio.timeout(self.settings.background_submit_timeout_seconds):
                result = await self._submit_event(event, progress=progress)
                await self._transition_submit_result(
                    event,
                    result=result,
                    worker_id=worker_id,
                )
        except asyncio.CancelledError:
            if progress.provider_post_started:
                await self._fail_submit_lease(
                    event,
                    worker_id=worker_id,
                    error_code="controller_shutdown_during_submission",
                    external_effect=ExternalEffect.MAY_HAVE_STARTED,
                    retry_not_before=None,
                )
            else:
                await self._resolve_definitely_unstarted_submit(
                    event,
                    worker_id=worker_id,
                    error_code="controller_shutdown_before_submission",
                )
            raise
        except TimeoutError:
            if progress.provider_post_started:
                await self._fail_submit_lease(
                    event,
                    worker_id=worker_id,
                    error_code="controller_submission_timed_out",
                    external_effect=ExternalEffect.MAY_HAVE_STARTED,
                    retry_not_before=None,
                )
            else:
                await self._resolve_definitely_unstarted_submit(
                    event,
                    worker_id=worker_id,
                    error_code="controller_submission_pre_post_timed_out",
                )
            raise
        except Exception:
            if progress.provider_post_started:
                await self._fail_submit_lease(
                    event,
                    worker_id=worker_id,
                    error_code="controller_submission_failed",
                    external_effect=ExternalEffect.MAY_HAVE_STARTED,
                    retry_not_before=None,
                )
            else:
                await self._resolve_definitely_unstarted_submit(
                    event,
                    worker_id=worker_id,
                    error_code="controller_submission_failed_before_post",
                )
            raise
        return True

    async def inbox_once(self) -> bool:
        if self.salad_client is None:
            return False
        worker_id = self._worker_id("inbox")
        async with self.sessions() as session:
            receipts = await claim_salad_webhook_receipts(
                session,
                worker_id=worker_id,
                limit=1,
                lease_seconds=self.settings.background_inbox_lease_seconds,
            )
        if not receipts:
            return False
        receipt = receipts[0]
        try:
            async with asyncio.timeout(self.settings.background_inbox_timeout_seconds):
                async with self.sessions() as session:
                    await process_salad_webhook_receipt(
                        session,
                        receipt_id=receipt.receipt_id,
                        worker_id=worker_id,
                        retry_delay_seconds=self.settings.background_retry_delay_seconds,
                    )
        except asyncio.CancelledError:
            await self._fail_inbox_lease(receipt, worker_id=worker_id)
            raise
        except TimeoutError:
            await self._fail_inbox_lease(receipt, worker_id=worker_id)
            raise
        except Exception:
            await self._fail_inbox_lease(receipt, worker_id=worker_id)
            raise
        return True

    async def reconcile_attempt_once(self) -> bool:
        if self.salad_client is None:
            return False
        now_monotonic = monotonic()
        self._next_attempt_reconciliation = {
            attempt_id: due_at
            for attempt_id, due_at in self._next_attempt_reconciliation.items()
            if due_at > now_monotonic
        }
        suppressed_ids = tuple(self._next_attempt_reconciliation)

        async def operation() -> bool:
            async with self.sessions() as session:
                await self._lock_budget_guard(session)
                query = (
                    select(GenerationAttempt)
                    .where(
                        GenerationAttempt.provider == "salad",
                        GenerationAttempt.state.in_(_RECONCILABLE_ATTEMPT_STATES),
                    )
                    .order_by(
                        GenerationAttempt.last_observed_at,
                        GenerationAttempt.created_at,
                        GenerationAttempt.id,
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if suppressed_ids:
                    query = query.where(GenerationAttempt.id.not_in(suppressed_ids))
                attempt = await session.scalar(query)
                if attempt is None:
                    await session.rollback()
                    return False
                self._next_attempt_reconciliation[attempt.id] = (
                    monotonic() + self.settings.background_reconciliation_interval_seconds
                )
                await reconcile_generation_attempt(
                    session,
                    self.salad_client,  # type: ignore[arg-type]
                    generation_attempt_id=attempt.id,
                    max_list_pages=2,
                    list_page_size=100,
                )
                return True

        async with asyncio.timeout(self.settings.background_reconcile_timeout_seconds):
            return await operation()

    async def collection_once(self) -> bool:
        if self.object_store is None:
            return False
        worker_id = self._worker_id("collection")
        async with self.sessions() as session:
            jobs = await claim_collection_jobs(
                session,
                worker_id=worker_id,
                limit=1,
                lease_seconds=self.settings.background_collection_lease_seconds,
            )
        if not jobs:
            return False
        job = jobs[0]
        try:
            async with asyncio.timeout(self.settings.background_collection_timeout_seconds):
                async with self.sessions() as session:
                    await collect_generation_job(
                        session,
                        self.object_store,
                        job_id=job.job_id,
                        worker_id=worker_id,
                        max_image_bytes=self.settings.storage_max_image_bytes,
                        retry_delay_seconds=self.settings.background_retry_delay_seconds,
                        verification_lease_seconds=(
                            self.settings.storage_verification_lease_seconds
                        ),
                        upload_grant_ttl_seconds=(self.settings.worker_upload_grant_ttl_seconds),
                    )
        except asyncio.CancelledError:
            await self._defer_collection_lease(job, worker_id=worker_id)
            raise
        except TimeoutError:
            await self._defer_collection_lease(job, worker_id=worker_id)
            raise
        except Exception:
            await self._defer_collection_lease(job, worker_id=worker_id)
            raise
        return True

    async def quality_once(self) -> bool:
        if self.object_store is None or not self.settings.quality_scoring_enabled:
            return False
        result = await run_quality_cycle(
            self.sessions,
            self.object_store,
            worker_id=self._worker_id("quality"),
            config=DEFAULT_QUALITY_CONFIG,
            max_attempts=self.settings.background_quality_max_attempts,
            lease_seconds=self.settings.background_quality_lease_seconds,
            retry_base_seconds=self.settings.background_quality_retry_base_seconds,
            retry_max_seconds=self.settings.background_quality_retry_max_seconds,
            policy=QualityIsolationPolicy(
                wall_timeout_seconds=(self.settings.background_quality_analysis_timeout_seconds),
                memory_limit_bytes=self.settings.background_quality_memory_limit_bytes,
            ),
        )
        return result.did_work

    async def semantic_anatomy_once(self) -> bool:
        client = self.semantic_vlm_client
        revision = self.settings.semantic_anatomy_model_revision
        if (
            self.object_store is None
            or client is None
            or revision is None
            or not self.settings.semantic_anatomy_enabled
        ):
            return False

        async def analyze(
            payload: bytes,
            content_type: str,
            sha256: str,
        ) -> SemanticAssessmentResult:
            return await client.assess(
                payload,
                content_type=content_type,
                asset_sha256=sha256,
            )

        result = await run_semantic_assessment_cycle(
            self.sessions,
            self.object_store,
            worker_id=self._worker_id("semantic-anatomy"),
            profile=SemanticAssessmentProfile(
                model_name=self.settings.semantic_anatomy_model,
                model_revision=revision,
            ),
            analyzer=analyze,
            max_attempts=self.settings.background_semantic_max_attempts,
            lease_seconds=self.settings.background_semantic_lease_seconds,
            retry_base_seconds=self.settings.background_semantic_retry_base_seconds,
            retry_max_seconds=self.settings.background_semantic_retry_max_seconds,
        )
        return result.did_work

    async def derivative_once(self) -> bool:
        if self.object_store is None or not self.settings.derivative_rendering_enabled:
            return False
        result = await run_derivative_cycle(
            self.sessions,
            self.object_store,
            worker_id=self._worker_id("derivative"),
            lease_seconds=self.settings.background_derivative_lease_seconds,
            retry_base_seconds=(self.settings.background_derivative_retry_base_seconds),
            retry_max_seconds=(self.settings.background_derivative_retry_max_seconds),
            isolation_policy=DerivativeIsolationPolicy(
                wall_timeout_seconds=(self.settings.background_derivative_render_timeout_seconds),
                memory_limit_bytes=(self.settings.background_derivative_memory_limit_bytes),
            ),
        )
        return result.did_work

    async def publication_once(self) -> bool:
        if self.object_store is None or not self.settings.publishing_enabled:
            return False
        result = await run_publication_cycle(
            self.sessions,
            self.object_store,
            worker_id=self._worker_id("publication"),
            x_oauth_provider=self.x_oauth_provider,
            expected_x_creator_user_id=self.settings.x_creator_user_id,
            lease_seconds=self.settings.background_publication_lease_seconds,
            retry_base_seconds=self.settings.background_publication_retry_base_seconds,
            retry_max_seconds=self.settings.background_publication_retry_max_seconds,
            max_package_bytes=self.settings.background_publication_max_package_bytes,
            patreon_driver=self.patreon_driver,
            patreon_browser_profile_reference=(self.settings.patreon_browser_profile_reference),
        )
        return result.did_work

    async def mega_delivery_once(self) -> bool:
        if (
            self.object_store is None
            or self.mega_client is None
            or not self.settings.mega_delivery_enabled
        ):
            return False
        result = await run_mega_delivery_cycle(
            self.sessions,
            store=self.object_store,
            client=self.mega_client,
            worker_id=self._worker_id("mega-delivery"),
            remote_root=self.settings.mega_remote_root,
            lease_seconds=self.settings.background_mega_lease_seconds,
            retry_base_seconds=self.settings.background_mega_retry_base_seconds,
            retry_max_seconds=self.settings.background_mega_retry_max_seconds,
            max_package_bytes=self.settings.background_mega_max_package_bytes,
        )
        return result.did_work

    async def _submit_event(
        self,
        event: ClaimedOutboxEvent,
        *,
        progress: _SubmissionProgress,
    ) -> SubmissionResult:
        if self.salad_client is None or self.object_store is None:
            raise RuntimeError("Salad submission dependencies are unavailable")
        signing_key_id = self.settings.worker_signing_key_id
        signing_private_key = self.settings.worker_signing_private_key
        manifest_json = self.settings.salad_worker_model_manifest_json
        manifest_sha256 = self.settings.salad_worker_model_manifest_sha256
        if (
            signing_key_id is None
            or signing_private_key is None
            or manifest_json is None
            or manifest_sha256 is None
        ):
            raise RuntimeError("worker signing configuration is unavailable")
        artifact_manifest = load_artifact_manifest(manifest_json.get_secret_value())

        async with self.sessions() as session:
            # Keep the established guard -> deployment/attempt lock order used
            # by every mutating Salad transaction. This also serializes the
            # idle-boundary decision without introducing a cross-controller
            # deadlock.
            await self._lock_budget_guard(session)
            deployment = await session.scalar(
                select(SaladDeployment)
                .join(
                    GenerationAttempt,
                    GenerationAttempt.salad_deployment_id == SaladDeployment.id,
                )
                .where(GenerationAttempt.id == event.aggregate_id)
                # Serialize the idle-to-active runtime refresh boundary across
                # controller instances. The submission transaction commits its
                # durable SUBMITTING marker before the provider POST, allowing
                # the next event to observe warm work and skip the rollout.
                .with_for_update(of=SaladDeployment)
            )
            if deployment is None:
                raise RuntimeError("generation attempt deployment is unavailable")
            input_provider = SaladWorkerJobInputProvider(
                session=session,
                store=self.object_store,
                signing_key_id=signing_key_id,
                signing_private_key=signing_private_key,
                artifact_manifest=artifact_manifest,
                artifact_manifest_sha256=manifest_sha256.get_secret_value(),
                signature_ttl_seconds=self.settings.worker_signature_ttl_seconds,
                upload_grant_ttl_seconds=self.settings.worker_upload_grant_ttl_seconds,
                max_upload_bytes=self.settings.storage_max_image_bytes,
            )
            active_attempt_id = await session.scalar(
                select(GenerationAttempt.id)
                .where(
                    GenerationAttempt.salad_deployment_id == deployment.id,
                    GenerationAttempt.state.in_(_RUNTIME_REFRESH_BLOCKING_ATTEMPT_STATES),
                )
                .limit(1)
            )
            if active_attempt_id is None:
                # Bootstrap credentials are refreshed only at an idle-to-active
                # boundary. PATCHing the container environment creates a Salad
                # version and restarts every replica, so doing this per batch
                # defeats warm GPU/model reuse. Subsequent ordered jobs reuse
                # the running deployment while any durable provider work exists.
                logger.info(
                    "salad_runtime_refresh_at_work_boundary",
                    salad_deployment_id=str(deployment.id),
                    generation_attempt_id=str(event.aggregate_id),
                )
                await refresh_container_group_runtime(
                    deployment,
                    self.salad_client,
                    self.secret_resolver,
                )
            else:
                logger.debug(
                    "salad_runtime_reused_for_batch",
                    salad_deployment_id=str(deployment.id),
                    generation_attempt_id=str(event.aggregate_id),
                    active_generation_attempt_id=str(active_attempt_id),
                )
            return await submit_prepared_attempt(
                session,
                self.salad_client,
                cast(SaladUploadIntentProvider, input_provider),
                generation_attempt_id=event.aggregate_id,
                webhook_url=(f"{str(self.settings.public_base_url).rstrip('/')}/webhooks/salad"),
                reservation_microusd=deployment.max_hourly_cost_microusd,
                provider_post_started=progress.mark_provider_post_started,
            )

    async def _transition_submit_result(
        self,
        event: ClaimedOutboxEvent,
        *,
        result: SubmissionResult,
        worker_id: str,
    ) -> None:
        if result.disposition == SubmissionDisposition.UNKNOWN:
            await self._fail_submit_lease(
                event,
                worker_id=worker_id,
                error_code="salad_submit_outcome_unknown",
                external_effect=ExternalEffect.MAY_HAVE_STARTED,
                retry_not_before=None,
            )
            return
        if result.disposition == SubmissionDisposition.BUDGET_BLOCKED:
            retry_at = result.retry_not_before or (
                datetime.now(UTC) + timedelta(seconds=self.settings.background_retry_delay_seconds)
            )
            await self._fail_submit_lease(
                event,
                worker_id=worker_id,
                error_code="provider_budget_blocked",
                external_effect=ExternalEffect.DEFINITELY_NOT_STARTED,
                retry_not_before=retry_at,
            )
            return
        async with self.sessions() as session:
            await succeed_outbox_event(
                session,
                event_id=event.id,
                worker_id=worker_id,
            )

    async def _fail_submit_lease(
        self,
        event: ClaimedOutboxEvent,
        *,
        worker_id: str,
        error_code: str,
        external_effect: ExternalEffect,
        retry_not_before: datetime | None,
    ) -> None:
        try:
            async with self.sessions() as session:
                await fail_outbox_event(
                    session,
                    event_id=event.id,
                    worker_id=worker_id,
                    error_code=error_code,
                    safe_error_detail="The controller could not complete the submission event.",
                    external_effect=external_effect,
                    retry_not_before=retry_not_before,
                )
        except OutboxError as error:
            logger.warning(
                "controller_submit_lease_cleanup_skipped",
                controller_instance_id=self.instance_id,
                outbox_event_id=str(event.id),
                error_type=type(error).__name__,
            )

    async def _resolve_definitely_unstarted_submit(
        self,
        event: ClaimedOutboxEvent,
        *,
        worker_id: str,
        error_code: str,
    ) -> None:
        async with self.sessions() as session:
            await fail_definitely_unstarted_submission(
                session,
                generation_attempt_id=event.aggregate_id,
                error_code=error_code,
                now=datetime.now(UTC),
            )
            await session.commit()
        async with self.sessions() as session:
            await succeed_outbox_event(
                session,
                event_id=event.id,
                worker_id=worker_id,
            )

    async def _disable_gpu_allocations(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> int:
        if self.settings.gpu_allocation_enabled:
            return 0
        deployments = list(
            (
                await session.scalars(
                    select(SaladDeployment)
                    .where(
                        SaladDeployment.desired_state != DesiredDeploymentState.STOPPED,
                        SaladDeployment.state != SaladDeploymentState.STOPPED,
                    )
                    .order_by(SaladDeployment.version_no)
                    .with_for_update()
                )
            ).all()
        )
        for deployment in deployments:
            deployment.desired_state = DesiredDeploymentState.STOPPED
            deployment.reconcile_after = now
            deployment.last_error_code = "gpu_allocation_disabled"
            deployment.last_error_detail = (
                "GPU allocation is disabled; provider execution must remain stopped."
            )
            deployment.lock_version += 1
            session.add(
                AuditEvent(
                    actor=self._worker_id("allocation-guard"),
                    action="salad_deployment.gpu_allocation_disabled",
                    resource_type="salad_deployment",
                    resource_id=deployment.id,
                    correlation_id=f"salad-deployment:{deployment.id}",
                    detail={"previous_desired_state": DesiredDeploymentState.ACTIVE.value},
                    occurred_at=now,
                )
            )
        await session.flush()
        return len(deployments)

    async def _fail_inbox_lease(
        self,
        receipt: ClaimedSaladWebhook,
        *,
        worker_id: str,
    ) -> None:
        try:
            async with self.sessions() as session:
                await fail_salad_webhook_receipt(
                    session,
                    receipt_id=receipt.receipt_id,
                    worker_id=worker_id,
                    error_code="controller_inbox_processing_failed",
                    safe_error_detail="The controller could not apply the webhook receipt.",
                    retryable=True,
                    retry_not_before=(
                        datetime.now(UTC)
                        + timedelta(seconds=self.settings.background_retry_delay_seconds)
                    ),
                )
        except SaladInboxError as error:
            logger.warning(
                "controller_inbox_lease_cleanup_skipped",
                controller_instance_id=self.instance_id,
                receipt_id=str(receipt.receipt_id),
                error_type=type(error).__name__,
            )

    async def _defer_collection_lease(
        self,
        claimed: ClaimedCollectionJob,
        *,
        worker_id: str,
    ) -> None:
        now = datetime.now(UTC)
        async with self.sessions() as session:
            job = await session.scalar(
                select(GenerationJob)
                .where(
                    GenerationJob.id == claimed.job_id,
                    GenerationJob.state == GenerationState.VERIFYING,
                    GenerationJob.lease_owner == worker_id,
                    GenerationJob.lease_expires_at > now,
                )
                .with_for_update()
            )
            if job is None:
                await session.rollback()
                return
            job.state = GenerationState.COLLECTING
            job.retry_at = now + timedelta(seconds=self.settings.background_retry_delay_seconds)
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error_code = "controller_collection_interrupted"
            job.last_error_detail = "Master collection was interrupted and will be retried."
            job.lock_version += 1
            session.add(
                AuditEvent(
                    actor=worker_id,
                    action="generation_job.collection_interrupted",
                    resource_type="generation_job",
                    resource_id=job.id,
                    correlation_id=str(job.id),
                    detail={"retry_at": job.retry_at.isoformat()},
                    occurred_at=now,
                )
            )
            await session.commit()

    async def _mark_deployment_unknown(
        self,
        deployment_id: UUID,
        *,
        error_code: str,
    ) -> None:
        now = datetime.now(UTC)
        try:
            async with self.sessions() as session:
                await self._lock_budget_guard(session)
                deployment = await session.scalar(
                    select(SaladDeployment)
                    .where(SaladDeployment.id == deployment_id)
                    .with_for_update()
                )
                if deployment is None or deployment.state == SaladDeploymentState.STOPPED:
                    await session.rollback()
                    return
                deployment.state = SaladDeploymentState.UNKNOWN
                deployment.unknown_since = deployment.unknown_since or now
                deployment.reconcile_after = now + timedelta(
                    seconds=self.settings.background_retry_delay_seconds
                )
                deployment.last_error_code = error_code
                deployment.last_error_detail = (
                    "The controller could not determine the provider operation outcome."
                )
                deployment.lock_version += 1
                session.add(
                    AuditEvent(
                        actor=self._worker_id("deployment"),
                        action="salad_deployment.controller_outcome_unknown",
                        resource_type="salad_deployment",
                        resource_id=deployment.id,
                        correlation_id=f"salad-deployment:{deployment.id}",
                        detail={"error_code": error_code},
                        occurred_at=now,
                    )
                )
                await session.commit()
        except Exception as error:
            logger.exception(
                "controller_deployment_unknown_marker_failed",
                controller_instance_id=self.instance_id,
                deployment_id=str(deployment_id),
                error_type=type(error).__name__,
            )

    async def _lock_budget_guard(self, session: AsyncSession) -> None:
        await session.scalar(
            select(ProviderBudgetGuard.id)
            .where(ProviderBudgetGuard.provider == "salad")
            .with_for_update()
        )

    def _worker_id(self, loop_name: str) -> str:
        return f"{self.instance_id}:{loop_name}"


def build_controller_runtime(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    salad_client: SaladClient | None,
    object_store: ObjectStore | None,
    secret_resolver: RuntimeSecretResolver | None = None,
    x_oauth_provider: XOAuthProvider | None = None,
    mega_client: MegaDeliveryClient | None = None,
    semantic_vlm_client: SemanticVlmClient | None = None,
    patreon_driver: PatreonPublicationDriver | None = None,
) -> ControllerRuntime:
    instance_id = f"controller-{uuid4()}"
    resolved_mega_client = mega_client
    if settings.mega_delivery_enabled and resolved_mega_client is None:
        profile_home = settings.mega_profile_home
        if profile_home is None:
            raise RuntimeError("validated MEGA profile settings are incomplete")
        resolved_mega_client = MegaCmdClient(
            profile_home=profile_home,
            command_timeout_seconds=(settings.background_mega_command_timeout_seconds),
        )
    workloads = ControllerWorkloads(
        settings=settings,
        sessions=sessions,
        instance_id=instance_id,
        salad_client=salad_client,
        object_store=object_store,
        secret_resolver=secret_resolver,
        x_oauth_provider=x_oauth_provider,
        mega_client=resolved_mega_client,
        semantic_vlm_client=semantic_vlm_client,
        patreon_driver=patreon_driver,
    )
    poll = settings.background_poll_interval_seconds
    loops: list[LoopSpec] = []
    if salad_client is not None:
        loops.extend(
            (
                LoopSpec(
                    name="budget",
                    cycle=workloads.budget_once,
                    idle_interval_seconds=max(poll, 30),
                    timeout_seconds=30,
                ),
                LoopSpec(
                    name="deployments",
                    cycle=workloads.deployment_once,
                    idle_interval_seconds=poll,
                    timeout_seconds=(settings.background_deployment_timeout_seconds + 15),
                ),
                LoopSpec(
                    name="salad-inbox",
                    cycle=workloads.inbox_once,
                    idle_interval_seconds=poll,
                    timeout_seconds=settings.background_inbox_timeout_seconds + 5,
                ),
                LoopSpec(
                    name="attempt-reconciliation",
                    cycle=workloads.reconcile_attempt_once,
                    idle_interval_seconds=poll,
                    timeout_seconds=settings.background_reconcile_timeout_seconds + 5,
                ),
            )
        )
        if settings.gpu_allocation_enabled:
            loops.extend(
                (
                    LoopSpec(
                        name="generation-dispatch",
                        cycle=workloads.dispatch_once,
                        idle_interval_seconds=poll,
                        timeout_seconds=30,
                    ),
                    LoopSpec(
                        name="salad-submit",
                        cycle=workloads.submit_once,
                        idle_interval_seconds=poll,
                        timeout_seconds=settings.background_submit_timeout_seconds + 15,
                    ),
                )
            )
    if object_store is not None:
        loops.append(
            LoopSpec(
                name="master-collection",
                cycle=workloads.collection_once,
                idle_interval_seconds=poll,
                timeout_seconds=settings.background_collection_timeout_seconds + 15,
            )
        )
        if settings.quality_scoring_enabled:
            loops.append(
                LoopSpec(
                    name="quality-scoring",
                    cycle=workloads.quality_once,
                    idle_interval_seconds=poll,
                    timeout_seconds=settings.background_quality_timeout_seconds + 5,
                )
            )
        if settings.semantic_anatomy_enabled:
            loops.append(
                LoopSpec(
                    name="semantic-anatomy-qc",
                    cycle=workloads.semantic_anatomy_once,
                    idle_interval_seconds=poll,
                    timeout_seconds=settings.background_semantic_timeout_seconds + 5,
                )
            )
        if settings.derivative_rendering_enabled:
            loops.append(
                LoopSpec(
                    name="derivative-rendering",
                    cycle=workloads.derivative_once,
                    idle_interval_seconds=poll,
                    timeout_seconds=(settings.background_derivative_timeout_seconds + 5),
                )
            )
        if settings.publishing_enabled:
            loops.append(
                LoopSpec(
                    name="publication-orchestration",
                    cycle=workloads.publication_once,
                    idle_interval_seconds=poll,
                    timeout_seconds=(settings.background_publication_timeout_seconds + 5),
                )
            )
        if settings.mega_delivery_enabled:
            loops.append(
                LoopSpec(
                    name="mega-completed-set-delivery",
                    cycle=workloads.mega_delivery_once,
                    idle_interval_seconds=poll,
                    timeout_seconds=settings.background_mega_timeout_seconds + 5,
                )
            )
    return ControllerRuntime(
        instance_id=instance_id,
        loops=loops,
        startup=workloads.initialize,
        error_backoff_max_seconds=settings.background_error_backoff_max_seconds,
        shutdown_grace_seconds=settings.background_shutdown_grace_seconds,
        shutdown_cancel_seconds=settings.background_shutdown_cancel_seconds,
        readiness_failure_threshold=settings.background_readiness_failure_threshold,
        liveness_failure_threshold=settings.background_liveness_failure_threshold,
        stale_after_seconds=settings.background_loop_stale_after_seconds,
    )
