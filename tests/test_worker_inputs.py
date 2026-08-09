import base64
import hashlib
import io
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import func, select

from gen_automation.db.models import Asset, GenerationJob, Project, Release, ReleaseVersion
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.controlled_duo import DuoCompositionPreset
from gen_automation.domain.deliverability import require_comfy_workflow_deliverability
from gen_automation.domain.enums import AssetState
from gen_automation.domain.release_spec import GenerationParameters
from gen_automation.domain.signing import derive_public_key, encode_base64url
from gen_automation.gpu_worker.app import create_worker_app
from gen_automation.gpu_worker.artifacts import (
    ArtifactKind,
    ArtifactManifest,
    ModelArtifactSpec,
    calculate_manifest_sha256,
)
from gen_automation.gpu_worker.models import (
    DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES,
    GenerateEnvelope,
    UploadGrant,
    WorkerEnvironment,
    WorkerSettings,
    validate_approved_workflow,
)
from gen_automation.gpu_worker.security import verify_authorization
from gen_automation.services.assets import finalize_raw_master
from gen_automation.services.salad import SaladJobInputContext
from gen_automation.services.worker_inputs import (
    CONTROLLED_DUO_MARKER_NODE_CLASS,
    SaladWorkerJobInputProvider,
    WorkerInputError,
    _controlled_duo_bindings,
)
from gen_automation.storage.base import PresignedUpload
from gen_automation.storage.memory import MemoryObjectStore

NOW = 2_000_000_000
SIGNING_PRIVATE_KEY = encode_base64url(bytes(range(1, 33)))
VERIFICATION_PUBLIC_KEY = derive_public_key(SIGNING_PRIVATE_KEY)
WORKFLOW_BODY = (
    Path(__file__).resolve().parents[1] / "workflows" / "illustrious-sdxl-base-v1.json"
).read_bytes()
HIRES_WORKFLOW_BODY = (
    Path(__file__).resolve().parents[1] / "workflows" / "illustrious-sdxl-hires-v1.json"
).read_bytes()
DETAILER_WORKFLOW_BODY = (
    Path(__file__).resolve().parents[1] / "workflows" / "illustrious-sdxl-hires-detailer-v1.json"
).read_bytes()
BASE_DETAILER_WORKFLOW_BODY = (
    Path(__file__).resolve().parents[1] / "workflows" / "illustrious-sdxl-base-detailer-v1.json"
).read_bytes()
COUPLE_BASE_WORKFLOW_BODY = (
    Path(__file__).resolve().parents[1] / "workflows" / "illustrious-sdxl-couple-base-v1.json"
).read_bytes()
COUPLE_BASE_DETAILER_WORKFLOW_BODY = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "illustrious-sdxl-couple-base-detailer-v1.json"
).read_bytes()
COUPLE_HIRES_WORKFLOW_BODY = (
    Path(__file__).resolve().parents[1] / "workflows" / "illustrious-sdxl-couple-hires-v1.json"
).read_bytes()
COUPLE_HIRES_DETAILER_WORKFLOW_BODY = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "illustrious-sdxl-couple-hires-detailer-v1.json"
).read_bytes()
CONTROLLED_DUO_BALANCED_WORKFLOW_BODY = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "illustrious-sdxl-controlled-duo-balanced-v2.json"
).read_bytes()
CONTROLLED_DUO_STRICT_WORKFLOW_BODY = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "illustrious-sdxl-controlled-duo-strict-v2.json"
).read_bytes()
COUPLE_WORKFLOW_SHA256S = {
    "base": "539bfdf81d9668b6e0c60c77034ac5ff3c4d233e468c1f3dd1fe398415892923",
    "base-detailer": "62f8db236c95a5c88f130c1fc10fa34dbc7455dba45695081aa5bc19d2bde890",
    "hires": "b674f45c8b62f6742b74c1364f4c6b172ef2ed3446f4e29034e1c1da3727773c",
    "hires-detailer": "9f2d863469bcc6ab069244c72797e19aa8a95aa9f253170fdd5066a754fe586e",
}
UPLOAD_ORIGIN = "https://uploads.example.test"


class _HttpsMemoryObjectStore(MemoryObjectStore):
    async def presign_upload(
        self,
        *,
        key: str,
        content_type: str,
        metadata: dict[str, str],
        expires_in: int,
        max_bytes: int,
    ) -> PresignedUpload:
        grant = await super().presign_upload(
            key=key,
            content_type=content_type,
            metadata=metadata,
            expires_in=expires_in,
            max_bytes=max_bytes,
        )
        return PresignedUpload(
            url=f"{UPLOAD_ORIGIN}/upload?expires={expires_in}",
            method=grant.method,
            fields=grant.fields,
            headers=grant.headers,
        )


def _png(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), color=color).save(output, format="PNG")
    return output.getvalue()


@dataclass
class _SyntheticComfyExecutor:
    workflows: list[dict[str, object]] = field(default_factory=list)

    def is_ready(self) -> bool:
        return True

    def execute(self, workflow: dict[str, object]) -> object:
        self.workflows.append(workflow)
        return [
            {
                "output_index": output_index,
                "media_type": "image/png",
                "data_base64": base64.b64encode(_png(color)).decode(),
            }
            for output_index, color in enumerate(((20, 40, 60), (80, 100, 120)))
        ]


@dataclass
class _StagingUploader:
    store: MemoryObjectStore

    async def upload(
        self,
        *,
        grant: UploadGrant,
        content: bytes,
        media_type: str,
    ) -> None:
        metadata = {
            name.removeprefix("x-amz-meta-"): value
            for name, value in grant.fields.items()
            if name.startswith("x-amz-meta-")
        }
        self.store.put_for_test(
            grant.fields["key"],
            content,
            content_type=media_type,
            metadata=metadata,
        )


@dataclass(frozen=True)
class WorkerInputContext:
    database: Database
    store: _HttpsMemoryObjectStore
    job_context: SaladJobInputContext
    workflow_key: str
    workflow_body: bytes
    artifact_manifest: ArtifactManifest


def _artifact_manifest() -> ArtifactManifest:
    artifacts = (
        ModelArtifactSpec(
            logical_name="illustrious",
            kind=ArtifactKind.CHECKPOINT,
            source_object_id="models/illustrious.safetensors",
            sha256="a" * 64,
            exact_size_bytes=100,
            max_size_bytes=100,
            target_filename="illustrious-runtime.safetensors",
        ),
        ModelArtifactSpec(
            logical_name="style",
            kind=ArtifactKind.LORA,
            source_object_id="loras/style.safetensors",
            sha256="b" * 64,
            exact_size_bytes=100,
            max_size_bytes=100,
            target_filename="style-runtime.safetensors",
        ),
    )
    return ArtifactManifest(
        version="v1",
        artifacts=artifacts,
        manifest_sha256=calculate_manifest_sha256(artifacts),
    )


