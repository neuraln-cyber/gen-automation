"""Pinned native Eros REF2VA/LoRA/refinement CPU contract; no model weights/jobs."""

import hashlib
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from verify_h3_native_workflow import verify_native_lora_and_schedule, verify_upstream_tile_layout

from gen_automation.i2v_worker.h3_upscale import H3_UPSCALER_FILENAME, H3_UPSCALER_ROLE
from gen_automation.i2v_worker.h3_variants import H3_EROS_MODELS
from gen_automation.i2v_worker.models import GenerationSettings
from gen_automation.i2v_worker.workflow import load_workflow_template, render_workflow


def verify_eros_workflow(nodes, template_path=None, reference_path=None):
    import torch
    from comfy_api.internal import _ComfyNodeInternal
    from comfy_api.latest import _io

    raw = Path(reference_path or "/opt/i2v/reference/h3-native-r2v.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "afeea99e9fd5a1456df348e0573e289c1243f2f28546fc95c1c963f808d61ddb"
    )
    saved = {n["id"]: n for n in json.loads(raw)["nodes"]}
    assert saved[136]["type"] == "MiniMaxH3ReferenceToVideo"
    assert (
        next(i for i in saved[136]["inputs"] if i["name"] == "ref_images.ref_image_0")["link"]
        == 278
    )
    assert saved[123]["widgets_values"] == ["res_multistep"]
    assert saved[124]["widgets_values"][0] == "simple"
    assert saved[128]["widgets_values"][0] == H3_EROS_MODELS["text_encoder"][2]
    registry = nodes.NODE_CLASS_MAPPINGS
    reference_cls = registry["MiniMaxH3ReferenceToVideo"]
    assert reference_cls.__name__ == "MiniMaxH3ReferenceToVideo"
    assert reference_cls.__module__.endswith("nodes_minimax_h3")
    assert registry["LoraLoaderModelOnly"] is nodes.LoraLoaderModelOnly
    assert not any("DaSiWa" in name or "Director" in name for name in registry)
    template = load_workflow_template(
        Path(template_path or "/opt/i2v/workflows/minimax-h3-eros-ref2va.api.json"),
        profile="minimax_h3",
    )
    paths = {role: identity[2] for role, identity in H3_EROS_MODELS.items()}
    paths[H3_UPSCALER_ROLE] = H3_UPSCALER_FILENAME
    for author_recipe, mode, upscale, cfg in itertools.product(
        (False, True), ("reference", "first_frame"), (False, True), (1, 2.5)
    ):
        settings = GenerationSettings(
            profile="minimax_h3",
            h3_model_variant="eros_beta5",
            h3_image_mode=mode,
            h3_attention_backend="comfy_kitchen" if author_recipe else "default",
            sampler="er_sde" if author_recipe else "res_multistep",
            scheduler="beta57" if author_recipe else "simple",
            steps=6 if author_recipe else 8,
            seed=0,
            width=1152 if upscale else 768,
            height=1504 if upscale else 992,
            cfg=cfg,
            h3_refine_cfg=cfg,
            match_source_resolution=upscale,
            h3_save_base_video=upscale,
            h3_loras=[{"artifact_id": UUID(int=3), "sha256": "a" * 64, "strength": 0.4}],
        )
        graph, seed, _ = render_workflow(
            template,
            input_filename="contract.png",
            guide_filename="guide.png" if mode == "first_frame" else None,
            positive_prompt="<Picture 1> moves.",
            negative_prompt="blur",
            settings=settings,
            job_id=UUID(int=1),
            attempt_id=UUID(int=2),
            model_paths=paths,
        )
        assert seed == 0 and graph["6"]["inputs"]["prompt"] == "<Picture 1> moves."
        assert graph["6"]["inputs"]["ref_images.ref_image_0"] == ["1", 0]
        assert not {"first_frame", "last_frame"} & graph["6"]["inputs"].keys()
        assert graph["7"]["inputs"]["model"] == ["h3-kitchen" if author_recipe else "h3-lora-0", 0]
        if author_recipe:
            assert graph["h3-kitchen"]["inputs"]["model"] == ["h3-lora-0", 0]
            assert graph["11"]["class_type"] == "BetaSamplingScheduler"
        assert graph["11"]["inputs"]["model"] == graph["9"]["inputs"]["model"] == ["7", 0]
        assert graph["11"]["inputs"]["steps"] == (6 if author_recipe else 8)
        conditioning = ["h3-first-frame", 0] if mode == "first_frame" else ["6", 0]
        assert graph["9"]["inputs"]["positive" if cfg != 1 else "conditioning"] == conditioning
        for node in graph.values():
            cls = registry[node["class_type"]]
            spec = cls.INPUT_TYPES()
            if issubclass(cls, _ComfyNodeInternal):
                spec, _, _ = _io.get_finalized_class_inputs(spec, node["inputs"])
            allowed = spec.get("required", {}) | spec.get("optional", {})
            assert set(node["inputs"]) <= set(allowed), node["class_type"]
            assert set(spec.get("required", {})) <= set(node["inputs"]), node["class_type"]
            for name, value in node["inputs"].items():
                if isinstance(value, list) and len(value) == 2 and value[0] in graph:
                    source = registry[graph[value[0]]["class_type"]]
                    assert source.RETURN_TYPES[value[1]] == allowed[name][0], (
                        node["class_type"],
                        name,
                    )
        if upscale:
            assert graph["h3-source-upscale"]["inputs"]["model"] == ["7", 0]
            assert graph["h3-source-upscale"]["inputs"]["conditioning"] == conditioning
            assert graph["h3-refine-sigmas"]["inputs"]["steps"] == 4
            temporal = registry["MMH3TemporalSplitParams"].execute(
                **graph["h3-temporal-params"]["inputs"]
            )[0]
            spatial = registry["MMH3SpatialSplitParams"].execute(
                **graph["h3-spatial-params"]["inputs"]
            )[0]
            verify_upstream_tile_layout(
                registry, temporal, spatial, first_frame=mode == "first_frame", keep_reference=True
            )
    clip = Mock()
    clip.encode_from_tokens_scheduled.return_value = [[torch.ones(1, 2, 8), {}]]
    vae = SimpleNamespace(
        encode=Mock(
            side_effect=lambda pixels: torch.ones(
                1, 24, 1, pixels.shape[1] // 16, pixels.shape[2] // 16
            )
        )
    )
    image = torch.full((1, 128, 96, 3), 0.5)
    cond, latent = reference_cls.execute(
        clip,
        "<Picture 1> moves.",
        768,
        992,
        124,
        ref_image_size="match",
        vae=vae,
        ref_images={"ref_image_0": image},
    )
    assert clip.tokenize.call_args.args == ("<Picture 1> moves.",)
    refs = clip.tokenize.call_args.kwargs["minimax_ref_items"]
    assert len(refs) == 1 and refs[0]["type"] == "image"
    assert "minimax_keyframes" not in cond[0][1]
    assert cond[0][1]["minimax_refs"][0]["latent"].shape == (1, 24, 1, 8, 6)
    assert [tuple(x.shape) for x in latent["samples"].tensors] == [
        (1, 24, 37, 62, 48),
        (1, 32, 2, 207),
    ]
    guided = registry["MiniMaxH3AddGuide"].execute(
        cond, latent, 0, vae=vae, image=torch.full((1, 992, 768, 3), 0.5)
    )[0]
    assert "minimax_keyframes" not in cond[0][1]  # input/reference branch unmodified
    assert guided[0][1]["minimax_refs"] == cond[0][1]["minimax_refs"]
    anchor = guided[0][1]["minimax_keyframes"]
    assert len(anchor) == 1 and anchor[0]["resolved_frame_index"] == 0
    assert anchor[0]["latent"].shape == (1, 24, 1, 62, 48)
    verify_native_lora_and_schedule(nodes)
    verify_eros_beta57(nodes)
    print(
        "Eros native REF2VA/schema/AV/LoRA/refinement CPU contracts passed; "
        "visual quality untested."
    )


