import copy
import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from gen_automation.db.models import (
    AdminUser,
    Asset,
    GenerationJob,
    Project,
    Release,
    ReleaseVersion,
    WorkflowApproval,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    ApprovalStatus,
    AssetKind,
    AssetState,
    GenerationState,
)
from gen_automation.domain.release_spec import GenerationParameters
from gen_automation.services.generation_details import _GenerationJobParametersV2

PROJECT_ID = UUID("00000000-0000-4000-8000-000000000701")
RELEASE_ID = UUID("00000000-0000-4000-8000-000000000702")
VERSION_ID = UUID("00000000-0000-4000-8000-000000000703")
JOB_ID = UUID("00000000-0000-4000-8000-000000000704")
ASSET_ID = UUID("00000000-0000-4000-8000-000000000705")
WILDCARD_LIBRARY_ID = UUID("00000000-0000-4000-8000-000000000706")
WILDCARD_VERSION_ID = UUID("00000000-0000-4000-8000-000000000707")
ADMIN_USER_ID = UUID("00000000-0000-4000-8000-000000000708")
WORKFLOW_APPROVAL_ID = UUID("00000000-0000-4000-8000-000000000709")
RELEASE_SPECIFICATION_SHA256 = "a" * 64
APPROVAL_SNAPSHOT_SHA256 = "b" * 64
ASSET_SHA256 = "c" * 64
MAX_SEED = (2**63) - 1


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _prompt_resolution(*, seed: int, resolved_prompt: str, entry_index: int) -> dict[str, Any]:
    source_prompt = "portrait, __poses__"
    negative_prompt = "bad anatomy"
    empty = _sha256("")
    entries_sha256 = "d" * 64
    return {
        "schema_version": 1,
        "seed": seed,
        "source_prompt": source_prompt,
        "source_negative_prompt": negative_prompt,
        "source_detailer_prompt": "",
        "source_detailer_negative_prompt": "",
        "source_prompt_sha256": _sha256(source_prompt),
        "source_negative_prompt_sha256": _sha256(negative_prompt),
        "source_detailer_prompt_sha256": empty,
        "source_detailer_negative_prompt_sha256": empty,
        "resolved_prompt_sha256": _sha256(resolved_prompt),
        "resolved_negative_prompt_sha256": _sha256(negative_prompt),
        "resolved_detailer_prompt_sha256": empty,
        "resolved_detailer_negative_prompt_sha256": empty,
        "wildcard_versions": [
            {
                "name": "poses",
                "library_id": str(WILDCARD_LIBRARY_ID),
                "version_id": str(WILDCARD_VERSION_ID),
                "version_no": 3,
                "entries_sha256": entries_sha256,
                "entry_count": 2,
            }
        ],
        "selections": [
            {
                "field": "prompt",
                "occurrence": 1,
                "depth": 1,
                "name": "poses",
                "version_id": str(WILDCARD_VERSION_ID),
                "version_no": 3,
                "entries_sha256": entries_sha256,
                "entry_index": entry_index,
                "entry_sha256": _sha256("standing" if entry_index == 0 else "sitting"),
            }
        ],
    }