@pytest.fixture
async def worker_input_context(tmp_path: Path) -> AsyncIterator[WorkerInputContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'worker-inputs.db').as_posix()}")
    await database.create_schema()
    store = _HttpsMemoryObjectStore()
    workflow_key = "workflows/illustrious-v1.json"
    workflow_body = WORKFLOW_BODY
    store.put_for_test(
        workflow_key,
        workflow_body,
        content_type="application/json",
    )
    now = datetime.now(UTC)
    parameters: dict[str, object] = {
        "schema_version": 1,
        "release_version_id": "",
        "release_specification_sha256": "f" * 64,
        "ordinal": 0,
        "subjects": [],
        "checkpoint": {
            "name": "illustrious.safetensors",
            "source_url": "https://models.example/checkpoint",
            "storage_key": "models/illustrious.safetensors",
            "sha256": "a" * 64,
            "license_url": "https://models.example/license",
            "commercial_use_approved": True,
            "adult_use_approved": True,
        },
        "loras": [
            {
                "name": "style.safetensors",
                "source_url": "https://models.example/lora",
                "storage_key": "loras/style.safetensors",
                "sha256": "b" * 64,
                "license_url": "https://models.example/lora-license",
                "commercial_use_approved": True,
                "adult_use_approved": True,
                "weight": 0.75,
            }
        ],
        "workflow": {
            "name": "illustrious-api",
            "version": "1",
            "object_key": workflow_key,
            "sha256": hashlib.sha256(workflow_body).hexdigest(),
        },
        "generation": {
            "prompt": "private test prompt",
            "negative_prompt": "bad anatomy",
            "detailer_prompt": "expressive face",
            "detailer_negative_prompt": "closed eyes",
            "seed": 42,
            "width": 1024,
            "height": 1024,
            "steps": 28,
            "sampler": "euler",
            "scheduler": "normal",
            "clip_skip": 2,
            "detailer_feather": 4,
            "outputs_per_job": 2,
        },
    }
    async with database.sessions() as session:
        project = Project(slug="worker-inputs", name="Worker Inputs")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="release",
            title="Release",
            desired_accepted_count=1,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={"schema_version": 1},
            specification_sha256="f" * 64,
            created_by="test",
            created_at=now,
        )
        session.add(version)
        await session.flush()
        parameters["release_version_id"] = str(version.id)
        job = GenerationJob(
            release_version_id=version.id,
            logical_key="c" * 64,
            parameters=parameters,
            parameters_sha256=canonical_sha256(parameters),
            provider="salad",
            expected_output_count=2,
        )
        session.add(job)
        await session.commit()
        context = SaladJobInputContext(
            generation_attempt_id=uuid4(),
            generation_job_id=job.id,
            release_version_id=version.id,
            salad_deployment_id=uuid4(),
            deployment_version_no=1,
            worker_image_digest=f"registry.example/worker@sha256:{'d' * 64}",
            expected_output_count=2,
            parameters=parameters,
            parameters_sha256=job.parameters_sha256,
            request_sha256="e" * 64,
        )
    try:
        yield WorkerInputContext(
            database=database,
            store=store,
            job_context=context,
            workflow_key=workflow_key,
            workflow_body=workflow_body,
            artifact_manifest=_artifact_manifest(),
        )
    finally:
        await database.dispose()


async def _build(context: WorkerInputContext) -> dict[str, object]:
    async with context.database.sessions() as session:
        provider = SaladWorkerJobInputProvider(
            session=session,
            store=context.store,
            signing_key_id="worker-key-1",
            signing_private_key=SecretStr(SIGNING_PRIVATE_KEY),
            artifact_manifest=context.artifact_manifest,
            artifact_manifest_sha256=context.artifact_manifest.manifest_sha256,
            now=lambda: NOW,
        )
        return await provider.build_job_input(context.job_context)


def _profile_context(
    context: WorkerInputContext,
    *,
    workflow_body: bytes,
    detector: bool = False,
    duo: bool = False,
) -> WorkerInputContext:
    context.store.put_for_test(
        context.workflow_key,
        workflow_body,
        content_type="application/json",
    )
    parameters = dict(context.job_context.parameters)
    raw_workflow = parameters["workflow"]
    assert isinstance(raw_workflow, dict)
    parameters["workflow"] = {
        **raw_workflow,
        "sha256": hashlib.sha256(workflow_body).hexdigest(),
    }
    if duo:
        raw_generation = parameters["generation"]
        assert isinstance(raw_generation, dict)
        parameters["generation"] = {
            **raw_generation,
            "composition_mode": "duo",
            "character_a_prompt": "nico robin, on the left",
            "character_b_prompt": "boa hancock, on the right",
        }
    job_context = SaladJobInputContext(
        **{
            **context.job_context.__dict__,
            "parameters": parameters,
            "parameters_sha256": canonical_sha256(parameters),
        }
    )
    manifest = context.artifact_manifest
    if detector:
        checkpoint, lora = manifest.artifacts
        face_detector = ModelArtifactSpec(
            logical_name="face-yolov8m",
            kind=ArtifactKind.DETECTOR,
            source_object_id="detectors/face-yolov8m.pt",
            sha256="c" * 64,
            exact_size_bytes=100,
            max_size_bytes=100,
            target_filename="face-yolov8m.pt",
        )
        artifacts = (checkpoint, face_detector, lora)
        manifest = ArtifactManifest(
            version="v1",
            artifacts=artifacts,
            manifest_sha256=calculate_manifest_sha256(artifacts),
        )
    return replace(
        context,
        workflow_body=workflow_body,
        job_context=job_context,
        artifact_manifest=manifest,
    )


def _controlled_duo_context(
    context: WorkerInputContext,
    *,
    workflow_body: bytes,
    isolation_mode: str,
    preset: str = "close_portrait",
    quality_mode: str = "standard",
    capabilities: list[str] | None = None,
) -> WorkerInputContext:
    context.store.put_for_test(
        context.workflow_key,
        workflow_body,
        content_type="application/json",
    )
    parameters = dict(context.job_context.parameters)
    raw_workflow = parameters["workflow"]
    raw_generation = parameters["generation"]
    assert isinstance(raw_workflow, dict)
    assert isinstance(raw_generation, dict)
    if capabilities is None:
        capabilities = ["controlled_duo_v2"]
        if isolation_mode == "strict":
            capabilities.append("duo_strict_isolation")
    parameters["workflow"] = {
        **raw_workflow,
        "sha256": hashlib.sha256(workflow_body).hexdigest(),
        "capabilities": capabilities,
    }
    parameters["generation"] = {
        **raw_generation,
        "composition_mode": "duo",
        "duo_contract_version": 2,
        "composition_preset_id": preset,
        "character_a_prompt": "short copper bob, green eyes, teal aviator jacket",
        "character_b_prompt": "long indigo braid, amber eyes, ivory tailored coat",
        "character_a_negative_prompt": "indigo hair, ivory coat",
        "character_b_negative_prompt": "copper hair, teal jacket",
        "interaction_prompt": "back to back, opposing gazes",
        "camera_prompt": "dynamic low camera, diagonal composition",
        "duo_isolation_mode": isolation_mode,
        "duo_quality_mode": quality_mode,
    }
    job_context = SaladJobInputContext(
        **{
            **context.job_context.__dict__,
            "parameters": parameters,
            "parameters_sha256": canonical_sha256(parameters),
        }
    )
    return replace(
        context,
        workflow_body=workflow_body,
        job_context=job_context,
    )


