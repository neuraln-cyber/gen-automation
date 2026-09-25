"""Build-time, CPU-only import/API check. Never loads weights or submits a prompt."""

import asyncio
import sys

from gen_automation.i2v_worker.settings import I2V_CUSTOM_NODES


async def main() -> None:
    sys.argv = ["h3-upscaler-build-check", "--cpu"]
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
    # Run the actual upstream parameter builders, without inference. These
    # classes use NodeOutput rather than ordinary tuples.
    temporal = nodes.NODE_CLASS_MAPPINGS["MMH3TemporalSplitParamsV10"].execute(
        56, 22, 0.999, "22", 24
    )[0]
    assert temporal["p"][:2] == (56, 22)
    spatial = nodes.NODE_CLASS_MAPPINGS["MMH3SpatialSplitParamsV10"].execute(
        1024, 1024, 0.25, 0.5, 256, 1.0
    )[0]
    assert spatial["tw"] == spatial["th"] == 64
    print("Pinned H3 upscaler and managed adapter import/API check passed (CPU, no weights).")


if __name__ == "__main__":
    asyncio.run(main())
