"""Managed adapter around the pinned community H3 upscaler/refiner.

ComfyUI-only imports stay inside execution so the control plane never needs Torch.
The learned upscaler accepts video tensors, not native H3's nested AV tensor.
Audio is preserved from the base pass, not refined or temporally blended.
"""

from typing import Any

from gen_automation.i2v_worker.h3_upscale import H3_UPSCALER_FILENAME


def h3_refinement_split_params(nodes: dict[str, Any]) -> tuple[Any, Any]:
    # Native MiniMax PackedLayout supports only first/last keyframes. The
    # community refiner's motion/identity anchors inject interior keyframes,
    # which fail before sampling. Keep its first-frame reanchor and overlaps.
    temporal = nodes["MMH3TemporalSplitParamsV10"].execute(
        chunk_frames=56,
        temporal_overlap_frames=22,
        anchor_strength=0.999,
        motion_anchor_frames="0",
        identity_anchor_frames=0,
    )[0]
    spatial = nodes["MMH3SpatialSplitParamsV10"].execute(
        tile_width=1024,
        tile_height=1024,
        overlap_ratio=0.25,
        fade_ratio=0.5,
        min_tile_size=256,
        seam_denoise=1.0,
    )[0]
    return temporal, spatial


class ManagedH3SourceUpscale:
    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:  # noqa: N802
        return {
            "required": {
                "latent": ("LATENT",),
                "conditioning": ("CONDITIONING",),
                "first_frame": ("IMAGE",),
                "vae": ("VAE",),
                "model": ("MODEL",),
                "noise": ("NOISE",),
                "sampler": ("SAMPLER",),
                "sigmas": ("SIGMAS",),
                "width": ("INT", {"min": 32, "max": 2048, "step": 32}),
                "height": ("INT", {"min": 32, "max": 2048, "step": 32}),
                "model_name": ([H3_UPSCALER_FILENAME],),
            }
        }

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "upscale"
    CATEGORY = "GenAutomation/H3"

    def upscale(
        self,
        latent: dict[str, Any],
        conditioning: Any,
        first_frame: Any,
        vae: Any,
        model: Any,
        noise: Any,
        sampler: Any,
        sigmas: Any,
        width: int,
        height: int,
        model_name: str,
    ) -> tuple[dict[str, Any]]:
        import comfy.model_management as mm  # type: ignore[import-not-found]
        import comfy.nested_tensor as nt  # type: ignore[import-not-found]
        import node_helpers  # type: ignore[import-not-found]
        from nodes import NODE_CLASS_MAPPINGS  # type: ignore[import-not-found]

        if model_name != H3_UPSCALER_FILENAME:
            raise ValueError("H3 upscaler is outside the pinned managed model library")
        samples = latent["samples"]
        if not isinstance(samples, nt.NestedTensor) or len(samples.tensors) != 2:
            raise ValueError("H3 upscaling requires a native video/audio latent")
        video, audio = samples.tensors
        if video.ndim != 5 or video.shape[0:2] != (1, 24) or audio.ndim != 4:
            raise ValueError("H3 upscaling requires a single native H3 video")
        if min(width, height) < 32 or max(width, height) > 2048 or width % 32 or height % 32:
            raise ValueError("H3 refinement dimensions are invalid")
        if tuple(first_frame.shape[1:3]) != (height, width):
            raise ValueError("H3 refinement requires the full prepared source image")
        expected_shape = (*video.shape[:3], height // 16, width // 16)
        if tuple(video.shape) == expected_shape:
            return (latent,)

        # The model patcher reloads H3 on demand after the lightweight upscaler
        # and source-keyframe VAE encode. Do not keep both networks in VRAM.
        mm.unload_all_models()
        upscaled = NODE_CLASS_MAPPINGS["MinimaxH3LatentUpscaler3D"].execute(
            latent={"samples": video},
            model_name=model_name,
            mode={"mode": "target dimensions", "width": width, "height": height},
            align=32,
            enable_temporal_chunking=True,
            force_unload=True,
            device="cuda",
            precision="bf16",
        )[0]["samples"]
        if tuple(upscaled.shape) != expected_shape:
            raise ValueError("H3 upscaler changed video duration or returned incorrect dimensions")

        # Reuse the original text embeddings; re-encode only the first-frame
        # image at final size so the refinement anchors to the original detail.
        keyframe = vae.encode(first_frame[:1, :, :, :3])
        high_conditioning = node_helpers.conditioning_set_values(
            conditioning,
            {"minimax_keyframes": [{"resolved_frame_index": 0, "latent": keyframe}]},
        )
        temporal, spatial = h3_refinement_split_params(NODE_CLASS_MAPPINGS)
        refined = (
            NODE_CLASS_MAPPINGS["MMH3SplitUpscale"]
            .execute(
                latent={"samples": nt.NestedTensor((upscaled, audio))},
                conditioning=high_conditioning,
                model=model,
                noise=noise,
                sampler=sampler,
                sigmas=sigmas,
                cfg=1.0,
                temporal_split_param=temporal,
                spatial_split_param=spatial,
                seam_polish="off",
                color_match=True,
            )[0]["samples"]
            .tensors[0]
        )
        if tuple(refined.shape) != expected_shape:
            raise ValueError("H3 refinement changed video duration or dimensions")
        return ({"samples": nt.NestedTensor((refined, audio))},)