def test_couple_workflow_template_hashes_are_frozen() -> None:
    assert {
        "base": hashlib.sha256(COUPLE_BASE_WORKFLOW_BODY).hexdigest(),
        "base-detailer": hashlib.sha256(COUPLE_BASE_DETAILER_WORKFLOW_BODY).hexdigest(),
        "hires": hashlib.sha256(COUPLE_HIRES_WORKFLOW_BODY).hexdigest(),
        "hires-detailer": hashlib.sha256(COUPLE_HIRES_DETAILER_WORKFLOW_BODY).hexdigest(),
    } == COUPLE_WORKFLOW_SHA256S


@pytest.mark.asyncio
async def test_builds_signed_envelope_with_rendered_workflow_and_fresh_uploads(
    worker_input_context: WorkerInputContext,
) -> None:
    result = await _build(worker_input_context)
    envelope = GenerateEnvelope.model_validate(result, strict=True)

    settings = WorkerSettings(
        environment=WorkerEnvironment.TEST,
        verification_keys={"worker-key-1": VERIFICATION_PUBLIC_KEY},
        allowed_upload_origin="https://uploads.example.test",
    )
    verify_authorization(envelope, settings, now=lambda: NOW)
    assert len(envelope.signature) == 86
    assert SIGNING_PRIVATE_KEY not in settings.model_dump_json()
    assert envelope.payload.job_id == str(worker_input_context.job_context.generation_job_id)
    assert envelope.payload.attempt_id == str(
        worker_input_context.job_context.generation_attempt_id
    )
    checkpoint_node = envelope.payload.workflow["1"]
    prompt_node = envelope.payload.workflow["6"]
    clip_skip_node = envelope.payload.workflow["3"]
    sampler_node = envelope.payload.workflow["9"]
    lora_node = envelope.payload.workflow["2-lora-1"]
    assert isinstance(checkpoint_node, dict)
    assert isinstance(prompt_node, dict)
    assert isinstance(clip_skip_node, dict)
    assert isinstance(sampler_node, dict)
    assert isinstance(lora_node, dict)
    checkpoint_inputs = checkpoint_node["inputs"]
    prompt_inputs = prompt_node["inputs"]
    sampler_inputs = sampler_node["inputs"]
    lora_inputs = lora_node["inputs"]
    assert isinstance(checkpoint_inputs, dict)
    assert isinstance(prompt_inputs, dict)
    assert isinstance(sampler_inputs, dict)
    assert isinstance(lora_inputs, dict)
    assert checkpoint_inputs["ckpt_name"] == "illustrious-runtime.safetensors"
    assert prompt_inputs["text"] == "private test prompt"
    assert prompt_inputs["clip"] == ["3", 0]
    assert clip_skip_node == {
        "class_type": "CLIPSetLastLayer",
        "inputs": {
            "clip": ["2-lora-1", 1],
            "stop_at_clip_layer": -2,
        },
    }
    assert sampler_inputs["seed"] == 42
    assert sampler_inputs["cfg"] == 5.0
    assert sampler_inputs["model"] == ["2-lora-1", 0]
    assert lora_inputs["lora_name"] == "style-runtime.safetensors"
    assert lora_inputs["strength_model"] == 0.75
    assert "2" not in envelope.payload.workflow
    assert [grant.output_index for grant in envelope.payload.uploads] == [0, 1]
    assert len({grant.upload_attempt_id for grant in envelope.payload.uploads}) == 2
    assert all("expires=10800" in grant.url for grant in envelope.payload.uploads)


@pytest.mark.asyncio
async def test_one_provider_job_renders_independent_prompt_branches(
    worker_input_context: WorkerInputContext,
) -> None:
    context = _profile_context(
        worker_input_context,
        workflow_body=DETAILER_WORKFLOW_BODY,
        detector=True,
    )
    parameters = dict(context.job_context.parameters)
    generation = dict(parameters["generation"])  # type: ignore[arg-type]
    first = {**generation, "outputs_per_job": 1}
    second = {
        **first,
        "prompt": "second independently expanded wildcard prompt",
        "detailer_prompt": "second face prompt",
        "seed": 43,
    }
    parameters.update(
        {
            "schema_version": 2,
            "output_generations": [first, second],
            "output_prompt_resolutions": [{"seed": 42}, {"seed": 43}],
        }
    )
    context = replace(
        context,
        job_context=SaladJobInputContext(
            **{
                **context.job_context.__dict__,
                "parameters": parameters,
                "parameters_sha256": canonical_sha256(parameters),
            }
        ),
    )

    envelope = GenerateEnvelope.model_validate(await _build(context), strict=True)
    workflow = envelope.payload.workflow
    output_nodes = {
        node_id: node
        for node_id, node in workflow.items()
        if isinstance(node, dict) and node.get("class_type") == "SaveImage"
    }
    latent_nodes = [
        node
        for node in workflow.values()
        if isinstance(node, dict) and node.get("class_type") == "EmptyLatentImage"
    ]
    prompt_texts = {
        node["inputs"]["text"]
        for node in workflow.values()
        if isinstance(node, dict)
        and node.get("class_type") == "CLIPTextEncode"
        and isinstance(node.get("inputs"), dict)
    }
    sampler_seeds = {
        node["inputs"]["seed"]
        for node in workflow.values()
        if isinstance(node, dict)
        and node.get("class_type") == "KSampler"
        and isinstance(node.get("inputs"), dict)
    }

    assert len(output_nodes) == 2
    assert all(node["inputs"]["batch_size"] == 1 for node in latent_nodes)
    assert "private test prompt" in prompt_texts
    assert "second independently expanded wildcard prompt" in prompt_texts
    assert {42, 43} <= sampler_seeds
    assert (
        sum(
            isinstance(node, dict) and node.get("class_type") == "FaceDetailer"
            for node in workflow.values()
        )
        == 2
    )
    assert (
        sum(
            isinstance(node, dict) and node.get("class_type") == "UltralyticsDetectorProvider"
            for node in workflow.values()
        )
        == 1
    )


@pytest.mark.asyncio
async def test_one_provider_job_allows_independent_regional_prompts_per_output(
    worker_input_context: WorkerInputContext,
) -> None:
    context = _profile_context(
        worker_input_context,
        workflow_body=COUPLE_BASE_WORKFLOW_BODY,
        duo=True,
    )
    parameters = dict(context.job_context.parameters)
    generation = dict(parameters["generation"])  # type: ignore[arg-type]
    first = {**generation, "outputs_per_job": 1}
    second = {
        **first,
        "character_a_prompt": "nico robin, seated on the left",
        "character_b_prompt": "boa hancock, standing on the right",
        "seed": 43,
    }
    parameters.update(
        {
            "schema_version": 2,
            "generation": {**first, "outputs_per_job": 2},
            "output_generations": [first, second],
            "output_prompt_resolutions": [{"seed": 42}, {"seed": 43}],
        }
    )
    context = replace(
        context,
        job_context=SaladJobInputContext(
            **{
                **context.job_context.__dict__,
                "parameters": parameters,
                "parameters_sha256": canonical_sha256(parameters),
            }
        ),
    )

    workflow = GenerateEnvelope.model_validate(
        await _build(context),
        strict=True,
    ).payload.workflow

    assert workflow["output-00-18"]["inputs"]["text"] == "nico robin, on the left"
    assert workflow["output-00-19"]["inputs"]["text"] == "boa hancock, on the right"
    assert workflow["output-01-18"]["inputs"]["text"] == "nico robin, seated on the left"
    assert workflow["output-01-19"]["inputs"]["text"] == "boa hancock, standing on the right"


