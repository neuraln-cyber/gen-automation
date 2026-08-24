import json
import re
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from gen_automation.config import Settings
from gen_automation.db.models import GenerationJob, Release, ReleaseVersion, WorkflowApproval
from gen_automation.db.session import Database
from gen_automation.domain.release_spec import ProjectCreate, ReleaseCreate, ReleaseSpecification
from gen_automation.domain.wildcards import WildcardCreate
from gen_automation.services.generation import approve_and_expand_generation_plan
from gen_automation.services.new_sets import (
    NewSetBatchSubmission,
    NewSetInputError,
    NewSetSubmission,
    create_and_approve_new_set,
    list_new_set_options,
)
from gen_automation.services.releases import create_project, create_release
from gen_automation.services.wildcards import create_wildcard_library
from tests.factories import seed_release_approvals, valid_release_payload


def test_batch_submission_preserves_a_64_bit_seed_from_json_text() -> None:
    seed = (2**63) - 1

    batch = NewSetBatchSubmission.model_validate(
        {
            "name": "Exact seed",
            "image_count": 4,
            "prompt": "masterpiece, __sfw__",
            "seed": str(seed),
        }
    )

    assert batch.seed == seed


def test_batch_submission_accepts_the_random_per_image_seed_sentinel() -> None:
    batch = NewSetBatchSubmission.model_validate(
        {
            "name": "Random seeds",
            "image_count": 4,
            "prompt": "masterpiece, __sfw__",
            "seed": "-1",
        }
    )

    assert batch.seed == -1


def test_batch_submission_dimensions_are_optional_but_must_be_a_valid_pair() -> None:
    legacy = NewSetBatchSubmission.model_validate(
        {"name": "Inherited size", "image_count": 4, "prompt": "portrait"}
    )
    landscape = NewSetBatchSubmission.model_validate(
        {
            "name": "Landscape",
            "image_count": 4,
            "prompt": "wide scene",
            "width": 1480,
            "height": 1144,
        }
    )

    assert (legacy.width, legacy.height) == (None, None)
    assert (landscape.width, landscape.height) == (1480, 1144)

    with pytest.raises(ValidationError, match="provided together"):
        NewSetBatchSubmission.model_validate(
            {"name": "Partial", "image_count": 4, "prompt": "portrait", "width": 1480}
        )
    with pytest.raises(ValidationError, match="multiple of 8"):
        NewSetBatchSubmission.model_validate(
            {
                "name": "Misaligned",
                "image_count": 4,
                "prompt": "portrait",
                "width": 1479,
                "height": 1144,
            }
        )


def test_new_set_submission_requires_a_complete_distinct_duo() -> None:
    base = {
        "slug": "duo-validation",
        "title": "Duo validation",
        "subject_approval_id": "00000000-0000-0000-0000-000000000001",
        "checkpoint_approval_id": "00000000-0000-0000-0000-000000000003",
        "workflow_approval_id": "00000000-0000-0000-0000-000000000004",
        "prompt": "two women together",
        "seed": 1,
        "width": 1024,
        "height": 1024,
        "steps": 30,
        "sampler": "euler",
        "scheduler": "normal",
        "outputs_per_job": 4,
        "planned_job_count": 1,
        "desired_accepted_count": 4,
    }

    with pytest.raises(ValidationError, match="requires a second subject"):
        NewSetSubmission.model_validate({**base, "composition_mode": "duo"})
    with pytest.raises(ValidationError, match="two different subjects"):
        NewSetSubmission.model_validate(
            {
                **base,
                "composition_mode": "duo",
                "secondary_subject_approval_id": base["subject_approval_id"],
                "character_a_prompt": "character a",
                "character_b_prompt": "character b",
            }
        )
    with pytest.raises(ValidationError, match="both character prompts"):
        NewSetSubmission.model_validate(
            {
                **base,
                "composition_mode": "duo",
                "secondary_subject_approval_id": ("00000000-0000-0000-0000-000000000002"),
                "character_a_prompt": "character a",
            }
        )

    parsed = NewSetSubmission.model_validate(
        {
            **base,
            "composition_mode": "duo",
            "secondary_subject_approval_id": "00000000-0000-0000-0000-000000000002",
            "character_a_prompt": "character a",
            "character_b_prompt": "character b",
        }
    )
    assert parsed.composition_mode == "duo"


@pytest.fixture
async def batch_database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'generation-batches.db').as_posix()}")
    await database.create_schema()
    try:
        yield database
    finally:
        await database.dispose()


