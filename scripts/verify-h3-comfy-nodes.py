"""Build-time, CPU-only import/API check. Never loads weights or submits a prompt."""

import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import patch

from gen_automation.i2v_worker.comfy_h3_upscale import h3_refinement_split_params
from gen_automation.i2v_worker.settings import H3_COMFY_MEMORY_ARGS, I2V_CUSTOM_NODES


def verify_native_refinement_anchors(nodes) -> None:
    """Execute real pinned tile/anchor code and native layout, without weights.

    Only diffusion sampling and preview are substituted. Every spatial tile in
    every temporal chunk reaches PackedLayout with its actual conditioning.
    The old interior-anchor settings must reproduce the production exception.
    """
    import torch
    from comfy.ldm.minimax.model import PackedLayout
    from comfy.nested_tensor import NestedTensor

    refiner = nodes.NODE_CLASS_MAPPINGS["MMH3SplitUpscale"]
    split = importlib.import_module(refiner.__module__)
    temporal, spatial = h3_refinement_split_params(nodes.NODE_CLASS_MAPPINGS)
    assert temporal["p"] == (56, 22, 0.999, 0, 0)
    assert spatial["tw"] == spatial["th"] == 64
    checked_tiles = 0

    def sample_piece(piece, conditioning, *_args, **_kwargs):
        nonlocal checked_tiles
        video, audio = piece["samples"].tensors
        for embedding, values in conditioning:
            keyframes = values["minimax_keyframes"]
            # The original source anchors chunk zero; subsequent chunks use
            # the previous chunk's matching frame, always at local index zero.
            assert keyframes
            for keyframe in keyframes:
                assert keyframe["latent"].shape[-2:] == video.shape[-2:]
            PackedLayout(
                text_len=embedding.shape[1],
                latent_t=video.shape[2],
                latent_h=video.shape[3],
                latent_w=video.shape[4],
                audio_t=audio.shape[-1],
                keyframes=keyframes,
                frame_count=split.frames_for_tokens(video.shape[2]),
            )
            assert [kf["resolved_frame_index"] for kf in keyframes] == [0]
        checked_tiles += 1
        return piece["samples"]

    noise = SimpleNamespace(
        seed=1, generate_noise=lambda latent: torch.zeros_like(latent["samples"])
    )
    with (
        patch.object(split, "build_guider", side_effect=lambda _model, cond, *_args: cond),
        patch.object(split, "sample_piece", side_effect=sample_piece),
        patch.object(split, "make_tile_progress", return_value=lambda _index: None),
    ):
        for height, width, tokens in ((94, 72, 37), (72, 94, 7)):
            video = torch.zeros(1, 24, tokens, height, width)
            audio = torch.zeros(
                1, 32, 2, round(split.frames_for_tokens(tokens) * split.FRAME_RESCALE)
            )
            kwargs = dict(
                latent={"samples": NestedTensor((video, audio))},
                conditioning=[
                    [
                        torch.zeros(1, 2, 4),
                        {
                            "minimax_keyframes": [
                                {"resolved_frame_index": 0, "latent": video[:, :, :1]}
                            ]
                        },
                    ]
                ],
                model=object(),
                noise=noise,
                sampler=None,
                sigmas=torch.tensor([0.35, 0.0]),
                cfg=1.0,
                spatial_split_param=spatial,
                seam_polish="off",
                color_match=True,
            )
            if tokens == 37:
                old = nodes.NODE_CLASS_MAPPINGS["MMH3TemporalSplitParamsV10"].execute(
                    56, 22, 0.999, "22", 24
                )[0]
                try:
                    refiner.execute(**kwargs, temporal_split_param=old)
                except ValueError as error:
                    assert "only first/last keyframe anchors" in str(error)
                else:
                    raise AssertionError("Negative control did not reproduce the rejected anchors")
            output = refiner.execute(**kwargs, temporal_split_param=temporal)[0]["samples"]
            assert output.tensors[0].shape == video.shape
            assert output.tensors[1].shape == audio.shape
    assert checked_tiles > 8, checked_tiles
    print(f"Native H3 layout accepted all {checked_tiles} refinement tiles; old anchors rejected.")


async def main() -> None:
    # Parse the exact runtime flags against the pinned ComfyUI CLI on CPU.
    # This verifies compatibility, not GPU memory sufficiency.
    sys.argv = ["h3-upscaler-build-check", "--cpu", *H3_COMFY_MEMORY_ARGS]
    sys.path.insert(0, "/opt/comfyui")
    import comfy.options  # type: ignore[import-not-found]

    comfy.options.enable_args_parsing()
    import nodes  # type: ignore[import-not-found]

    # Exercise exactly the allowlist used at runtime, not a separate build list.
    for directory in I2V_CUSTOM_NODES:
        if not await nodes.load_custom_node(f"/opt/comfyui/custom_nodes/{directory}"):
            raise RuntimeError(f"H3 extension failed to import: {directory}")
    for name in (
        "ManagedH3LoraLoader",
        "ManagedH3SourceUpscale",
        "MinimaxH3LatentUpscaler3D",
        "MMH3TemporalSplitParamsV10",
        "MMH3SpatialSplitParamsV10",
        "MMH3SplitUpscale",
    ):
        cls = nodes.NODE_CLASS_MAPPINGS[name]
        assert "required" in cls.INPUT_TYPES(), name
    verify_native_refinement_anchors(nodes)
    print("Pinned H3 upscaler and managed adapter import/API check passed (CPU, no weights).")


if __name__ == "__main__":
    asyncio.run(main())