@pytest.mark.asyncio
async def test_one_provider_job_renders_twenty_five_detailer_branches(
    worker_input_context: WorkerInputContext,
) -> None:
    context = _profile_context(
        worker_input_context,
        workflow_body=BASE_DETAILER_WORKFLOW_BODY,
        detector=True,
    )
    parameters = dict(context.job_context.parameters)
    generation = dict(parameters["generation"])  # type: ignore[arg-type]
    outputs = [
        {
            **generation,
            "prompt": (
                "private test prompt"
                if output_index == 0
                else f"independently resolved prompt {output_index + 1}"
            ),
            "seed": 42 + output_index,
            "outputs_per_job": 1,
        }
        for output_index in range(25)
    ]
    parameters.update(
        {
            "schema_version": 2,
            "generation": {**outputs[0], "outputs_per_job": 25},
            "output_generations": outputs,
            "output_prompt_resolutions": [
                {"seed": 42 + output_index} for output_index in range(25)
            ],
        }
    )
    parameters_sha256 = canonical_sha256(parameters)
    async with context.database.sessions() as session:
        job = await session.get(GenerationJob, context.job_context.generation_job_id)
        assert job is not None
        job.expected_output_count = 25
        job.parameters = parameters
        job.parameters_sha256 = parameters_sha256
        await session.commit()
    context = replace(
        context,
        job_context=SaladJobInputContext(
            **{
                **context.job_context.__dict__,
                "expected_output_count": 25,
                "parameters": parameters,
                "parameters_sha256": parameters_sha256,
            }
        ),
    )

    envelope = GenerateEnvelope.model_validate(await _build(context), strict=True)
    workflow = envelope.payload.workflow

    assert [grant.output_index for grant in envelope.payload.uploads] == list(range(25))
    assert (
        sum(
            isinstance(node, dict) and node.get("class_type") == "SaveImage"
            for node in workflow.values()
        )
        == 25
    )
    assert (
        sum(
            isinstance(node, dict) and node.get("class_type") == "FaceDetailer"
            for node in workflow.values()
        )
        == 25
    )
    assert (
        sum(
            isinstance(node, dict) and node.get("class_type") == "UltralyticsDetectorProvider"
            for node in workflow.values()
        )
        == 1
    )
    latent_nodes = [
        node
        for node in workflow.values()
        if isinstance(node, dict) and node.get("class_type") == "EmptyLatentImage"
    ]
    assert len(latent_nodes) == 25
    assert all(node["inputs"]["batch_size"] == 1 for node in latent_nodes)


@pytest.mark.asyncio
async def test_runtime_expands_eight_ordered_loras(
    worker_input_context: WorkerInputContext,
) -> None:
    parameters = dict(worker_input_context.job_context.parameters)
    lora_parameters: list[dict[str, object]] = []
    lora_artifacts: list[ModelArtifactSpec] = []
    for index in range(1, 9):
        digest = f"{index:x}" * 64
        storage_key = f"loras/style-{index}.safetensors"
        target_filename = f"style-{index}-runtime.safetensors"
        lora_parameters.append(
            {
                "name": f"style-{index}.safetensors",
                "source_url": f"https://models.example/lora-{index}",
                "storage_key": storage_key,
                "sha256": digest,
                "license_url": f"https://models.example/lora-{index}-license",
                "commercial_use_approved": True,
                "adult_use_approved": True,
                "weight": index / 10,
            }
        )
        lora_artifacts.append(
            ModelArtifactSpec(
                logical_name=f"style-{index}",
                kind=ArtifactKind.LORA,
                source_object_id=storage_key,
                sha256=digest,
                exact_size_bytes=100,
                max_size_bytes=100,
                target_filename=target_filename,
            )
        )
    parameters["loras"] = lora_parameters
    job_context = SaladJobInputContext(
        **{
            **worker_input_context.job_context.__dict__,
            "parameters": parameters,
            "parameters_sha256": canonical_sha256(parameters),
        }
    )
    checkpoint = worker_input_context.artifact_manifest.artifacts[0]
    artifacts = (checkpoint, *lora_artifacts)
    manifest = ArtifactManifest(
        version="v1",
        artifacts=artifacts,
        manifest_sha256=calculate_manifest_sha256(artifacts),
    )
    context = replace(
        worker_input_context,
        job_context=job_context,
        artifact_manifest=manifest,
    )

    envelope = GenerateEnvelope.model_validate(await _build(context), strict=True)
    workflow = envelope.payload.workflow
    assert all(f"2-lora-{index}" in workflow for index in range(1, 9))
    assert workflow["9"]["inputs"]["model"] == ["2-lora-8", 0]
    assert workflow["3"]["inputs"]["clip"] == ["2-lora-8", 1]


@pytest.mark.asyncio
async def test_hires_profile_renders_a_core_two_pass_latent_workflow(
    worker_input_context: WorkerInputContext,
) -> None:
    context = _profile_context(
        worker_input_context,
        workflow_body=HIRES_WORKFLOW_BODY,
    )

    envelope = GenerateEnvelope.model_validate(await _build(context), strict=True)
    workflow = envelope.payload.workflow
    upscale = workflow["10"]
    second_pass = workflow["11"]
    assert isinstance(upscale, dict)
    assert isinstance(second_pass, dict)
    assert upscale == {
        "class_type": "LatentUpscaleBy",
        "inputs": {
            "samples": ["9", 0],
            "scale_by": 1.5,
            "upscale_method": "bislerp",
        },
    }
    assert second_pass["inputs"]["latent_image"] == ["10", 0]
    assert second_pass["inputs"]["denoise"] == 0.35
    validate_approved_workflow(workflow, DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES)


@pytest.mark.asyncio
async def test_detailer_profile_binds_the_single_manifest_verified_face_detector(
    worker_input_context: WorkerInputContext,
) -> None:
    context = _profile_context(
        worker_input_context,
        workflow_body=DETAILER_WORKFLOW_BODY,
        detector=True,
    )

    envelope = GenerateEnvelope.model_validate(await _build(context), strict=True)
    workflow = envelope.payload.workflow
    detector = workflow["13"]
    detailer = workflow["14"]
    assert isinstance(detector, dict)
    assert isinstance(detailer, dict)
    assert detector == {
        "class_type": "UltralyticsDetectorProvider",
        "inputs": {"model_name": "bbox/face-yolov8m.pt"},
    }
    assert detailer["class_type"] == "FaceDetailer"
    assert detailer["inputs"]["bbox_detector"] == ["13", 0]
    assert detailer["inputs"]["guide_size"] == 768
    assert detailer["inputs"]["max_size"] == 1024
    assert detailer["inputs"]["denoise"] == 0.35
    assert detailer["inputs"]["feather"] == 4
    assert detailer["inputs"]["positive"] == ["16", 0]
    assert detailer["inputs"]["negative"] == ["17", 0]
    assert workflow["16"]["inputs"]["text"] == "expressive face"
    assert workflow["17"]["inputs"]["text"] == "closed eyes"
    validate_approved_workflow(workflow, DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES)


