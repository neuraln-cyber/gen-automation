"""Real-package, CPU-only author graph contract; no model weights or generation."""

import hashlib
import importlib
import json
import tempfile
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
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
        h3_model_variant="hybrid_v2",
        steps=20,
        sampler="res_multistep",
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
            assert params["mode"] == "I2VA" and "ref2va_model" not in params
            assert "external_prompt_overwrite" not in params
            params["fl2va_model"] = object()
            director = nodes.NODE_CLASS_MAPPINGS["MiniMaxH3Director"]().build_guide(**params)
        assert director[1] == settings.frame_count
        expected_prompt = (
            "For the target video, at 0.00 seconds into the target video, "
            "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
            "integrated_multimodal_description: A test scene.\n\n"
            "overall_soundscape: \n\nnon_diegetic_music: N/A"
        )
        assert director[2] == expected_prompt
        assert director[5] is params["fl2va_model"] and director[6] is True
        guide = director[0]
        assert guide["mode"] == "I2VA" and guide["last_frame"] is None
        assert guide["first_frame"].shape == (1, 64, 64, 3)
        assert not any(guide[k] for k in ("ref_images", "ref_videos", "ref_audios"))
        guide_cls = nodes.NODE_CLASS_MAPPINGS["MiniMaxH3DirectorGuide"]
        native = importlib.import_module("comfy_extras.nodes_minimax_h3")
        # Execute actual native conditioning, resizing and AV latent construction.
        # Only heavyweight CLIP/VAE inference is substituted (no weights or GPU).
        clip = Mock()
        clip.encode_from_tokens_scheduled.return_value = [[torch.ones(1, 2, 8), {}]]
        vae = SimpleNamespace(
            encode=Mock(
                side_effect=lambda pixels: torch.ones(
                    1, 24, 1, pixels.shape[1] // 16, pixels.shape[2] // 16
                )
            )
        )
        with (
            patch.object(
                native.MiniMaxH3ImageToVideo, "execute", wraps=native.MiniMaxH3ImageToVideo.execute
            ) as call,
            patch.object(native.MiniMaxH3ReferenceToVideo, "execute") as reference_call,
        ):
            positive, latent, _ = guide_cls().apply(clip, vae, guide, audio_vae=object())
        assert call.call_count == 1
        reference_call.assert_not_called()
        assert call.call_args.args[2:6] == (expected_prompt, guide["width"], guide["height"], 124)
        assert call.call_args.args[6] is guide["first_frame"]
        assert call.call_args.args[7] is None
        assert clip.tokenize.call_args.args == (expected_prompt,)
        images = clip.tokenize.call_args.kwargs["images"]
        assert len(images) == 1 and images[0].shape == (1, guide["height"], guide["width"], 3)
        vae.encode.assert_called_once()
        metadata = positive[0][1]
        assert "minimax_refs" not in metadata
        assert len(metadata["minimax_keyframes"]) == 1
        anchor = metadata["minimax_keyframes"][0]
        assert anchor["resolved_frame_index"] == 0
        assert anchor["latent"].shape == (1, 24, 1, guide["height"] // 16, guide["width"] // 16)
        video, audio = latent["samples"].tensors
        assert video.shape == (1, 24, 37, guide["height"] // 16, guide["width"] // 16)
        assert audio.shape == (1, 32, 2, 207)

    registry = nodes.NODE_CLASS_MAPPINGS
    temporal = registry["MMH3TemporalSplitParams"].execute(**graph["h3-temporal-params"]["inputs"])[
        0
    ]
    spatial = registry["MMH3SpatialSplitParams"].execute(**graph["h3-spatial-params"]["inputs"])[0]
    assert temporal["chunk_length"] == 85 and temporal["temporal_overlap"] == 17
    assert spatial["tile_width"] > 0 and spatial["tile_height"] > 0
    verify_upstream_tile_layout(registry, temporal, spatial)
    verify_upstream_tile_layout(registry, temporal, spatial, first_frame=True)
    verify_open_sampling_controls(registry, template, paths)
    assert torch.__version__.split("+")[0] == "2.9.1"
    print("Full author package, Director/Guide, seed and graph API contracts passed (CPU).")


def verify_open_sampling_controls(registry, template, paths):
    """Actual native choices/schedules/CFG wiring, without weights or GPU jobs."""
    import comfy.samplers
    import torch
    from comfy.model_sampling import ModelSamplingAV

    from gen_automation.i2v_worker.h3_sampling import H3_SAMPLERS, H3_SCHEDULERS

    assert tuple(comfy.samplers.SAMPLER_NAMES) == H3_SAMPLERS
    assert tuple(comfy.samplers.SCHEDULER_NAMES) == H3_SCHEDULERS
    sampling = ModelSamplingAV(SimpleNamespace(sampling_settings={"shift": 12, "audio_shift": 3}))
    model = SimpleNamespace(
        model_options={},
        is_dynamic=lambda: False,
        get_model_object=lambda name: sampling if name == "model_sampling" else None,
    )
    for sampler, scheduler, steps in (
        ("res_multistep", "simple", 20),
        ("euler", "simple", 25),
        ("er_sde", "beta", 4),
    ):
        assert registry["KSamplerSelect"].execute(sampler)[0].sampler_function
        sigmas = registry["BasicScheduler"].execute(model, scheduler, steps, 1.0)[0]
        assert len(sigmas) == steps + 1 and torch.isfinite(sigmas).all() and sigmas[-1] == 0
        settings = GenerationSettings(
            profile="minimax_h3",
            h3_model_variant="hybrid_v2",
            sampler=sampler,
            scheduler=scheduler,
            steps=steps,
            cfg=2.5,
            match_source_resolution=True,
            width=1152,
            height=1504,
            h3_refine_cfg=1.8,
            h3_refine_steps=3,
            h3_refine_sampler="euler",
            h3_refine_scheduler="beta",
        )
        graph, _, _ = render_workflow(
            template,
            input_filename="contract.png",
            positive_prompt="A test scene.",
            negative_prompt="blur",
            settings=settings,
            job_id=UUID(int=1),
            attempt_id=UUID(int=2),
            model_paths=paths,
        )
        assert graph["h3-negative"]["inputs"]["text"] == "blur"
        assert graph["9"]["inputs"]["cfg"] == 2.5
        for node in graph.values():
            schema = registry[node["class_type"]].INPUT_TYPES()
            assert set(schema.get("required", {})) <= set(node["inputs"])
            assert set(node["inputs"]) - set(schema.get("required", {})) - set(
                schema.get("optional", {})
            ) <= {"codec.encoding"}
    positive = [[torch.ones(1, 2, 8), {}]]
    negative = [[torch.zeros(1, 2, 8), {}]]
    guider = registry["CFGGuider"].execute(model, positive, negative, 2.5)[0]
    assert guider.cfg == 2.5 and set(guider.original_conds) == {"positive", "negative"}
    upstream = importlib.import_module(registry["MMH3UltimateUpscale"].__module__)
    refine_guider = upstream.build_guider(model, positive, negative, 1.8)
    assert refine_guider.cfg == 1.8 and "negative" in refine_guider.original_conds
    for cfg in (0, 1, 2.5):
        value = comfy.samplers.cfg_function(
            None, torch.ones(2), torch.zeros(2), cfg, torch.zeros(2), torch.ones(1)
        )
        assert torch.equal(value, torch.full((2,), float(cfg)))
    print("Native sampler registry, V2/expert schedules and effective base/refinement CFG passed.")


def verify_upstream_tile_layout(registry, temporal, spatial, *, first_frame=False):
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
            {
                "minimax_refs": [
                    {
                        "kind": "image",
                        "latent_h": 8,
                        "latent_w": 8,
                        "latent": torch.zeros(1, 24, 1, 8, 8),
                    }
                ]
            },
        ]
    ]
    if first_frame:
        # Base-resolution I2VA keyframe must be resized/cropped by upstream for
        # target-resolution tiles, not mistaken for an unaligned REF2VA reference.
        conditioning[0][1] = {
            "minimax_keyframes": [
                {
                    "resolved_frame_index": 0,
                    "latent": torch.ones(1, 24, 1, 62, 48),
                }
            ]
        }
    calls = []

    def sample(piece, cond, model, noise, sampler, sigmas, negative, cfg):
        tile, sound = piece["samples"].tensors
        _, metadata = cond[0]
        anchors = metadata.get("minimax_keyframes", [])
        refs = metadata.get("minimax_refs", [])
        if first_frame:
            assert not refs and len(anchors) == 1
            assert anchors[0]["resolved_frame_index"] == 0
            assert anchors[0]["latent"].shape[3:] == tile.shape[3:]
            if len(calls) < 6:
                assert torch.all(anchors[0]["latent"] == 1)
        else:
            assert len(refs) == 1 and refs[0]["kind"] == "image"
            assert refs[0]["latent"].shape == (1, 24, 1, 8, 8)
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
        ) + sum(patchify_video(ref["latent"], (1, 2, 2)).shape[0] for ref in refs)
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
    if first_frame:
        assert conditioning[0][1]["minimax_keyframes"][0]["latent"].shape == (1, 24, 1, 62, 48)
    print("Actual upstream temporal/spatial conditioning and native packed-layout contract passed.")
