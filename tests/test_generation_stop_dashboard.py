import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import gen_automation.controller.runtime as controller_runtime
from gen_automation.app import create_app
from gen_automation.config import Settings
from gen_automation.controller.runtime import ControllerWorkloads
from gen_automation.db.models import (
    Asset,
    AuditEvent,
    ComplianceCheck,
    GenerationAttempt,
    GenerationJob,
    OutboxEvent,
    Project,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    AssetKind,
    AssetState,
    ComplianceResult,
    DesiredDeploymentState,
    GenerationAttemptState,
    GenerationState,
    OutboxStatus,
    ReleasePhase,
    SaladDeploymentState,
)
from gen_automation.domain.release_spec import ReleaseSpecification
from gen_automation.gpu_worker.artifacts import ArtifactManifest
from gen_automation.integrations.salad.client import SaladClient
from gen_automation.services.compliance import validate_release_approvals
from gen_automation.services.generation_control import (
    GENERATION_STOPPED_ACTION,
    request_generation_stop,
    settle_stopped_generation_once,
)
from gen_automation.services.outbox import SALAD_JOB_SUBMIT_TOPIC
from gen_automation.services.scheduling import dispatch_generation_jobs
from gen_automation.storage.base import ObjectStore
from tests.factories import seed_release_approvals, valid_release_payload
from tests.test_controller_spend_stop import (
    RUNTIME_MANIFEST,
    WORKER_SIGNING_PRIVATE_KEY,
    _seed_submission_database,
)
from tests.test_review_api import (
    ORIGIN,
    ReviewApiContext,
    _login,
    _seed_review_api,
    _settings,
)
from tests.test_scheduling import SchedulingContext

_FORM_HEADERS = {
    "Origin": ORIGIN,
    "Sec-Fetch-Site": "same-origin",
}
_STOP_ACTION = "release.generation_stop_requested"


@dataclass(frozen=True, slots=True)
class GenerationStopDashboardContext:
    auth: ReviewApiContext
    release_id: UUID
    release_version_id: UUID
    succeeded_job_id: UUID
    queued_job_ids: tuple[UUID, UUID]
    asset_ids: tuple[UUID, UUID]


def _hidden_value(page: str, name: str) -> str:
    match = re.search(rf'<input type="hidden" name="{name}" value="([^"]+)">', page)
    assert match is not None
    return match.group(1)


async def _seed_generation_stop_dashboard(
    database_path: Path,
) -> GenerationStopDashboardContext:
    settings = _settings(database_path)
    auth = await _seed_review_api(settings)
    database = Database(settings.database_url)
    try:
        now = datetime.now(UTC)
        async with database.sessions() as session:
            project = Project(slug="generation-stop", name="Generation stop")
            session.add(project)
            await session.flush()
            release = Release(
                project_id=project.id,
                slug="partially-generated-set",
                title="Partially generated set",
                phase=ReleasePhase.GENERATING,
                current_version_no=1,
                desired_accepted_count=4,
                lock_version=1,
            )
            session.add(release)
            await session.flush()
            version = ReleaseVersion(
                release_id=release.id,
                version_no=1,
                specification={"schema_version": 1, "generation": {"provider": "salad"}},
                specification_sha256="1" * 64,
                created_by="test",
                created_at=now,
            )
            session.add(version)
            await session.flush()

            succeeded = GenerationJob(
                release_version_id=version.id,
                logical_key="2" * 64,
                parameters={"schema_version": 1, "ordinal": 0},
                parameters_sha256="3" * 64,
                provider="salad",
                state=GenerationState.SUCCEEDED,
                priority=100,
                expected_output_count=2,
                attempt_count=1,
                max_attempts=3,
                lock_version=1,
            )
            queued = tuple(
                GenerationJob(
                    release_version_id=version.id,
                    logical_key=f"{index + 4:064x}",
                    parameters={"schema_version": 1, "ordinal": index},
                    parameters_sha256=f"{index + 7:064x}",
                    provider="salad",
                    state=GenerationState.QUEUED,
                    priority=100 + index,
                    expected_output_count=2,
                    attempt_count=0,
                    max_attempts=3,
                    lock_version=1,
                )
                for index in (1, 2)
            )
            session.add(succeeded)
            session.add_all(queued)
            await session.flush()

            assets = tuple(
                Asset(
                    id=uuid4(),
                    release_id=release.id,
                    generation_job_id=succeeded.id,
                    output_index=index,
                    kind=AssetKind.RAW_MASTER,
                    state=AssetState.AVAILABLE,
                    storage_backend="s3",
                    storage_bucket="private-generation-stop-bucket",
                    object_key=f"raw/saved-before-stop-{index}.png",
                    object_version_id=f"saved-version-{index}",
                    sha256=f"{index + 10:064x}",
                    content_type="image/png",
                    image_format="PNG",
                    width=1024,
                    height=1408,
                    byte_size=10_000 + index,
                    asset_metadata={"saved_before_stop": True},
                    available_at=now,
                )
                for index in range(2)
            )
            session.add_all(assets)
            await session.commit()
            return GenerationStopDashboardContext(
                auth=auth,
                release_id=release.id,
                release_version_id=version.id,
                succeeded_job_id=succeeded.id,
                queued_job_ids=(queued[0].id, queued[1].id),
                asset_ids=(assets[0].id, assets[1].id),
            )
    finally:
        await database.dispose()