@pytest.mark.asyncio
async def test_base_detailer_profile_skips_hires_refinement(
    worker_input_context: WorkerInputContext,
) -> None:
    context = _profile_context(
        worker_input_context,
        workflow_body=BASE_DETAILER_WORKFLOW_BODY,
        detector=True,
    )

    envelope = GenerateEnvelope.model_validate(await _build(context), strict=True)
    workflow = envelope.payload.workflow
    assert not any(
        isinstance(node, dict) and node.get("class_type") == "LatentUpscaleBy"
        for node in workflow.values()
    )
    assert workflow["10"]["class_type"] == "VAEDecode"
    assert workflow["14"]["inputs"]["image"] == ["10", 0]
    validate_approved_workflow(workflow, DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow_body", "detector", "expected_sampler_count"),
    (
        (COUPLE_BASE_WORKFLOW_BODY, False, 1),
        (COUPLE_BASE_DETAILER_WORKFLOW_BODY, True, 1),
        (COUPLE_HIRES_WORKFLOW_BODY, False, 2),
        (COUPLE_HIRES_DETAILER_WORKFLOW_BODY, True, 2),
    ),
)
async def test_couple_profiles_render_two_overlapping_core_conditioning_regions(
    worker_input_context: WorkerInputContext,
    workflow_body: bytes,
    detector: bool,
    expected_sampler_count: int,
) -> None:
    context = _profile_context(
        worker_input_context,
        workflow_body=workflow_body,
        detector=detector,
        duo=True,
    )

    envelope = GenerateEnvelope.model_validate(await _build(context), strict=True)
    workflow = envelope.payload.workflow
    assert workflow["18"]["inputs"]["text"] == "nico robin, on the left"
    assert workflow["19"]["inputs"]["text"] == "boa hancock, on the right"
    assert workflow["20"] == {
        "class_type": "ConditioningSetAreaPercentage",
        "inputs": {
            "conditioning": ["18", 0],
            "height": 1.0,
            "strength": 1.0,
            "width": 0.55,
            "x": 0.0,
            "y": 0.0,
        },
    }
    assert workflow["21"]["inputs"] == {
        "conditioning": ["19", 0],
        "height": 1.0,
        "strength": 1.0,
        "width": 0.55,
        "x": 0.45,
        "y": 0.0,
    }
    assert workflow["22"]["inputs"] == {
        "conditioning_1": ["6", 0],
        "conditioning_2": ["20", 0],
    }
    assert workflow["23"]["inputs"] == {
        "conditioning_1": ["22", 0],
        "conditioning_2": ["21", 0],
    }
    samplers = [
        node
        for node in workflow.values()
        if isinstance(node, dict) and node.get("class_type") == "KSampler"
    ]
    assert len(samplers) == expected_sampler_count
    assert all(node["inputs"]["positive"] == ["23", 0] for node in samplers)
    assert require_comfy_workflow_deliverability(workflow)
    validate_approved_workflow(workflow, DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES)


@pytest.mark.asyncio
async def test_controlled_duo_balanced_renders_disjoint_masked_conditioning(
    worker_input_context: WorkerInputContext,
) -> None:
    context = _controlled_duo_context(
        worker_input_context,
        workflow_body=CONTROLLED_DUO_BALANCED_WORKFLOW_BODY,
        isolation_mode="balanced",
    )

    workflow = GenerateEnvelope.model_validate(
        await _build(context),
        strict=True,
    ).payload.workflow

    assert not any(
        isinstance(node, dict) and node.get("class_type") == CONTROLLED_DUO_MARKER_NODE_CLASS
        for node in workflow.values()
    )
    assert workflow["5"]["inputs"]["width"] == 448
    assert workflow["8"]["inputs"]["width"] == 448
    assert workflow["7"]["inputs"]["x"] == 40
    assert workflow["7"]["inputs"]["y"] == 80
    assert workflow["10"]["inputs"]["x"] == 536
    assert workflow["10"]["inputs"]["y"] == 80
    assert workflow["26"]["inputs"]["steps"] == 28
    assert workflow["6"]["inputs"]["right"] == 16
    assert workflow["9"]["inputs"]["left"] == 16
    assert workflow["17"]["inputs"]["mask"] == ["7", 0]
    assert workflow["18"]["inputs"]["mask"] == ["10", 0]
    assert workflow["19"]["inputs"]["mask"] == ["7", 0]
    assert workflow["20"]["inputs"]["mask"] == ["10", 0]
    assert "short copper bob" in workflow["13"]["inputs"]["text"]
    assert "long indigo braid" in workflow["14"]["inputs"]["text"]
    assert "indigo hair" in workflow["15"]["inputs"]["text"]
    assert "copper hair" in workflow["16"]["inputs"]["text"]
    assert "the other character's hair traits" in workflow["15"]["inputs"]["text"]
    assert "mixed hair color" not in workflow["15"]["inputs"]["text"]
    assert "mixed outfit" not in workflow["15"]["inputs"]["text"]
    assert (
        sum(
            isinstance(node, dict) and node.get("class_type") == "KSampler"
            for node in workflow.values()
        )
        == 1
    )
    assert not any(
        isinstance(node, dict) and node.get("class_type") == "FaceDetailer"
        for node in workflow.values()
    )
    assert require_comfy_workflow_deliverability(workflow)
    validate_approved_workflow(workflow, DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES)


@pytest.mark.asyncio
async def test_controlled_duo_strict_renders_two_sequential_region_refinements(
    worker_input_context: WorkerInputContext,
) -> None:
    context = _controlled_duo_context(
        worker_input_context,
        workflow_body=CONTROLLED_DUO_STRICT_WORKFLOW_BODY,
        isolation_mode="strict",
        preset="diagonal_depth",
    )

    workflow = GenerateEnvelope.model_validate(
        await _build(context),
        strict=True,
    ).payload.workflow

    assert workflow["5"]["inputs"]["width"] == 536
    assert workflow["7"]["inputs"]["x"] == 24
    assert workflow["7"]["inputs"]["y"] == 304
    assert workflow["8"]["inputs"]["width"] == 400
    assert workflow["10"]["inputs"]["x"] == 592
    assert workflow["10"]["inputs"]["y"] == 48
    assert workflow["19"]["inputs"] == {
        "samples": ["18", 0],
        "mask": ["7", 0],
    }
    assert workflow["20"]["inputs"]["latent_image"] == ["19", 0]
    assert workflow["20"]["inputs"]["steps"] == 14
    assert workflow["20"]["inputs"]["denoise"] == 0.42
    assert workflow["18"]["inputs"]["positive"] == ["34", 0]
    assert workflow["18"]["inputs"]["negative"] == ["36", 0]
    assert workflow["18"]["inputs"]["steps"] == 28
    assert workflow["21"]["inputs"] == {
        "samples": ["20", 0],
        "mask": ["10", 0],
    }
    assert workflow["22"]["inputs"]["latent_image"] == ["21", 0]
    assert workflow["22"]["inputs"]["steps"] == 14
    assert "short copper bob" in workflow["13"]["inputs"]["text"]
    assert "long indigo braid" in workflow["14"]["inputs"]["text"]
    assert (
        sum(
            isinstance(node, dict) and node.get("class_type") == "KSampler"
            for node in workflow.values()
        )
        == 3
    )
    assert (
        sum(
            isinstance(node, dict) and node.get("class_type") == "SetLatentNoiseMask"
            for node in workflow.values()
        )
        == 2
    )
    assert not any(
        isinstance(node, dict) and node.get("class_type") == "FaceDetailer"
        for node in workflow.values()
    )
    assert require_comfy_workflow_deliverability(workflow)
    validate_approved_workflow(workflow, DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES)


