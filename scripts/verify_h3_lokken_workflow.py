"""Real installed-node contracts on CPU; no model weights, GPU or provider jobs."""

import hashlib
import importlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from gen_automation.i2v_worker.h3_lokken import lokken_geometry
from gen_automation.i2v_worker.h3_variants import H3_LOKKEN_MODELS
from gen_automation.i2v_worker.models import GenerationSettings
from gen_automation.i2v_worker.workflow import load_workflow_template, render_workflow


def verify_lokken_workflow(nodes, template_path=None, reference_path=None):
    import av
    import comfy.quant_ops
    import comfyui_version
    import torch
    from comfy_api.internal import _ComfyNodeInternal
    from comfy_api.latest import _io

    assert comfyui_version.__version__ == "0.38.0"
    assert comfy.quant_ops.QUANT_ALGOS["w6a8_int8"]["comfy_tensor_layout"] == "AsymW4A8Int8Layout"
    raw = Path(reference_path or "/opt/i2v/reference/lokken-simple-v5.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "d0273aff260458898adb07d984be701d14aef9892303c98da84e83e362a4623d"
    )
    source = json.loads(raw)
    saved = {n["id"]: n for n in source["nodes"]}
    assert saved[24]["widgets_values"] == ["sgm_uniform", 8, 1]
    assert saved[102]["widgets_values"] == ["simple", 3, 0.3]
    assert saved[38]["widgets_values"] == [12, 3]
    assert saved[39]["widgets_values"] == [
        "scale by multiplier",
        1.2,
        "ULTRA",
        1,
        "10-bit RGB (RGB10A2)",
    ]
    assert saved[178]["widgets_values"] == ["medium"]
    # Frame guides are auto-managed by the active starting-image loader relay,
    # not by their saved mode=4 flags in isolation.
    assert saved[183]["properties"]["on_any_active_inputs"] == "ACTIVE"
    for guide_id in (181, 182):
        assert saved[guide_id]["type"] == "MiniMaxH3AddGuide"
        assert saved[guide_id]["widgets_values"] == [0]
    sub = source["definitions"]["subgraphs"][0]
    links = {link["id"]: link for link in sub["links"]}
    assert (links[43]["origin_id"], links[43]["origin_slot"]) == (21, 1)
    assert (links[423]["origin_id"], links[423]["origin_slot"]) == (35, 1)
    assert (links[183]["origin_id"], links[183]["origin_slot"]) == (86, 0)
    assert (links[173]["origin_id"], links[173]["origin_slot"]) == (16, 0)
    assert (links[413]["origin_id"], links[413]["origin_slot"]) == (95, 0)
    sub_nodes = {n["id"]: n for n in sub["nodes"]}
    assert sub_nodes[86]["widgets_values"][0] is True  # Disabled halt: audio passthrough.
    assert saved[94]["widgets_values"] == [False]  # Full, not preview.
    registry = nodes.NODE_CLASS_MAPPINGS
    template = load_workflow_template(
        Path(template_path or "/opt/i2v/workflows/minimax-h3-eros-ref2va.api.json"),
        profile="minimax_h3",
    )
    settings = GenerationSettings(
        profile="minimax_h3",
        h3_model_variant="eros_beta7_lokken",
        seed=0,
        width=640,
        height=480,
    )
    graph, _, _ = render_workflow(
        template,
        settings=settings,
        input_filename="contract.png",
        positive_prompt="<Picture 1> moves.",
        negative_prompt="",
        job_id=UUID(int=1),
        attempt_id=UUID(int=2),
        model_paths={role: value[2] for role, value in H3_LOKKEN_MODELS.items()},
    )
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
                output_type = source.RETURN_TYPES[value[1]]
                assert (
                    allowed[name][0] == "*" or output_type == "*" or output_type == allowed[name][0]
                ), (
                    node["class_type"],
                    name,
                    output_type,
                    allowed[name][0],
                )
    # Exercise native reference conditioning + both real guides at their actual
    # sizes. Only the weight-backed VAE/CLIP calls are tiny deterministic doubles.
    clip = Mock()
    clip.encode_from_tokens_scheduled.return_value = [[torch.ones(1, 2, 8), {}]]
    vae = SimpleNamespace(
        encode=Mock(
            side_effect=lambda pixels: torch.zeros(
                1,
                24,
                1,
                pixels.shape[1] // 16,
                pixels.shape[2] // 16,
            )
        )
    )
    image = torch.ones(1, 480, 640, 3) * 0.5
    cond, latent = registry["MiniMaxH3ReferenceToVideo"].execute(
        clip,
        "<Picture 1> moves.",
        640,
        480,
        124,
        ref_image_size="match",
        vae=vae,
        ref_images={"ref_image_0": image},
    )
    first = registry["MiniMaxH3AddGuide"].execute(cond, latent, 0, vae=vae, image=image)[0]
    video, audio = registry["LTXVSeparateAVLatent"].execute(latent)
    sizes = lokken_geometry(settings)
    video = {
        **video,
        "samples": torch.zeros(
            1, 24, video["samples"].shape[2], sizes.refine_height // 16, sizes.refine_width // 16
        ),
    }
    combined = registry["LTXVConcatAVLatent"].execute(video, audio)[0]
    second = registry["MiniMaxH3AddGuide"].execute(cond, combined, 0, vae=vae, image=image)[0]
    assert "minimax_keyframes" not in cond[0][1]
    assert first[0][1]["minimax_keyframes"][0]["latent"].shape[-2:] == (30, 40)
    assert second[0][1]["minimax_keyframes"][0]["latent"].shape[-2:] == (56, 74)
    # Exercise the actual 3D network, normalization, alignment and temporal
    # chunking with a tiny randomly initialized CPU network, never real weights.
    upscaler = registry["MinimaxH3LatentUpscaler3D"]
    implementation = importlib.import_module(upscaler.__module__)
    tiny = implementation.LatentResizer3D(
        channels=32, in_blocks=1, out_blocks=1, dropout=0, temporal_every=1
    ).eval()
    latent = {"samples": torch.zeros(1, 24, 33, 4, 4)}
    with patch.object(implementation, "load_model", return_value=tiny):
        result = upscaler.execute(
            latent,
            "synthetic-contract.safetensors",
            {"mode": "megapixels", "megapixels": 0.015625},
            align=32,
            enable_temporal_chunking=True,
            force_unload=True,
            device="cpu",
            precision="fp32",
        )[0]
    assert result["samples"].shape == (1, 24, 33, 8, 8)
    assert torch.isfinite(result["samples"]).all()
    assert torch.count_nonzero(latent["samples"]) == 0
    # Real CPU grain + PyAV encoding smoke check with nonsensitive synthetic
    # frames. No RTX invocation and no generation of an H3 test video.
    post = registry["LokkenVideoPostProcessing"]()
    encoded, fps, _ = post.process(
        images=torch.full((4, 64, 64, 3), 0.5),
        audio_pass={"waveform": torch.zeros(1, 2, 8000), "sample_rate": 48000},
        upscale=False,
        interpolate=False,
        grain=True,
        grain_intensity=0.005,
        saturation_mix=0.25,
        original_fps=24,
        compress=True,
        crf=18,
        encoder_preset="medium",
        h264_profile="auto",
        batching_mode="custom",
        custom_batch_size=4,
    )
    components = encoded.get_components()
    assert fps == 24 and components.images.shape == (4, 64, 64, 3)
    assert torch.isfinite(components.images).all()

    # The SaveVideo automatic path must preserve Lokken's encoded pictures,
    # not run a second lossy encode with Comfy defaults.
    def picture_packets(path):
        with av.open(path) as container:
            return [bytes(packet) for packet in container.demux(video=0) if packet.size]

    before = picture_packets(encoded.videos[0].get_stream_source())
    with tempfile.TemporaryDirectory(prefix="lokken-remux-contract-") as temp:
        output = str(Path(temp) / "contract.mp4")
        encoded.save_to(output, format="mp4", codec="h264")
        assert picture_packets(output) == before
        with av.open(output) as container:
            assert container.streams.audio[0].codec_context.name == "aac"
    encoded._lokken_temp_dir.cleanup()
    print(
        "Lokken real-node schemas, two guide geometries and CPU encoder passed; "
        "GPU quality untested."
    )