def _job_parameters() -> dict[str, Any]:
    first = GenerationParameters(
        prompt="portrait, standing",
        negative_prompt="bad anatomy",
        detailer_prompt="",
        detailer_negative_prompt="",
        seed=MAX_SEED - 1,
        width=1024,
        height=1536,
        steps=30,
        cfg=6.0,
        sampler="euler_ancestral",
        scheduler="karras",
        clip_skip=2,
        outputs_per_job=1,
        hires_scale=1.5,
        hires_denoise=0.4,
        hires_upscale_method="bislerp",
        detailer_guide_size=768,
        detailer_max_size=1024,
        detailer_denoise=0.4,
        detailer_bbox_threshold=0.3,
        detailer_bbox_dilation=4,
        detailer_bbox_crop_factor=3.0,
        detailer_feather=4,
    )
    second = first.model_copy(update={"prompt": "portrait, sitting", "seed": MAX_SEED})
    first_json = first.model_dump(mode="json")
    second_json = second.model_dump(mode="json")
    return {
        "schema_version": 2,
        "release_version_id": str(VERSION_ID),
        "release_specification_sha256": RELEASE_SPECIFICATION_SHA256,
        "approval_snapshot_sha256": APPROVAL_SNAPSHOT_SHA256,
        "ordinal": 7,
        "subjects": [
            {
                "name": "Example adult character",
                "canonical_age": 25,
                "canonical_source_url": "https://private-source.example/character",
            }
        ],
        "checkpoint": {
            "name": "Example checkpoint",
            "source_url": "https://private-source.example/checkpoint",
            "storage_key": "private/checkpoints/example.safetensors",
            "sha256": "1" * 64,
            "license_url": "https://private-source.example/checkpoint-license",
            "commercial_use_approved": True,
            "adult_use_approved": True,
        },
        "loras": [
            {
                "name": "First style",
                "source_url": "https://private-source.example/lora-one",
                "storage_key": "private/loras/one.safetensors",
                "sha256": "2" * 64,
                "license_url": "https://private-source.example/lora-one-license",
                "commercial_use_approved": True,
                "adult_use_approved": True,
                "weight": 0.5,
            },
            {
                "name": "Second style",
                "source_url": "https://private-source.example/lora-two",
                "storage_key": "private/loras/two.safetensors",
                "sha256": "3" * 64,
                "license_url": "https://private-source.example/lora-two-license",
                "commercial_use_approved": True,
                "adult_use_approved": True,
                "weight": 0.35,
            },
        ],
        "workflow": {
            "name": "Illustrious hires detailer",
            "version": "v1",
            "object_key": "private/workflows/hires-detailer.json",
            "sha256": "4" * 64,
        },
        "generation": {**first_json, "outputs_per_job": 2},
        "prompt_resolution": _prompt_resolution(
            seed=MAX_SEED - 1,
            resolved_prompt="portrait, standing",
            entry_index=0,
        ),
        "output_generations": [first_json, second_json],
        "output_prompt_resolutions": [
            _prompt_resolution(
                seed=MAX_SEED - 1,
                resolved_prompt="portrait, standing",
                entry_index=0,
            ),
            _prompt_resolution(
                seed=MAX_SEED,
                resolved_prompt="portrait, sitting",
                entry_index=1,
            ),
        ],
        "batch": {
            "index": 2,
            "name": "Second prompt structure",
            "image_offset": 4,
            "image_count": 100,
        },
    }


def _duo_prompt_resolution(
    *,
    seed: int,
    resolved_prompt: str,
    resolved_character_a_prompt: str,
    resolved_character_b_prompt: str,
    entry_index: int,
) -> dict[str, Any]:
    evidence = _prompt_resolution(
        seed=seed,
        resolved_prompt=resolved_prompt,
        entry_index=entry_index,
    )
    source_character_a_prompt = "nico robin, __poses__, left side"
    source_character_b_prompt = "boa hancock, right side"
    evidence.update(
        {
            "schema_version": 2,
            "source_character_a_prompt": source_character_a_prompt,
            "source_character_b_prompt": source_character_b_prompt,
            "source_character_a_prompt_sha256": _sha256(source_character_a_prompt),
            "source_character_b_prompt_sha256": _sha256(source_character_b_prompt),
            "resolved_character_a_prompt_sha256": _sha256(resolved_character_a_prompt),
            "resolved_character_b_prompt_sha256": _sha256(resolved_character_b_prompt),
        }
    )
    evidence["selections"].append(
        {
            "field": "character_a_prompt",
            "occurrence": 1,
            "depth": 1,
            "name": "poses",
            "version_id": str(WILDCARD_VERSION_ID),
            "version_no": 3,
            "entries_sha256": "d" * 64,
            "entry_index": entry_index,
            "entry_sha256": _sha256("standing" if entry_index == 0 else "sitting"),
        }
    )
    return evidence