def verify_eros_beta57(nodes):
    """Real native beta/split arithmetic equals the published beta57 definition."""
    import comfy.ldm.modules.attention
    import comfy.samplers
    import torch
    from comfy.model_sampling import ModelSamplingAV

    sampling = ModelSamplingAV(SimpleNamespace(sampling_settings={"shift": 12, "audio_shift": 3}))
    model = Mock()
    model.get_model_object.return_value = sampling
    registry = nodes.NODE_CLASS_MAPPINGS
    for steps, denoise in ((4, 1), (6, 1), (8, 0.5), (6, 0.37), (4, 0.2)):
        total = int(steps / denoise)
        # RES4LYF's beta57 branch, with its unchanged partial-denoise tail.
        expected = comfy.samplers.beta_scheduler(sampling, total, alpha=0.5, beta=0.7).cpu()
        expected = expected[-(steps + 1) :]
        actual = registry["BetaSamplingScheduler"].execute(model, total, 0.5, 0.7)[0]
        if total > steps:
            actual = registry["SplitSigmas"].execute(actual, total - steps)[1]
        torch.testing.assert_close(actual, expected, atol=0, rtol=0)
    native_beta = comfy.samplers.beta_scheduler(sampling, 6, alpha=0.6, beta=0.6)
    beta57 = registry["BetaSamplingScheduler"].execute(model, 6, 0.5, 0.7)[0]
    assert not torch.equal(beta57, native_beta)
    # Verify the real node requests the explicit backend without performing a
    # synthetic GPU benchmark. GPU availability is checked before submission.
    attention_fn = Mock()
    with patch.object(
        comfy.ldm.modules.attention, "get_attention_function", return_value=attention_fn
    ) as lookup:
        selected = registry["ModelAttentionBackend"].execute(model, "comfy kitchen attention")[0]
        lookup.assert_called_once_with("comfy_kitchen_int8", None)
        selected.set_model_optimized_attention.assert_called_once_with(attention_fn)
    print("Eros beta57 exact native numerical equivalence and Kitchen routing passed.")