def _batch_release_payload() -> dict[str, object]:
    payload = deepcopy(valid_release_payload())
    payload["desired_accepted_count"] = 8
    specification = payload["specification"]
    assert isinstance(specification, dict)
    first_generation = specification["generation"]
    assert isinstance(first_generation, dict)
    first_generation.update(
        {
            "prompt": "first prompt structure, __first-poses__",
            "seed": 100,
            "outputs_per_job": 4,
        }
    )
    second_generation = deepcopy(first_generation)
    second_generation.update({"prompt": "second prompt structure, __second-poses__", "seed": 200})
    specification.update(
        {
            "schema_version": 2,
            "planned_job_count": 3,
            "generation_batches": [
                {
                    "name": "First five",
                    "image_count": 5,
                    "generation": deepcopy(first_generation),
                },
                {
                    "name": "Next three",
                    "image_count": 3,
                    "generation": second_generation,
                },
            ],
        }
    )
    return payload


def test_batch_specification_requires_an_exact_provider_job_count() -> None:
    payload = _batch_release_payload()
    specification = payload["specification"]
    assert isinstance(specification, dict)
    parsed = ReleaseSpecification.model_validate(specification)

    assert parsed.planned_job_count == 3
    assert [batch.image_count for batch in parsed.generation_batches] == [5, 3]

    specification["planned_job_count"] = 2
    with pytest.raises(ValidationError, match="planned job count"):
        ReleaseSpecification.model_validate(specification)


def test_legacy_specification_serialization_does_not_add_an_empty_batch_field() -> None:
    specification = ReleaseSpecification.model_validate(valid_release_payload()["specification"])

    assert "generation_batches" not in specification.model_dump(mode="json")


def test_new_set_batch_submission_derives_job_count_and_allows_overproduction() -> None:
    common = {
        "slug": "overnight-chain",
        "title": "Overnight chain",
        "subject_approval_id": "10000000-0000-4000-8000-000000000001",
        "checkpoint_approval_id": "20000000-0000-4000-8000-000000000002",
        "workflow_approval_id": "30000000-0000-4000-8000-000000000003",
        "prompt": "",
        "negative_prompt": "low quality",
        "seed": 1234,
        "width": 1024,
        "height": 1024,
        "steps": 30,
        "sampler": "euler_ancestral",
        "scheduler": "karras",
        "outputs_per_job": 4,
        "planned_job_count": 1,
        "desired_accepted_count": 100,
    }
    command = NewSetSubmission.model_validate(
        {
            **common,
            "batches": [
                {"name": "NSFW", "image_count": 20, "prompt": "__nsfw__"},
                {"name": "NNSFW", "image_count": 100, "prompt": "__nnsfw__"},
            ],
        }
    )

    assert command.effective_planned_job_count == 30
    assert sum(batch.image_count for batch in command.batches) == 120


def test_large_wildcard_queue_preserves_stage_order_and_derives_all_jobs() -> None:
    ordered_batches = (
        ("SFW", 50, "__sfw__"),
        ("NNSFW", 100, "__nnsfw__"),
        ("NSFW", 50, "__nsfw__"),
        ("Oral", 20, "__oral__"),
        ("Reworked", 100, "__reworked__"),
        ("Group", 20, "__group__"),
    )
    command = NewSetSubmission.model_validate(
        {
            "slug": "large-ordered-queue",
            "title": "Large ordered queue",
            "subject_approval_id": "10000000-0000-4000-8000-000000000001",
            "checkpoint_approval_id": "20000000-0000-4000-8000-000000000002",
            "workflow_approval_id": "30000000-0000-4000-8000-000000000003",
            "prompt": "",
            "negative_prompt": "low quality",
            "seed": 1234,
            "width": 1024,
            "height": 1024,
            "steps": 30,
            "sampler": "euler_ancestral",
            "scheduler": "karras",
            "outputs_per_job": 4,
            "planned_job_count": 1,
            "desired_accepted_count": 300,
            "batches": [
                {"name": name, "image_count": count, "prompt": prompt}
                for name, count, prompt in ordered_batches
            ],
        }
    )

    assert command.effective_planned_job_count == 86
    assert command.desired_accepted_count == 340
    assert [batch.name for batch in command.batches] == [item[0] for item in ordered_batches]
    assert [batch.image_count for batch in command.batches] == [item[1] for item in ordered_batches]
    assert sum(batch.image_count for batch in command.batches) == 340

    capped = NewSetSubmission.model_validate(
        {
            **command.model_dump(),
            "batches": (),
            "prompt": "portrait",
            "outputs_per_job": 25,
            "planned_job_count": 21,
        }
    )
    assert capped.planned_output_count == 525
    assert capped.desired_accepted_count == 500


