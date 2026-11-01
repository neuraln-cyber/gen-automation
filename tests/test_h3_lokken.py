import json
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from gen_automation.i2v_worker.h3_lokken import (
    LOKKEN_UPSCALER_ROLE,
    lokken_geometry,
    megapixel_canvas,
)
from gen_automation.i2v_worker.h3_variants import (
    H3_LOKKEN_MODELS,
    h3_job_matches_variant,
    h3_lokken_models_match,
)
from gen_automation.i2v_worker.media import resolve_generation_settings
from gen_automation.i2v_worker.models import GenerationSettings
from gen_automation.i2v_worker.settings import I2VWorkerSettings
from gen_automation.i2v_worker.workflow import load_workflow_template, render_workflow
from gen_automation.services.i2v import _normalized_settings
from gen_automation.services.i2v_runtime import _worker_settings_snapshot

ROOT = Path(__file__).parents[1]


def settings(**updates):
    return GenerationSettings(profile="minimax_h3", h3_model_variant="eros_beta7_lokken", **updates)


def graph(value=None):
    return render_workflow(
        load_workflow_template(
            ROOT / "workflows/minimax-h3-eros-ref2va.api.json", profile="minimax_h3"
        ),
        input_filename="original.png",
        positive_prompt="<Picture 1> in a breeze.",
        negative_prompt="not sampled at CFG1",
        settings=value or settings(seed=42),
        job_id=UUID(int=1),
        attempt_id=UUID(int=2),
        model_paths={role: identity[2] for role, identity in H3_LOKKEN_MODELS.items()},
    )[0]


def test_author_recipe_topology_and_not_legacy_tiled_upscale():
    value = settings(
        seed=42,
        h3_loras=[
            {"artifact_id": UUID(int=3), "sha256": "a" * 64, "strength": 0.35},
        ],
    )
    workflow = graph(value)
    assert workflow["7"]["inputs"]["model"] == ["h3-lora-0", 0]
    assert workflow["lokken-vsa"]["inputs"]["model"] == ["7", 0]
    assert workflow["lokken-kitchen"]["inputs"]["model"] == ["lokken-vsa", 0]
    assert workflow["lokken-separate"]["inputs"]["av_latent"] == ["12", 1]
    assert workflow["lokken-concat"]["inputs"]["audio_latent"] == ["lokken-separate", 1]
    assert workflow["15"]["inputs"]["samples"] == ["lokken-separate", 1]
    assert workflow["13"]["inputs"]["samples"] == ["lokken-refine", 0]
    assert workflow["12"]["inputs"]["noise"] == workflow["lokken-refine"]["inputs"]["noise"]
    assert workflow["12"]["inputs"]["sampler"] == workflow["lokken-refine"]["inputs"]["sampler"]
    assert workflow["lokken-base-guide"]["inputs"]["image"] == ["1", 0]
    assert workflow["lokken-refine-guide"]["inputs"]["image"] == ["1", 0]
    assert workflow["lokken-refine-guide"]["inputs"]["latent"] == ["lokken-concat", 0]
    assert workflow["lokken-refine-guide"]["inputs"]["positive"] == ["6", 0]
    assert workflow["lokken-refine-sigmas"]["inputs"]["steps"] == 3
    assert workflow["lokken-refine-sigmas"]["inputs"]["denoise"] == 0.3
    assert workflow["lokken-upscale"]["inputs"]["force_unload"]
    assert workflow["lokken-upscale"]["inputs"]["enable_temporal_chunking"]
    assert workflow["lokken-rtx"]["inputs"]["image_encoding"] == "10-bit RGB (RGB10A2)"
    assert workflow["lokken-post"]["inputs"]["encoder_preset"] == "medium"
    assert workflow["lokken-post"]["inputs"]["grain_intensity"] == 0.005
    assert workflow["lokken-post"]["inputs"]["crf"] == 18
    assert workflow["14"]["inputs"]["video"] == ["lokken-post", 0]
    assert not {"h3-source-upscale", "h3-first-frame", "h3-base-save", "16"} & workflow.keys()
    assert workflow["6"]["inputs"]["prompt"] == "<Picture 1> in a breeze."