@pytest.fixture
async def generation_stop_scheduling_context(
    tmp_path: Path,
) -> AsyncIterator[SchedulingContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'stop-scheduling.db').as_posix()}")
    await database.create_schema()
    now = datetime.now(UTC)
    async with database.sessions() as session:
        payload = valid_release_payload()
        specification = ReleaseSpecification.model_validate(payload["specification"])
        await seed_release_approvals(session, payload)
        approval_snapshot = await validate_release_approvals(session, specification)
        project = Project(slug="stop-scheduling", name="Stop scheduling")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="release",
            title="Release",
            phase=ReleasePhase.READY,
            desired_accepted_count=2,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification=specification.model_dump(mode="json"),
            specification_sha256=canonical_sha256(specification),
            created_by="test",
            created_at=now,
        )
        session.add(version)
        await session.flush()
        session.add_all(
            [
                ComplianceCheck(
                    release_version_id=version.id,
                    check_type=check_type,
                    result=ComplianceResult.PASSED,
                    evidence=approval_snapshot.checks[check_type],
                    checked_by="test",
                    checked_at=now,
                )
                for check_type in (
                    "adult_subject_gate",
                    "artifact_license_gate",
                    "workflow_integrity_gate",
                )
            ]
        )
        jobs: list[GenerationJob] = []
        for index in range(2):
            parameters = {
                "schema_version": 1,
                "ordinal": index,
                "approval_snapshot_sha256": approval_snapshot.sha256,
            }
            job = GenerationJob(
                release_version_id=version.id,
                logical_key=f"{index + 20:064x}",
                parameters=parameters,
                parameters_sha256=canonical_sha256(parameters),
                provider="salad",
                state=GenerationState.QUEUED,
                priority=100 + index,
                expected_output_count=2,
                attempt_count=0,
                max_attempts=3,
            )
            session.add(job)
            jobs.append(job)
        deployment = SaladDeployment(
            version_no=1,
            config_sha256="b" * 64,
            provider_configuration={},
            worker_image_digest=f"registry.example/worker@sha256:{'c' * 64}",
            organization_name="organization",
            project_name="project",
            queue_name="queue-v1",
            provider_queue_id="queue-id",
            container_group_name="group-v1",
            provider_container_group_id="group-id",
            state=SaladDeploymentState.ACTIVE,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=True,
            min_replicas=0,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=1_000_000,
        )
        session.add(deployment)
        await session.commit()
        context = SchedulingContext(
            database=database,
            release_id=release.id,
            deployment_id=deployment.id,
            job_ids=tuple(job.id for job in jobs),
        )
    try:
        yield context
    finally:
        await database.dispose()


async def _stop_database_state(
    context: GenerationStopDashboardContext,
) -> tuple[Release, list[GenerationJob], list[Asset], int]:
    database = Database(context.auth.settings.database_url)
    try:
        async with database.sessions() as session:
            release = await session.get(Release, context.release_id)
            assert release is not None
            jobs = list(
                (
                    await session.scalars(
                        select(GenerationJob)
                        .where(GenerationJob.release_version_id == context.release_version_id)
                        .order_by(GenerationJob.priority, GenerationJob.id)
                    )
                ).all()
            )
            assets = list(
                (
                    await session.scalars(
                        select(Asset)
                        .where(Asset.id.in_(context.asset_ids))
                        .order_by(Asset.output_index)
                    )
                ).all()
            )
            audit_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.resource_type == "release",
                        AuditEvent.resource_id == context.release_id,
                        AuditEvent.action == _STOP_ACTION,
                    )
                )
                or 0
            )
            session.expunge(release)
            for row in (*jobs, *assets):
                session.expunge(row)
            return release, jobs, assets, audit_count
    finally:
        await database.dispose()


