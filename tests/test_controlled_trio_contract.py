import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from urllib.parse import urlencode

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from starlette.requests import Request

from gen_automation.api.browser_new_set_forms import read_new_set_form
from gen_automation.config import Settings
from gen_automation.db.models import GenerationJob, ReleaseVersion
from gen_automation.db.session import Database
from gen_automation.domain.artifact_onboarding import WorkflowOnboardingEntry
from gen_automation.domain.controlled_duo import TrioCompositionPreset
from gen_automation.domain.release_spec import ReleaseCreate
from gen_automation.services.artifact_onboarding import (
    _validate_controlled_composition_onboarding_evidence,
)
from gen_automation.services.new_sets import (
    NewSetBatchSubmission,
    NewSetSubmission,
    create_and_approve_new_set,
    list_new_set_options,
)
from gen_automation.services.worker_inputs import WorkerInputError
from tests.factories import seed_release_approvals, valid_release_payload


def _controlled_trio_release_payload() -> dict[str, object]:
    payload = deepcopy(valid_release_payload())
    specification = payload["specification"]
    assert isinstance(specification, dict)
    subjects = specification["subjects"]
    workflow = specification["workflow"]
    generation = specification["generation"]
    assert isinstance(subjects, list)
    assert isinstance(workflow, dict)
    assert isinstance(generation, dict)
    for name, suffix in (
        ("Second Approved Adult Character", "second-adult-character"),
        ("Third Approved Adult Character", "third-adult-character"),
    ):
        subject = deepcopy(subjects[0])
        subject.update(
            {
                "name": name,
                "canonical_source_url": f"https://example.com/{suffix}",
            }
        )
        subjects.append(subject)
    workflow["capabilities"] = ["controlled_trio_v1"]
    generation.update(
        {
            "composition_mode": "trio",
            "duo_contract_version": 3,
            "composition_preset_id": "trio_flexible",
            "character_a_prompt": "adult woman, copper bob, green jacket",
            "character_b_prompt": "adult woman, indigo braid, ivory coat",
            "character_c_prompt": "adult woman, silver curls, crimson dress",
            "character_a_pose_prompt": "standing, one hand raised",
            "character_b_pose_prompt": "seated between the others",
            "character_c_pose_prompt": "kneeling, looking toward B",
            "character_a_negative_prompt": "indigo hair, silver curls",
            "character_b_negative_prompt": "copper hair, crimson dress",
            "character_c_negative_prompt": "green jacket, indigo braid",
            "interaction_prompt": "all three embracing in one coordinated pose",
            "camera_prompt": "wide full-body framing, all faces visible",
            "duo_isolation_mode": "balanced",
            "duo_quality_mode": "standard",
        }
    )
    return payload


def test_release_requires_three_distinct_subjects_and_trio_capability() -> None:
    payload = _controlled_trio_release_payload()
    parsed = ReleaseCreate.model_validate(payload)
    assert parsed.specification.generation.composition_mode == "trio"
    assert parsed.specification.generation.composition_preset_id == (TrioCompositionPreset.FLEXIBLE)

    workflow = payload["specification"]["workflow"]  # type: ignore[index]
    assert isinstance(workflow, dict)
    workflow["capabilities"] = []
    with pytest.raises(ValidationError, match="explicitly capable workflow"):
        ReleaseCreate.model_validate(payload)

    workflow["capabilities"] = ["controlled_trio_v1"]
    subjects = payload["specification"]["subjects"]  # type: ignore[index]
    assert isinstance(subjects, list)
    subjects.pop()
    with pytest.raises(ValidationError, match="exactly three distinct subjects"):
        ReleaseCreate.model_validate(payload)


def test_direct_trio_specs_support_twenty_five_but_not_twenty_six_outputs() -> None:
    payload = _controlled_trio_release_payload()
    generation = payload["specification"]["generation"]  # type: ignore[index]
    assert isinstance(generation, dict)
    generation["outputs_per_job"] = 25
    assert ReleaseCreate.model_validate(payload).specification.generation.outputs_per_job == 25

    generation["outputs_per_job"] = 26
    with pytest.raises(ValidationError, match="less than or equal to 25"):
        ReleaseCreate.model_validate(payload)