@pytest.mark.asyncio
async def test_controlled_duo_draft_reduces_real_sampler_steps(
    worker_input_context: WorkerInputContext,
) -> None:
    balanced_context = _controlled_duo_context(
        worker_input_context,
        workflow_body=CONTROLLED_DUO_BALANCED_WORKFLOW_BODY,
        isolation_mode="balanced",
        quality_mode="draft",
    )
    balanced = GenerateEnvelope.model_validate(
        await _build(balanced_context),
        strict=True,
    ).payload.workflow
    assert balanced["26"]["inputs"]["steps"] == 17

    strict_context = _controlled_duo_context(
        worker_input_context,
        workflow_body=CONTROLLED_DUO_STRICT_WORKFLOW_BODY,
        isolation_mode="strict",
        quality_mode="draft",
    )
    strict = GenerateEnvelope.model_validate(
        await _build(strict_context),
        strict=True,
    ).payload.workflow
    assert strict["18"]["inputs"]["steps"] == 17
    assert strict["20"]["inputs"]["steps"] == 7
    assert strict["22"]["inputs"]["steps"] == 7
    assert strict["20"]["inputs"]["denoise"] == 0.30


@pytest.mark.asyncio
async def test_controlled_duo_multi_output_keeps_each_prompt_lane_independent(
    worker_input_context: WorkerInputContext,
) -> None:
    context = _controlled_duo_context(
        worker_input_context,
        workflow_body=CONTROLLED_DUO_BALANCED_WORKFLOW_BODY,
        isolation_mode="balanced",
    )
    parameters = dict(context.job_context.parameters)
    generation = dict(parameters["generation"])  # type: ignore[arg-type]
    first = {**generation, "outputs_per_job": 1}
    second = {
        **first,
        "character_a_prompt": "silver pixie cut, red coat",
        "character_b_prompt": "dark green curls, gold dress",
        "character_a_negative_prompt": "green curls",
        "character_b_negative_prompt": "silver hair",
        "interaction_prompt": "running in opposite directions",
        "camera_prompt": "overhead action camera",
        "seed": 43,
    }
    parameters.update(
        {
            "schema_version": 2,
            "generation": {**first, "outputs_per_job": 2},
            "output_generations": [first, second],
            "output_prompt_resolutions": [{"seed": 42}, {"seed": 43}],
        }
    )
    context = replace(
        context,
        job_context=SaladJobInputContext(
            **{
                **context.job_context.__dict__,
                "parameters": parameters,
                "parameters_sha256": canonical_sha256(parameters),
            }
        ),
    )

    workflow = GenerateEnvelope.model_validate(
        await _build(context),
        strict=True,
    ).payload.workflow

    assert "short copper bob" in workflow["output-00-13"]["inputs"]["text"]
    assert "silver pixie cut" in workflow["output-01-13"]["inputs"]["text"]
    assert "long indigo braid" in workflow["output-00-14"]["inputs"]["text"]
    assert "dark green curls" in workflow["output-01-14"]["inputs"]["text"]
    assert "dynamic low camera" in workflow["output-00-11"]["inputs"]["text"]
    assert "overhead action camera" in workflow["output-01-11"]["inputs"]["text"]
    assert workflow["output-00-26"]["inputs"]["seed"] == 42
    assert workflow["output-01-26"]["inputs"]["seed"] == 43


@pytest.mark.asyncio
async def test_controlled_duo_marker_and_capabilities_fail_closed(
    worker_input_context: WorkerInputContext,
) -> None:
    graph = json.loads(CONTROLLED_DUO_BALANCED_WORKFLOW_BODY)
    graph["99"]["inputs"]["character_a_mask_node_id"] = "10"
    tampered = json.dumps(graph, separators=(",", ":")).encode()
    context = _controlled_duo_context(
        worker_input_context,
        workflow_body=tampered,
        isolation_mode="balanced",
    )
    with pytest.raises(WorkerInputError, match="workflow evidence is invalid"):
        await _build(context)

    missing_capability = _controlled_duo_context(
        worker_input_context,
        workflow_body=CONTROLLED_DUO_BALANCED_WORKFLOW_BODY,
        isolation_mode="balanced",
        capabilities=[],
    )
    with pytest.raises(WorkerInputError, match="workflow capability is invalid"):
        await _build(missing_capability)

    wrong_slot_graph = json.loads(CONTROLLED_DUO_STRICT_WORKFLOW_BODY)
    wrong_slot_graph["22"]["inputs"]["positive"] = ["13", 0]
    wrong_slot = json.dumps(wrong_slot_graph, separators=(",", ":")).encode()
    wrong_slot_context = _controlled_duo_context(
        worker_input_context,
        workflow_body=wrong_slot,
        isolation_mode="strict",
    )
    with pytest.raises(WorkerInputError, match="workflow evidence is invalid"):
        await _build(wrong_slot_context)

    extra_prompt_graph = json.loads(CONTROLLED_DUO_BALANCED_WORKFLOW_BODY)
    extra_prompt_graph["98"] = {
        "class_type": "CLIPTextEncode",
        "inputs": {"clip": ["3", 0], "text": "unreviewed alternate conditioning"},
    }
    extra_prompt = json.dumps(extra_prompt_graph, separators=(",", ":")).encode()
    extra_prompt_context = _controlled_duo_context(
        worker_input_context,
        workflow_body=extra_prompt,
        isolation_mode="balanced",
    )
    with pytest.raises(WorkerInputError, match="workflow evidence is invalid"):
        await _build(extra_prompt_context)

    bypass_lora_graph = json.loads(CONTROLLED_DUO_BALANCED_WORKFLOW_BODY)
    bypass_lora_graph["26"]["inputs"]["model"] = ["1", 0]
    bypass_lora = json.dumps(bypass_lora_graph, separators=(",", ":")).encode()
    bypass_lora_context = _controlled_duo_context(
        worker_input_context,
        workflow_body=bypass_lora,
        isolation_mode="balanced",
    )
    with pytest.raises(WorkerInputError, match="workflow evidence is invalid"):
        await _build(bypass_lora_context)

    extra_output_graph = json.loads(CONTROLLED_DUO_BALANCED_WORKFLOW_BODY)
    extra_output_graph["97"] = {
        "class_type": "SaveImageWebsocket",
        "inputs": {"images": ["27", 0]},
    }
    extra_output = json.dumps(extra_output_graph, separators=(",", ":")).encode()
    extra_output_context = _controlled_duo_context(
        worker_input_context,
        workflow_body=extra_output,
        isolation_mode="balanced",
    )
    with pytest.raises(WorkerInputError, match="workflow evidence is invalid"):
        await _build(extra_output_context)