def test_stop_control_is_visible_only_to_release_managers(tmp_path: Path) -> None:
    context = asyncio.run(_seed_generation_stop_dashboard(tmp_path / "manager-visibility.db"))
    app = create_app(context.auth.settings)

    with TestClient(app, base_url=ORIGIN, client=("192.0.2.120", 50000)) as client:
        for role in AdminRole:
            _login(client, context.auth.settings, context.auth.users[role])
            page = client.get(f"/dashboard/releases/{context.release_id}/status")

            assert page.status_code == 200
            assert "data-stop-generation-status" in page.text
            if role in {AdminRole.OWNER, AdminRole.ADMIN}:
                assert "data-stop-generation-open" in page.text
                assert "data-stop-generation-dialog" in page.text
                assert "data-stop-generation-form" in page.text
                assert (
                    f'action="/dashboard/releases/{context.release_id}:stop-generation"'
                    in page.text
                )
                assert _hidden_value(page.text, "csrf_token")
                assert re.fullmatch(
                    r"web-new-set-[0-9a-f]{64}",
                    _hidden_value(page.text, "idempotency_key"),
                )
            else:
                assert "data-stop-generation-open" not in page.text
                assert "data-stop-generation-dialog" not in page.text
                assert "data-stop-generation-form" not in page.text


def test_stop_mutation_requires_manager_origin_csrf_and_strict_form(
    tmp_path: Path,
) -> None:
    context = asyncio.run(_seed_generation_stop_dashboard(tmp_path / "stop-security.db"))
    app = create_app(context.auth.settings)
    endpoint = f"/dashboard/releases/{context.release_id}:stop-generation"

    with TestClient(app, base_url=ORIGIN, client=("192.0.2.121", 50000)) as client:
        _login(client, context.auth.settings, context.auth.users[AdminRole.OWNER])
        page = client.get(f"/dashboard/releases/{context.release_id}/status")
        form = {
            "csrf_token": _hidden_value(page.text, "csrf_token"),
            "idempotency_key": _hidden_value(page.text, "idempotency_key"),
        }

        missing_origin = client.post(endpoint, data=form)
        wrong_csrf = client.post(
            endpoint,
            data={**form, "csrf_token": "wrong-csrf-token"},
            headers=_FORM_HEADERS,
        )
        json_body = client.post(endpoint, json={}, headers=_FORM_HEADERS)
        extra_field = client.post(
            endpoint,
            data={**form, "unexpected": "field"},
            headers=_FORM_HEADERS,
        )

        _login(client, context.auth.settings, context.auth.users[AdminRole.REVIEWER])
        forbidden = client.post(
            endpoint,
            data={
                "csrf_token": client.cookies[context.auth.settings.auth_csrf_cookie_name],
                "idempotency_key": f"web-new-set-{'a' * 64}",
            },
            headers=_FORM_HEADERS,
        )

    assert missing_origin.status_code == 403
    assert wrong_csrf.status_code == 403
    assert json_body.status_code == 415
    assert extra_field.status_code == 400
    assert forbidden.status_code == 403
    release, jobs, assets, audit_count = asyncio.run(_stop_database_state(context))
    assert release.phase == ReleasePhase.GENERATING
    assert [job.state for job in jobs].count(GenerationState.QUEUED) == 2
    assert [asset.state for asset in assets] == [AssetState.AVAILABLE, AssetState.AVAILABLE]
    assert audit_count == 0