def test_new_set_submission_accepts_one_twenty_five_image_provider_job() -> None:
    command = NewSetSubmission.model_validate(
        {
            "slug": "one-job-twenty-five",
            "title": "One job, twenty-five images",
            "subject_approval_id": "10000000-0000-4000-8000-000000000001",
            "checkpoint_approval_id": "20000000-0000-4000-8000-000000000002",
            "workflow_approval_id": "30000000-0000-4000-8000-000000000003",
            "negative_prompt": "low quality",
            "seed": 500,
            "width": 1144,
            "height": 1480,
            "steps": 30,
            "sampler": "euler_ancestral",
            "scheduler": "karras",
            "outputs_per_job": 25,
            "planned_job_count": 1,
            "desired_accepted_count": 25,
            "batches": [
                {
                    "name": "One provider job",
                    "image_count": 25,
                    "prompt": "portrait, __sfw__",
                }
            ],
        }
    )

    assert command.effective_planned_job_count == 1


async def test_generation_batches_split_exact_counts_and_keep_ordered_prompt_metadata(
    batch_database: Database,
) -> None:
    payload = _batch_release_payload()
    async with batch_database.sessions() as session:
        await create_wildcard_library(
            session,
            command=WildcardCreate(name="first-poses", entries=["standing", "seated"]),
            actor="fixture-owner",
        )
        await create_wildcard_library(
            session,
            command=WildcardCreate(name="second-poses", entries=["indoors", "outdoors"]),
            actor="fixture-owner",
        )
        project = await create_project(session, ProjectCreate(slug="main", name="Main"))
        result = await create_release(
            session,
            project_id=project.id,
            command=ReleaseCreate.model_validate(payload),
            idempotency_key="create-batch-release",
        )
        await seed_release_approvals(session, payload)

    async with batch_database.sessions() as session:
        plan = await approve_and_expand_generation_plan(
            session,
            release_id=result.response.id,
            idempotency_key="approve-batch-release",
            settings=Settings(),
        )
        jobs = list(
            (
                await session.scalars(
                    select(GenerationJob).where(
                        GenerationJob.release_version_id == plan.response.release_version_id
                    )
                )
            ).all()
        )

    jobs.sort(key=lambda job: int(job.parameters["ordinal"]))
    assert plan.response.total_jobs == 3
    assert [job.expected_output_count for job in jobs] == [4, 1, 3]
    assert [job.parameters["batch"]["name"] for job in jobs] == [
        "First five",
        "First five",
        "Next three",
    ]
    assert [job.parameters["batch"]["image_offset"] for job in jobs] == [0, 4, 0]
    assert all(
        job.parameters["generation"]["prompt"].startswith("first prompt structure, ")
        for job in jobs[:2]
    )
    assert jobs[2].parameters["generation"]["prompt"].startswith("second prompt structure, ")
    assert all("__" not in job.parameters["generation"]["prompt"] for job in jobs)
    assert [output["seed"] for job in jobs for output in job.parameters["output_generations"]] == [
        100,
        101,
        102,
        103,
        104,
        200,
        201,
        202,
    ]


async def test_eight_image_batch_expands_to_one_provider_job(
    batch_database: Database,
) -> None:
    payload = deepcopy(valid_release_payload())
    payload["desired_accepted_count"] = 8
    specification = payload["specification"]
    assert isinstance(specification, dict)
    generation = specification["generation"]
    assert isinstance(generation, dict)
    generation.update(
        {
            "prompt": "portrait, __sfw__",
            "seed": 500,
            "outputs_per_job": 8,
        }
    )
    specification.update(
        {
            "schema_version": 2,
            "planned_job_count": 1,
            "generation_batches": [
                {
                    "name": "One provider job",
                    "image_count": 8,
                    "generation": deepcopy(generation),
                }
            ],
        }
    )

    async with batch_database.sessions() as session:
        await create_wildcard_library(
            session,
            command=WildcardCreate(name="sfw", entries=["standing", "seated", "walking"]),
            actor="fixture-owner",
        )
        project = await create_project(session, ProjectCreate(slug="one-job", name="One job"))
        result = await create_release(
            session,
            project_id=project.id,
            command=ReleaseCreate.model_validate(payload),
            idempotency_key="create-one-job-release",
        )
        await seed_release_approvals(session, payload)

    async with batch_database.sessions() as session:
        plan = await approve_and_expand_generation_plan(
            session,
            release_id=result.response.id,
            idempotency_key="approve-one-job-release",
            settings=Settings(),
        )
        jobs = list(
            (
                await session.scalars(
                    select(GenerationJob).where(
                        GenerationJob.release_version_id == plan.response.release_version_id
                    )
                )
            ).all()
        )

    assert plan.response.total_jobs == 1
    assert len(jobs) == 1
    job = jobs[0]
    outputs = job.parameters["output_generations"]
    resolutions = job.parameters["output_prompt_resolutions"]
    assert job.expected_output_count == 8
    assert job.parameters["generation"]["outputs_per_job"] == 8
    assert len(outputs) == 8
    assert len(resolutions) == 8
    assert [output["seed"] for output in outputs] == list(range(500, 508))
    assert [resolution["seed"] for resolution in resolutions] == list(range(500, 508))
    assert all(output["outputs_per_job"] == 1 for output in outputs)
    assert all("__sfw__" not in output["prompt"] for output in outputs)


