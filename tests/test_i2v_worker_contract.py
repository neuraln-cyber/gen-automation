from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr, ValidationError

from gen_automation.i2v_worker.lora_catalog import LORA_ARTIFACTS_BY_ROLE, LORA_CATALOG
from gen_automation.i2v_worker.models import GenerationSettings, I2VJob, ModelObject
from gen_automation.i2v_worker.settings import I2VWorkerSettings
from gen_automation.i2v_worker.supervisor import _comfy_command
from gen_automation.i2v_worker.workflow import (
    WorkflowError,
    effective_negative_prompt,
    effective_positive_prompt,
    load_workflow_template,
    lora_provenance,
    render_workflow,
)

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile.i2v-worker"
LOCK = ROOT / "requirements-i2v-worker.lock"
FACE_LOCK = ROOT / "requirements-i2v-face.lock"
FACE_NOTICES = ROOT / "THIRD_PARTY_LICENSES.md"
FACE_STABILIZER = ROOT / "src/gen_automation/i2v_worker/face_stabilizer.py"
RUNPOD_HANDLER = ROOT / "src/gen_automation/i2v_worker/runpod_handler.py"
NAG_PATCH = ROOT / "patches/comfyui-nag/chroma-stream-blocks.patch"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
WORKFLOW = ROOT / "workflows/dasiwa-wan22-i2v-v1.api.json"
SHA = "a" * 64


def _objects() -> list[dict[str, object]]:
    baseline = [
        {
            "role": role,
            "bucket": "private-models",
            "key": f"worker/i2v/sha256/{index:064x}",
            "version_id": f"version-{index}",
            "byte_size": index,
            "sha256": f"{index:064x}",
            "install_path": path,
        }
        for index, (role, path) in enumerate(
            (
                ("diffusion_model_high", "models/diffusion_models/high.safetensors"),
                ("diffusion_model_low", "models/diffusion_models/low.safetensors"),
                ("text_encoder", "models/text_encoders/text.safetensors"),
                ("vae", "models/vae/Wan/vae.safetensors"),
            ),
            start=1,
        )
    ]
    loras = [
        {
            "role": role,
            "bucket": "private-models",
            "key": f"worker/i2v/sha256/{artifact.sha256}",
            "version_id": f"version-{role}",
            "byte_size": artifact.byte_size,
            "sha256": artifact.sha256,
            "install_path": artifact.install_path,
        }
        for role, artifact in LORA_ARTIFACTS_BY_ROLE.items()
    ]
    return baseline + loras


def _settings(tmp_path: Path, *, lora_worker_enabled: bool = True) -> I2VWorkerSettings:
    return I2VWorkerSettings(
        model_objects_json=SecretStr(
            json.dumps(_objects() if lora_worker_enabled else _objects()[:4])
        ),
        environment="test",
        comfy_root=tmp_path / "comfy",
        runtime_root=tmp_path / "runtime",
        workflow_template=WORKFLOW,
        comfy_python=tmp_path / "venv/python",
        comfy_main=tmp_path / "comfy/main.py",
        queue_worker_enabled=False,
        lora_worker_enabled=lora_worker_enabled,
        source_revision="b" * 40 if lora_worker_enabled else None,
        private_manifest_source_sha256="c" * 64 if lora_worker_enabled else None,
    )


def _job() -> dict[str, object]:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    return {
        "schema": "i2v-job/v2",
        "job_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "request_sha256": SHA,
        "input_snapshot": {
            "storage_backend": "s3",
            "storage_bucket": "inputs",
            "object_key": "i2v/input.png",
            "object_version_id": "v1",
            "sha256": SHA,
            "content_type": "image/png",
            "width": 576,
            "height": 1024,
            "byte_size": 100,
        },
        "positive_prompt": "gentle natural motion",
        "negative_prompt": "camera shake",
        "settings_snapshot": {},
        "input_grant": {"method": "GET", "url": "https://example.test/in", "expires_at": expires},
        "output_grant": {
            "method": "PUT",
            "url": "https://example.test/out",
            "headers": {
                "Content-Type": "video/mp4",
                "Cache-Control": "private, no-store, max-age=0",
                "x-amz-server-side-encryption": "AES256",
            },
            "storage_backend": "s3",
            "storage_bucket": "outputs",
            "object_key": "i2v/output.mp4",
            "expires_at": expires,
        },
    }