@pytest.mark.parametrize(
    "width,height,expected",
    [
        (4, 3, (640, 480)),
        (16, 9, (736, 416)),
        (1, 1, (576, 576)),
    ],
)
def test_resolution_matches_authors_table(width, height, expected):
    assert megapixel_canvas(width, height, 0.3) == expected


def test_two_stage_rounding_and_rtx_output_follow_actual_base_aspect():
    value = settings(width=640, height=480)
    sizes = lokken_geometry(value)
    assert (sizes.base_width, sizes.base_height) == (640, 480)
    assert (sizes.refine_width, sizes.refine_height) == (1184, 896)
    assert (sizes.output_width, sizes.output_height) == (1424, 1072)
    portrait = resolve_generation_settings(
        settings(match_source_aspect=True), source_width=810, source_height=1440
    )
    assert lokken_geometry(portrait) == lokken_geometry(settings(width=416, height=736))


def test_legacy_snapshots_and_new_recipe_round_trip_without_silent_migration():
    legacy = GenerationSettings(profile="minimax_h3", h3_model_variant="eros_beta7")
    assert legacy.h3_lokken is None and "h3_lokken" not in legacy.model_dump(mode="json")
    original = settings(steps=10, h3_lokken={"base_megapixels": 0.5, "target_megapixels": 1.5})
    wire = _worker_settings_snapshot(_normalized_settings(original.model_dump(mode="json")))
    assert GenerationSettings.model_validate(wire) == original
    assert not h3_job_matches_variant(legacy.model_dump(), "eros_beta7_lokken")


@pytest.mark.parametrize(
    "updates",
    [
        {"h3_lokken": None},
        {"h3_lokken": {"base_megapixels": float("nan")}},
        {"h3_lokken": {"target_megapixels": 16}},
        {"match_source_resolution": True},
        {"h3_save_base_video": True},
        {"cfg": 2},
        {"h3_refine_cfg": 2},
        {"h3_image_mode": "reference"},
        {"h3_attention_backend": "default"},
        {"h3_latent_contrast": 0.9},
        {"h3_refine_sampler": "euler"},
    ],
)
def test_unreviewed_combinations_fail_closed(updates):
    with pytest.raises(ValidationError):
        settings(**updates)


def test_full_model_identity_and_missing_support_are_rejected():
    manifest = {
        role: {"sha256": sha, "bytes": size, "target_filename": name}
        for role, (sha, size, name) in H3_LOKKEN_MODELS.items()
    }
    assert h3_lokken_models_match(manifest)
    folders = {
        "diffusion_model": "diffusion_models",
        "text_encoder": "text_encoders",
        "video_vae": "vae",
        "audio_vae": "vae",
        LOKKEN_UPSCALER_ROLE: "latent_upscale_models",
    }
    objects = [
        {
            "role": role,
            "bucket": "models",
            "key": f"worker/i2v/sha256/{sha}",
            "version_id": "immutable",
            "sha256": sha,
            "byte_size": size,
            "install_path": f"models/{folders[role]}/{name}",
        }
        for role, (sha, size, name) in H3_LOKKEN_MODELS.items()
    ]
    config = dict(
        profile="minimax_h3", source_revision="a" * 40, private_manifest_source_sha256="b" * 64
    )
    worker = I2VWorkerSettings(model_objects_json=SecretStr(json.dumps(objects)), **config)
    assert worker.h3_model_variant == "eros_beta7_lokken"
    from gen_automation.i2v_worker.settings import H3_LOKKEN_CUSTOM_NODES
    from gen_automation.i2v_worker.supervisor import _comfy_command

    command = _comfy_command(worker)
    allowed = command[
        command.index("--whitelist-custom-nodes") + 1 : command.index("--disable-api-nodes")
    ]
    assert allowed == H3_LOKKEN_CUSTOM_NODES
    assert "GenAutomationH3Contrast" not in allowed
    with pytest.raises(ValidationError):
        I2VWorkerSettings(model_objects_json=SecretStr(json.dumps(objects[:-1])), **config)
    objects[1]["sha256"] = "c" * 64
    objects[1]["key"] = "worker/i2v/sha256/" + "c" * 64
    with pytest.raises(ValidationError, match="exact reviewed HQ"):
        I2VWorkerSettings(model_objects_json=SecretStr(json.dumps(objects)), **config)


