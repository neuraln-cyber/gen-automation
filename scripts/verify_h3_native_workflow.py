"""Real pinned native H3 contract on CPU; no weights, GPU or generation jobs."""

import gc
import hashlib
import importlib
import json
import tempfile
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from gen_automation.i2v_worker.h3_sampling import H3_SAMPLERS, H3_SCHEDULERS
from gen_automation.i2v_worker.h3_upscale import H3_UPSCALER_FILENAME, H3_UPSCALER_ROLE
from gen_automation.i2v_worker.h3_variants import H3_NATIVE_MODELS
from gen_automation.i2v_worker.models import GenerationSettings
from gen_automation.i2v_worker.workflow import load_workflow_template, render_workflow


def verify_native_workflow(nodes, template_path=None, reference_path=None):
    import comfy.samplers
    import comfy.supported_models
    import comfyui_version
    import torch

    assert comfyui_version.__version__ == "0.37.0"
    assert torch.__version__.split("+")[0] == "2.9.1"
    for package, expected in (
        ("comfyui-frontend-package", "1.53.6"),
        ("comfy-kitchen", "0.2.35"),
        ("comfy-aimdo", "0.5.5"),
    ):
        assert version(package) == expected, package
    registry = nodes.NODE_CLASS_MAPPINGS
    assert not any("DaSiWa" in name or "Director" in name for name in registry)
    assert "ManagedH3LoraLoader" not in registry and "KSamplerWithNAG (Advanced)" not in registry
    assert registry["LoraLoaderModelOnly"] is nodes.LoraLoaderModelOnly
    assert comfy.supported_models.MiniMaxH3.sampling_settings == {
        "shift": 12.0,
        "audio_shift": 3.0,
    }
    assert tuple(comfy.samplers.SAMPLER_NAMES) == H3_SAMPLERS
    assert tuple(comfy.samplers.SCHEDULER_NAMES) == H3_SCHEDULERS
    raw = Path(reference_path or "/opt/i2v/reference/h3-native-i2v.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "34ee39544808fd3b0dc8de9df082940d4d41c3771beb80d3988c1ea5531cec0d"
    )
    reference = json.loads(raw)
    sub = reference["definitions"]["subgraphs"][0]
    saved = {n["id"]: n for n in sub["nodes"]}
    assert saved[6]["widgets_values"][0] == H3_NATIVE_MODELS["diffusion_model"][2]
    assert saved[13]["widgets_values"] == [
        H3_NATIVE_MODELS["text_encoder"][2],
        "minimax",
        "default",
    ]
    assert saved[17]["widgets_values"] == ["res_multistep"]
    assert saved[9]["widgets_values"][0] == "simple"
    assert saved[124]["widgets_values"][0] == 20
    assert saved[126]["widgets_values"] == [False]
    links = {x["id"]: x for x in sub["links"]}
    for link, origin, target in ((232, 6, 122), (236, 124, 123), (234, 122, 9), (241, 122, 16)):
        assert (links[link]["origin_id"], links[link]["target_id"]) == (origin, target)
    outer = next(n for n in reference["nodes"] if n["id"] == 105)
    assert next(i for i in outer["inputs"] if i["name"] == "last_frame")["link"] is None
    assert next(i for i in outer["inputs"] if i["name"] == "first_frame")["link"] == 218

    template = load_workflow_template(
        Path(template_path or "/opt/i2v/workflows/minimax-h3-native-i2v.api.json"),
        profile="minimax_h3",
    )
    paths = {role: identity[2] for role, identity in H3_NATIVE_MODELS.items()}
    paths[H3_UPSCALER_ROLE] = H3_UPSCALER_FILENAME
    for upscale, cfg in ((False, 1), (False, 2.5), (True, 1), (True, 2.5)):
        settings = GenerationSettings(
            profile="minimax_h3",
            h3_model_variant="fl2va_int8",
            seed=0,
            cfg=cfg,
            width=1152 if upscale else 768,
            height=1504 if upscale else 992,
            match_source_resolution=upscale,
            h3_save_base_video=upscale,
            h3_refine_cfg=cfg,
            h3_loras=[{"artifact_id": UUID(int=3), "sha256": "a" * 64, "strength": 0.8}],
        )
        graph, seed, _ = render_workflow(
            template,
            input_filename="contract.png",
            positive_prompt="A test scene.",
            negative_prompt="blur",
            settings=settings,
            job_id=UUID(int=1),
            attempt_id=UUID(int=2),
            model_paths=paths,
        )
        assert seed == 0 and graph["6"]["inputs"]["prompt"] == "A test scene."
        assert graph["6"]["inputs"]["first_frame"] == ["1", 0]
        assert "last_frame" not in graph["6"]["inputs"]
        assert graph["7"]["inputs"]["model"] == ["h3-lora-0", 0]
        assert graph["11"]["inputs"]["model"] == graph["9"]["inputs"]["model"] == ["7", 0]
        for node in graph.values():
            cls = registry[node["class_type"]]
            spec = cls.INPUT_TYPES()
            allowed = set(spec.get("required", {})) | set(spec.get("optional", {}))
            assert set(node["inputs"]) - allowed <= {"codec.encoding"}, node["class_type"]
            assert set(spec.get("required", {})) <= set(node["inputs"]), node["class_type"]
            for name, value in node["inputs"].items():
                if isinstance(value, list) and len(value) == 2 and value[0] in graph:
                    source = registry[graph[value[0]]["class_type"]]
                    assert value[1] < len(source.RETURN_TYPES)
                    expected = (spec.get("required", {}) | spec.get("optional", {}))[name][0]
                    assert source.RETURN_TYPES[value[1]] == expected, (node["class_type"], name)
        if upscale:
            assert graph["h3-source-upscale"]["inputs"]["model"] == ["7", 0]
            assert graph["h3-refine-sigmas"]["inputs"]["model"] == ["7", 0]
            temporal = registry["MMH3TemporalSplitParams"].execute(
                **graph["h3-temporal-params"]["inputs"]
            )[0]
            spatial = registry["MMH3SpatialSplitParams"].execute(
                **graph["h3-spatial-params"]["inputs"]
            )[0]
            verify_upstream_tile_layout(registry, temporal, spatial, first_frame=True)

    native = importlib.import_module("comfy_extras.nodes_minimax_h3")
    image = torch.full((1, 64, 96, 3), 0.5)
    clip = Mock()
    clip.encode_from_tokens_scheduled.return_value = [[torch.ones(1, 2, 8), {}]]
    vae = SimpleNamespace(
        encode=Mock(
            side_effect=lambda pixels: torch.ones(
                1,
                24,
                1,
                pixels.shape[1] // 16,
                pixels.shape[2] // 16,
            )
        )
    )
    positive, latent = native.MiniMaxH3ImageToVideo.execute(
        clip,
        vae,
        "A test scene.",
        768,
        992,
        124,
        first_frame=image,
    )
    assert clip.tokenize.call_args.args == ("A test scene.",)
    assert len(clip.tokenize.call_args.kwargs["images"]) == 1
    anchor = positive[0][1]["minimax_keyframes"]
    assert len(anchor) == 1 and anchor[0]["resolved_frame_index"] == 0
    assert anchor[0]["latent"].shape == (1, 24, 1, 62, 48)
    assert "minimax_refs" not in positive[0][1]
    assert [tuple(x.shape) for x in latent["samples"].tensors] == [
        (1, 24, 37, 62, 48),
        (1, 32, 2, 207),
    ]
    verify_native_lora_and_schedule(nodes)
    print("Native H3 graph/first-frame/AV/native LoRA/schedule/upscale CPU contracts passed.")


