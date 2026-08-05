from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from gen_automation.db.models import (
    ComplianceCheck,
    GenerationJob,
    Release,
    ReleaseVersion,
    SubjectApproval,
    WorkflowApproval,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import ReleasePhase
from gen_automation.domain.release_spec import ProjectCreate, ReleaseCreate
from gen_automation.schemas import ProjectRead, ReleaseRead
from gen_automation.services.generation import (
    GenerationPlanConflictError,
    approve_and_expand_generation_plan,
)
from gen_automation.services.releases import create_project, create_release
from tests.factories import seed_release_approvals, valid_release_payload


@pytest.fixture
async def generation_database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'generation-plan.db').as_posix()}")
    await database.create_schema()
    try:
        yield database
    finally:
        await database.dispose()


async def _create_release(
    database: Database,
    *,
    seed_approvals: bool = True,
    payload: dict[str, object] | None = None,
) -> tuple[ProjectRead, ReleaseRead]:
    selected_payload = payload or valid_release_payload()
    async with database.sessions() as session:
        project = await create_project(
            session,
            ProjectCreate(slug="main", name="Main"),
        )
        release = await create_release(
            session,
            project_id=project.id,
            command=ReleaseCreate.model_validate(selected_payload),
            idempotency_key="create-release-for-plan",
        )
        if seed_approvals:
            await seed_release_approvals(session, selected_payload)
    return project, release.response


async def test_plan_expansion_is_deterministic_and_revalidated(
    generation_database: Database,
) -> None:
    _project, release = await _create_release(generation_database)

    async with generation_database.sessions() as session:
        first = await approve_and_expand_generation_plan(
            session,
            release_id=release.id,
            idempotency_key="approve-generation-plan",
        )
        replay = await approve_and_expand_generation_plan(
            session,
            release_id=release.id,
            idempotency_key="approve-generation-plan",
        )

        jobs = list(
            (
                await session.scalars(
                    select(GenerationJob).where(
                        GenerationJob.release_version_id == first.response.release_version_id
                    )
                )
            ).all()
        )
        checks = list(
            (
                await session.scalars(
                    select(ComplianceCheck).where(
                        ComplianceCheck.release_version_id == first.response.release_version_id
                    )
                )
            ).all()
        )

    assert first.replayed is False
    assert replay.replayed is True
    assert first.response.total_jobs == 3
    assert len(jobs) == 3
    assert len({job.logical_key for job in jobs}) == 3
    assert len({job.parameters_sha256 for job in jobs}) == 3
    assert sorted(job.parameters["generation"]["seed"] for job in jobs) == [
        1234,
        1235,
        1236,
    ]
    assert all(job.expected_output_count == 4 for job in jobs)
    assert all(job.parameters["schema_version"] == 2 for job in jobs)
    assert all(len(job.parameters["output_generations"]) == 4 for job in jobs)
    assert sorted(
        output["seed"] for job in jobs for output in job.parameters["output_generations"]
    ) == list(range(1234, 1246))
    assert all(
        output["outputs_per_job"] == 1
        for job in jobs
        for output in job.parameters["output_generations"]
    )
    assert all(len(job.parameters["approval_snapshot_sha256"]) == 64 for job in jobs)
    assert {check.check_type for check in checks} == {
        "adult_subject_gate",
        "artifact_license_gate",
        "workflow_integrity_gate",
    }
    assert all("prompt" not in str(check.evidence).lower() for check in checks)
    assert all(check.evidence["gate_version"] == 1 for check in checks)


async def test_plan_approval_fails_closed_without_server_owned_approvals(
    generation_database: Database,
) -> None:
    _project, release = await _create_release(
        generation_database,
        seed_approvals=False,
    )

    async with generation_database.sessions() as session:
        with pytest.raises(
            GenerationPlanConflictError,
            match="active approval registry",
        ):
            await approve_and_expand_generation_plan(
                session,
                release_id=release.id,
                idempotency_key="missing-server-approval",
            )


async def test_duo_plan_rejects_a_standard_workflow_before_creating_jobs(
    generation_database: Database,
) -> None:
    payload = valid_release_payload()
    specification = payload["specification"]
    assert isinstance(specification, dict)
    subjects = specification["subjects"]
    generation = specification["generation"]
    assert isinstance(subjects, list)
    assert isinstance(generation, dict)
    second_subject = deepcopy(subjects[0])
    second_subject.update(
        {
            "name": "Second Approved Adult Character",
            "canonical_source_url": "https://example.com/second-character",
        }
    )
    subjects.append(second_subject)
    generation.update(
        {
            "composition_mode": "duo",
            "character_a_prompt": "first adult character, on the left",
            "character_b_prompt": "second adult character, on the right",
        }
    )
    _project, release = await _create_release(generation_database, payload=payload)

    async with generation_database.sessions() as session:
        with pytest.raises(GenerationPlanConflictError, match="approved regional workflow"):
            await approve_and_expand_generation_plan(
                session,
                release_id=release.id,
                idempotency_key="duo-with-standard-workflow",
            )
        assert int(await session.scalar(select(func.count()).select_from(GenerationJob)) or 0) == 0