def test_native_delivery_copies_exact_lokken_video_and_audio(tmp_path, monkeypatch):
    from gen_automation.i2v_worker import media

    calls = []
    value = settings(width=640, height=480)
    monkeypatch.setattr(media.subprocess, "run", lambda command, **kwargs: calls.append(command))

    def probe(path, generation, *, width, height, frame_count):
        assert generation == value
        assert (width, height, frame_count) == (1424, 1072, 124)
        return {"width": width, "height": height}

    monkeypatch.setattr(media, "_probe_video", probe)
    _, metadata = media.finalize_native_video(
        (tmp_path / "lokken.mp4",),
        value,
        tmp_path,
        source_width=640,
        source_height=480,
    )
    command = calls[0]
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "copy"
    assert "-crf" not in command and "-vf" not in command
    assert (metadata["native_width"], metadata["native_height"]) == (640, 480)
    assert metadata["upscale"] == "lokken_3d_refine_rtx"


def test_lokken_dashboard_describes_the_actual_recipe_and_source_access(client):
    config = client.app.state.settings
    config.i2v_profile = "minimax_h3"
    config.i2v_h3_model_variant = "eros_beta7_lokken"
    config.i2v_h3_advanced_sampling_enabled = True
    page = client.get("/dashboard/animations").text
    assert "Lokken HQ" in page
    assert 'name="lokken_base_megapixels" type="number" value="0.3"' in page
    assert 'name="lokken_target_megapixels" type="number" value="1"' in page
    assert 'name="lokken_grain_strength" type="number" value="0.005"' in page
    assert "First-pass audio" in page
    assert "not seed-controlled" in page
    assert "GPL/AGPL" in page and "Exact node source and licenses" in page
    assert 'name="h3_save_base_video"' not in page
    assert 'name="h3_latent_contrast"' not in page
    assert "optional extension" not in page


def test_browser_lokken_fields_roundtrip_and_geometry_match_worker():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node needed for actual browser function contracts")
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const src=fs.readFileSync('src/gen_automation/static/i2v.js','utf8');
const defaults={base_megapixels:.3,target_megapixels:1,rtx_scale:1.2,
 grain_strength:.005,encoder_preset:'medium'};
const elements={width:{value:'640'},height:{value:'480'},
 match_source_aspect:{type:'checkbox',checked:false},
 match_source_resolution:{type:'checkbox',checked:false}};
Object.entries(defaults).forEach(([k,v])=>elements['lokken_'+k]={value:String(v)});
Object.entries(elements).forEach(([k,v])=>v.name=k);
const summary={},state={selected:{sourceWidth:810,sourceHeight:1440},
 loraSelections:new Map(),loraCatalog:[]};