def _duo_job_parameters() -> dict[str, Any]:
    parameters = _job_parameters()
    legacy_outputs = parameters["output_generations"]
    assert isinstance(legacy_outputs, list)
    first = GenerationParameters.model_validate(legacy_outputs[0]).model_copy(
        update={
            "composition_mode": "duo",
            "character_a_prompt": "nico robin, standing, left side",
            "character_b_prompt": "boa hancock, right side",
        }
    )
    second = GenerationParameters.model_validate(legacy_outputs[1]).model_copy(
        update={
            "composition_mode": "duo",
            "character_a_prompt": "nico robin, sitting, left side",
            "character_b_prompt": "boa hancock, right side",
        }
    )
    outputs = [first.model_dump(mode="json"), second.model_dump(mode="json")]
    resolutions = [
        _duo_prompt_resolution(
            seed=first.seed,
            resolved_prompt=first.prompt,
            resolved_character_a_prompt=first.character_a_prompt,
            resolved_character_b_prompt=first.character_b_prompt,
            entry_index=0,
        ),
        _duo_prompt_resolution(
            seed=second.seed,
            resolved_prompt=second.prompt,
            resolved_character_a_prompt=second.character_a_prompt,
            resolved_character_b_prompt=second.character_b_prompt,
            entry_index=1,
        ),
    ]
    parameters.update(
        {
            "subjects": [
                *parameters["subjects"],
                {
                    "name": "Second example adult character",
                    "canonical_age": 31,
                    "canonical_source_url": "https://private-source.example/character-two",
                },
            ],
            "generation": {**outputs[0], "outputs_per_job": 2},
            "prompt_resolution": resolutions[0],
            "output_generations": outputs,
            "output_prompt_resolutions": resolutions,
        }
    )
    return parameters


def test_generation_details_accept_twenty_five_output_snapshots() -> None:
    parameters = _job_parameters()
    base = GenerationParameters.model_validate(parameters["output_generations"][0])
    outputs: list[dict[str, Any]] = []
    resolutions: list[dict[str, Any]] = []
    for output_index in range(25):
        entry_index = output_index % 2
        prompt = "portrait, standing" if entry_index == 0 else "portrait, sitting"
        generation = base.model_copy(
            update={"prompt": prompt, "seed": 100 + output_index}
        ).model_dump(mode="json")
        outputs.append(generation)
        resolutions.append(
            _prompt_resolution(
                seed=100 + output_index,
                resolved_prompt=prompt,
                entry_index=entry_index,
            )
        )
    parameters.update(
        {
            "generation": {**outputs[0], "outputs_per_job": 25},
            "prompt_resolution": resolutions[0],
            "output_generations": outputs,
            "output_prompt_resolutions": resolutions,
            "batch": {
                "index": 0,
                "name": "One provider job",
                "image_offset": 0,
                "image_count": 25,
            },
        }
    )

    parsed = _GenerationJobParametersV2.model_validate(parameters)

    assert len(parsed.output_generations) == 25
    assert parsed.generation.outputs_per_job == 25
    assert parsed.output_generations[-1].seed == 124


async def _seed_generation_details(client: TestClient) -> None:
    parameters = _job_parameters()
    database = client.app.state.database
    async with database.sessions() as session:
        now = datetime(2026, 8, 1, tzinfo=UTC)
        user = AdminUser(
            id=ADMIN_USER_ID,
            username_normalized="metadata-owner",
            display_name="Metadata owner",
            password_hash="test-only-password-hash",  # noqa: S106
            role=AdminRole.OWNER,
            is_active=True,
            password_changed_at=now,
        )
        workflow_evidence = {"source": "generation-details-test"}
        workflow = WorkflowApproval(
            id=WORKFLOW_APPROVAL_ID,
            workflow_sha256="4" * 64,
            name="Illustrious hires detailer",
            version="v1",
            object_key="private/workflows/hires-detailer.json",
            reviewed_node_classes=["KSampler", "LatentUpscaleBy", "FaceDetailer"],
            evidence=workflow_evidence,
            evidence_sha256=canonical_sha256(workflow_evidence),
            status=ApprovalStatus.APPROVED,
            is_current=True,
            approval_version=1,
            approved_by_user_id=ADMIN_USER_ID,
            approved_at=now,
        )
        project = Project(id=PROJECT_ID, slug="metadata", name="Metadata")
        release = Release(
            id=RELEASE_ID,
            project_id=PROJECT_ID,
            slug="metadata-release",
            title="Metadata release",
            desired_accepted_count=1,
        )
        version = ReleaseVersion(
            id=VERSION_ID,
            release_id=RELEASE_ID,
            version_no=1,
            specification={"schema_version": 2},
            specification_sha256=RELEASE_SPECIFICATION_SHA256,
            created_by="test",
            created_at=now,
        )
        job = GenerationJob(
            id=JOB_ID,
            release_version_id=VERSION_ID,
            logical_key="5" * 64,
            parameters=parameters,
            parameters_sha256=canonical_sha256(parameters),
            provider="salad",
            state=GenerationState.SUCCEEDED,
            expected_output_count=2,
        )
        asset = Asset(
            id=ASSET_ID,
            release_id=RELEASE_ID,
            generation_job_id=JOB_ID,
            output_index=1,
            kind=AssetKind.RAW_MASTER,
            state=AssetState.AVAILABLE,
            storage_backend="s3",
            storage_bucket="private-bucket",
            object_key="private/masters/exact.png",
            object_version_id="private-version",
            sha256=ASSET_SHA256,
            content_type="image/png",
            image_format="PNG",
            width=1536,
            height=2304,
            byte_size=2_000_000,
            asset_metadata={"upload_attempt_id": "private-upload-attempt"},
        )
        session.add_all([user, workflow, project, release, version, job, asset])
        await session.commit()


