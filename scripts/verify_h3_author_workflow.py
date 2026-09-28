"""Real-package, CPU-only author graph contract; no model weights or generation."""

import hashlib
import importlib
import json
import tempfile
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from gen_automation.i2v_worker.h3_upscale import H3_UPSCALER_FILENAME, H3_UPSCALER_ROLE
from gen_automation.i2v_worker.models import GenerationSettings
from gen_automation.i2v_worker.workflow import load_workflow_template, render_workflow


def verify_author_workflow(nodes, template_path=None, reference_path=None):
    import comfyui_version
    import folder_paths
    import torch
    from comfy_extras.nodes_custom_sampler import Noise_RandomNoise
    from PIL import Image

    assert comfyui_version.__version__ == "0.37.0"
    for distribution, expected in (
        ("comfyui-frontend-package", "1.53.6"),
        ("comfy-kitchen", "0.2.35"),
        ("comfy-aimdo", "0.5.5"),
    ):
        assert version(distribution) == expected, distribution
    assert "ManagedH3SourceUpscale" not in nodes.NODE_CLASS_MAPPINGS
    assert "ManagedH3LoraLoader" not in nodes.NODE_CLASS_MAPPINGS
    assert "KSamplerWithNAG (Advanced)" not in nodes.NODE_CLASS_MAPPINGS
    template = load_workflow_template(
        Path(template_path or "/opt/i2v/workflows/dasiwa-minimax-h3-i2v-v1.api.json"),
        profile="minimax_h3",
    )
    settings = GenerationSettings(
        profile="minimax_h3",
        fps=24,
        frame_count=124,
        steps=8,
        scheduler="simple",
        width=1152,
        height=1504,
        seed=0,
        match_source_resolution=True,
        h3_save_base_video=True,
    )
    paths = {
        role: f"{role}.safetensors"
        for role in ("diffusion_model", "text_encoder", "video_vae", "audio_vae")
    }
    paths[H3_UPSCALER_ROLE] = H3_UPSCALER_FILENAME
    graph, _, _ = render_workflow(
        template,
        input_filename="contract.png",
        positive_prompt="A test scene.",
        negative_prompt="",
        settings=settings,
        job_id=UUID(int=1),
        attempt_id=UUID(int=2),
        model_paths=paths,
    )
    reference = Path(reference_path or "/opt/i2v/reference/h3-author-v23.json").read_bytes()
    assert hashlib.sha256(reference).hexdigest() == (
        "51a20a9e79fb9a505c0bada2e4828f39605738cd98aee327af7cd114f3015bef"
    )
    saved = json.loads(reference)
    subgraph = saved["definitions"]["subgraphs"][0]
    saved_nodes = {n["id"]: n for n in subgraph["nodes"]}
    links = {link["id"]: link for link in subgraph["links"]}
    for author_id in (2590, 2692, 2769, 2777):
        model_input = next(i for i in saved_nodes[author_id]["inputs"] if i["name"] == "model")
        link = links[model_input["link"]]
        # All four consume the same outer LoRA MODEL, before SigmaShift.
        assert (link["origin_id"], link["origin_slot"]) == (-10, 24)
    assert (
        list(graph["h3-refine-sigmas"]["inputs"].values())[1:]
        == (saved_nodes[2777]["widgets_values"])
    )
    assert (
        list(graph["h3-temporal-params"]["inputs"].values())
        == (saved_nodes[2774]["widgets_values"])
    )
    # The first two saved spatial widgets are linked dimensions, not fixed 1024s.
    assert (
        list(graph["h3-spatial-params"]["inputs"].values())[2:]
        == (saved_nodes[2773]["widgets_values"][2:])
    )
    # Validate required input names and all connected outputs against the actual
    # installed classes, including v3-schema-backed nodes. No stand-in registry.
    for node in graph.values():
        cls = nodes.NODE_CLASS_MAPPINGS[node["class_type"]]
        spec = cls.INPUT_TYPES()
        allowed = set(spec.get("required", {})) | set(spec.get("optional", {}))
        # SaveVideo's dynamic codec.encoding input is validated by Comfy at runtime.
        assert set(node["inputs"]) - allowed <= {"codec.encoding"}, node["class_type"]
        assert set(spec.get("required", {})) <= set(node["inputs"]), node["class_type"]
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and value[0] in graph:
                source = nodes.NODE_CLASS_MAPPINGS[graph[value[0]]["class_type"]]
                assert value[1] < len(source.RETURN_TYPES)
    seed = nodes.NODE_CLASS_MAPPINGS["DaSiWa_SeedControl"]().execute(
        0, json.dumps({"mode": "fixed"})
    )
    assert seed[0] == 0 and isinstance(seed[1], Noise_RandomNoise) and seed[1].seed == 0

    # Execute the real Director's image loading / prompt / frame-grid resolution,
    # then check its real Guide forwards exactly that to native Comfy conditioning.
    with tempfile.TemporaryDirectory(prefix="h3-author-contract-") as tmp:
        Image.new("RGB", (64, 64), "blue").save(Path(tmp) / "contract.png")
        with patch.object(folder_paths, "get_input_directory", return_value=tmp):
            params = dict(graph["1"]["inputs"])
            params["fl2va_model"] = object()
            director = nodes.NODE_CLASS_MAPPINGS["MiniMaxH3Director"]().build_guide(**params)
        assert director[1] == settings.frame_count
        assert director[2] == "A test scene."
        assert director[5] is params["fl2va_model"]
        guide_cls = nodes.NODE_CLASS_MAPPINGS["MiniMaxH3DirectorGuide"]
        native = importlib.import_module("comfy_extras.nodes_minimax_h3")
        with patch.object(native.MiniMaxH3ImageToVideo, "execute", return_value=([], {})) as call:
            guide_cls().apply(object(), object(), director[0], audio_vae=object())
        assert call.call_count == 1
        # Keyword contract is native Comfy's; creator code must not be copied or patched.
        args = call.call_args
        assert any(value == "A test scene." for value in args.args if isinstance(value, str)) or (
            any(
                value == "A test scene." for value in args.kwargs.values() if isinstance(value, str)
            )
        )

    registry = nodes.NODE_CLASS_MAPPINGS
    temporal = registry["MMH3TemporalSplitParams"].execute(**graph["h3-temporal-params"]["inputs"])[
        0
    ]
    spatial = registry["MMH3SpatialSplitParams"].execute(**graph["h3-spatial-params"]["inputs"])[0]
    assert temporal["chunk_length"] == 85 and temporal["temporal_overlap"] == 17
    assert spatial["tile_width"] > 0 and spatial["tile_height"] > 0
    verify_upstream_tile_layout(registry, temporal, spatial)
    assert torch.__version__.split("+")[0] == "2.9.1"
    print("Full author package, Director/Guide, seed and graph API contracts passed (CPU).")


