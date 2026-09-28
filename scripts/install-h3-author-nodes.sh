#!/bin/bash
# Build-only installation of complete, unmodified creator packages.
set -euo pipefail
install_node() {
    local name="$1" repository="$2" revision="$3"
    local target="/opt/comfyui/custom_nodes/$name"
    test ! -e "$target"
    git init "$target"
    git -C "$target" remote add origin "$repository"
    git -C "$target" fetch --depth=1 origin "$revision"
    git -C "$target" checkout --detach FETCH_HEAD
    test "$(git -C "$target" rev-parse HEAD)" = "$revision"
    test -z "$(git -C "$target" status --porcelain)"
    printf '%s\n' "$revision" > "$target/UPSTREAM_REVISION"
    rm -rf "$target/.git"
    # Read-only code, traversable directories for runtime UID 10002. Writable
    # user data belongs in Comfy's configured runtime/user directory, not here.
    find "$target" -type d -exec chmod 0755 {} +
    find "$target" -type f -exec chmod 0444 {} +
}
install_node ComfyUI-DaSiWa-Nodes \
    https://github.com/darksidewalker/ComfyUI-DaSiWa-Nodes.git \
    9f5aef4a2748bba9486dda0a7efec7689462d7e0
install_node ComfyUI-KJNodes \
    https://github.com/kijai/ComfyUI-KJNodes.git \
    d3cfe21625e5170126ce06fbfcfe1d88108688c3
install_node Comfyui-MMH3-UltimateUpscale \
    https://github.com/bbaudio-2025/Comfyui-MMH3-UltimateUpscale.git \
    fe6658f6d144066f14150d3526247b417683ff2b