def test_async_stop_returns_progress_in_place_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    context = asyncio.run(_seed_generation_stop_dashboard(tmp_path / "stop-json.db"))
    app = create_app(context.auth.settings)
    endpoint = f"/dashboard/releases/{context.release_id}:stop-generation"

    with TestClient(app, base_url=ORIGIN, client=("192.0.2.122", 50000)) as client:
        _login(client, context.auth.settings, context.auth.users[AdminRole.ADMIN])
        page = client.get(f"/dashboard/releases/{context.release_id}/status")
        form = {
            "csrf_token": _hidden_value(page.text, "csrf_token"),
            "idempotency_key": _hidden_value(page.text, "idempotency_key"),
        }
        headers = {**_FORM_HEADERS, "Accept": "application/json"}
        first = client.post(endpoint, data=form, headers=headers, follow_redirects=False)
        replay = client.post(endpoint, data=form, headers=headers, follow_redirects=False)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert "location" not in first.headers
    assert first.json() == replay.json()
    payload = first.json()
    assert payload["schema_version"] == 1
    assert payload["release_id"] == str(context.release_id)
    assert payload["phase"] == "paused"
    assert payload["stage"]["key"] == "paused"
    assert payload["stage"]["label"] == "Stopping generation"
    assert payload["images"] == {"generated": 2, "expected": 2, "percent": 100.0}
    assert payload["jobs"] == {
        "completed": 3,
        "total": 3,
        "active": 0,
        "failed": 0,
        "states": {"cancelled": 2, "succeeded": 1},
    }
    assert payload["stop"] == {
        "available": False,
        "requested": True,
        "settled": False,
    }
    assert payload["error"] is None
    assert payload["ready_for_review"] is False

    release, jobs, assets, audit_count = asyncio.run(_stop_database_state(context))
    assert release.phase == ReleasePhase.PAUSED
    assert [job.state for job in jobs] == [
        GenerationState.SUCCEEDED,
        GenerationState.CANCELLED,
        GenerationState.CANCELLED,
    ]
    assert [(asset.id, asset.object_key, asset.sha256) for asset in assets] == [
        (
            context.asset_ids[index],
            f"raw/saved-before-stop-{index}.png",
            f"{index + 10:064x}",
        )
        for index in range(2)
    ]
    assert audit_count == 1


def test_html_stop_redirects_then_partial_saved_set_advances_to_scoring(
    tmp_path: Path,
) -> None:
    context = asyncio.run(_seed_generation_stop_dashboard(tmp_path / "stop-settle.db"))
    app = create_app(context.auth.settings)
    endpoint = f"/dashboard/releases/{context.release_id}:stop-generation"

    with TestClient(app, base_url=ORIGIN, client=("192.0.2.123", 50000)) as client:
        _login(client, context.auth.settings, context.auth.users[AdminRole.OWNER])
        page = client.get(f"/dashboard/releases/{context.release_id}/status")
        form = {
            "csrf_token": _hidden_value(page.text, "csrf_token"),
            "idempotency_key": _hidden_value(page.text, "idempotency_key"),
        }
        stopped = client.post(endpoint, data=form, headers=_FORM_HEADERS, follow_redirects=False)

        assert stopped.status_code == 303
        assert stopped.headers["location"] == (f"/dashboard/releases/{context.release_id}/status")

        async def settle() -> object:
            database = Database(context.auth.settings.database_url)
            try:
                async with database.sessions() as session:
                    return await settle_stopped_generation_once(
                        session,
                        release_id=context.release_id,
                        actor="test-controller",
                    )
            finally:
                await database.dispose()

        assert client.portal is not None
        settled = client.portal.call(settle)
        assert settled.settled is True
        assert settled.available_asset_count == 2
        assert settled.desired_accepted_count == 2
        assert settled.phase == ReleasePhase.REVIEWING
        progress = client.get(f"/dashboard/releases/{context.release_id}/progress")

    assert progress.status_code == 200
    payload = progress.json()
    assert payload["phase"] == "reviewing"
    assert payload["stage"]["key"] == "scoring"
    assert payload["images"] == {"generated": 2, "expected": 2, "percent": 100.0}
    assert payload["stop"] == {
        "available": False,
        "requested": True,
        "settled": True,
    }
    assert payload["error"] is None

    release, _jobs, assets, audit_count = asyncio.run(_stop_database_state(context))
    assert release.phase == ReleasePhase.REVIEWING
    assert release.desired_accepted_count == 2
    assert [asset.state for asset in assets] == [AssetState.AVAILABLE, AssetState.AVAILABLE]
    assert audit_count == 1