const ctx=vm.createContext({lokken:true,isH3:true,erosBeta7:true,
 h3ModelVariant:'eros_beta7_lokken',h3AdvancedSamplingEnabled:true,
 lokkenDefaults:defaults,workerSettingDefaults:{profile:'minimax_h3',
 h3_image_mode:'first_frame',h3_attention_backend:'comfy_kitchen'},
 state,form:{elements},CSS:{escape:v=>v},q:()=>summary,
 advanced:{querySelectorAll:()=>Object.values(elements),
 querySelector:s=>elements[s.match(/name="([^"]+)"/)?.[1]]},
 root:{querySelector:()=>null},loraList:{querySelectorAll:()=>[]},
 loraWriteBlocked:()=>false,setLoraSelections:()=>{},updateDuration:()=>{}});
vm.runInContext(src.slice(src.indexOf('  function collectSettings('),
 src.indexOf('  function setLoraSelections(')),ctx);
vm.runInContext('syncAspectControls()',ctx);
assert.match(summary.textContent,/Base 640 \u00d7 480/);
assert.match(summary.textContent,/RTX output 1424 \u00d7 1072/);
let value=vm.runInContext('collectSettings()',ctx);
assert.equal(value.h3_lokken.encoder_preset,'medium');
assert.equal(value.h3_image_mode,'first_frame');
assert.equal(value.h3_model_variant,'eros_beta7_lokken');
assert.equal(value.h3_lokken.target_megapixels,1);
assert.equal(value.lokken_base_megapixels,undefined);
ctx.saved={width:640,height:480,h3_lokken:{...defaults,grain_strength:0,encoder_preset:'slow'}};
vm.runInContext('applySettings(saved)',ctx);
value=vm.runInContext('collectSettings()',ctx);
assert.equal(value.h3_lokken.grain_strength,0);
assert.equal(value.h3_lokken.encoder_preset,'slow');
elements.match_source_aspect.checked=true;
vm.runInContext('syncAspectControls()',ctx);
assert.equal(elements.width.value,'810');assert.equal(elements.height.value,'1440');
assert.match(summary.textContent,/RTX output 920 \u00d7 1648/);
console.log(JSON.stringify(vm.runInContext('collectSettings()',ctx)));
"""
    result = subprocess.run(  # noqa: S603 - fixed local test
        [node, "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    collected = json.loads(result.stdout)
    sizes = lokken_geometry(GenerationSettings.model_validate(collected))
    assert (sizes.output_width, sizes.output_height) == (920, 1648)


@pytest.mark.parametrize("mp", [0.3, 0.4, 0.5])
@pytest.mark.parametrize("source", [(128, 288), (128, 352), (128, 416), (810, 1440), (1987, 1093)])
def test_source_aspect_is_rounded_once_not_rescaled_from_a_rounded_base(mp, source):
    original = settings(match_source_aspect=True, h3_lokken={"base_megapixels": mp})
    resolved = resolve_generation_settings(
        original, source_width=source[0], source_height=source[1]
    )
    assert (resolved.width, resolved.height) == source
    expected = megapixel_canvas(*source, mp)
    sizes = lokken_geometry(resolved)
    assert (sizes.base_width, sizes.base_height) == expected
    assert (
        resolve_generation_settings(resolved, source_width=source[0], source_height=source[1])
        == resolved
    )
    assert graph(resolved)["6"]["inputs"]["width"] == expected[0]
    assert graph(resolved)["6"]["inputs"]["height"] == expected[1]


def test_source_artifact_catalog_matches_all_five_immutable_tuples():
    catalog = json.loads((ROOT / "i2v-models/eros-beta7-lokken.sources.json").read_text())
    entries = {item["role"]: item for item in catalog["sources"]}
    assert set(entries) == set(H3_LOKKEN_MODELS)
    for role, identity in H3_LOKKEN_MODELS.items():
        entry = entries[role]
        assert (entry["sha256"], entry["expected_bytes"], entry["target_filename"]) == identity
        assert entry["url"].startswith("https://huggingface.co/")
        assert not entry["optional"]


async def test_lokken_readiness_never_requires_disabled_legacy_plugins():
    from gen_automation.i2v_worker.comfy import ComfyClient

    client = ComfyClient(
        base_url="http://127.0.0.1:8188",
        request_timeout_seconds=1,
        network_attempts=1,
        poll_seconds=0.01,
        profile="minimax_h3",
        h3_model_variant="eros_beta7_lokken",
        source_resolution_enabled=True,
    )
    try:
        names = {name for name, _ in client.required_nodes}
        assert {
            "MinimaxH3LatentUpscaler3D",
            "LokkenVideoPostProcessing",
            "RTXVideoSuperResolution",
            "BlockSparseAttention",
        } <= names
        assert not any(name.startswith("MMH3") for name in names)
        assert not {"MiniMaxH3LatentContrast", "ManagedH3DiagnosticDecode"} & names
    finally:
        await client.close()


async def test_controller_checks_lokken_geometry_recipe_and_immutable_media():
    import hashlib

    from gen_automation.domain.i2v import I2VAttemptSnapshot, I2VJobSnapshot, I2VOutputRegistration
    from gen_automation.i2v_worker.h3_lokken import LOKKEN_RECIPE
    from gen_automation.services.i2v_media import I2VMediaConflictError, I2VSignedGrantBuilder
    from gen_automation.storage.memory import MemoryObjectStore

    store = MemoryObjectStore(bucket="private")
    store.backend = "s3"
    value = settings(match_source_aspect=True, h3_lokken={"base_megapixels": 0.4})
    job = I2VJobSnapshot.model_construct(
        job_id=UUID(int=1),
        request_sha256="a" * 64,
        input_snapshot={"width": 128, "height": 288},
        settings_snapshot=value.model_dump(mode="json"),
    )
    attempt = I2VAttemptSnapshot.model_construct(attempt_id=UUID(int=2))
    key = f"i2v/outputs/{job.job_id}/{attempt.attempt_id}.mp4"
    body = b"synthetic-output-contract"
    store.put_for_test(
        key,
        body,
        content_type="video/mp4",
        metadata={
            "i2v-job-id": str(job.job_id),
            "i2v-attempt-id": str(attempt.attempt_id),
            "request-sha256": job.request_sha256,
        },
    )
    stored = await store.head(key)
    geometry = lokken_geometry(
        resolve_generation_settings(value, source_width=128, source_height=288)
    )
    output = I2VOutputRegistration(
        storage_backend="s3",
        storage_bucket="private",
        object_key=key,
        object_version_id=stored.version_id,
        sha256=hashlib.sha256(body).hexdigest(),
        content_type="video/mp4",
        width=geometry.output_width,
        height=geometry.output_height,
        frame_count=124,
        fps=24,
        duration_ms=5167,
        byte_size=len(body),
        metadata={"workflow": LOKKEN_RECIPE},
    )
    builder = I2VSignedGrantBuilder(store=store, expires_in=300)
    await builder.verify_output(job=job, attempt=attempt, output=output)
    for change in (
        {"width": output.width + 8},
        {"height": output.height + 8},
        {"frame_count": 158},
        {"fps": 30},
        {"metadata": {"workflow": "other"}},
        {"sha256": "b" * 64},
    ):
        with pytest.raises(I2VMediaConflictError):
            await builder.verify_output(
                job=job, attempt=attempt, output=output.model_copy(update=change)
            )


def test_license_source_link_is_bound_to_the_worker_revision(client):
    config = client.app.state.settings
    config.i2v_profile = "minimax_h3"
    config.i2v_h3_model_variant = "eros_beta7_lokken"
    config.i2v_worker_source_revision = "b" * 40
    html = client.get("/dashboard/animations").text
    assert "https://github.com/neuraln-cyber/gen-automation/tree/" + "b" * 40 in html
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in (ROOT / "licenses/AGPL-3.0-only.txt").read_text()


@pytest.mark.parametrize(
    "dockerfile",
    [
        "Dockerfile",
        "Dockerfile.mega",
        "Dockerfile.semantic-gateway",
        "Dockerfile.patreon-browser",
        "Dockerfile.i2v-worker",
    ],
)
def test_every_image_containing_the_recipe_ships_its_license_and_notices(dockerfile):
    text = (ROOT / dockerfile).read_text()
    assert "COPY THIRD_PARTY_LICENSES.md " in text
    assert "COPY licenses/AGPL-3.0-only.txt " in text