async def test_random_seed_sentinel_resolves_unique_nonsequential_seeds_per_image(
    batch_database: Database,
) -> None:
    payload = deepcopy(valid_release_payload())
    payload["desired_accepted_count"] = 8
    specification = payload["specification"]
    assert isinstance(specification, dict)
    generation = specification["generation"]
    assert isinstance(generation, dict)
    generation.update(
        {
            "prompt": "portrait, __sfw__",
            "seed": -1,
            "outputs_per_job": 8,
        }
    )
    specification.update(
        {
            "schema_version": 2,
            "planned_job_count": 1,
            "generation_batches": [
                {
                    "name": "Randomized provider job",
                    "image_count": 8,
                    "generation": deepcopy(generation),
                }
            ],
        }
    )

    async with batch_database.sessions() as session:
        await create_wildcard_library(
            session,
            command=WildcardCreate(name="sfw", entries=["standing", "seated", "walking"]),
            actor="fixture-owner",
        )
        project = await create_project(
            session,
            ProjectCreate(slug="random-seeds", name="Random seeds"),
        )
        result = await create_release(
            session,
            project_id=project.id,
            command=ReleaseCreate.model_validate(payload),
            idempotency_key="create-random-seed-release",
        )
        await seed_release_approvals(session, payload)

    async with batch_database.sessions() as session:
        first = await approve_and_expand_generation_plan(
            session,
            release_id=result.response.id,
            idempotency_key="approve-random-seed-release",
            settings=Settings(),
        )
        replay = await approve_and_expand_generation_plan(
            session,
            release_id=result.response.id,
            idempotency_key="approve-random-seed-release",
            settings=Settings(),
        )
        job = await session.scalar(
            select(GenerationJob).where(
                GenerationJob.release_version_id == first.response.release_version_id
            )
        )

    assert replay.replayed is True
    assert job is not None
    outputs = job.parameters["output_generations"]
    resolutions = job.parameters["output_prompt_resolutions"]
    seeds = [output["seed"] for output in outputs]
    assert len(seeds) == 8
    assert len(set(seeds)) == 8
    assert all(0 <= seed <= (2**63) - 1 for seed in seeds)
    assert seeds != list(range(seeds[0], seeds[0] + 8))
    assert [resolution["seed"] for resolution in resolutions] == seeds
    assert all(output["seed"] != -1 for output in outputs)


