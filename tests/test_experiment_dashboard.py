import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

import gen_automation.api.routes.experiment_dashboard as experiment_dashboard
from gen_automation.db.models import (
    ExperimentWarmLease,
    GenerationJob,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.domain.enums import DesiredDeploymentState, SaladDeploymentState
from gen_automation.gpu_worker.artifacts import (
    ArtifactKind,
    ModelArtifactSpec,
    create_artifact_manifest,
)
from gen_automation.services.budgets import ensure_budget_guard
from gen_automation.services.experiment_warm_leases import (
    activate_ready_experiment_warm_leases,
    mark_experiment_warm_runtime_refreshed_locked,
)
from gen_automation.services.experiments import ExperimentInputError
from gen_automation.services.new_sets import list_new_set_options
from tests.factories import seed_release_approvals, valid_release_payload


def _hidden_value(page: str, name: str) -> str:
    match = re.search(rf'<input type="hidden" name="{name}" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def _variant(*, options: object, label: str, prompt: str, steps: int = 24) -> dict[str, object]:
    return {
        "label": label,
        "subject_id": str(options.subjects[0].approval_id),
        "subject_2_id": "",
        "composition_mode": "single",
        "character_a_prompt": "",
        "character_b_prompt": "",
        "checkpoint_id": str(options.checkpoints[0].approval_id),
        "workflow_id": str(options.workflows[0].approval_id),
        "prompt": prompt,
        "negative_prompt": "low quality",
        "detailer_prompt": "detailed face",
        "detailer_negative_prompt": "closed eyes",
        "seed": 777,
        "width": 1024,
        "height": 1024,
        "cfg": 6.0,
        "steps": steps,
        "sampler": "euler",
        "scheduler": "normal",
        "clip_skip": 2,
        "hires_scale": 1.0,
        "hires_denoise": 0.35,
        "hires_upscale_method": "bislerp",
        "detailer_guide_size": 768,
        "detailer_max_size": 1024,
        "detailer_denoise": 0.4,
        "detailer_bbox_threshold": 0.3,
        "detailer_bbox_dilation": 4,
        "detailer_bbox_crop_factor": 1.5,
        "detailer_feather": 4,
        "loras": [],
    }


def _seed_options(client: TestClient) -> object:
    database = client.app.state.database
    assert client.portal is not None

    async def seed() -> object:
        async with database.sessions() as session:
            await seed_release_approvals(session, deepcopy(valid_release_payload()))
            return await list_new_set_options(session)

    return client.portal.call(seed)


def _configure_worker_manifest(client: TestClient, options: object) -> None:
    checkpoint = options.checkpoints[0]
    manifest = create_artifact_manifest(
        (
            ModelArtifactSpec(
                logical_name="test-checkpoint",
                kind=ArtifactKind.CHECKPOINT,
                source_object_id="models/test-checkpoint.safetensors",
                source_object_version_id="version-1",
                sha256=checkpoint.sha256,
                exact_size_bytes=100,
                max_size_bytes=100,
                target_filename="test-checkpoint.safetensors",
            ),
        )
    )
    client.app.state.settings.salad_worker_model_manifest_json = SecretStr(
        manifest.model_dump_json()
    )
    client.app.state.settings.salad_worker_model_manifest_sha256 = SecretStr(
        manifest.manifest_sha256
    )


def _seed_warm_deployment(client: TestClient) -> None:
    database = client.app.state.database
    assert client.portal is not None

    async def seed() -> None:
        async with database.sessions() as session:
            now = datetime.now(UTC)
            await ensure_budget_guard(
                session,
                provider="salad",
                daily_limit_usd=Decimal("100"),
                monthly_limit_usd=Decimal("1000"),
                now=now,
            )
            session.add(
                SaladDeployment(
                    version_no=1,
                    config_sha256="c" * 64,
                    provider_configuration={
                        "container": {},
                        "queue_autoscaler": {"polling_period": 30},
                    },
                    worker_image_digest="registry.example.test/worker@sha256:" + "d" * 64,
                    organization_name="organization",
                    project_name="project",
                    queue_name="generation",
                    provider_queue_id="queue-id",
                    container_group_name="worker",
                    provider_container_group_id="group-id",
                    state=SaladDeploymentState.ACTIVE,
                    desired_state=DesiredDeploymentState.ACTIVE,
                    is_current=True,
                    min_replicas=0,
                    max_replicas=1,
                    desired_queue_length=1,
                    max_hourly_cost_microusd=360_000,
                    observed_replicas=0,
                    ready_replicas=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

    client.portal.call(seed)


def test_experiment_lab_exposes_bounded_warm_aware_builder(client: TestClient) -> None:
    _seed_options(client)

    page = client.get("/dashboard/experiments/new")
    script = client.get("/static/dashboard.js")
    stylesheet = client.get("/static/dashboard_ux.css")

    assert page.status_code == 200
    assert "Compare ideas without repeated cold starts" in page.text
    assert "data-experiment-form" in page.text
    assert "data-experiment-plan" in page.text
    assert "data-experiment-save-variant" in page.text
    assert "data-experiment-variant-list" in page.text
    assert 'name="negative_prompt"' in page.text
    assert 'name="keep_warm" value="true"' in page.text
    assert "Maximum added idle exposure" in page.text
    assert "worker restart" in page.text
    assert 'data-warm-status-url="/dashboard/experiments/warm-session"' in page.text
    assert "Start 15-minute session" in page.text
    assert "Experiment Lab" in client.get("/dashboard").text

    assert script.status_code == 200
    assert "initializeExperimentLab" in script.text
    assert "profileIsWarmReady" in script.text
    assert "duration_minutes: 15" in script.text
    assert "initializeExperimentResults" in script.text
    assert "Full prompt & settings" in script.text
    assert stylesheet.status_code == 200
    assert ".experiment-workbench" in stylesheet.text
    assert ".experiment-comparison-grid" in stylesheet.text


def test_experiment_submission_creates_one_release_job_per_variant_with_paired_seeds(
    client: TestClient,
) -> None:
    options = _seed_options(client)
    _configure_worker_manifest(client, options)
    _seed_warm_deployment(client)
    page = client.get("/dashboard/experiments/new")
    group_slug = _hidden_value(page.text, "group_slug")
    variants = [
        _variant(options=options, label="Baseline", prompt="portrait, style a", steps=24),
        _variant(options=options, label="More steps", prompt="portrait, style a", steps=30),
    ]
    form = {
        "csrf_token": _hidden_value(page.text, "csrf_token"),
        "submission_id": _hidden_value(page.text, "submission_id"),
        "idempotency_key": _hidden_value(page.text, "idempotency_key"),
        "group_slug": group_slug,
        "experiment_title": "Portrait sampler test",
        "outputs_per_variant": "2",
        "paired_seeds": "true",
        "keep_warm": "true",
        "base_seed": "456789",
        "variant_plan": json.dumps(variants),
    }

    first = client.post("/dashboard/experiments/new", data=form, follow_redirects=False)
    replay = client.post("/dashboard/experiments/new", data=form, follow_redirects=False)

    assert first.status_code == 303
    assert replay.status_code == 303
    assert first.headers["location"] == f"/dashboard/experiments/{group_slug}"
    assert replay.headers["location"] == first.headers["location"]

    database = client.app.state.database
    assert client.portal is not None

    async def state() -> tuple[
        list[Release],
        list[GenerationJob],
        list[ReleaseVersion],
        list[ExperimentWarmLease],
    ]:
        async with database.sessions() as session:
            releases = list((await session.scalars(select(Release).order_by(Release.slug))).all())
            jobs = list(
                (await session.scalars(select(GenerationJob).order_by(GenerationJob.id))).all()
            )
            versions = list(
                (await session.scalars(select(ReleaseVersion).order_by(ReleaseVersion.id))).all()
            )
            leases = list((await session.scalars(select(ExperimentWarmLease))).all())
            return releases, jobs, versions, leases

    releases, jobs, versions, leases = client.portal.call(state)
    assert len(releases) == 2
    assert len(jobs) == 2
    assert all(job.expected_output_count == 2 for job in jobs)
    assert [release.slug.split("-")[2] for release in releases] == ["01", "02"]
    assert [release.desired_accepted_count for release in releases] == [2, 2]
    assert {version.specification["generation"]["seed"] for version in versions} == {456789}
    assert sorted(version.specification["generation"]["steps"] for version in versions) == [24, 30]
    assert len(leases) == 1

    results = client.get(first.headers["location"])
    progress = client.get(f"{first.headers['location']}/progress")
    assert results.status_code == 200
    assert "Baseline" in results.text
    assert "More steps" in results.text
    assert "data-experiment-results" in results.text
    assert progress.status_code == 200
    assert progress.json()["expected"] == 4
    assert len(progress.json()["variants"]) == 2


def test_failed_experiment_creation_rolls_back_the_uncommitted_warm_lease(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _seed_options(client)
    _configure_worker_manifest(client, options)
    _seed_warm_deployment(client)
    page = client.get("/dashboard/experiments/new")

    async def fail_creation(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ExperimentInputError("forced experiment failure")

    monkeypatch.setattr(experiment_dashboard, "create_experiment", fail_creation)
    response = client.post(
        "/dashboard/experiments/new",
        data={
            "csrf_token": _hidden_value(page.text, "csrf_token"),
            "submission_id": _hidden_value(page.text, "submission_id"),
            "idempotency_key": _hidden_value(page.text, "idempotency_key"),
            "group_slug": _hidden_value(page.text, "group_slug"),
            "experiment_title": "Rollback warm setup",
            "outputs_per_variant": "1",
            "paired_seeds": "true",
            "keep_warm": "true",
            "base_seed": "123",
            "variant_plan": json.dumps(
                [
                    _variant(options=options, label="A", prompt="portrait a"),
                    _variant(options=options, label="B", prompt="portrait b"),
                ]
            ),
        },
    )

    assert response.status_code == 409
    database = client.app.state.database
    assert client.portal is not None

    async def lease_count() -> int:
        async with database.sessions() as session:
            return len(list((await session.scalars(select(ExperimentWarmLease))).all()))

    assert client.portal.call(lease_count) == 0


def test_replaying_a_completed_experiment_form_does_not_renew_an_active_warm_lease(
    client: TestClient,
) -> None:
    options = _seed_options(client)
    _configure_worker_manifest(client, options)
    _seed_warm_deployment(client)
    page = client.get("/dashboard/experiments/new")
    form = {
        "csrf_token": _hidden_value(page.text, "csrf_token"),
        "submission_id": _hidden_value(page.text, "submission_id"),
        "idempotency_key": _hidden_value(page.text, "idempotency_key"),
        "group_slug": _hidden_value(page.text, "group_slug"),
        "experiment_title": "Replay warm setup",
        "outputs_per_variant": "1",
        "paired_seeds": "true",
        "keep_warm": "true",
        "base_seed": "123",
        "variant_plan": json.dumps(
            [
                _variant(options=options, label="A", prompt="portrait a"),
                _variant(options=options, label="B", prompt="portrait b"),
            ]
        ),
    }
    created = client.post("/dashboard/experiments/new", data=form, follow_redirects=False)
    assert created.status_code == 303

    database = client.app.state.database
    assert client.portal is not None

    async def activate_and_snapshot() -> tuple[datetime | None, datetime]:
        async with database.sessions() as session:
            lease = await session.scalar(select(ExperimentWarmLease))
            deployment = await session.scalar(select(SaladDeployment))
            assert lease is not None
            assert deployment is not None
            mark_experiment_warm_runtime_refreshed_locked(
                session,
                lease,
                provider_version=2,
                actor="test",
            )
            deployment.observed_replicas = 1
            deployment.ready_replicas = 1
            await session.commit()
        async with database.sessions() as session:
            activated = await activate_ready_experiment_warm_leases(session, actor="test")
            assert len(activated) == 1
            await session.commit()
            lease = await session.scalar(select(ExperimentWarmLease))
            assert lease is not None
            return lease.last_activity_at, lease.expires_at

    before = client.portal.call(activate_and_snapshot)
    replay = client.post("/dashboard/experiments/new", data=form, follow_redirects=False)
    assert replay.status_code == 303

    async def snapshot() -> tuple[datetime | None, datetime]:
        async with database.sessions() as session:
            lease = await session.scalar(select(ExperimentWarmLease))
            assert lease is not None
            return lease.last_activity_at, lease.expires_at

    assert client.portal.call(snapshot) == before


def test_experiment_form_rejects_an_unbounded_variant_queue(client: TestClient) -> None:
    options = _seed_options(client)
    page = client.get("/dashboard/experiments/new")
    group_slug = _hidden_value(page.text, "group_slug")
    variant = _variant(options=options, label="Variant", prompt="portrait")
    variants = []
    for index in range(13):
        current = dict(variant)
        current["label"] = f"Variant {index + 1}"
        variants.append(current)
    response = client.post(
        "/dashboard/experiments/new",
        data={
            "csrf_token": _hidden_value(page.text, "csrf_token"),
            "submission_id": _hidden_value(page.text, "submission_id"),
            "idempotency_key": _hidden_value(page.text, "idempotency_key"),
            "group_slug": group_slug,
            "experiment_title": "Too many",
            "outputs_per_variant": "2",
            "paired_seeds": "true",
            "base_seed": "123",
            "variant_plan": json.dumps(variants),
        },
    )

    assert response.status_code == 422
    assert "between 2 and 12 variants" in response.text


def test_warm_session_routes_are_exact_authenticated_and_idempotent(client: TestClient) -> None:
    _seed_warm_deployment(client)
    page = client.get("/dashboard/experiments/new")
    csrf = _hidden_value(page.text, "csrf_token")
    headers = {"X-CSRF-Token": csrf}

    initial = client.get("/dashboard/experiments/warm-session")
    rejected = client.post(
        "/dashboard/experiments/warm-session/start",
        json={"duration_minutes": 15},
    )
    started = client.post(
        "/dashboard/experiments/warm-session/start",
        json={"duration_minutes": 15},
        headers=headers,
    )
    replay = client.post(
        "/dashboard/experiments/warm-session/start",
        json={"duration_minutes": 15},
        headers=headers,
    )

    assert initial.status_code == 200
    assert initial.json()["available"] is True
    assert initial.json()["state"] == "off"
    assert rejected.status_code == 403
    assert started.status_code == 200
    assert started.json()["state"] == "starting"
    assert started.json()["remaining_seconds"] == 0
    assert started.json()["controller_auto_stop_minutes"] == 90
    assert "lease_id" not in started.json()
    assert replay.status_code == 200
    assert replay.json()["state"] == "starting"

    database = client.app.state.database
    assert client.portal is not None

    async def activate() -> None:
        async with database.sessions() as session:
            lease = await session.scalar(select(ExperimentWarmLease))
            deployment = await session.scalar(select(SaladDeployment))
            assert lease is not None
            assert deployment is not None
            mark_experiment_warm_runtime_refreshed_locked(
                session,
                lease,
                provider_version=2,
                actor="test",
            )
            deployment.observed_replicas = 1
            deployment.ready_replicas = 1
            await session.commit()
        async with database.sessions() as session:
            activated = await activate_ready_experiment_warm_leases(session, actor="test")
            assert len(activated) == 1
            await session.commit()

    client.portal.call(activate)
    extended = client.post(
        "/dashboard/experiments/warm-session/extend",
        json={"duration_minutes": 15},
        headers=headers,
    )
    ended = client.post(
        "/dashboard/experiments/warm-session/end",
        json={},
        headers=headers,
    )
    final = client.get("/dashboard/experiments/warm-session")

    assert extended.status_code == 200
    assert extended.json()["state"] == "warm"
    assert extended.json()["remaining_seconds"] >= 29 * 60
    assert ended.status_code == 200
    assert ended.json()["state"] == "ending"
    assert final.status_code == 200
    assert final.json()["state"] == "ending"


def test_optional_auto_warm_failure_does_not_fail_a_queued_experiment(
    client: TestClient,
) -> None:
    options = _seed_options(client)
    _configure_worker_manifest(client, options)
    page = client.get("/dashboard/experiments/new")
    group_slug = _hidden_value(page.text, "group_slug")
    response = client.post(
        "/dashboard/experiments/new",
        data={
            "csrf_token": _hidden_value(page.text, "csrf_token"),
            "submission_id": _hidden_value(page.text, "submission_id"),
            "idempotency_key": _hidden_value(page.text, "idempotency_key"),
            "group_slug": group_slug,
            "experiment_title": "Warm fallback",
            "outputs_per_variant": "1",
            "paired_seeds": "true",
            "keep_warm": "true",
            "base_seed": "123",
            "variant_plan": json.dumps(
                [
                    _variant(options=options, label="A", prompt="portrait a"),
                    _variant(options=options, label="B", prompt="portrait b"),
                ]
            ),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (f"/dashboard/experiments/{group_slug}?warm=unavailable")
    results = client.get(response.headers["location"])
    assert results.status_code == 200
    assert "comparison was queued successfully" in results.text
