"""Pinned native Eros REF2VA/LoRA/refinement CPU contract; no model weights/jobs."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
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
    for upscale, cfg in ((False, 1), (False, 2.5), (True, 1), (True, 2.5)):
        settings = GenerationSettings(
            profile="minimax_h3",
            h3_model_variant="eros_beta5",
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
        assert graph["7"]["inputs"]["model"] == ["h3-lora-0", 0]
        assert graph["11"]["inputs"]["model"] == graph["9"]["inputs"]["model"] == ["7", 0]
        assert graph["11"]["inputs"]["steps"] == 8
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
            assert graph["h3-source-upscale"]["inputs"]["conditioning"] == ["6", 0]
            assert graph["h3-refine-sigmas"]["inputs"]["steps"] == 4
            temporal = registry["MMH3TemporalSplitParams"].execute(
                **graph["h3-temporal-params"]["inputs"]
            )[0]
            spatial = registry["MMH3SpatialSplitParams"].execute(
                **graph["h3-spatial-params"]["inputs"]
            )[0]
            verify_upstream_tile_layout(registry, temporal, spatial, first_frame=False)
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
    verify_native_lora_and_schedule(nodes)
    print(
        "Eros native REF2VA/schema/AV/LoRA/refinement CPU contracts passed; "
        "visual quality untested."
    )
