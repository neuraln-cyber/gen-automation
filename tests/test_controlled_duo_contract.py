import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlencode

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from starlette.requests import Request

from gen_automation.api.browser_experiment_forms import _decode_variant_plan
from gen_automation.api.browser_new_set_forms import read_new_set_form
from gen_automation.config import Settings
from gen_automation.db.models import GenerationJob, ReleaseVersion
from gen_automation.db.session import Database
from gen_automation.domain.controlled_duo import (
    LEGACY_REGIONAL_PROMPT_NODE_CLASSES,
    WorkflowCapability,
    effective_workflow_capabilities,
)
from gen_automation.domain.release_spec import GenerationParameters, ReleaseCreate
from gen_automation.services.generation_details import (
    _prompt_payload,
    _PromptResolutionV3,
    _resolution_matches_generation,
)
from gen_automation.services.new_sets import (
    NewSetSubmission,
    create_and_approve_new_set,
    list_new_set_options,
)
from gen_automation.services.wildcards import FrozenWildcardCatalog, resolve_wildcard_prompts
from tests.factories import seed_release_approvals, valid_release_payload


def _generation(**updates: object) -> GenerationParameters:
    values: dict[str, object] = {
        "prompt": "shared scene and lighting only",
        "seed": 1,
        "width": 1024,
        "height": 1024,
        "steps": 30,
        "sampler": "euler",
        "scheduler": "normal",
        "composition_mode": "duo",
        "character_a_prompt": "adult woman, copper bob, green jacket",
        "character_b_prompt": "adult woman, indigo braid, ivory coat",
    }
    values.update(updates)
    return GenerationParameters.model_validate(values)


def test_controlled_duo_v2_is_explicit_and_v1_remains_compatible() -> None:
    legacy = _generation()
    assert legacy.duo_contract_version == 1
    assert legacy.composition_preset_id is None

    with pytest.raises(ValidationError, match="requires a composition preset"):
        _generation(duo_contract_version=2)

    controlled = _generation(
        duo_contract_version=2,
        composition_preset_id="diagonal_depth",
        character_a_negative_prompt="indigo hair, ivory coat",
        character_b_negative_prompt="copper hair, green jacket",
        interaction_prompt="running past each other",
        camera_prompt="low camera, diagonal depth",
        duo_isolation_mode="strict",
        duo_quality_mode="standard",
    )
    assert controlled.composition_preset_id is not None
    assert controlled.composition_preset_id.value == "diagonal_depth"

    with pytest.raises(ValidationError, match="require duo contract version 2"):
        _generation(character_a_negative_prompt="indigo hair")


def test_only_the_legacy_regional_capability_is_inferred_from_nodes() -> None:
    capabilities = effective_workflow_capabilities(
        (),
        reviewed_node_classes=LEGACY_REGIONAL_PROMPT_NODE_CLASSES,
    )
    assert capabilities == frozenset({WorkflowCapability.REGIONAL_PROMPTING_V1})
    assert WorkflowCapability.CONTROLLED_DUO_V2 not in capabilities


def _controlled_release_payload() -> dict[str, object]:
    payload = deepcopy(valid_release_payload())
    specification = payload["specification"]
    assert isinstance(specification, dict)
    subjects = specification["subjects"]
    workflow = specification["workflow"]
    generation = specification["generation"]
    assert isinstance(subjects, list)
    assert isinstance(workflow, dict)
    assert isinstance(generation, dict)
    second = deepcopy(subjects[0])
    second.update(
        {
            "name": "Second Approved Adult Character",
            "canonical_source_url": "https://example.com/second-adult-character",
        }
    )
    subjects.append(second)
    workflow["capabilities"] = ["controlled_duo_v2", "duo_strict_isolation"]
    generation.update(
        {
            "composition_mode": "duo",
            "duo_contract_version": 2,
            "composition_preset_id": "low_angle",
            "character_a_prompt": "adult woman, copper bob, green jacket",
            "character_b_prompt": "adult woman, indigo braid, ivory coat",
            "character_a_negative_prompt": "indigo hair, ivory coat",
            "character_b_negative_prompt": "copper hair, green jacket",
            "interaction_prompt": "standing shoulder to shoulder",
            "camera_prompt": "low camera, full-body framing",
            "duo_isolation_mode": "strict",
            "duo_quality_mode": "standard",
        }
    )
    return payload