async def test_new_set_service_freezes_a_batch_queue_with_inherited_prompt_settings(
    batch_database: Database,
) -> None:
    payload = valid_release_payload()
    async with batch_database.sessions() as session:
        await seed_release_approvals(session, payload)
        options = await list_new_set_options(session)
        command = NewSetSubmission(
            slug="queued-set",
            title="Queued set",
            subject_approval_id=options.subjects[0].approval_id,
            checkpoint_approval_id=options.checkpoints[0].approval_id,
            workflow_approval_id=options.workflows[0].approval_id,
            prompt="",
            negative_prompt="shared negative",
            detailer_prompt="shared detailer",
            detailer_negative_prompt="shared detailer negative",
            batches=(
                {
                    "name": "Five images",
                    "image_count": 5,
                    "prompt": "first queue prompt",
                    "width": 1480,
                    "height": 1144,
                },
                {
                    "name": "Three images",
                    "image_count": 3,
                    "prompt": "second queue prompt",
                    "negative_prompt": "",
                },
            ),
            seed=100,
            width=1144,
            height=1480,
            cfg=6,
            steps=30,
            sampler="euler_ancestral",
            scheduler="karras",
            outputs_per_job=4,
            planned_job_count=1,
            desired_accepted_count=8,
        )
        result = await create_and_approve_new_set(
            session,
            command=command,
            idempotency_key="new-set-batch-queue",
            settings=Settings(),
            actor="fixture-owner",
        )

    async with batch_database.sessions() as session:
        version = await session.scalar(
            select(ReleaseVersion).where(ReleaseVersion.release_id == result.release.id)
        )
        assert version is not None
        jobs = list(
            (
                await session.scalars(
                    select(GenerationJob).where(GenerationJob.release_version_id == version.id)
                )
            ).all()
        )

    frozen_batches = version.specification["generation_batches"]
    assert version.specification["schema_version"] == 2
    assert version.specification["planned_job_count"] == 3
    assert frozen_batches[0]["generation"]["negative_prompt"] == "shared negative"
    assert frozen_batches[1]["generation"]["negative_prompt"] == ""
    assert frozen_batches[0]["generation"]["seed"] == 100
    assert frozen_batches[1]["generation"]["seed"] == 105
    assert version.specification["generation"]["width"] == 1480
    assert version.specification["generation"]["height"] == 1144
    assert [
        (batch["generation"]["width"], batch["generation"]["height"]) for batch in frozen_batches
    ] == [(1480, 1144), (1144, 1480)]

    jobs.sort(key=lambda job: int(job.parameters["ordinal"]))
    assert [job.expected_output_count for job in jobs] == [4, 1, 3]
    assert [
        (job.parameters["generation"]["width"], job.parameters["generation"]["height"])
        for job in jobs
    ] == [(1480, 1144), (1480, 1144), (1144, 1480)]
    assert [
        (output["width"], output["height"])
        for job in jobs
        for output in job.parameters["output_generations"]
    ] == [(1480, 1144)] * 5 + [(1144, 1480)] * 3


async def test_new_set_rejects_an_undeliverable_batch_before_creating_a_release(
    batch_database: Database,
) -> None:
    payload = valid_release_payload()
    async with batch_database.sessions() as session:
        await seed_release_approvals(session, payload)
        options = await list_new_set_options(session)
        command = NewSetSubmission(
            slug="oversized-batch",
            title="Oversized batch",
            subject_approval_id=options.subjects[0].approval_id,
            checkpoint_approval_id=options.checkpoints[0].approval_id,
            workflow_approval_id=options.workflows[0].approval_id,
            prompt="",
            batches=(
                {
                    "name": "Too large",
                    "image_count": 1,
                    "prompt": "portrait",
                    "width": 4096,
                    "height": 4096,
                },
            ),
            seed=100,
            width=1024,
            height=1024,
            cfg=6,
            steps=30,
            sampler="euler_ancestral",
            scheduler="karras",
            outputs_per_job=1,
            planned_job_count=1,
        )

        with pytest.raises(NewSetInputError, match="exceeds"):
            await create_and_approve_new_set(
                session,
                command=command,
                idempotency_key="new-set-oversized-batch",
                settings=Settings(),
                actor="fixture-owner",
            )

    async with batch_database.sessions() as session:
        releases = list((await session.scalars(select(Release))).all())

    assert releases == []