@pytest.mark.parametrize("preset", list(DuoCompositionPreset))
def test_controlled_duo_preset_bindings_are_disjoint_and_bounded(
    preset: DuoCompositionPreset,
) -> None:
    generation = GenerationParameters(
        composition_mode="duo",
        duo_contract_version=2,
        composition_preset_id=preset,
        prompt="dynamic anime composition",
        character_a_prompt="short copper hair, teal jacket",
        character_b_prompt="long indigo hair, ivory coat",
        interaction_prompt="back to back",
        camera_prompt="dramatic camera",
        seed=42,
        width=1024,
        height=1536,
        steps=28,
        sampler="euler",
        scheduler="normal",
    )

    bindings = _controlled_duo_bindings(generation)
    assert bindings is not None
    character_a = bindings["character_a"]
    character_b = bindings["character_b"]
    assert isinstance(character_a, dict)
    assert isinstance(character_b, dict)
    expected_regions = {
        DuoCompositionPreset.CLOSE_PORTRAIT: ((40, 120, 448, 1288), (536, 120, 448, 1288)),
        DuoCompositionPreset.OVERHEAD: ((72, 184, 408, 1104), (552, 104, 400, 1168)),
        DuoCompositionPreset.LOW_ANGLE: ((32, 336, 464, 1152), (536, 168, 464, 1320)),
        DuoCompositionPreset.DIAGONAL_DEPTH: ((24, 464, 536, 1032), (592, 80, 400, 800)),
        DuoCompositionPreset.BACK_TO_BACK: ((48, 136, 440, 1288), (536, 136, 440, 1288)),
        DuoCompositionPreset.FULL_BODY: ((80, 48, 392, 1440), (552, 48, 392, 1440)),
    }
    assert (
        character_a["x"],
        character_a["y"],
        character_a["width"],
        character_a["height"],
    ) == expected_regions[preset][0]
    assert (
        character_b["x"],
        character_b["y"],
        character_b["width"],
        character_b["height"],
    ) == expected_regions[preset][1]
    for region in (character_a, character_b):
        assert 0 <= region["x"] < generation.width
        assert 0 <= region["y"] < generation.height
        assert region["x"] + region["width"] <= generation.width
        assert region["y"] + region["height"] <= generation.height
    assert character_a["width"] % 8 == character_b["width"] % 8 == 0
    horizontal_overlap = max(character_a["x"], character_b["x"]) < min(
        character_a["x"] + character_a["width"],
        character_b["x"] + character_b["width"],
    )
    vertical_overlap = max(character_a["y"], character_b["y"]) < min(
        character_a["y"] + character_a["height"],
        character_b["y"] + character_b["height"],
    )
    assert not (horizontal_overlap and vertical_overlap)
    assert bindings["base_steps"] == 28
    assert bindings["refinement_steps"] == 14
    assert bindings["refinement_denoise"] == 0.42


def test_controlled_duo_default_node_allowlist_is_core_only() -> None:
    assert {
        "ConditioningSetMask",
        "FeatherMask",
        "MaskComposite",
        "SetLatentNoiseMask",
        "SolidMask",
    }.issubset(DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES)
    assert CONTROLLED_DUO_MARKER_NODE_CLASS not in DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES


def test_generation_parameters_keep_single_mode_backward_compatible_and_guard_duo() -> None:
    legacy = GenerationParameters(
        prompt="one adult fictional character",
        seed=1,
        width=1024,
        height=1024,
        steps=20,
        sampler="euler",
        scheduler="normal",
    )
    assert legacy.composition_mode == "single"
    assert legacy.character_a_prompt == ""
    assert legacy.character_b_prompt == ""

    with pytest.raises(ValueError, match="requires both character prompts"):
        GenerationParameters.model_validate(
            {
                **legacy.model_dump(mode="json"),
                "composition_mode": "duo",
                "character_a_prompt": "nico robin",
            }
        )