def verify_native_lora_and_schedule(nodes):
    import comfy.sd
    import folder_paths
    import torch
    from comfy.model_patcher import ModelPatcher
    from comfy.model_sampling import ModelSamplingAV
    from safetensors.torch import save_file

    cpu = torch.device("cpu")
    root = torch.nn.Module()
    root.diffusion_model = torch.nn.Module()
    root.diffusion_model.proj = torch.nn.Linear(8, 8, bias=False)
    root.model_config = SimpleNamespace(
        unet_config={}, sampling_settings={"shift": 12, "audio_shift": 3}
    )
    root.model_sampling = ModelSamplingAV(root.model_config)
    source = ModelPatcher(root, cpu, cpu)
    before = root.diffusion_model.proj.weight.detach().clone()
    loaders = []
    with tempfile.TemporaryDirectory(prefix="h3-native-contract-") as directory:
        selected = source
        deltas = []
        for index, strength in enumerate((0.8, -0.2)):
            file = Path(directory) / f"{index}.safetensors"
            up, down = torch.full((8, 2), 0.02 * (index + 1)), torch.full((2, 8), 0.03)
            md = {"contract_adapter": str(index)}
            save_file(
                {
                    "diffusion_model.proj.lora_A.weight": down,
                    "diffusion_model.proj.lora_B.weight": up,
                },
                str(file),
                metadata=md,
            )
            loader = nodes.LoraLoaderModelOnly()
            loaders.append(loader)
            with (
                patch.object(folder_paths, "get_full_path_or_raise", return_value=str(file)),
                patch.object(
                    comfy.sd, "load_lora_for_models", wraps=comfy.sd.load_lora_for_models
                ) as native,
            ):
                selected = loader.load_lora_model_only(selected, f"{index}.safetensors", strength)[
                    0
                ]
                assert native.call_args.args[1] is None
                assert native.call_args.args[3:] == (strength, 0)
                assert native.call_args.kwargs == {"lora_metadata": md}
            deltas.append((up @ down) * strength)
        actual = selected.patch_weight_to_device(
            "diffusion_model.proj.weight", device_to=cpu, return_weight=True
        )
        torch.testing.assert_close(actual, before + sum(deltas))
        assert len(selected.patches["diffusion_model.proj.weight"]) == 2 and not source.patches
        assert loaders[0].load_lora_model_only(source, "absent.safetensors", 0)[0] is source
        shifted = nodes.NODE_CLASS_MAPPINGS["MiniMaxH3SigmaShift"].execute(selected, 12.0, 3.0)[0]
        for scheduler, steps in (("simple", 20), ("beta", 4)):
            original_sigmas = nodes.NODE_CLASS_MAPPINGS["BasicScheduler"].execute(
                source, scheduler, steps, 1.0
            )[0]
            actual_sigmas = nodes.NODE_CLASS_MAPPINGS["BasicScheduler"].execute(
                shifted, scheduler, steps, 1.0
            )[0]
            torch.testing.assert_close(original_sigmas, actual_sigmas, rtol=0, atol=0)
        assert shifted.model_options["transformer_options"]["minimax_h3_sigma_shift_audio"] == 3
        assert not source.patches and not source.model_options.get("transformer_options")
        torch.testing.assert_close(root.diffusion_model.proj.weight, before, rtol=0, atol=0)
        for loader in loaders:
            loader.loaded_lora = None
        del selected, shifted, native
        gc.collect()


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