def test_trio_workflow_onboarding_requires_matching_marker_evidence() -> None:
    graph = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "workflows"
            / "illustrious-sdxl-controlled-trio-balanced-v1.json"
        ).read_text()
    )
    entry = cast(
        WorkflowOnboardingEntry,
        SimpleNamespace(capabilities=["controlled_trio_v1"]),
    )
    _validate_controlled_composition_onboarding_evidence(graph, entry=entry)

    extra_output = deepcopy(graph)
    extra_output["97"] = {
        "class_type": "SaveImageWebsocket",
        "inputs": {"images": ["36", 0]},
    }
    with pytest.raises(WorkerInputError, match="Controlled Trio onboarding evidence"):
        _validate_controlled_composition_onboarding_evidence(extra_output, entry=entry)

    missing_lane = deepcopy(graph)
    del missing_lane["18"]
    with pytest.raises(WorkerInputError, match="Controlled Trio onboarding evidence"):
        _validate_controlled_composition_onboarding_evidence(missing_lane, entry=entry)

    mismatched = cast(WorkflowOnboardingEntry, SimpleNamespace(capabilities=[]))
    with pytest.raises(WorkerInputError, match="Controlled Trio onboarding evidence"):
        _validate_controlled_composition_onboarding_evidence(graph, entry=mismatched)

    graph["99"]["inputs"]["mask_topology"] = "unreviewed"
    with pytest.raises(WorkerInputError, match="Controlled Trio onboarding evidence"):
        _validate_controlled_composition_onboarding_evidence(graph, entry=entry)