def test_release_requires_explicit_capabilities_for_requested_v2_modes() -> None:
    payload = _controlled_release_payload()
    parsed = ReleaseCreate.model_validate(payload)
    assert parsed.specification.generation.duo_contract_version == 2

    workflow = payload["specification"]["workflow"]  # type: ignore[index]
    assert isinstance(workflow, dict)
    workflow["capabilities"] = ["controlled_duo_v2"]
    with pytest.raises(ValidationError, match="strict duo isolation"):
        ReleaseCreate.model_validate(payload)

    workflow["capabilities"] = ["controlled_duo_v2", "duo_strict_isolation"]
    generation = payload["specification"]["generation"]  # type: ignore[index]
    assert isinstance(generation, dict)
    generation["duo_isolation_mode"] = "balanced"
    generation["duo_quality_mode"] = "standard"
    with pytest.raises(ValidationError, match="strict-isolation workflow requires strict"):
        ReleaseCreate.model_validate(payload)

    workflow["capabilities"] = [
        "controlled_duo_v2",
        "duo_strict_isolation",
        "duo_high_quality",
    ]
    generation["duo_isolation_mode"] = "strict"
    generation["duo_quality_mode"] = "high"
    with pytest.raises(ValidationError, match="high duo quality is not implemented"):
        ReleaseCreate.model_validate(payload)


def test_every_controlled_duo_batch_is_capability_checked_before_release() -> None:
    payload = _controlled_release_payload()
    payload["desired_accepted_count"] = 1
    specification = payload["specification"]
    assert isinstance(specification, dict)
    workflow = specification["workflow"]
    generation = specification["generation"]
    assert isinstance(workflow, dict)
    assert isinstance(generation, dict)
    workflow["capabilities"] = ["controlled_duo_v2"]
    generation["duo_isolation_mode"] = "balanced"
    generation["duo_quality_mode"] = "standard"
    strict_batch_generation = deepcopy(generation)
    strict_batch_generation["duo_isolation_mode"] = "strict"
    specification.update(
        {
            "schema_version": 2,
            "planned_job_count": 2,
            "generation_batches": [
                {
                    "name": "Balanced first",
                    "image_count": 1,
                    "generation": deepcopy(generation),
                },
                {
                    "name": "Strict later",
                    "image_count": 1,
                    "generation": strict_batch_generation,
                },
            ],
        }
    )

    with pytest.raises(ValidationError, match="strict duo isolation"):
        ReleaseCreate.model_validate(payload)


def test_controlled_duo_prompt_evidence_is_versioned_and_complete() -> None:
    specification = ReleaseCreate.model_validate(_controlled_release_payload()).specification
    generation = specification.generation
    resolved = resolve_wildcard_prompts(
        specification,
        FrozenWildcardCatalog(by_name={}),
        seed=generation.seed,
    )
    assert resolved.evidence["schema_version"] == 3
    evidence = _PromptResolutionV3.model_validate(resolved.evidence)
    assert _resolution_matches_generation(evidence, generation)
    prompts = _prompt_payload(generation, evidence)
    assert prompts["character_a_negative"]["resolved"] == "indigo hair, ivory coat"  # type: ignore[index]
    assert prompts["interaction"]["resolved"] == "standing shoulder to shoulder"  # type: ignore[index]