async def test_new_set_service_freezes_two_subjects_and_regional_prompts_in_order(
    batch_database: Database,
) -> None:
    payload = valid_release_payload()
    specification = payload["specification"]
    assert isinstance(specification, dict)
    subjects = specification["subjects"]
    assert isinstance(subjects, list)
    second_subject = deepcopy(subjects[0])
    second_subject.update(
        {
            "name": "Second Approved Adult Character",
            "canonical_source_url": "https://example.com/second-character",
            "canonical_age": 31,
            "adult_approval_evidence": "Official profile states age 31.",
        }
    )
    subjects.append(second_subject)

    async with batch_database.sessions() as session:
        await seed_release_approvals(session, payload)
        workflow = await session.scalar(select(WorkflowApproval))
        assert workflow is not None
        workflow.reviewed_node_classes = [
            *workflow.reviewed_node_classes,
            "ConditioningSetAreaPercentage",
            "ConditioningCombine",
        ]
        await session.flush()
        options = await list_new_set_options(session)
        primary = next(item for item in options.subjects if item.name == "Approved Adult Character")
        secondary = next(
            item for item in options.subjects if item.name == "Second Approved Adult Character"
        )
        assert options.workflows[0].has_regional_prompting is True
        result = await create_and_approve_new_set(
            session,
            command=NewSetSubmission(
                slug="regional-duo",
                title="Regional duo",
                subject_approval_id=primary.approval_id,
                secondary_subject_approval_id=secondary.approval_id,
                composition_mode="duo",
                character_a_prompt="nico robin (one piece), adult woman",
                character_b_prompt="boa hancock (one piece), adult woman",
                checkpoint_approval_id=options.checkpoints[0].approval_id,
                workflow_approval_id=options.workflows[0].approval_id,
                prompt="2girls, together, snowy onsen",
                seed=700,
                width=1024,
                height=1024,
                cfg=6,
                steps=30,
                sampler="euler_ancestral",
                scheduler="karras",
                outputs_per_job=25,
                planned_job_count=1,
                desired_accepted_count=25,
            ),
            idempotency_key="new-set-regional-duo",
            settings=Settings(),
            actor="fixture-owner",
        )

    async with batch_database.sessions() as session:
        version = await session.scalar(
            select(ReleaseVersion).where(ReleaseVersion.release_id == result.release.id)
        )
        jobs = list(
            (
                await session.scalars(
                    select(GenerationJob).where(
                        GenerationJob.release_version_id
                        == result.generation_plan.release_version_id
                    )
                )
            ).all()
        )
        assert version is not None

    assert [item["name"] for item in version.specification["subjects"]] == [
        "Approved Adult Character",
        "Second Approved Adult Character",
    ]
    generation = version.specification["generation"]
    assert generation["composition_mode"] == "duo"
    assert generation["character_a_prompt"] == "nico robin (one piece), adult woman"
    assert generation["character_b_prompt"] == "boa hancock (one piece), adult woman"
    assert generation["outputs_per_job"] == 25
    assert version.specification["schema_version"] == 1
    assert version.specification["planned_job_count"] == 1
    assert "generation_batches" not in version.specification
    assert [job.expected_output_count for job in jobs] == [25]


def test_browser_new_set_accepts_the_optional_batch_plan_json_field(
    client: TestClient,
) -> None:
    database = client.app.state.database
    assert client.portal is not None

    async def seed_and_options() -> object:
        async with database.sessions() as session:
            await seed_release_approvals(session, valid_release_payload())
            return await list_new_set_options(session)

    options = client.portal.call(seed_and_options)
    page = client.get("/dashboard/new-set")

    def hidden(name: str) -> str:
        match = re.search(
            rf'<input type="hidden" name="{name}" value="([^"]+)">',
            page.text,
        )
        assert match is not None
        return match.group(1)

    form = {
        "csrf_token": hidden("csrf_token"),
        "submission_id": hidden("submission_id"),
        "idempotency_key": hidden("idempotency_key"),
        "slug": "browser-batch-queue",
        "title": "Browser batch queue",
        "subject_id": str(options.subjects[0].approval_id),
        "checkpoint_id": str(options.checkpoints[0].approval_id),
        "workflow_id": str(options.workflows[0].approval_id),
        "prompt": "",
        "negative_prompt": "shared negative",
        "detailer_prompt": "shared detailer",
        "detailer_negative_prompt": "shared detailer negative",
        "seed": "100",
        "width": "1024",
        "height": "1024",
        "cfg": "6",
        "steps": "30",
        "sampler": "euler_ancestral",
        "scheduler": "karras",
        "clip_skip": "2",
        "outputs_per_job": "4",
        "hires_scale": "1.5",
        "hires_denoise": "0.35",
        "hires_upscale_method": "bislerp",
        "detailer_guide_size": "768",
        "detailer_max_size": "1024",
        "detailer_denoise": "0.4",
        "detailer_bbox_threshold": "0.3",
        "detailer_bbox_dilation": "4",
        "detailer_bbox_crop_factor": "3",
        "detailer_feather": "4",
        "planned_job_count": "1",
        "desired_accepted_count": "8",
        "batch_plan": json.dumps(
            [
                {"name": "Five images", "image_count": 5, "prompt": "first"},
                {
                    "name": "Three images",
                    "image_count": 3,
                    "prompt": "second",
                    "width": 1480,
                    "height": 1144,
                },
            ]
        ),
    }
    for slot in range(1, 9):
        form[f"lora_{slot}_id"] = ""
        form[f"lora_{slot}_weight"] = ""

    response = client.post("/dashboard/new-set", data=form, follow_redirects=False)

    assert response.status_code == 303