def verify_upstream_tile_layout(registry, temporal, spatial):
    """Exercise real split/anchor/packing/stitching; substitute only GPU sampling."""
    import torch
    from comfy.ldm.minimax.model import PackedLayout, patchify_video
    from comfy.nested_tensor import NestedTensor

    cls = registry["MMH3UltimateUpscale"]
    upstream = importlib.import_module(cls.__module__)
    # Two temporal segments, all six spatial tiles; no weights or benchmarks.
    video = torch.zeros(1, 24, 37, 94, 72)
    audio = torch.arange(32 * 2 * 207, dtype=torch.float32).reshape(1, 32, 2, 207)
    conditioning = [
        [
            torch.zeros(1, 2, 8),
            {"minimax_keyframes": [{"resolved_frame_index": 0, "latent": video[:, :, :1].clone()}]},
        ]
    ]
    calls = []

    def sample(piece, cond, model, noise, sampler, sigmas, negative, cfg):
        tile, sound = piece["samples"].tensors
        _, metadata = cond[0]
        anchors = metadata.get("minimax_keyframes", [])
        layout = PackedLayout(
            2,
            tile.shape[2],
            tile.shape[3],
            tile.shape[4],
            sound.shape[-1],
            keyframes=anchors,
            refs=metadata.get("minimax_refs"),
        )
        expected = sum(
            patchify_video(k["latent"], (1, 2, 2)).shape[0] for k in anchors if "latent" in k
        )
        assert int((~layout.img_update).sum()) == expected
        assert piece["noise_mask"].tensors[1].count_nonzero() == 0
        calls.append(tile.shape)
        return piece["samples"]

    patcher = SimpleNamespace(model_options={})
    patcher.set_model_denoise_mask_function = lambda fn: patcher.model_options.update(
        denoise_mask_function=fn
    )
    with patch.object(upstream, "sample_piece", side_effect=sample):
        result = cls.execute(
            {"samples": NestedTensor((video, audio))},
            conditioning,
            patcher,
            SimpleNamespace(seed=0),
            object(),
            torch.tensor([0.2, 0.0]),
            temporal_split_param=temporal,
            spatial_split_param=spatial,
        )
    actual_video, actual_audio = result[0]["samples"].tensors
    assert len(calls) == 12
    assert actual_video.shape == video.shape
    assert torch.allclose(actual_audio, audio)
    assert not patcher.model_options
    print("Actual upstream temporal/spatial conditioning and native packed-layout contract passed.")