@pytest.mark.asyncio
async def test_new_set_service_freezes_the_controlled_duo_contract(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'controlled-duo.db').as_posix()}")
    await database.create_schema()
    payload = _controlled_release_payload()
    try:
        async with database.sessions() as session:
            await seed_release_approvals(session, payload)
            options = await list_new_set_options(session)
            assert options.workflows[0].supports_controlled_duo_v2 is True
            assert options.workflows[0].supports_duo_strict_isolation is True
            assert options.workflows[0].supports_duo_high_quality is False
            result = await create_and_approve_new_set(
                session,
                command=NewSetSubmission(
                    slug="controlled-duo",
                    title="Controlled duo",
                    subject_approval_id=options.subjects[0].approval_id,
                    secondary_subject_approval_id=options.subjects[1].approval_id,
                    composition_mode="duo",
                    duo_contract_version=2,
                    composition_preset_id="low_angle",
                    character_a_prompt="adult woman, copper bob, green jacket",
                    character_b_prompt="adult woman, indigo braid, ivory coat",
                    character_a_negative_prompt="indigo hair, ivory coat",
                    character_b_negative_prompt="copper hair, green jacket",
                    interaction_prompt="standing shoulder to shoulder",
                    camera_prompt="low camera, full-body framing",
                    duo_isolation_mode="strict",
                    duo_quality_mode="standard",
                    checkpoint_approval_id=options.checkpoints[0].approval_id,
                    workflow_approval_id=options.workflows[0].approval_id,
                    prompt="shared dramatic scene",
                    seed=9,
                    width=1024,
                    height=1024,
                    cfg=6,
                    steps=30,
                    sampler="euler",
                    scheduler="normal",
                    outputs_per_job=1,
                    planned_job_count=1,
                    desired_accepted_count=1,
                ),
                idempotency_key="controlled-duo-contract",
                settings=Settings(),
                actor="fixture-owner",
            )
        async with database.sessions() as session:
            version = await session.scalar(
                select(ReleaseVersion).where(ReleaseVersion.release_id == result.release.id)
            )
            job = await session.scalar(
                select(GenerationJob).where(
                    GenerationJob.release_version_id == result.generation_plan.release_version_id
                )
            )
            assert version is not None
            assert job is not None
            generation = version.specification["generation"]
            assert generation["duo_contract_version"] == 2
            assert generation["composition_preset_id"] == "low_angle"
            assert generation["character_a_negative_prompt"] == "indigo hair, ivory coat"
            assert generation["camera_prompt"] == "low camera, full-body framing"
            assert version.specification["workflow"]["capabilities"] == [
                "controlled_duo_v2",
                "duo_strict_isolation",
            ]
            assert job.parameters["prompt_resolution"]["schema_version"] == 3
            assert job.parameters["output_generations"][0]["interaction_prompt"] == (
                "standing shoulder to shoulder"
            )
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
async def test_browser_form_parses_controlled_duo_fields() -> None:
    values = {
        "csrf_token": "csrf-token",
        "submission_id": "00000000-0000-0000-0000-000000000001",
        "idempotency_key": "web-new-set-" + ("a" * 64),
        "slug": "browser-controlled-duo",
        "title": "Browser controlled duo",
        "subject_id": "00000000-0000-0000-0000-000000000001",
        "subject_2_id": "00000000-0000-0000-0000-000000000002",
        "composition_mode": "duo",
        "duo_contract_version": "2",
        "composition_preset_id": "overhead",
        "character_a_prompt": "adult woman, copper bob",
        "character_b_prompt": "adult woman, indigo braid",
        "character_a_negative_prompt": "indigo hair",
        "character_b_negative_prompt": "copper hair",
        "interaction_prompt": "looking up together",
        "camera_prompt": "overhead wide lens",
        "duo_isolation_mode": "balanced",
        "duo_quality_mode": "standard",
        "checkpoint_id": "00000000-0000-0000-0000-000000000003",
        "workflow_id": "00000000-0000-0000-0000-000000000004",
        "prompt": "shared scene",
        "negative_prompt": "third person",
        "detailer_prompt": "expressive faces",
        "detailer_negative_prompt": "closed eyes",
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
    assert parsed.command.duo_contract_version == 2
    assert parsed.command.composition_preset_id is not None
    assert parsed.command.composition_preset_id.value == "overhead"
    assert parsed.command.character_b_negative_prompt == "copper hair"
    assert parsed.command.camera_prompt == "overhead wide lens"


def test_experiment_variant_parser_accepts_legacy_and_controlled_duo_profiles() -> None:
    base: dict[str, object] = {
        "label": "Baseline",
        "subject_id": "00000000-0000-0000-0000-000000000001",
        "subject_2_id": "",
        "composition_mode": "single",
        "character_a_prompt": "",
        "character_b_prompt": "",
        "checkpoint_id": "00000000-0000-0000-0000-000000000003",
        "workflow_id": "00000000-0000-0000-0000-000000000004",
        "prompt": "portrait",
        "negative_prompt": "low quality",
        "detailer_prompt": "expressive face",
        "detailer_negative_prompt": "closed eyes",
        "seed": 1,
        "width": 1024,
        "height": 1024,
        "cfg": 6,
        "steps": 30,
        "sampler": "euler",
        "scheduler": "normal",
        "clip_skip": 2,
        "hires_scale": 1.5,
        "hires_denoise": 0.35,
        "hires_upscale_method": "bislerp",
        "detailer_guide_size": 768,
        "detailer_max_size": 1024,
        "detailer_denoise": 0.35,
        "detailer_bbox_threshold": 0.5,
        "detailer_bbox_dilation": 10,
        "detailer_bbox_crop_factor": 3,
        "detailer_feather": 4,
        "loras": [],
    }
    legacy = _decode_variant_plan(
        json.dumps([base, {**base, "label": "Legacy copy"}]),
        group_slug="experiment-0123456789ab",
        title="Compatibility",
        outputs_per_variant=1,
    )
    assert all(item.profile.duo_contract_version == 1 for item in legacy)

    controlled = {
        **base,
        "label": "Controlled",
        "subject_2_id": "00000000-0000-0000-0000-000000000002",
        "composition_mode": "duo",
        "duo_contract_version": 2,
        "composition_preset_id": "back_to_back",
        "character_a_prompt": "adult woman, copper bob",
        "character_b_prompt": "adult woman, indigo braid",
        "character_a_negative_prompt": "indigo hair",
        "character_b_negative_prompt": "copper hair",
        "interaction_prompt": "back to back",
        "camera_prompt": "waist-height diagonal camera",
        "duo_isolation_mode": "strict",
        "duo_quality_mode": "standard",
    }
    variants = _decode_variant_plan(
        json.dumps([controlled, {**controlled, "label": "Controlled copy"}]),
        group_slug="experiment-0123456789ab",
        title="Controlled comparison",
        outputs_per_variant=1,
    )
    assert variants[0].profile.duo_contract_version == 2
    assert variants[0].profile.composition_preset_id is not None
    assert variants[0].profile.composition_preset_id.value == "back_to_back"
