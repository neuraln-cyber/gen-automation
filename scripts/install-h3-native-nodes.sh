#!/bin/bash
# Only the optional community upscaler; base H3 conditioning/LoRA are Comfy core.
set -euo pipefail
target=/opt/comfyui/custom_nodes/Comfyui-MMH3-UltimateUpscale
revision=fe6658f6d144066f14150d3526247b417683ff2b
test ! -e "$target"
git init "$target"
git -C "$target" remote add origin https://github.com/bbaudio-2025/Comfyui-MMH3-UltimateUpscale.git
git -C "$target" fetch --depth=1 origin "$revision"
git -C "$target" checkout --detach FETCH_HEAD
test "$(git -C "$target" rev-parse HEAD)" = "$revision"
test -z "$(git -C "$target" status --porcelain)"
printf '%s\n' "$revision" > "$target/UPSTREAM_REVISION"
rm -rf "$target/.git"
find "$target" -type d -exec chmod 0755 {} +
find "$target" -type f -exec chmod 0444 {} +