async def test_plan_rejects_post_hires_size_before_creating_gpu_jobs(
    generation_database: Database,
) -> None:
    payload = valid_release_payload()
    specification = payload["specification"]
    assert isinstance(specification, dict)
    generation = specification["generation"]
    assert isinstance(generation, dict)
    generation.update({"width": 2048, "height": 2048, "hires_scale": 2.0})
    _project, release = await _create_release(generation_database, payload=payload)

    async with generation_database.sessions() as session:
        workflow = await session.scalar(select(WorkflowApproval))
        assert workflow is not None
        workflow.reviewed_node_classes = [
            *workflow.reviewed_node_classes,
            "LatentUpscaleBy",
        ]
        await session.commit()

    async with generation_database.sessions() as session:
        with pytest.raises(
            GenerationPlanConflictError,
            match=r"4096x4096.*12000000 pixels",
        ):
            await approve_and_expand_generation_plan(
                session,
                release_id=release.id,
                idempotency_key="post-hires-too-large",
            )
        assert int(await session.scalar(select(func.count()).select_from(GenerationJob)) or 0) == 0


async def test_changed_approval_evidence_fails_closed(
    generation_database: Database,
) -> None:
    _project, release = await _create_release(generation_database)
    async with generation_database.sessions() as session:
        approval = await session.scalar(select(SubjectApproval))
        assert approval is not None
        approval.evidence = {"tampered": True}
        await session.commit()

    async with generation_database.sessions() as session:
        with pytest.raises(
            GenerationPlanConflictError,
            match="active approval registry",
        ):
            await approve_and_expand_generation_plan(
                session,
                release_id=release.id,
                idempotency_key="tampered-approval-evidence",
            )


async def test_changed_frozen_specification_fails_digest_check(
    generation_database: Database,
) -> None:
    _project, release = await _create_release(generation_database)
    async with generation_database.sessions() as session:
        version = await session.scalar(
            select(ReleaseVersion).where(ReleaseVersion.release_id == release.id)
        )
        assert version is not None
        changed = dict(version.specification)
        changed["planned_job_count"] = 4
        version.specification = changed
        await session.commit()

    async with generation_database.sessions() as session:
        with pytest.raises(
            GenerationPlanConflictError,
            match="digest mismatch",
        ):
            await approve_and_expand_generation_plan(
                session,
                release_id=release.id,
                idempotency_key="tampered-plan",
            )


@pytest.mark.parametrize(
    "phase",
    [
        ReleasePhase.PAUSED,
        ReleasePhase.GENERATING,
        ReleasePhase.REVIEWING,
        ReleasePhase.APPROVED,
        ReleasePhase.RENDERING,
    ],
)
async def test_plan_approval_does_not_regress_an_advanced_release_phase(
    generation_database: Database,
    phase: ReleasePhase,
) -> None:
    _project, release = await _create_release(generation_database)
    async with generation_database.sessions() as session:
        stored = await session.get(Release, release.id)
        assert stored is not None
        stored.phase = phase
        await session.commit()

        with pytest.raises(GenerationPlanConflictError, match="phase"):
            await approve_and_expand_generation_plan(
                session,
                release_id=release.id,
                idempotency_key=f"invalid-phase-{phase.value}",
            )


def test_generation_plan_approval_api_is_idempotent(client: TestClient) -> None:
    payload = valid_release_payload()
    project = client.post(
        "/api/v1/projects",
        json={"slug": "main", "name": "Main"},
    ).json()
    release = client.post(
        f"/api/v1/projects/{project['id']}/releases",
        json=payload,
        headers={"Idempotency-Key": "create-release-plan-api"},
    ).json()
    database = client.app.state.database
    assert client.portal is not None

    async def seed() -> None:
        async with database.sessions() as session:
            await seed_release_approvals(session, payload)

    client.portal.call(seed)
    headers = {"Idempotency-Key": "approve-release-plan-api"}
    url = f"/api/v1/releases/{release['id']}/generation-plan:approve"

    first = client.post(url, headers=headers)
    replay = client.post(url, headers=headers)

    assert first.status_code == 200
    assert first.json()["total_jobs"] == 3
    assert first.json()["jobs_created"] == 3
    assert first.headers["idempotency-replayed"] == "false"
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers["idempotency-replayed"] == "true"