async def _replace_job_parameters(
    client: TestClient,
    parameters: dict[str, Any],
    digest: str | None = None,
) -> None:
    database = client.app.state.database
    async with database.sessions() as session:
        job = await session.get(GenerationJob, JOB_ID)
        assert job is not None
        job.parameters = parameters
        job.parameters_sha256 = digest or canonical_sha256(parameters)
        await session.commit()


async def _replace_workflow_node_classes(
    client: TestClient,
    node_classes: list[str],
) -> None:
    database = client.app.state.database
    async with database.sessions() as session:
        workflow = await session.get(WorkflowApproval, WORKFLOW_APPROVAL_ID)
        assert workflow is not None
        workflow.reviewed_node_classes = node_classes
        await session.commit()


def test_generation_details_selects_exact_output_and_returns_only_safe_fields(
    client: TestClient,
) -> None:
    client.portal.call(_seed_generation_details, client)

    response = client.get(f"/dashboard/assets/{ASSET_ID}/generation-details")

    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    payload = response.json()
    assert set(payload) == {
        "available",
        "asset",
        "job",
        "provider",
        "output",
        "batch",
        "subjects",
        "composition",
        "prompts",
        "sampling",
        "hires",
        "detailer",
        "checkpoint",
        "loras",
        "workflow",
        "wildcards",
        "integrity",
    }
    assert payload["available"] is True
    assert payload["output"] == {"index": 1}
    assert payload["sampling"]["seed"] == str(MAX_SEED)
    assert payload["hires"]["enabled"] is True
    assert payload["detailer"]["enabled"] is True
    assert payload["composition"] == {"mode": "single"}
    assert payload["prompts"]["positive"]["source"] == "portrait, __poses__"
    assert payload["prompts"]["positive"]["resolved"] == "portrait, sitting"
    assert payload["prompts"]["character_a"]["source"] == ""
    assert payload["prompts"]["character_a"]["resolved"] == ""
    assert payload["prompts"]["character_b"]["source"] == ""
    assert payload["prompts"]["character_b"]["resolved"] == ""
    assert payload["prompts"]["detailer_positive"] == {
        **payload["prompts"]["positive"],
        "inherited": True,
    }
    assert payload["prompts"]["detailer_negative"] == {
        **payload["prompts"]["negative"],
        "inherited": True,
    }
    assert [(item["name"], item["weight"]) for item in payload["loras"]] == [
        ("First style", 0.5),
        ("Second style", 0.35),
    ]
    assert payload["wildcards"]["selections"][0]["entry_index"] == 1
    assert payload["batch"]["name"] == "Second prompt structure"
    assert payload["integrity"]["job_parameters_sha256"] == canonical_sha256(_job_parameters())

    serialized = response.text
    for forbidden in (
        "private-source.example",
        "private/checkpoints",
        "private/loras",
        "private/workflows",
        "private/masters",
        "private-bucket",
        "private-version",
        "private-upload-attempt",
        "source_url",
        "storage_key",
        "license_url",
        "object_key",
        "asset_metadata",
    ):
        assert forbidden not in serialized