@pytest.mark.asyncio
async def test_new_set_service_freezes_trio_batch_overrides(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'controlled-trio.db').as_posix()}")
    await database.create_schema()
    payload = _controlled_trio_release_payload()
    try:
        async with database.sessions() as session:
            await seed_release_approvals(session, payload)
            options = await list_new_set_options(session)
            subjects = {subject.name: subject.approval_id for subject in options.subjects}
            workflow = next(item for item in options.workflows if item.supports_controlled_trio_v1)
            result = await create_and_approve_new_set(
                session,
                command=NewSetSubmission(
                    slug="controlled-trio",
                    title="Controlled trio",
                    subject_approval_id=subjects["Approved Adult Character"],
                    secondary_subject_approval_id=subjects["Second Approved Adult Character"],
                    tertiary_subject_approval_id=subjects["Third Approved Adult Character"],
                    composition_mode="trio",
                    duo_contract_version=3,
                    composition_preset_id="trio_triangle",
                    character_a_prompt="adult woman, copper bob, green jacket",
                    character_b_prompt="adult woman, indigo braid, ivory coat",
                    character_c_prompt="adult woman, silver curls, crimson dress",
                    character_a_pose_prompt="standing, one hand raised",
                    character_b_pose_prompt="seated between the others",
                    character_c_pose_prompt="kneeling, looking toward B",
                    character_a_negative_prompt="indigo hair, silver curls",
                    character_b_negative_prompt="copper hair, crimson dress",
                    character_c_negative_prompt="green jacket, indigo braid",
                    interaction_prompt="all three embracing in one coordinated pose",
                    camera_prompt="wide full-body framing, all faces visible",
                    checkpoint_approval_id=options.checkpoints[0].approval_id,
                    workflow_approval_id=workflow.approval_id,
                    prompt="shared dramatic scene",
                    batches=(
                        NewSetBatchSubmission(
                            name="Different pose",
                            image_count=29,
                            prompt="shared dramatic scene",
                            character_a_prompt="adult woman, copper bob, black dress",
                            character_c_pose_prompt="reclining and looking toward A",
                            character_b_negative_prompt="",
                            interaction_prompt="A and C reaching toward seated B",
                            camera_prompt="overhead wide shot",
                        ),
                    ),
                    seed=9,
                    width=1024,
                    height=1024,
                    cfg=6,
                    steps=30,
                    sampler="euler",
                    scheduler="normal",
                    outputs_per_job=25,
                    planned_job_count=1,
                    desired_accepted_count=29,
                ),
                idempotency_key="controlled-trio-contract",
                settings=Settings(),
                actor="fixture-owner",
            )
        async with database.sessions() as session:
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
            assert len(jobs) == 2
            job = jobs[0]
            generation = version.specification["generation"]
            assert generation["composition_mode"] == "trio"
            assert generation["duo_contract_version"] == 3
            assert generation["composition_preset_id"] == "trio_triangle"
            assert generation["character_a_prompt"].endswith("black dress")
            assert generation["character_c_pose_prompt"] == "reclining and looking toward A"
            assert generation["character_b_negative_prompt"] == ""
            assert generation["interaction_prompt"] == "A and C reaching toward seated B"
            assert generation["camera_prompt"] == "overhead wide shot"
            assert generation["outputs_per_job"] == 25
            assert version.specification["planned_job_count"] == 2
            assert version.specification["generation_batches"][0]["image_count"] == 29
            assert sorted(item.expected_output_count for item in jobs) == [4, 25]
            assert version.specification["workflow"]["capabilities"] == ["controlled_trio_v1"]
            assert job.parameters["output_generations"][0]["character_c_pose_prompt"] == (
                "reclining and looking toward A"
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_trio_legacy_plan_preserves_all_images_when_automatically_chunked(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'trio-chunking.db').as_posix()}")
    await database.create_schema()
    payload = _controlled_trio_release_payload()
    try:
        async with database.sessions() as session:
            await seed_release_approvals(session, payload)
            options = await list_new_set_options(session)
            subjects = {subject.name: subject.approval_id for subject in options.subjects}
            workflow = next(item for item in options.workflows if item.supports_controlled_trio_v1)
            result = await create_and_approve_new_set(
                session,
                command=NewSetSubmission(
                    slug="controlled-trio-legacy-plan",
                    title="Controlled trio legacy plan",
                    subject_approval_id=subjects["Approved Adult Character"],
                    secondary_subject_approval_id=subjects["Second Approved Adult Character"],
                    tertiary_subject_approval_id=subjects["Third Approved Adult Character"],
                    composition_mode="trio",
                    duo_contract_version=3,
                    composition_preset_id="trio_flexible",
                    character_a_prompt="adult woman, copper bob, green jacket",
                    character_b_prompt="adult woman, indigo braid, ivory coat",
                    character_c_prompt="adult woman, silver curls, crimson dress",
                    checkpoint_approval_id=options.checkpoints[0].approval_id,
                    workflow_approval_id=workflow.approval_id,
                    prompt="three adults in a coordinated scene",
                    seed=19,
                    width=1024,
                    height=1024,
                    cfg=6,
                    steps=30,
                    sampler="euler",
                    scheduler="normal",
                    outputs_per_job=25,
                    planned_job_count=2,
                    desired_accepted_count=50,
                ),
                idempotency_key="controlled-trio-legacy-chunking",
                settings=Settings(),
                actor="fixture-owner",
            )
        async with database.sessions() as session:
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
        assert version.specification["schema_version"] == 1
        assert version.specification["planned_job_count"] == 2
        assert version.specification["generation"]["outputs_per_job"] == 25
        assert "generation_batches" not in version.specification
        assert sorted(job.expected_output_count for job in jobs) == [25, 25]
        assert sum(job.expected_output_count for job in jobs) == 50
    finally:
        await database.dispose()


def _form_request(values: dict[str, str]) -> Request:
    body = urlencode(values).encode()
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/dashboard/new-set",
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(body)).encode()),
            ],
        },
        receive,
    )


