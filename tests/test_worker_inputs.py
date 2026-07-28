import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from gen_automation.db.models import GenerationJob, Project, Release, ReleaseVersion
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.signing import derive_public_key, encode_base64url
from gen_automation.gpu_worker.models import GenerateEnvelope, WorkerEnvironment, WorkerSettings
from gen_automation.gpu_worker.security import verify_authorization
from gen_automation.services.salad import SaladJobInputContext
from gen_automation.services.worker_inputs import (
    SaladWorkerJobInputProvider,
    WorkerInputError,
)
from gen_automation.storage.memory import MemoryObjectStore

NOW = 2_000_000_000
SIGNING_PRIVATE_KEY = encode_base64url(bytes(range(1, 33)))
VERIFICATION_PUBLIC_KEY = derive_public_key(SIGNING_PRIVATE_KEY)


@dataclass(frozen=True)
class WorkerInputContext:
    database: Database
    store: MemoryObjectStore
    job_context: SaladJobInputContext
    workflow_key: str
    workflow_body: bytes


@pytest.fixture
async def worker_input_context(tmp_path: Path) -> AsyncIterator[WorkerInputContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'worker-inputs.db').as_posix()}")
    await database.create_schema()
    store = MemoryObjectStore()
    workflow_key = "workflows/illustrious-v1.json"
    workflow_body = json.dumps(
        {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": {"$gen": "checkpoint.name"}},
            },
            "2": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": {"$gen": "generation.prompt"}},
            },
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": {"$gen": "generation.seed"},
                    "steps": {"$gen": "generation.steps"},
                    "sampler_name": {"$gen": "generation.sampler"},
                    "scheduler": {"$gen": "generation.scheduler"},
                    "lora_weight": {"$gen": "loras.0.weight"},
                },
            },
        },
        separators=(",", ":"),
    ).encode()
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
            "seed": 42,
            "width": 1024,
            "height": 1024,
            "steps": 28,
            "sampler": "euler",
            "scheduler": "normal",
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
            now=lambda: NOW,
        )
        return await provider.build_job_input(context.job_context)


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
    prompt_node = envelope.payload.workflow["2"]
    sampler_node = envelope.payload.workflow["3"]
    assert isinstance(prompt_node, dict)
    assert isinstance(sampler_node, dict)
    prompt_inputs = prompt_node["inputs"]
    sampler_inputs = sampler_node["inputs"]
    assert isinstance(prompt_inputs, dict)
    assert isinstance(sampler_inputs, dict)
    assert prompt_inputs["text"] == "private test prompt"
    assert sampler_inputs["seed"] == 42
    assert [grant.output_index for grant in envelope.payload.uploads] == [0, 1]
    assert len({grant.upload_attempt_id for grant in envelope.payload.uploads}) == 2
    assert all("expires=10800" in grant.url for grant in envelope.payload.uploads)


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
                signature_ttl_seconds=7200,
                upload_grant_ttl_seconds=8000,
            )