@pytest.mark.asyncio
async def test_legacy_generation_parameters_receive_detailer_and_clip_defaults(
    worker_input_context: WorkerInputContext,
) -> None:
    parameters = dict(worker_input_context.job_context.parameters)
    generation = dict(parameters["generation"])  # type: ignore[arg-type]
    for field_name in (
        "clip_skip",
        "detailer_prompt",
        "detailer_negative_prompt",
        "detailer_feather",
    ):
        generation.pop(field_name)
    parameters["generation"] = generation
    legacy_context = replace(
        worker_input_context,
        job_context=SaladJobInputContext(
            **{
                **worker_input_context.job_context.__dict__,
                "parameters": parameters,
                "parameters_sha256": canonical_sha256(parameters),
            }
        ),
    )
    context = _profile_context(
        legacy_context,
        workflow_body=BASE_DETAILER_WORKFLOW_BODY,
        detector=True,
    )

    envelope = GenerateEnvelope.model_validate(await _build(context), strict=True)
    workflow = envelope.payload.workflow
    assert workflow["3"]["inputs"]["stop_at_clip_layer"] == -2
    assert workflow["16"]["inputs"]["text"] == "private test prompt"
    assert workflow["17"]["inputs"]["text"] == "bad anatomy"
    assert workflow["14"]["inputs"]["feather"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("geometry_case", ("hardcoded_canvas", "chained_upscalers"))
async def test_rejects_oversized_exact_rendered_workflow_before_upload_grants(
    worker_input_context: WorkerInputContext,
    geometry_case: str,
) -> None:
    if geometry_case == "hardcoded_canvas":
        graph = json.loads(WORKFLOW_BODY)
        graph["8"]["inputs"]["width"] = 16384
        graph["8"]["inputs"]["height"] = 64
    else:
        graph = json.loads(HIRES_WORKFLOW_BODY)
        graph["8"]["inputs"]["width"] = 1024
        graph["8"]["inputs"]["height"] = 1024
        graph["10"]["inputs"]["scale_by"] = 2.0
        graph["10-second"] = {
            "class_type": "LatentUpscaleBy",
            "inputs": {
                "samples": ["10", 0],
                "scale_by": 2.0,
                "upscale_method": "bislerp",
            },
        }
        graph["11"]["inputs"]["latent_image"] = ["10-second", 0]
    workflow_body = json.dumps(graph, separators=(",", ":")).encode()
    context = _profile_context(
        worker_input_context,
        workflow_body=workflow_body,
    )

    with pytest.raises(WorkerInputError, match="geometry"):
        await _build(context)

    async with context.database.sessions() as session:
        asset_count = await session.scalar(select(func.count(Asset.id)))
    assert asset_count == 0


@pytest.mark.asyncio
async def test_synthetic_production_prompt_reaches_immutable_raw_masters(
    worker_input_context: WorkerInputContext,
) -> None:
    request = await _build(worker_input_context)
    executor = _SyntheticComfyExecutor()
    uploader = _StagingUploader(worker_input_context.store)
    application = create_worker_app(
        settings=WorkerSettings(
            environment=WorkerEnvironment.TEST,
            verification_keys={"worker-key-1": VERIFICATION_PUBLIC_KEY},
            allowed_upload_origin=UPLOAD_ORIGIN,
        ),
        executor=executor,
        uploader=uploader,
        now=lambda: NOW,
    )

    with TestClient(application) as client:
        response = client.post(
            "/jobs/generate",
            json=request,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert len(executor.workflows) == 1
    assert executor.workflows[0]["2-lora-1"] == {
        "class_type": "LoraLoader",
        "inputs": {
            "model": ["1", 0],
            "clip": ["1", 1],
            "lora_name": "style-runtime.safetensors",
            "strength_model": 0.75,
            "strength_clip": 0.75,
        },
    }

    asset_ids = [UUID(item["asset_id"]) for item in response.json()["outputs"]]
    async with worker_input_context.database.sessions() as session:
        for asset_id in asset_ids:
            finalized = await finalize_raw_master(
                session,
                worker_input_context.store,
                asset_id=asset_id,
                max_bytes=1_000_000,
                actor="synthetic-contract",
            )
            assert finalized.object_key.startswith("masters/")
        assets = list(
            (
                await session.scalars(
                    select(Asset).where(Asset.id.in_(asset_ids)).order_by(Asset.output_index)
                )
            ).all()
        )

    assert [asset.state for asset in assets] == [
        AssetState.AVAILABLE,
        AssetState.AVAILABLE,
    ]
    assert all(asset.object_key in worker_input_context.store.objects for asset in assets)


@pytest.mark.asyncio
async def test_rebuild_rotates_expiring_upload_grants(
    worker_input_context: WorkerInputContext,
) -> None:
    first = GenerateEnvelope.model_validate(await _build(worker_input_context), strict=True)
    second = GenerateEnvelope.model_validate(await _build(worker_input_context), strict=True)

    assert [grant.asset_id for grant in first.payload.uploads] == [
        grant.asset_id for grant in second.payload.uploads
    ]
    assert [grant.upload_attempt_id for grant in first.payload.uploads] != [
        grant.upload_attempt_id for grant in second.payload.uploads
    ]


@pytest.mark.asyncio
async def test_rejects_parameter_digest_mismatch(
    worker_input_context: WorkerInputContext,
) -> None:
    bad_context = SaladJobInputContext(
        **{
            **worker_input_context.job_context.__dict__,
            "parameters_sha256": "0" * 64,
        }
    )
    async with worker_input_context.database.sessions() as session:
        provider = SaladWorkerJobInputProvider(
            session=session,
            store=worker_input_context.store,
            signing_key_id="worker-key-1",
            signing_private_key=SecretStr(SIGNING_PRIVATE_KEY),
            artifact_manifest=worker_input_context.artifact_manifest,
            artifact_manifest_sha256=(worker_input_context.artifact_manifest.manifest_sha256),
        )
        with pytest.raises(WorkerInputError, match="integrity"):
            await provider.build_job_input(bad_context)


@pytest.mark.asyncio
async def test_rejects_changed_workflow_bytes(
    worker_input_context: WorkerInputContext,
) -> None:
    worker_input_context.store.put_for_test(
        worker_input_context.workflow_key,
        b'{"changed":true}',
        content_type="application/json",
    )

    with pytest.raises(WorkerInputError, match="integrity"):
        await _build(worker_input_context)


@pytest.mark.asyncio
async def test_rejects_release_artifact_outside_the_materialized_worker_manifest(
    worker_input_context: WorkerInputContext,
) -> None:
    parameters = dict(worker_input_context.job_context.parameters)
    raw_checkpoint = parameters["checkpoint"]
    assert isinstance(raw_checkpoint, dict)
    parameters["checkpoint"] = {**raw_checkpoint, "sha256": "c" * 64}
    context = SaladJobInputContext(
        **{
            **worker_input_context.job_context.__dict__,
            "parameters": parameters,
            "parameters_sha256": canonical_sha256(parameters),
        }
    )

    async with worker_input_context.database.sessions() as session:
        provider = SaladWorkerJobInputProvider(
            session=session,
            store=worker_input_context.store,
            signing_key_id="worker-key-1",
            signing_private_key=SecretStr(SIGNING_PRIVATE_KEY),
            artifact_manifest=worker_input_context.artifact_manifest,
            artifact_manifest_sha256=(worker_input_context.artifact_manifest.manifest_sha256),
        )
        with pytest.raises(WorkerInputError, match="worker manifest"):
            await provider.build_job_input(context)
        assert list(await session.scalars(select(Asset))) == []


@pytest.mark.asyncio
async def test_rejects_unknown_or_structural_template_bindings(
    worker_input_context: WorkerInputContext,
) -> None:
    for marker in ("generation.missing", "generation"):
        body = json.dumps({"1": {"inputs": {"value": {"$gen": marker}}}}).encode()
        worker_input_context.store.put_for_test(
            worker_input_context.workflow_key,
            body,
            content_type="application/json",
        )
        parameters = dict(worker_input_context.job_context.parameters)
        raw_workflow = parameters["workflow"]
        assert isinstance(raw_workflow, dict)
        workflow = dict(raw_workflow)
        workflow["sha256"] = hashlib.sha256(body).hexdigest()
        parameters["workflow"] = workflow
        bad_context = SaladJobInputContext(
            **{
                **worker_input_context.job_context.__dict__,
                "parameters": parameters,
                "parameters_sha256": canonical_sha256(parameters),
            }
        )
        async with worker_input_context.database.sessions() as session:
            provider = SaladWorkerJobInputProvider(
                session=session,
                store=worker_input_context.store,
                signing_key_id="worker-key-1",
                signing_private_key=SecretStr(SIGNING_PRIVATE_KEY),
                artifact_manifest=worker_input_context.artifact_manifest,
                artifact_manifest_sha256=(worker_input_context.artifact_manifest.manifest_sha256),
            )
            with pytest.raises(WorkerInputError, match="binding"):
                await provider.build_job_input(bad_context)


@pytest.mark.asyncio
async def test_upload_grant_ttl_must_cover_acceptance_and_execution(
    worker_input_context: WorkerInputContext,
) -> None:
    async with worker_input_context.database.sessions() as session:
        with pytest.raises(ValueError, match="cover execution"):
            SaladWorkerJobInputProvider(
                session=session,
                store=worker_input_context.store,
                signing_key_id="worker-key-1",
                signing_private_key=SecretStr(SIGNING_PRIVATE_KEY),
                artifact_manifest=worker_input_context.artifact_manifest,
                artifact_manifest_sha256=(worker_input_context.artifact_manifest.manifest_sha256),
                signature_ttl_seconds=7200,
                upload_grant_ttl_seconds=8000,
            )