def test_stop_is_a_durable_dispatch_fence_after_controller_restart(
    generation_stop_scheduling_context: SchedulingContext,
) -> None:
    scheduling_context = generation_stop_scheduling_context

    async def request_then_simulate_stale_controller() -> tuple[int, int]:
        async with scheduling_context.database.sessions() as session:
            await request_generation_stop(
                session,
                release_id=scheduling_context.release_id,
                actor="test-owner",
                correlation_id="durable-stop-fence",
            )

        # A stale controller must not be able to undo the durable stop intent merely by
        # restoring a dispatchable phase or re-observing a previously queued job.
        async with scheduling_context.database.sessions() as session:
            release = await session.get(Release, scheduling_context.release_id)
            job = await session.get(GenerationJob, scheduling_context.job_ids[0])
            assert release is not None
            assert job is not None
            release.phase = ReleasePhase.GENERATING
            job.state = GenerationState.QUEUED
            job.last_error_code = None
            job.last_error_detail = None
            await session.commit()

        # A fresh service call represents a restarted controller with no process-local
        # memory of the stop request.
        async with scheduling_context.database.sessions() as session:
            result = await dispatch_generation_jobs(
                session,
                salad_deployment_id=scheduling_context.deployment_id,
                gpu_allocation_enabled=True,
                max_inflight=3,
            )
            attempt_count = int(
                await session.scalar(select(func.count()).select_from(GenerationAttempt)) or 0
            )
            return len(result.dispatched), attempt_count

    dispatched_count, attempt_count = asyncio.run(request_then_simulate_stale_controller())
    assert dispatched_count == 0
    assert attempt_count == 0


def test_stop_javascript_posts_the_existing_form_and_updates_without_navigation(
    client: TestClient,
) -> None:
    script = client.get("/static/dashboard.js")

    assert script.status_code == 200
    start = script.text.index("function initializeGenerationProgress()")
    end = script.text.index("const initializeCharacterComposition", start)
    generation_progress = script.text[start:end]
    assert 'document.querySelector("[data-stop-generation-form]")' in generation_progress
    assert "new URL(stopForm.action, window.location.href)" in generation_progress
    assert "stopUrl.origin !== window.location.origin" in generation_progress
    assert "new URLSearchParams(new FormData(stopForm))" in generation_progress
    assert 'Accept: "application/json"' in generation_progress
    assert '"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"' in (
        generation_progress
    )
    assert "if (response.redirected || !response.ok || !render(payload))" in (generation_progress)
    assert "schedule(500)" in generation_progress
    assert ".reload(" not in generation_progress
    assert "location.assign(" not in generation_progress
    assert "location.replace(" not in generation_progress


