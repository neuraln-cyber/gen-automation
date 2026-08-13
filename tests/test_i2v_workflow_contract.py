import hashlib
import json
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / "workflows" / "dasiwa-wan22-i2v-v1.api.json"
BINDINGS_PATH = ROOT / "workflows" / "dasiwa-wan22-i2v-v1.bindings.json"
MANIFEST_PATH = ROOT / "i2v-models" / "dasiwa-wan22-i2v-v1.json"
PROVENANCE_PATH = ROOT / "docs" / "i2v-workflow-provenance.md"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bindings_by_path(bindings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = bindings["bindings"]
    assert isinstance(rows, list)
    indexed = {row["path"]: row for row in rows}
    assert len(indexed) == len(rows), "binding paths must be unique"
    return indexed


def _walk_placeholders(value: Any, location: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        if set(value) == {"$i2v"}:
            path = value["$i2v"]
            assert isinstance(path, str) and path
            return [(location, path)]
        found: list[tuple[str, str]] = []
        for key, child in value.items():
            found.extend(_walk_placeholders(child, f"{location}/{key}"))
        return found
    if isinstance(value, list):
        found = []
        for index, child in enumerate(value):
            found.extend(_walk_placeholders(child, f"{location}/{index}"))
        return found
    return []


def _expand(value: Any, resolved: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$i2v"}:
            return resolved[value["$i2v"]]
        return {key: _expand(child, resolved) for key, child in value.items()}
    if isinstance(value, list):
        return [_expand(child, resolved) for child in value]
    return value


def _json_pointer(value: Any, pointer: str) -> Any:
    assert pointer.startswith("/")
    current = value
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def test_contract_documents_are_valid_and_content_addressed() -> None:
    workflow = _load(WORKFLOW_PATH)
    bindings = _load(BINDINGS_PATH)
    manifest = _load(MANIFEST_PATH)

    assert bindings["schema_version"] == 1
    assert bindings["workflow_id"] == manifest["id"] == "dasiwa-wan22-i2v-v1"
    assert bindings["template"] == manifest["workflow"]["api_template"]
    assert bindings["placeholder_key"] == "$i2v"
    assert workflow

    assert manifest["workflow"]["workflow_template_sha256"] == _sha256(WORKFLOW_PATH)
    assert manifest["workflow"]["bindings_sha256"] == _sha256(BINDINGS_PATH)
    assert manifest["workflow"]["bindings"] == ("workflows/dasiwa-wan22-i2v-v1.bindings.json")


def test_graph_is_a_minimal_native_comfyui_prompt() -> None:
    workflow = _load(WORKFLOW_PATH)
    class_counts = Counter(node["class_type"] for node in workflow.values())
    assert class_counts == Counter(
        {
            "LoadImage": 1,
            "UNETLoader": 2,
            "CLIPLoader": 1,
            "VAELoader": 1,
            "CLIPTextEncode": 2,
            "ModelSamplingSD3": 2,
            "WanImageToVideo": 1,
            "KSamplerAdvanced": 2,
            "VAEDecode": 1,
            "SaveImage": 1,
        }
    )

    forbidden_fragments = (
        "DaSiWa",
        "GGUF",
        "Sage",
        "Triton",
        "NAGuidance",
        "LoraLoader",
        "VideoCombine",
        "FrameInterpolate",
        "Upscale",
        "Watermark",
    )
    assert not any(
        fragment in node["class_type"]
        for node in workflow.values()
        for fragment in forbidden_fragments
    )

    node_ids = set(workflow)
    assert node_ids == {str(index) for index in range(1, 15)}
    for node_id, node in workflow.items():
        assert set(node) == {"class_type", "inputs"}, node_id
        assert isinstance(node["inputs"], dict)
        for value in node["inputs"].values():
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and isinstance(value[1], int)
            ):
                assert value[0] in node_ids
                assert value[1] >= 0


def test_every_placeholder_has_exact_declared_targets_and_defaults_expand() -> None:
    workflow = _load(WORKFLOW_PATH)
    contract = _load(BINDINGS_PATH)
    indexed = _bindings_by_path(contract)
    postprocess = contract["postprocess"]

    occurrences = _walk_placeholders(workflow, "workflow") + _walk_placeholders(
        postprocess, "postprocess"
    )
    assert {path for _, path in occurrences} == set(indexed)

    declared_targets: dict[str, set[str]] = {}
    for path, row in indexed.items():
        targets: set[str] = set()
        for target in row["targets"]:
            if target["document"] == "workflow":
                node_id = target["node_id"]
                input_name = target["input"]
                expected = {"$i2v": path}
                assert workflow[node_id]["inputs"][input_name] == expected
                targets.add(f"workflow/{node_id}/inputs/{input_name}")
            else:
                assert target["document"] == "postprocess"
                pointer = target["json_pointer"]
                assert _json_pointer(postprocess, pointer) == {"$i2v": path}
                targets.add(f"postprocess{pointer}")
        declared_targets[path] = targets

    actual_targets: dict[str, set[str]] = {path: set() for path in indexed}
    for location, path in occurrences:
        actual_targets[path].add(location)
    assert actual_targets == declared_targets

    defaults = {path: row["default"] for path, row in indexed.items()}
    defaults["input.image"] = "uploaded/source.png"
    expanded_workflow = _expand(deepcopy(workflow), defaults)
    expanded_postprocess = _expand(deepcopy(postprocess), defaults)
    assert not _walk_placeholders(expanded_workflow)
    assert not _walk_placeholders(expanded_postprocess)
    assert expanded_workflow["1"]["inputs"]["image"] == "uploaded/source.png"


def test_high_low_sampling_topology_and_defaults_match_author_baseline() -> None:
    workflow = _load(WORKFLOW_PATH)
    contract = _load(BINDINGS_PATH)
    defaults = {path: row["default"] for path, row in _bindings_by_path(contract).items()}
    defaults["input.image"] = "source.png"
    expanded = _expand(deepcopy(workflow), defaults)

    assert expanded["2"]["inputs"] == {
        "unet_name": "DasiwaWAN22I2V14BLightspeed_snatchkissHighV11.safetensors",
        "weight_dtype": "default",
    }
    assert expanded["3"]["inputs"] == {
        "unet_name": "DasiwaWAN22I2V14BLightspeed_snatchkissLowV11.safetensors",
        "weight_dtype": "default",
    }
    assert expanded["4"]["inputs"] == {
        "clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "device": "default",
        "type": "wan",
    }
    assert expanded["5"]["inputs"] == {"vae_name": "Wan/wan_2.1_vae.safetensors"}
    assert expanded["8"]["inputs"] == {"model": ["2", 0], "shift": 5.0}
    assert expanded["9"]["inputs"] == {"model": ["3", 0], "shift": 5.0}

    latent = expanded["10"]["inputs"]
    assert latent["start_image"] == ["1", 0]
    assert latent["positive"] == ["6", 0]
    assert latent["negative"] == ["7", 0]
    assert latent["vae"] == ["5", 0]
    assert (latent["width"], latent["height"]) == (576, 1024)
    assert latent["width"] % 32 == latent["height"] % 32 == 0
    assert 520_000 <= latent["width"] * latent["height"] <= 830_000
    assert latent["length"] == 81
    assert latent["batch_size"] == 1

    high = expanded["11"]["inputs"]
    low = expanded["12"]["inputs"]
    for sampler in (high, low):
        assert sampler["steps"] == 4
        assert sampler["cfg"] == 1.0
        assert sampler["sampler_name"] == "euler"
        assert sampler["scheduler"] == "linear_quadratic"
        assert sampler["noise_seed"] == 1
        assert sampler["positive"] == ["10", 0]
        assert sampler["negative"] == ["10", 1]

    assert high["model"] == ["8", 0]
    assert high["latent_image"] == ["10", 2]
    assert high["add_noise"] == "enable"
    assert high["start_at_step"] == 0
    assert high["end_at_step"] == 2
    assert high["return_with_leftover_noise"] == "enable"

    assert low["model"] == ["9", 0]
    assert low["latent_image"] == ["11", 0]
    assert low["add_noise"] == "disable"
    assert low["start_at_step"] == 2
    assert low["end_at_step"] == 4
    assert low["return_with_leftover_noise"] == "disable"
    assert expanded["13"]["inputs"] == {"samples": ["12", 0], "vae": ["5", 0]}


def test_ffmpeg_is_external_deterministic_h264_faststart() -> None:
    contract = _load(BINDINGS_PATH)
    indexed = _bindings_by_path(contract)
    defaults = {path: row["default"] for path, row in indexed.items()}
    ffmpeg = _expand(contract["postprocess"]["ffmpeg"], defaults)

    assert ffmpeg["binary"] == "ffmpeg"
    assert ffmpeg["frame_input"]["node_id"] == "14"
    assert ffmpeg["arguments"] == [
        "-y",
        "-framerate",
        16,
        "-start_number",
        "0",
        "-i",
        "frame-%06d.png",
        "-frames:v",
        81,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        "output.mp4",
    ]
    assert ffmpeg["output_contract"] == {
        "codec": "h264",
        "container": "mp4",
        "pixel_format": "yuv420p",
        "streaming_layout": "faststart",
    }
    assert 81 / 16 == 5.0625


def test_manifest_pins_exact_required_artifacts_and_no_extra_model_stack() -> None:
    manifest = _load(MANIFEST_PATH)
    artifacts = manifest["required_artifacts"]
    assert manifest["required_artifact_bytes"] == sum(artifact["bytes"] for artifact in artifacts)
    assert manifest["required_artifact_bytes"] == 36_047_286_759
    assert len(artifacts) == 4

    expected = {
        "diffusion_model_high": (
            "DasiwaWAN22I2V14BLightspeed_snatchkissHighV11.safetensors",
            14_528_782_272,
            "fa4202ea621725c57b0cbb84543bd6a5548de1d85c0c5a9f18db0bcf91202a54",
        ),
        "diffusion_model_low": (
            "DasiwaWAN22I2V14BLightspeed_snatchkissLowV11.safetensors",
            14_528_782_272,
            "6e746571355bb589b966a72ed7a8717a09af0aeaf699391138e9788bace224d1",
        ),
        "text_encoder": (
            "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
            6_735_906_897,
            "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
        ),
        "vae": (
            "wan_2.1_vae.safetensors",
            253_815_318,
            "2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b",
        ),
    }
    assert {
        artifact["role"]: (
            artifact["filename"],
            artifact["bytes"],
            artifact["sha256"],
        )
        for artifact in artifacts
    } == expected

    assert len({artifact["install_path"] for artifact in artifacts}) == 4
    assert all(SHA256_RE.fullmatch(artifact["sha256"]) for artifact in artifacts)
    assert all(artifact["download_url"].startswith("https://") for artifact in artifacts)

    high, low = artifacts[:2]
    assert high["precision"] == low["precision"] == "fp8"
    assert high["civitai"] == {
        "file_id": 2837908,
        "model_id": 1981116,
        "version_id": 2953474,
    }
    assert low["civitai"] == {
        "file_id": 2837910,
        "model_id": 1981116,
        "version_id": 2953485,
    }


def test_reviewed_paired_loras_are_exact_and_absent_from_disabled_base_graph() -> None:
    manifest = _load(MANIFEST_PATH)
    workflow = _load(WORKFLOW_PATH)
    assert len(manifest["optional_paired_loras"]) == 5
    pairs = {entry["id"]: entry for entry in manifest["optional_paired_loras"]}
    assert set(pairs) == {
        "wan-general-nsfw-v0.08a",
        "bouncing-boobs-wan22",
        "m4crom4sti4-natural-breasts-k3nk",
        "dr34ml4y-aio-nsfw-wan22-v2",
        "smoothmix-xxx-animations-wan22",
    }
    pair = pairs["wan-general-nsfw-v0.08a"]
    assert pair["id"] == "wan-general-nsfw-v0.08a"
    assert pair["enabled_by_default"] is False
    assert pair["graph_injection"] == (
        "core LoraLoaderModelOnly on each stage before ModelSamplingSD3"
    )
    assert pair["recommended_initial_strength"] == 0.3
    assert pair["trigger_words"] == ["nsfwsks"]
    assert pair["high"]["civitai"]["version_id"] == 2073605
    assert pair["low"]["civitai"]["version_id"] == 2083303
    assert pair["high"]["bytes"] == pair["low"]["bytes"] == 613_516_752
    assert pair["high"]["sha256"] == (
        "34e2144d3cd65360f97d09ccbe03e1c39a096df6c9234af5fe3899d1b63cda39"
    )
    assert pair["low"]["sha256"] == (
        "d6b783742f4d5fd63a0223ae1d5bf64fc995a6b408480ac2a00528ae0d4146db"
    )
    bounce = pairs["bouncing-boobs-wan22"]
    assert bounce["trigger_words"] == ["her breasts are bouncing"]
    assert bounce["high"]["civitai"]["version_id"] == 2191217
    assert bounce["low"]["civitai"]["version_id"] == 2191270
    assert bounce["high"]["sha256"] == (
        "a4f4398031e9f39571310355f23e2d104c21143f517cf053e06d21f1c48d3d52"
    )
    assert bounce["low"]["sha256"] == (
        "3ba8320137ba7d99885624dc512d8e0ea02f24364eabbe31e803fec785339ecb"
    )
    physics = pairs["m4crom4sti4-natural-breasts-k3nk"]
    assert physics["trigger_words"] == ["m4crom4sti4"]
    assert physics["high"]["civitai"]["version_id"] == 2265575
    assert physics["low"]["civitai"]["version_id"] == 2266727
    assert physics["high"]["sha256"] == (
        "851c928737235b4a4a2c5993c893c79ee46a3131aa9b16eb56de1dcc576c3ad9"
    )
    assert physics["low"]["sha256"] == (
        "c8a940ad5ab59a15c7f39624f694482a020f0dd047cec56f498b58418d3d937c"
    )
    dream = pairs["dr34ml4y-aio-nsfw-wan22-v2"]
    assert dream["automatic_trigger_words"] == []
    assert dream["trigger_words"] == [
        "m15510n4ry",
        "bl0wj0b",
        "c0wg1rl",
        "d0gg1e",
        "d0ubl3_bj",
    ]
    assert dream["high"]["civitai"]["version_id"] == 2553151
    assert dream["low"]["civitai"]["version_id"] == 2553271
    smooth = pairs["smoothmix-xxx-animations-wan22"]
    assert smooth["automatic_trigger_words"] == smooth["trigger_words"] == []
    assert smooth["high"]["civitai"]["version_id"] == 2376136
    assert smooth["low"]["civitai"]["version_id"] == 2376143
    assert all("Lora" not in node["class_type"] for node in workflow.values())


def test_runtime_and_reference_sources_are_immutable() -> None:
    manifest = _load(MANIFEST_PATH)
    runtime = manifest["runtime"]
    assert runtime["gpu"] == "NVIDIA GeForce RTX 5090"
    assert runtime["python"] == "3.12"
    assert runtime["torch"] == "2.9.1+cu128"
    assert runtime["torchvision"] == "0.24.1+cu128"
    assert runtime["torchaudio"] == "2.9.1+cu128"
    assert runtime["author_installer"] == {
        "commit": "b15469d45524f51440c282da7f5213ac80b9b9b2",
        "repository": "https://github.com/darksidewalker/dasiwa-comfyui-installer",
    }
    assert runtime["comfyui"] == {
        "commit": "c2bcbecd82ec5ae66594340b395c24ef0217b238",
        "release": "v0.32.0",
        "repository": "https://github.com/Comfy-Org/ComfyUI",
    }
    assert manifest["workflow"]["required_custom_nodes"] == []

    reference_workflows = {
        item["civitai_version_id"]: item for item in manifest["reference_workflows"]
    }
    assert set(reference_workflows) == {2712329, 2405252, 2580650}
    assert reference_workflows[2712329]["used_as_design_source"] is True
    assert reference_workflows[2712329]["sha256"] == (
        "9c0646ce576bb08761425a9653cfbd9bd0132580f8e6e88029327d370583c3e09"
    )
    assert reference_workflows[2712329]["repository_commit"] == (
        "603b067be2d47e0532fda398f41ad6a2719d075e"
    )

    expected_node_pins = {
        "https://github.com/darksidewalker/ComfyUI-DaSiWa-Nodes": (
            "85b38df1619d1fa6e67dc17d27f82b179f89a21f"
        ),
        "https://github.com/rgthree/rgthree-comfy": ("6b76ee6f2c5a007710b5a16f97c94330d6ecc871"),
        "https://github.com/Artificial-Sweetener/comfyui-WhiteRabbit": (
            "4815da41473c99400da6ca4127f0e324dbfd865a"
        ),
        "https://github.com/kijai/ComfyUI-KJNodes": ("6ab7e8130e449ed2c0037589bcf84146ceb7fc9c"),
        "https://github.com/city96/ComfyUI-GGUF": ("6ea2651e7df66d7585f6ffee804b20e92fb38b8a"),
        "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite": (
            "4ee72c065db22c9d96c2427954dc69e7b908444b"
        ),
        "https://github.com/Lightricks/ComfyUI-LTXVideo": (
            "ac4d99839020b983e956a8ab67ec38aec1b6e65a"
        ),
        "https://github.com/WhatDreamsCost/WhatDreamsCost-ComfyUI": (
            "a3c809c8b593a74c2ddcd6c1f83ad85ebebe3c64"
        ),
    }
    assert {
        item["repository"]: item["commit"] for item in manifest["reference_custom_node_pins"]
    } == expected_node_pins
    assert all(
        item["required_by_minimal_graph"] is False
        for item in manifest["reference_custom_node_pins"]
    )


def test_bindings_do_not_hide_small_hard_generation_ceilings() -> None:
    indexed = _bindings_by_path(_load(BINDINGS_PATH))
    for path in (
        "generation.width",
        "generation.height",
        "generation.frame_count",
        "generation.fps",
        "sampling.steps",
    ):
        rules = indexed[path].get("rules", {})
        assert "maximum" not in rules
    assert indexed["generation.frame_count"]["rules"]["recommended_modulo"] == {
        "divisor": 8,
        "remainder": 1,
    }
    assert indexed["sampling.steps"]["rules"]["recommended_maximum"] == 8
    assert indexed["sampling.sampler"]["recommended_values"] == ["euler"]
    assert indexed["sampling.scheduler"]["recommended_values"] == [
        "linear_quadratic",
        "simple",
    ]
    assert all("allowed" not in row for row in indexed.values())


def test_provenance_names_current_and_outdated_sources_unambiguously() -> None:
    text = PROVENANCE_PATH.read_text(encoding="utf-8")
    for required in (
        "https://civitai.com/models/1981116",
        "https://civitai.com/articles/20293",
        "https://civitai.com/models/1823089",
        "https://civitai.com/articles/26508",
        "https://civitai.com/models/1307155",
        "FastFidelity C-AiO v8.9",
        "explicitly outdated",
        "external FFmpeg",
        "81 frames at 16 FPS",
        "CFG 1",
        "linear_quadratic",
        "underbaked",
        "graph_injection",
    ):
        assert required in text
    assert "9c0646ce576bb08761425a9653cfbd9bd0132580f8e6e88029327d370583c3e09" in text
    assert "fa4202ea621725c57b0cbb84543bd6a5548de1d85c0c5a9f18db0bcf91202a54" in text