def test_job_contract_is_strict_and_uses_wire_schema_alias() -> None:
    parsed = I2VJob.model_validate_json(json.dumps(_job()), strict=True)

    assert parsed.schema_version == "i2v-job/v2"
    assert parsed.model_dump(mode="json", by_alias=True)["schema"] == "i2v-job/v2"
    invalid = _job()
    invalid["unexpected"] = True
    with pytest.raises(ValidationError):
        I2VJob.model_validate_json(json.dumps(invalid), strict=True)


def test_baseline_accepts_wan_shape_and_only_reviewed_loras() -> None:
    assert GenerationSettings().frame_count == 81
    assert GenerationSettings().face_fidelity == "off"
    assert GenerationSettings(face_fidelity="stable_expression").face_fidelity == (
        "stable_expression"
    )
    with pytest.raises(ValidationError):
        GenerationSettings(face_fidelity="unreviewed")
    delivery = GenerationSettings(
        width=768,
        height=992,
        match_source_aspect=True,
        upscale="source",
        loop=True,
        loop_count=2,
    )
    assert (delivery.width, delivery.height) == (768, 992)
    assert delivery.upscale == "source"
    assert delivery.match_source_aspect is True
    assert delivery.loop is True
    assert delivery.loop_count == 2
    assert GenerationSettings().loop is False
    assert GenerationSettings().loop_count == 2
    with pytest.raises(ValidationError):
        GenerationSettings(frame_count=80)
    reviewed = GenerationSettings(
        loras=[{"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3}]
    )
    assert reviewed.loras[0].catalog_id == "wan-general-nsfw-v0.08a"
    three_reviewed = GenerationSettings(
        loras=[
            {"catalog_id": catalog_id, "strength": 0.25} for catalog_id in list(LORA_CATALOG)[:3]
        ]
    )
    assert [selection.catalog_id for selection in three_reviewed.loras] == list(LORA_CATALOG)[:3]
    with pytest.raises(ValidationError):
        GenerationSettings(
            loras=[
                {"catalog_id": catalog_id, "strength": 0.25}
                for catalog_id in list(LORA_CATALOG)[:4]
            ]
        )
    with pytest.raises(ValidationError):
        GenerationSettings(loras=[{"catalog_id": "unreviewed", "strength": 0.3}])
    with pytest.raises(ValidationError):
        GenerationSettings(loras=[{"catalog_id": "wan-general-nsfw-v0.08a", "strength": 2.01}])
    with pytest.raises(ValidationError):
        GenerationSettings(
            loras=[
                {"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3},
                {"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.4},
            ]
        )
    with pytest.raises(ValidationError):
        GenerationSettings(tiled_vae=True)
    with pytest.raises(ValidationError):
        GenerationSettings(loop_count=0)
    with pytest.raises(ValidationError):
        GenerationSettings(loop_count=21)
    with pytest.raises(ValidationError, match="looped output duration must not exceed 25 seconds"):
        GenerationSettings(loop=True, loop_count=3)
    with pytest.raises(ValidationError, match="looped output duration must not exceed 25 seconds"):
        GenerationSettings(frame_count=209, fps=16, loop=True, loop_count=1)
    with pytest.raises(ValidationError, match="stable expression does not support looped delivery"):
        GenerationSettings(loop=True, face_fidelity="stable_expression")
    long_non_loop = GenerationSettings(frame_count=409, fps=16, loop=False, loop_count=20)
    assert long_non_loop.loop is False


def test_model_objects_are_exact_versioned_and_confined_to_comfy_models() -> None:
    assert ModelObject.model_validate(_objects()[0]).install_path.startswith("models/")
    invalid = dict(_objects()[0])
    invalid["install_path"] = "models/../main.py"
    with pytest.raises(ValidationError):
        ModelObject.model_validate(invalid)


def test_settings_require_every_baseline_and_reviewed_lora_role_once(tmp_path: Path) -> None:
    assert len(_settings(tmp_path).model_objects) == 14
    assert len(_settings(tmp_path, lora_worker_enabled=False).model_objects) == 4
    with pytest.raises(ValidationError):
        I2VWorkerSettings(
            model_objects_json=SecretStr(json.dumps(_objects()[:-1])),
            comfy_root=tmp_path / "comfy",
            runtime_root=tmp_path / "runtime",
            lora_worker_enabled=True,
            source_revision="b" * 40,
            private_manifest_source_sha256="c" * 64,
        )
    with pytest.raises(ValidationError, match="immutable manifest and source identity"):
        I2VWorkerSettings(
            model_objects_json=SecretStr(json.dumps(_objects())),
            comfy_root=tmp_path / "comfy",
            runtime_root=tmp_path / "runtime",
            lora_worker_enabled=True,
        )


def test_workflow_renders_all_runtime_values_without_mutating_template() -> None:
    template = load_workflow_template(WORKFLOW)
    original = json.dumps(template, sort_keys=True)
    job_id, attempt_id = uuid4(), uuid4()
    rendered, seed, prefix = render_workflow(
        template,
        input_filename="source.png",
        positive_prompt="slow hip sway",
        negative_prompt="jitter",
        settings=GenerationSettings(seed=42),
        job_id=job_id,
        attempt_id=attempt_id,
    )

    assert seed == 42
    assert rendered["1"]["inputs"]["image"] == "source.png"
    assert rendered["11"]["inputs"]["end_at_step"] == 2
    assert rendered["12"]["inputs"]["start_at_step"] == 2
    assert rendered["14"]["inputs"]["filename_prefix"] == prefix
    assert "$i2v" not in json.dumps(rendered)
    assert json.dumps(template, sort_keys=True) == original


def test_face_fidelity_uses_pinned_nag_and_effective_expression_anchors() -> None:
    template = load_workflow_template(WORKFLOW)
    settings = GenerationSettings(seed=42, face_fidelity="stable_expression")

    rendered, _seed, _prefix = render_workflow(
        template,
        input_filename="source.png",
        positive_prompt="one gentle torso sway",
        negative_prompt="camera shake",
        settings=settings,
        job_id=uuid4(),
        attempt_id=uuid4(),
    )

    assert rendered["6"]["inputs"]["text"] == effective_positive_prompt(
        "one gentle torso sway", settings
    )
    assert "one subtle natural blink" in rendered["6"]["inputs"]["text"]
    assert "exact source angle" in rendered["6"]["inputs"]["text"]
    assert rendered["7"]["inputs"]["text"] == effective_negative_prompt("camera shake", settings)
    assert "expression change" in rendered["7"]["inputs"]["text"]
    assert "repeated blinking" in rendered["7"]["inputs"]["text"]
    assert rendered["10"]["class_type"] == "WanImageToVideo"
    assert rendered["10"]["inputs"]["start_image"] == ["1", 0]
    assert "end_image" not in rendered["10"]["inputs"]
    for node_id in ("11", "12"):
        sampler = rendered[node_id]
        assert sampler["class_type"] == "KSamplerWithNAG (Advanced)"
        assert sampler["inputs"]["positive"] == ["10", 0]
        assert sampler["inputs"]["negative"] == ["10", 1]
        assert sampler["inputs"]["nag_negative"] == ["10", 1]
        assert sampler["inputs"]["nag_scale"] == 11.0
        assert sampler["inputs"]["nag_tau"] == 2.37
        assert sampler["inputs"]["nag_alpha"] == 0.25
        assert sampler["inputs"]["nag_sigma_end"] == 0.0
    assert rendered["11"]["inputs"]["latent_image"] == ["10", 2]
    assert template["11"]["class_type"] == "KSamplerAdvanced"
    assert template["12"]["class_type"] == "KSamplerAdvanced"
    assert template["10"]["class_type"] == "WanImageToVideo"
    assert "end_image" not in template["10"]["inputs"]


def test_face_fidelity_anchors_are_idempotent_and_off_is_byte_compatible() -> None:
    enabled = GenerationSettings(face_fidelity="stable_expression")
    positive = effective_positive_prompt("subtle body movement", enabled)
    negative = effective_negative_prompt("jitter", enabled)
    assert effective_positive_prompt(positive, enabled) == positive
    assert effective_negative_prompt(negative, enabled) == negative

    disabled = GenerationSettings(face_fidelity="off")
    assert effective_positive_prompt("subtle body movement", disabled) == ("subtle body movement")
    assert effective_negative_prompt("jitter", disabled) == "jitter"

    template = load_workflow_template(WORKFLOW)
    rendered, _seed, _prefix = render_workflow(
        template,
        input_filename="source.png",
        positive_prompt="subtle body movement",
        negative_prompt="jitter",
        settings=GenerationSettings(seed=42, face_fidelity="off"),
        job_id=UUID("00000000-0000-0000-0000-000000000001"),
        attempt_id=UUID("00000000-0000-0000-0000-000000000002"),
    )
    assert "end_image" not in rendered["10"]["inputs"]
    assert rendered["11"]["class_type"] == "KSamplerAdvanced"
    assert rendered["12"]["class_type"] == "KSamplerAdvanced"
    canonical = json.dumps(rendered, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == (
        "c0c9d3f9dd99519e2f52772a35affb2dbc72535142e07b934a918b2f657023dc"
    )


def test_face_fidelity_rejects_unreviewed_conditioning_topology() -> None:
    template = load_workflow_template(WORKFLOW)
    template["10"]["inputs"]["start_image"] = ["unreviewed", 0]

    with pytest.raises(WorkflowError, match="conditioning topology"):
        render_workflow(
            template,
            input_filename="source.png",
            positive_prompt="subtle body movement",
            negative_prompt="jitter",
            settings=GenerationSettings(face_fidelity="stable_expression"),
            job_id=uuid4(),
            attempt_id=uuid4(),
        )


def test_reviewed_loras_chain_each_stage_before_sampling_and_inject_triggers_once() -> None:
    template = load_workflow_template(WORKFLOW)
    rendered, _seed, _prefix = render_workflow(
        template,
        input_filename="source.png",
        positive_prompt="NSFWSKS, slow movement, nsfwsks",
        negative_prompt="jitter",
        settings=GenerationSettings(
            seed=42,
            loras=[
                {"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3},
                {"catalog_id": "bouncing-boobs-wan22", "strength": 1.0},
            ],
        ),
        job_id=uuid4(),
        attempt_id=uuid4(),
    )

    assert rendered["6"]["inputs"]["text"].casefold().count("nsfwsks") == 1
    assert rendered["6"]["inputs"]["text"].endswith("her breasts are bouncing")
    for branch, loader_node, first_model, last_sampling in (
        ("high", "lora-high", "2", "8"),
        ("low", "lora-low", "3", "9"),
    ):
        first = rendered[f"{loader_node}-1"]
        second = rendered[f"{loader_node}-2"]
        assert first["class_type"] == second["class_type"] == "LoraLoaderModelOnly"
        assert first["inputs"]["model"] == [first_model, 0]
        assert second["inputs"]["model"] == [f"{loader_node}-1", 0]
        assert rendered[last_sampling]["inputs"]["model"] == [f"{loader_node}-2", 0]
        entries = tuple(LORA_CATALOG.values())
        expected = entries[0].high if branch == "high" else entries[0].low
        assert first["inputs"]["lora_name"] == expected.filename
        assert first["inputs"]["strength_model"] == 0.3
    provenance = lora_provenance(
        GenerationSettings(loras=[{"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3}])
    )[0]
    assert provenance["high"] == {
        "role": "lora_wan_general_nsfw_high",
        "filename": "NSFW-22-H-e8.safetensors",
        "byte_size": 613_516_752,
        "sha256": "34e2144d3cd65360f97d09ccbe03e1c39a096df6c9234af5fe3899d1b63cda39",
        "civitai_model_id": 1_307_155,
        "civitai_version_id": 2_073_605,
        "civitai_file_id": 1_969_798,
        "canonical_version_url": ("https://civitai.com/models/1307155?modelVersionId=2073605"),
    }
    assert provenance["creator_name"] == "CubeyAI"
    assert provenance["canonical_source_url"] == "https://civitai.com/models/1307155"


def test_three_reviewed_loras_chain_both_model_stages() -> None:
    template = load_workflow_template(WORKFLOW)
    catalog_ids = list(LORA_CATALOG)[:3]
    settings = GenerationSettings(
        loras=[{"catalog_id": catalog_id, "strength": 0.25} for catalog_id in catalog_ids]
    )

    rendered, _seed, _prefix = render_workflow(
        template,
        input_filename="source.png",
        positive_prompt="controlled conservative motion",
        negative_prompt="jitter",
        settings=settings,
        job_id=uuid4(),
        attempt_id=uuid4(),
    )

    for branch, base_model, sampling_node in (
        ("high", "2", "8"),
        ("low", "3", "9"),
    ):
        previous_model: list[object] = [base_model, 0]
        for index, catalog_id in enumerate(catalog_ids, start=1):
            node_id = f"lora-{branch}-{index}"
            node = rendered[node_id]
            entry = LORA_CATALOG[catalog_id]
            artifact = entry.high if branch == "high" else entry.low
            assert node["class_type"] == "LoraLoaderModelOnly"
            assert node["inputs"] == {
                "model": previous_model,
                "lora_name": artifact.filename,
                "strength_model": 0.25,
            }
            previous_model = [node_id, 0]
        assert rendered[sampling_node]["inputs"]["model"] == previous_model
        assert f"lora-{branch}-4" not in rendered

    assert len(lora_provenance(settings)) == 3


@pytest.mark.parametrize("selection_count", [4, 5])
def test_four_and_five_reviewed_loras_are_rejected_before_rendering(
    selection_count: int,
) -> None:
    catalog_ids = list(LORA_CATALOG)[:selection_count]
    with pytest.raises(ValidationError):
        GenerationSettings(
            loras=[{"catalog_id": catalog_id, "strength": 0.25} for catalog_id in catalog_ids]
        )


def test_manual_and_triggerless_loras_do_not_mutate_the_author_prompt() -> None:
    template = load_workflow_template(WORKFLOW)
    prompt = "controlled natural motion"
    rendered, _seed, _prefix = render_workflow(
        template,
        input_filename="source.png",
        positive_prompt=prompt,
        negative_prompt="jitter",
        settings=GenerationSettings(
            loras=[
                {"catalog_id": "dr34ml4y-aio-nsfw-wan22-v2", "strength": 0.7},
                {"catalog_id": "smoothmix-xxx-animations-wan22", "strength": 1.0},
            ]
        ),
        job_id=uuid4(),
        attempt_id=uuid4(),
    )

    assert rendered["6"]["inputs"]["text"] == prompt


def test_dream_lora_rejects_multiple_mutually_exclusive_concept_terms() -> None:
    template = load_workflow_template(WORKFLOW)
    settings = GenerationSettings(
        loras=[{"catalog_id": "dr34ml4y-aio-nsfw-wan22-v2", "strength": 0.7}]
    )

    with pytest.raises(
        WorkflowError,
        match="mutually exclusive concept terms",
    ):
        render_workflow(
            template,
            input_filename="source.png",
            positive_prompt="M15510N4RY motion followed by bl0wj0b motion",
            negative_prompt="jitter",
            settings=settings,
            job_id=uuid4(),
            attempt_id=uuid4(),
        )


def test_comfy_command_uses_supported_base_directory_and_only_pinned_nag(tmp_path: Path) -> None:
    command = _comfy_command(_settings(tmp_path))

    assert command[command.index("--base-directory") + 1] == (tmp_path / "comfy").as_posix()
    assert "--models-directory" not in command
    assert "--disable-all-custom-nodes" in command
    assert command[command.index("--whitelist-custom-nodes") + 1] == "ComfyUI-NAG"
    assert "--highvram" not in command
    assert command[command.index("--reserve-vram") + 1] == "4"


def test_image_is_model_free_pinned_and_non_root() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    pytorch_image = (
        "pytorch/pytorch:2.9.1-cuda12.8-cudnn9-runtime@"
        "sha256:7b324d212a4450795b49edba9949b7cdc72429148a64e974334bfe5774d51385"
    )

    assert from_lines == [f"FROM {pytorch_image}"]
    assert all("@sha256:" in line for line in from_lines)
    assert "c2bcbecd82ec5ae66594340b395c24ef0217b238" in dockerfile
    assert "ef8a641be08983cf5f06669f70719b6eecce3c7f" in dockerfile
    assert "https://github.com/ChenDarYen/ComfyUI-NAG.git" in dockerfile
    assert "ARG COMFYUI_NAG_COMMIT=ef8a641be08983cf5f06669f70719b6eecce3c7f" in dockerfile
    assert (
        'LABEL org.opencontainers.image.comfyui-nag.revision="${COMFYUI_NAG_COMMIT}"' in dockerfile
    )
    nag_patch = NAG_PATCH.read_text(encoding="utf-8")
    assert hashlib.sha256(NAG_PATCH.read_bytes()).hexdigest() in dockerfile
    assert nag_patch.count("+from comfy.ldm.flux.layers import") == 2
    assert nag_patch.count("-from comfy.ldm.chroma.layers import") == 2
    assert "spec_from_file_location('comfyui_nag'" in dockerfile
    assert "sys.argv=['comfyui-build-check','--cpu']" in dockerfile
    assert "comfy.options.enable_args_parsing()" in dockerfile
    assert "'KSamplerWithNAG (Advanced)' in m.NODE_CLASS_MAPPINGS" in dockerfile
    heavyweight_runtime_end = dockerfile.index(
        "grep -Fq 'class WanImageToVideo' /opt/comfyui/comfy_extras/nodes_wan.py"
    )
    nag_install = dockerfile.index("ARG COMFYUI_NAG_COMMIT=")
    assert nag_install > heavyweight_runtime_end
    assert "COMFYUI_NAG" not in dockerfile[:heavyweight_runtime_end]
    assert "USER 0:0" in dockerfile
    assert "COPY --chmod=0555 scripts/i2v-runpod-entrypoint.sh" in dockerfile
    entrypoint = (ROOT / "scripts/i2v-runpod-entrypoint.sh").read_text(encoding="utf-8")
    assert "stat -c '%d' \"$volume_root\"" in entrypoint
    assert 'find "$namespace" -xdev' in entrypoint
    assert '--reuid "$worker_uid"' in entrypoint
    assert '--regid "$worker_gid"' in entrypoint
    assert "--no-new-privs" in entrypoint
    assert "COPY i2v-models" not in dockerfile
    detector_urls = {
        (
            "https://huggingface.co/hysts/anime-face-detector-yolov3/resolve/"
            "afdd4226a79ae8bb81f334dbcffd34f8cc000c38/model.safetensors"
        ),
        (
            "https://huggingface.co/hysts/anime-face-detector-hrnetv2/resolve/"
            "9b3435248b26aeb82e2a8578fe9d86d5d57158af/model.safetensors"
        ),
    }
    generation_model_urls = dockerfile
    for detector_url in detector_urls:
        assert detector_url in generation_model_urls
        generation_model_urls = generation_model_urls.replace(detector_url, "")
    assert not re.search(r"(?:civitai|huggingface)\.com/.+safetensors", generation_model_urls)
    for identity in (
        "7db835de7a3a052eb4d68d241ae9f2cf28a0b509",
        "9a6a8c1384b7a57fab8ce9988f814271ff88bac52a9dd871490a28b61dff7692",
        "23bbc708146bcbc1c910f00fe152adbc70d7658d875a0121eaf4ee61d978b2c4",
        "e71271376406a743c01528a0460637fcc06e72aeeea583f85007cc72dc8b7a4a",
        "211e581f5a4670acbbe08fff36a35e9946039d2eea28b80394632d036d1be527",
    ):
        assert identity in dockerfile
    detector_import = "from anime_face_detector.detector import LandmarkDetector"
    assert detector_import in dockerfile
    face_stabilizer = FACE_STABILIZER.read_text(encoding="utf-8")
    assert detector_import in face_stabilizer
    assert "from anime_face_detector import LandmarkDetector" not in face_stabilizer
    assert "device='cpu'" in dockerfile
    assert "assert len(result)==1" in dockerfile
    assert "assert float(confidence.min())>=0.60" in dockerfile
    assert "assert float(confidence.mean())>=0.85" in dockerfile
    assert "static-source-head-single-blink-v2" in dockerfile
    assert "COPY THIRD_PARTY_LICENSES.md /opt/i2v/licenses/THIRD_PARTY_LICENSES.md" in dockerfile
    face_lock = FACE_LOCK.read_text(encoding="utf-8")
    assert "opencv-python-headless" in face_lock
    assert "211e581f5a4670acbbe08fff36a35e9946039d2eea28b80394632d036d1be527" in face_lock
    notices = FACE_NOTICES.read_text(encoding="utf-8")
    for identity in (
        "7db835de7a3a052eb4d68d241ae9f2cf28a0b509",
        "23bbc708146bcbc1c910f00fe152adbc70d7658d875a0121eaf4ee61d978b2c4",
        "e71271376406a743c01528a0460637fcc06e72aeeea583f85007cc72dc8b7a4a",
        "211e581f5a4670acbbe08fff36a35e9946039d2eea28b80394632d036d1be527",
    ):
        assert identity in notices
    assert "gen_automation.i2v_worker.runpod_handler" in entrypoint
    assert "gen_automation.i2v_worker.runpod_pod_api" in entrypoint
    assert "GEN_I2V_WORKER_RUNPOD_MODE:-serverless" in entrypoint
    handler = RUNPOD_HANDLER.read_text(encoding="utf-8")
    assert '"schema": "gen-automation/i2v-runpod-runtime/v1"' in handler
    for metric in (
        "worker_reused",
        "volume_bootstrap_ms",
        "worker_startup_ms",
        "generation_ms",
        "total_handler_ms",
    ):
        assert f'"{metric}"' in handler
    assert 'org.opencontainers.image.runpod-sdk.version="1.11.0"' in dockerfile
    assert "--system-site-packages" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--no-deps" in dockerfile
    assert "sys.version_info[:2] == (3, 11)" in dockerfile
    assert "openssl-3.6.3-h35e630c_1.conda" in dockerfile
    assert "012096056b97abf1f68c46b7146bd2cbd68c1be762340b4f5dad4fbbe99177bc" in dockerfile
    assert "openssl version | awk" in dockerfile
    lock = LOCK.read_text(encoding="utf-8")
    assert "--python-version 3.11" in lock
    assert "numpy==2.4.6" in lock
    assert "scipy==1.17.1" in lock
    for package in ("torch==2.9.1", "torchvision==0.24.1", "torchaudio==2.9.1"):
        assert package in lock
    assert "runpod==1.11.0" in lock
    assert "'/opt/i2v-venv/' not in module.__file__" in dockerfile
    assert "salad-http-job-queue-worker" not in dockerfile
    assert "strict-http-status.patch" not in dockerfile


def test_ci_builds_smokes_and_scans_the_model_free_worker() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "docker build" in workflow
    assert "--file Dockerfile.i2v-worker" in workflow
    assert "--tag gen-automation-i2v-worker:test" in workflow
    assert "import gen_automation.i2v_worker.runpod_handler" in workflow
    assert "import gen_automation.i2v_worker.runpod_pod_api" in workflow
    assert "image: gen-automation-i2v-worker:test" in workflow
    assert "output-file: i2v-worker.spdx.json" in workflow
    assert "sbom: i2v-worker.spdx.json" in workflow
