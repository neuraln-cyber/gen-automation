import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

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
from gen_automation.services.new_sets import list_new_set_options
from tests.factories import seed_release_approvals, valid_release_payload


def _hidden_value(page: str, name: str) -> str:
    match = re.search(rf'<input type="hidden" name="{name}" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


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
    client.app.state.settings.salad_worker_artifact_bucket = SecretStr("test-artifact-bucket")
    client.app.state.settings.salad_container_storage_bytes = 10 * 1024**3


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


def _automation_form(
    page: str,
    options: object,
    *,
    slug: str,
    title: str,
    batches: list[dict[str, object]],
    width: int = 1024,
    steps: int = 30,
) -> dict[str, str]:
    form = {
        "csrf_token": _hidden_value(page, "csrf_token"),
        "submission_id": _hidden_value(page, "submission_id"),
        "idempotency_key": _hidden_value(page, "idempotency_key"),
        "slug": slug,
        "title": title,
        "subject_id": str(options.subjects[0].approval_id),
        "subject_2_id": "",
        "composition_mode": "single",
        "duo_contract_version": "1",
        "composition_preset_id": "",
        "character_a_prompt": "",
        "character_b_prompt": "",
        "character_a_negative_prompt": "",
        "character_b_negative_prompt": "",
        "interaction_prompt": "",
        "camera_prompt": "",
        "duo_isolation_mode": "balanced",
        "duo_quality_mode": "standard",
        "checkpoint_id": str(options.checkpoints[0].approval_id),
        "workflow_id": str(options.workflows[0].approval_id),
        "prompt": "shared prompt",
        "negative_prompt": "low quality",
        "detailer_prompt": "detailed face",
        "detailer_negative_prompt": "closed eyes",
        "batch_plan": json.dumps(batches),
        "seed": "-1",
        "width": str(width),
        "height": "1024",
        "cfg": "6.0",
        "steps": str(steps),
        "sampler": "euler",
        "scheduler": "normal",
        "clip_skip": "2",
        "outputs_per_job": "25",
        "hires_scale": "1.0",
        "hires_denoise": "0.35",
        "hires_upscale_method": "bislerp",
        "detailer_guide_size": "768",
        "detailer_max_size": "1024",
        "detailer_denoise": "0.4",
        "detailer_bbox_threshold": "0.3",
        "detailer_bbox_dilation": "4",
        "detailer_bbox_crop_factor": "1.5",
        "detailer_feather": "4",
        "planned_job_count": "1",
        "desired_accepted_count": str(
            min(sum(int(batch["image_count"]) for batch in batches), 500)
        ),
    }
    for slot in range(1, 9):
        form[f"lora_{slot}_id"] = ""
        form[f"lora_{slot}_weight"] = ""
    return form


def test_experiment_lab_is_the_full_automation_builder_plus_warm_controls(
    client: TestClient,
) -> None:
    _seed_options(client)

    automation = client.get("/dashboard/new-set")
    lab = client.get("/dashboard/experiments/new")
    script = client.get("/static/dashboard.js")

    assert automation.status_code == 200
    assert lab.status_code == 200
    for contract in (
        "data-automation-form",
        'id="batch-builder"',
        'id="batch-list"',
        'id="batch-row-template"',
        "data-automation-presets",
        "data-lora-picker",
        'name="outputs_per_job"',
        'name="desired_accepted_count"',
    ):
        assert contract in automation.text
        assert contract in lab.text
    assert 'action="/dashboard/new-set?mode=experiment"' in lab.text
    assert 'data-automation-draft-scope="experiment"' in lab.text
    assert 'data-automation-draft-scope="automation"' in automation.text
    assert 'data-warm-status-url="/dashboard/experiments/warm-session"' in lab.text
    assert "Interactive testing" in lab.text
    assert "Queue test" in lab.text
    assert 'data-warm-duration-minutes="15"' in lab.text
    assert 'data-warm-duration-minutes="90"' in lab.text
    assert "Start 90 min focus" in lab.text
    assert "data-experiment-form" not in lab.text
    assert "variant_plan" not in lab.text
    assert "outputs_per_variant" not in lab.text
    assert "paired_seeds" not in lab.text
    assert "comparison" not in lab.text.casefold()
    assert "queued variants" not in script.text
    assert "Focus session · GPU ready for follow-up tests" in script.text
    assert "configuredDuration === 90" in script.text
    assert "duration_minutes: durationMinutes" in script.text
    assert "hardSeconds - seconds >= 15 * 60" in script.text
    assert 'document.querySelector("[data-automation-draft-scope]")' in script.text
    assert 'if (draftScope === "automation") return scopedStorageKey(key);' in script.text

    legacy_post = client.post("/dashboard/experiments/new", data={}, follow_redirects=False)
    assert legacy_post.status_code == 303
    assert legacy_post.headers["location"] == "/dashboard/experiments/new"


def test_experiment_lab_queues_large_multi_batch_automation_and_reuses_one_warm_lease(
    client: TestClient,
) -> None:
    options = _seed_options(client)
    _configure_worker_manifest(client, options)
    _seed_warm_deployment(client)
    batches = [
        {
            "name": "Large batch",
            "image_count": 61,
            "prompt": "large batch prompt",
            "negative_prompt": "large batch negative",
            "seed": 100,
        },
        *[
            {
                "name": f"Batch {index}",
                "image_count": 5,
                "prompt": f"batch {index} prompt",
                "seed": 100 + index,
            }
            for index in range(2, 14)
        ],
    ]
    first_page = client.get("/dashboard/experiments/new")
    first_form = _automation_form(
        first_page.text,
        options,
        slug="warm-lab-first",
        title="Warm Lab First",
        batches=batches,
    )

    first = client.post(
        "/dashboard/new-set?mode=experiment",
        data=first_form,
        follow_redirects=False,
    )
    database = client.app.state.database
    assert client.portal is not None

    async def lease_snapshot() -> tuple[int, datetime | None, datetime]:
        async with database.sessions() as session:
            lease = await session.scalar(select(ExperimentWarmLease))
            assert lease is not None
            return lease.lock_version, lease.last_activity_at, lease.expires_at

    before_replay = client.portal.call(lease_snapshot)
    replay = client.post(
        "/dashboard/new-set?mode=experiment",
        data=first_form,
        follow_redirects=False,
    )

    assert first.status_code == 303
    assert replay.status_code == 303
    assert first.headers["location"] == replay.headers["location"]
    assert "&mode=experiment" in first.headers["location"]
    assert client.portal.call(lease_snapshot) == before_replay

    second_page = client.get("/dashboard/experiments/new")
    second_form = _automation_form(
        second_page.text,
        options,
        slug="warm-lab-second",
        title="Warm Lab Second",
        batches=[{"name": "Follow-up", "image_count": 7, "prompt": "different prompt"}],
        width=1152,
        steps=40,
    )
    second = client.post(
        "/dashboard/new-set?mode=experiment",
        data=second_form,
        follow_redirects=False,
    )
    assert second.status_code == 303

    async def state() -> tuple[
        list[Release],
        list[GenerationJob],
        list[ReleaseVersion],
        list[ExperimentWarmLease],
    ]:
        async with database.sessions() as session:
            releases = list((await session.scalars(select(Release).order_by(Release.slug))).all())
            jobs = list(
                (
                    await session.scalars(
                        select(GenerationJob).order_by(
                            GenerationJob.release_version_id,
                            GenerationJob.logical_key,
                        )
                    )
                ).all()
            )
            versions = list(
                (await session.scalars(select(ReleaseVersion).order_by(ReleaseVersion.id))).all()
            )
            leases = list((await session.scalars(select(ExperimentWarmLease))).all())
            return releases, jobs, versions, leases

    releases, jobs, versions, leases = client.portal.call(state)
    assert [release.slug for release in releases] == ["warm-lab-first", "warm-lab-second"]
    assert len(leases) == 1
    assert len(jobs) == 21
    first_version = next(version for version in versions if version.release_id == releases[0].id)
    assert len(first_version.specification["generation_batches"]) == 13
    assert first_version.specification["generation_batches"][0]["image_count"] == 61
    first_job_outputs = sorted(
        job.expected_output_count for job in jobs if job.release_version_id == first_version.id
    )
    assert first_job_outputs == [5] * 13 + [8] * 7
    second_version = next(version for version in versions if version.release_id == releases[1].id)
    assert second_version.specification["generation"]["width"] == 1152
    assert second_version.specification["generation"]["steps"] == 40

    status_page = client.get(first.headers["location"])
    assert status_page.status_code == 200
    assert "Queue next test" in status_page.text
    assert 'data-warm-duration-minutes="90"' in status_page.text
    assert "data-experiment-warm-session" in status_page.text
    assert "data-generation-progress" in status_page.text
    assert 'data-automation-draft-scope="experiment"' in status_page.text
    assert "data-experiment-results" not in status_page.text


def test_legacy_experiment_comparison_remains_readable(client: TestClient) -> None:
    options = _seed_options(client)
    _configure_worker_manifest(client, options)
    group_slug = "experiment-abcdef123456"
    for index, label in enumerate(("Baseline", "Alternate"), start=1):
        page = client.get("/dashboard/new-set")
        form = _automation_form(
            page.text,
            options,
            slug=f"{group_slug}-{index:02d}-{label.casefold()}",
            title=f"Legacy comparison - {label}",
            batches=[{"name": label, "image_count": 2, "prompt": label.casefold()}],
        )
        created = client.post("/dashboard/new-set", data=form, follow_redirects=False)
        assert created.status_code == 303

    legacy = client.get(f"/dashboard/experiments/{group_slug}")
    progress = client.get(f"/dashboard/experiments/{group_slug}/progress")
    assert legacy.status_code == 200
    assert "data-experiment-results" in legacy.text
    assert "Queue a warm batch" in legacy.text
    assert progress.status_code == 200
    assert len(progress.json()["variants"]) == 2


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


def test_focus_session_can_hold_the_shared_worker_for_ninety_minutes(
    client: TestClient,
) -> None:
    _seed_warm_deployment(client)
    page = client.get("/dashboard/experiments/new")
    csrf = _hidden_value(page.text, "csrf_token")

    started = client.post(
        "/dashboard/experiments/warm-session/start",
        json={"duration_minutes": 90},
        headers={"X-CSRF-Token": csrf},
    )

    assert started.status_code == 200
    payload = started.json()
    assert payload["state"] == "starting"
    assert payload["idle_ttl_seconds"] == 90 * 60
    assert payload["controller_auto_stop_minutes"] == 90
    assert payload["hard_remaining_seconds"] >= (89 * 60)
    assert payload["max_cost_usd"] == "0.54"


def test_optional_auto_warm_failure_does_not_fail_a_queued_lab_set(
    client: TestClient,
) -> None:
    options = _seed_options(client)
    _configure_worker_manifest(client, options)
    page = client.get("/dashboard/experiments/new")
    form = _automation_form(
        page.text,
        options,
        slug="warm-fallback",
        title="Warm fallback",
        batches=[{"name": "Fallback", "image_count": 9, "prompt": "portrait"}],
    )
    response = client.post(
        "/dashboard/new-set?mode=experiment",
        data=form,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "mode=experiment" in response.headers["location"]
    assert "warm=unavailable" in response.headers["location"]
    status_page = client.get(response.headers["location"])
    assert status_page.status_code == 200
    assert "This set was queued, but the optional warm hold could not be started" in (
        status_page.text
    )
