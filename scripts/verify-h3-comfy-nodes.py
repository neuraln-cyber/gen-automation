"""Build-time, CPU-only import/API check. Never loads weights or submits a prompt."""

import asyncio
import os
import sys

from gen_automation.i2v_worker.settings import H3_COMFY_MEMORY_ARGS, H3_CUSTOM_NODES


def verify_runtime_identity() -> None:
    """Never allow privileged build imports to stand in for the runtime contract."""
    if os.geteuid() != 10002 or os.getegid() != 10002:
        raise RuntimeError("H3 import check must run as runtime UID/GID 10002")


async def main() -> None:
    verify_runtime_identity()
    # Parse the exact runtime flags against the pinned ComfyUI CLI on CPU.
    # This verifies compatibility, not GPU memory sufficiency.
    sys.argv = [
        "h3-upscaler-build-check",
        "--cpu",
        "--input-directory",
        "/opt/i2v/runtime/input",
        "--output-directory",
        "/opt/i2v/runtime/output",
        "--temp-directory",
        "/opt/i2v/runtime/temp",
        "--user-directory",
        "/opt/i2v/runtime/user",
        *H3_COMFY_MEMORY_ARGS,
    ]
    sys.path.insert(0, "/opt/comfyui")
    import comfy.options  # type: ignore[import-not-found]

    comfy.options.enable_args_parsing()
    import folder_paths  # type: ignore[import-not-found]
    from comfy.cli_args import args  # type: ignore[import-not-found]

    # Normal Comfy main applies these CLI paths before constructing the server.
    # Do the same here instead of letting the import contract write into code dirs.
    folder_paths.set_input_directory(args.input_directory)
    folder_paths.set_output_directory(args.output_directory)
    folder_paths.set_temp_directory(args.temp_directory)
    folder_paths.set_user_directory(args.user_directory)
    import nodes  # type: ignore[import-not-found]
    from app.assets.manager import default_asset_manager  # type: ignore[import-not-found]
    from server import PromptServer  # type: ignore[import-not-found]

    # Built-in/custom route registration matches startup, without binding a socket.
    PromptServer(asyncio.get_running_loop(), default_asset_manager())

    # Exercise exactly the allowlist used at runtime, not a separate build list.
    await nodes.init_builtin_extra_nodes()
    for directory in H3_CUSTOM_NODES:
        if not await nodes.load_custom_node(f"/opt/comfyui/custom_nodes/{directory}"):
            raise RuntimeError(f"H3 extension failed to import: {directory}")
    from verify_h3_native_workflow import verify_native_workflow

    verify_native_workflow(nodes)
    from verify_h3_eros_workflow import verify_eros_kitchen_api, verify_eros_workflow

    verify_eros_workflow(nodes)
    await verify_eros_kitchen_api(nodes)
    from verify_h3_contrast import verify_contrast

    verify_contrast(nodes)
    print("Pinned H3 import/API check passed as runtime UID/GID 10002 (CPU, no weights).")


if __name__ == "__main__":
    asyncio.run(main())