def test_generation_details_exposes_source_and_resolved_duo_character_prompts(
    client: TestClient,
) -> None:
    client.portal.call(_seed_generation_details, client)
    parameters = _duo_job_parameters()
    client.portal.call(_replace_job_parameters, client, parameters)

    response = client.get(f"/dashboard/assets/{ASSET_ID}/generation-details")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["composition"] == {"mode": "duo"}
    assert [subject["name"] for subject in payload["subjects"]] == [
        "Example adult character",
        "Second example adult character",
    ]
    assert payload["prompts"]["character_a"] == {
        "source": "nico robin, __poses__, left side",
        "resolved": "nico robin, sitting, left side",
        "source_sha256": _sha256("nico robin, __poses__, left side"),
        "resolved_sha256": _sha256("nico robin, sitting, left side"),
        "inherited": False,
    }
    assert payload["prompts"]["character_b"] == {
        "source": "boa hancock, right side",
        "resolved": "boa hancock, right side",
        "source_sha256": _sha256("boa hancock, right side"),
        "resolved_sha256": _sha256("boa hancock, right side"),
        "inherited": False,
    }
    assert {item["field"] for item in payload["wildcards"]["selections"]} == {
        "prompt",
        "character_a_prompt",
    }

    tampered = copy.deepcopy(parameters)
    tampered["output_prompt_resolutions"][1]["resolved_character_a_prompt_sha256"] = "f" * 64
    client.portal.call(_replace_job_parameters, client, tampered)
    assert client.get(f"/dashboard/assets/{ASSET_ID}/generation-details").json() == {
        "available": False,
        "message": "Generation details are unavailable for this image.",
    }


def test_generation_details_reports_base_detailer_without_upscaler(
    client: TestClient,
) -> None:
    client.portal.call(_seed_generation_details, client)
    client.portal.call(_replace_workflow_node_classes, client, ["KSampler", "FaceDetailer"])

    response = client.get(f"/dashboard/assets/{ASSET_ID}/generation-details")

    assert response.status_code == 200
    assert response.json()["hires"]["enabled"] is False
    assert response.json()["detailer"]["enabled"] is True


def test_generation_details_fail_soft_for_legacy_malformed_and_tampered_data(
    client: TestClient,
) -> None:
    client.portal.call(_seed_generation_details, client)
    unavailable = {
        "available": False,
        "message": "Generation details are unavailable for this image.",
    }

    legacy = {"schema_version": 1, "private": "must-not-leak"}
    client.portal.call(_replace_job_parameters, client, legacy)
    legacy_response = client.get(f"/dashboard/assets/{ASSET_ID}/generation-details")
    assert legacy_response.status_code == 200
    assert legacy_response.json() == unavailable
    assert "must-not-leak" not in legacy_response.text

    malformed = copy.deepcopy(_job_parameters())
    malformed["output_generations"] = []
    client.portal.call(_replace_job_parameters, client, malformed)
    malformed_response = client.get(f"/dashboard/assets/{ASSET_ID}/generation-details")
    assert malformed_response.status_code == 200
    assert malformed_response.json() == unavailable

    valid = _job_parameters()
    client.portal.call(_replace_job_parameters, client, valid, "f" * 64)
    tampered_response = client.get(f"/dashboard/assets/{ASSET_ID}/generation-details")
    assert tampered_response.status_code == 200
    assert tampered_response.json() == unavailable
    assert "no-store" in tampered_response.headers["cache-control"]


def test_generation_details_fail_soft_when_output_exceeds_batch_bounds(
    client: TestClient,
) -> None:
    client.portal.call(_seed_generation_details, client)
    malformed = _job_parameters()
    malformed["batch"] = {
        "index": 2,
        "name": "Second prompt structure",
        "image_offset": 99,
        "image_count": 100,
    }
    client.portal.call(_replace_job_parameters, client, malformed)

    response = client.get(f"/dashboard/assets/{ASSET_ID}/generation-details")

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "message": "Generation details are unavailable for this image.",
    }


def test_generation_details_missing_asset_is_generic_and_not_cached(client: TestClient) -> None:
    response = client.get(
        "/dashboard/assets/00000000-0000-4000-8000-000000000799/generation-details"
    )

    assert response.status_code == 404
    assert response.json() == {
        "available": False,
        "message": "Generation details are unavailable for this image.",
    }
    assert "no-store" in response.headers["cache-control"]
