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

# Output-only contrast: retain pinned upstream class/math, expose no stash routes
# or alternative upscalers. The original upstream entrypoint remains for audit.
target=/opt/comfyui/custom_nodes/GenAutomationH3Contrast
revision=895e3c471164423f0ea0e8eaf45eb701efe641ae
test ! -e "$target"
git init "$target"
git -C "$target" remote add origin https://github.com/Tr1dae/ComfyUI-MiniMaxH3_LatentUpscaler.git
git -C "$target" fetch --depth=1 origin "$revision"
git -C "$target" checkout --detach FETCH_HEAD
test "$(git -C "$target" rev-parse HEAD)" = "$revision"
test -z "$(git -C "$target" status --porcelain)"
printf '%s\n' "$revision" > "$target/UPSTREAM_REVISION"
mv "$target/__init__.py" "$target/UPSTREAM_INIT.py"
cp /opt/i2v/bin/h3-contrast-entrypoint.py "$target/__init__.py"
rm -rf "$target/.git"
find "$target" -type d -exec chmod 0755 {} +
find "$target" -type f -exec chmod 0444 {} +