@pytest.mark.asyncio
async def test_cancelled_submit_outbox_never_refreshes_or_restarts_salad(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'stale-submit.db').as_posix()}")
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        async with database.sessions() as session:
            release = await session.scalar(
                select(Release)
                .join(ReleaseVersion, ReleaseVersion.release_id == Release.id)
                .join(GenerationJob, GenerationJob.release_version_id == ReleaseVersion.id)
                .where(GenerationJob.id == context.job_id)
            )
            assert release is not None
            release.phase = ReleasePhase.GENERATING
            await session.commit()
            release_id = release.id

        async with database.sessions() as session:
            stopped = await request_generation_stop(
                session,
                release_id=release_id,
                actor="test-owner",
                correlation_id="cancel-stale-submit",
            )
        assert stopped.cancelled_attempt_ids == (context.attempt_id,)

        refreshes: list[UUID] = []

        async def fail_if_refreshed(
            deployment: SaladDeployment,
            client: object,
            resolver: object,
        ) -> object:
            del client, resolver
            refreshes.append(deployment.id)
            raise AssertionError("a cancelled generation must not PATCH the Salad runtime")

        monkeypatch.setattr(
            controller_runtime,
            "refresh_container_group_runtime",
            fail_if_refreshed,
        )
        monkeypatch.setattr(
            controller_runtime,
            "load_artifact_manifest",
            lambda _raw: ArtifactManifest.model_construct(
                version="v1",
                artifacts=(),
                manifest_sha256="0" * 64,
            ),
        )
        workloads = ControllerWorkloads(
            settings=Settings(
                public_base_url="https://automation.example.test",
                worker_signing_key_id="worker-key-1",
                worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
                salad_worker_model_manifest_json=RUNTIME_MANIFEST,
                salad_worker_model_manifest_sha256="0" * 64,
            ).model_copy(update={"gpu_allocation_enabled": True}),
            sessions=database.sessions,
            instance_id="controller-stop-submit-test",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )

        assert await workloads.submit_once() is True
        assert refreshes == []
        async with database.sessions() as session:
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            outbox = await session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.topic == SALAD_JOB_SUBMIT_TOPIC,
                    OutboxEvent.aggregate_id == context.attempt_id,
                )
            )
            assert attempt is not None
            assert outbox is not None
            assert attempt.state == GenerationAttemptState.FAILED
            assert outbox.status == OutboxStatus.SUCCEEDED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_stop_settlement_rotates_past_draining_release_and_repairs_stale_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'stop-rotation.db').as_posix()}")
    await database.create_schema()
    now = datetime.now(UTC)
    draining_release_id = UUID(int=1)
    ready_release_id = UUID(int=2)
    try:
        async with database.sessions() as session:
            project = Project(slug="stop-rotation", name="Stop rotation")
            session.add(project)
            await session.flush()
            draining_release = Release(
                id=draining_release_id,
                project_id=project.id,
                slug="draining",
                title="Draining",
                phase=ReleasePhase.PAUSED,
                current_version_no=1,
                desired_accepted_count=1,
            )
            ready_release = Release(
                id=ready_release_id,
                project_id=project.id,
                slug="ready",
                title="Ready",
                # Simulate stale controller state after the durable stop marker.
                phase=ReleasePhase.GENERATING,
                current_version_no=1,
                desired_accepted_count=1,
            )
            session.add_all([draining_release, ready_release])
            await session.flush()
            draining_version = ReleaseVersion(
                release_id=draining_release.id,
                version_no=1,
                specification={"schema_version": 1},
                specification_sha256="d" * 64,
                created_by="test",
                created_at=now,
            )
            ready_version = ReleaseVersion(
                release_id=ready_release.id,
                version_no=1,
                specification={"schema_version": 1},
                specification_sha256="e" * 64,
                created_by="test",
                created_at=now,
            )
            session.add_all([draining_version, ready_version])
            await session.flush()
            session.add(
                GenerationJob(
                    release_version_id=draining_version.id,
                    logical_key="f" * 64,
                    parameters={"schema_version": 1},
                    parameters_sha256="a" * 64,
                    provider="salad",
                    state=GenerationState.RUNNING,
                    expected_output_count=1,
                    attempt_count=1,
                    max_attempts=3,
                )
            )
            session.add_all(
                [
                    AuditEvent(
                        actor="test-owner",
                        action=_STOP_ACTION,
                        resource_type="release",
                        resource_id=release_id,
                        correlation_id=f"stop:{release_id}",
                        detail={"safe_drain": True},
                        occurred_at=now,
                    )
                    for release_id in (draining_release.id, ready_release.id)
                ]
            )
            await session.commit()

        async def no_progressive_collection(*args: object, **kwargs: object) -> object:
            del args, kwargs
            return SimpleNamespace(finalized=False)

        async def no_collection_jobs(*args: object, **kwargs: object) -> list[object]:
            del args, kwargs
            return []

        monkeypatch.setattr(controller_runtime, "_STOP_SETTLEMENT_CANDIDATE_LIMIT", 1)
        monkeypatch.setattr(
            controller_runtime,
            "collect_next_ready_running_asset",
            no_progressive_collection,
        )
        monkeypatch.setattr(controller_runtime, "claim_collection_jobs", no_collection_jobs)
        workloads = ControllerWorkloads(
            settings=Settings(),
            sessions=database.sessions,
            instance_id="controller-stop-rotation-test",
            salad_client=None,
            object_store=cast(ObjectStore, object()),
        )

        assert await workloads.collection_once() is False
        assert await workloads.collection_once() is True
        async with database.sessions() as session:
            loaded_draining_release = await session.get(Release, draining_release_id)
            loaded_ready_release = await session.get(Release, ready_release_id)
            stopped_release_ids = set(
                (
                    await session.scalars(
                        select(AuditEvent.resource_id).where(
                            AuditEvent.action == GENERATION_STOPPED_ACTION
                        )
                    )
                ).all()
            )
            assert loaded_draining_release is not None
            assert loaded_ready_release is not None
            assert loaded_draining_release.phase == ReleasePhase.PAUSED
            assert loaded_ready_release.phase == ReleasePhase.CANCELLED
            assert stopped_release_ids == {ready_release_id}
    finally:
        await database.dispose()