@pytest.mark.asyncio
async def test_browser_form_parses_controlled_trio_fields() -> None:
    values = {
        "csrf_token": "csrf-token",
        "submission_id": "00000000-0000-0000-0000-000000000001",
        "idempotency_key": "web-new-set-" + ("a" * 64),
        "slug": "browser-controlled-trio",
        "title": "Browser controlled trio",
        "subject_id": "00000000-0000-0000-0000-000000000001",
        "subject_2_id": "00000000-0000-0000-0000-000000000002",
        "subject_3_id": "00000000-0000-0000-0000-000000000003",
        "composition_mode": "trio",
        "duo_contract_version": "3",
        "composition_preset_id": "trio_depth",
        "character_a_prompt": "adult woman, copper bob",
        "character_b_prompt": "adult woman, indigo braid",
        "character_c_prompt": "adult woman, silver curls",
        "character_a_pose_prompt": "standing, hand raised",
        "character_b_pose_prompt": "seated between A and C",
        "character_c_pose_prompt": "kneeling, looking toward B",
        "character_a_negative_prompt": "indigo hair",
        "character_b_negative_prompt": "copper hair",
        "character_c_negative_prompt": "green jacket",
        "interaction_prompt": "coordinated three-character pose",
        "camera_prompt": "overhead wide shot",
        "duo_isolation_mode": "balanced",
        "duo_quality_mode": "standard",
        "checkpoint_id": "00000000-0000-0000-0000-000000000004",
        "workflow_id": "00000000-0000-0000-0000-000000000005",
        "prompt": "shared scene",
        "negative_prompt": "fourth person",
        "detailer_prompt": "expressive faces",
        "detailer_negative_prompt": "closed eyes",
        "batch_plan": json.dumps(
            [
                {
                    "name": "Pose batch",
                    "image_count": 1,
                    "prompt": "shared scene",
                    "negative_prompt": None,
                    "character_a_prompt": None,
                    "character_b_prompt": None,
                    "character_c_prompt": "adult woman, silver curls, red dress",
                    "character_a_pose_prompt": None,
                    "character_b_pose_prompt": None,
                    "character_c_pose_prompt": "__poses/trio_c__",
                    "character_a_negative_prompt": None,
                    "character_b_negative_prompt": None,
                    "character_c_negative_prompt": "",
                    "interaction_prompt": "__poses/trio__",
                    "camera_prompt": "",
                    "detailer_prompt": None,
                    "detailer_negative_prompt": None,
                    "seed": None,
                }
            ]
        ),
        "seed": "1",
        "width": "1024",
        "height": "1024",
        "cfg": "6",
        "steps": "30",
        "sampler": "euler",
        "scheduler": "normal",
        "clip_skip": "2",
        "outputs_per_job": "1",
        "hires_scale": "1.5",
        "hires_denoise": "0.35",
        "hires_upscale_method": "bislerp",
        "detailer_guide_size": "768",
        "detailer_max_size": "1024",
        "detailer_denoise": "0.35",
        "detailer_bbox_threshold": "0.5",
        "detailer_bbox_dilation": "10",
        "detailer_bbox_crop_factor": "3",
        "detailer_feather": "4",
        "planned_job_count": "1",
        "desired_accepted_count": "1",
        **{key: "" for slot in range(1, 9) for key in (f"lora_{slot}_id", f"lora_{slot}_weight")},
    }
    parsed = await read_new_set_form(_form_request(values))
    assert parsed.command.composition_mode == "trio"
    assert parsed.command.duo_contract_version == 3
    assert parsed.command.tertiary_subject_approval_id is not None
    assert parsed.command.composition_preset_id == TrioCompositionPreset.DEPTH
    assert parsed.command.character_c_prompt == "adult woman, silver curls"
    assert parsed.command.character_c_pose_prompt == "kneeling, looking toward B"
    assert parsed.command.batches[0].character_c_prompt.endswith("red dress")
    assert parsed.command.batches[0].character_c_pose_prompt == "__poses/trio_c__"
    assert parsed.command.batches[0].character_c_negative_prompt == ""
    assert parsed.command.batches[0].interaction_prompt == "__poses/trio__"
