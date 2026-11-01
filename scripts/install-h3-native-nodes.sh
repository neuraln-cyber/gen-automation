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

# Lokken's simple-v5 companion graph. Saved workflow metadata predates the actual
# node interfaces, so pin audited implementations rather than those stale labels.
for binding in \
    'GenAutomationH3LokkenUpscale|LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler|40316cf008b2fd8663263270669eb4da23f89d2c' \
    'ComfyUI_Nvidia_VFX_Nodes|Comfy-Org/ComfyUI_Nvidia_VFX_Nodes|38d790ded00650681fa3645362efe28c8560cf40' \
    'ComfyUI-Lokken-VideoTools|LordLocksley/ComfyUI-Lokken-VideoTools|0f2eeb3c15e9bf04dff57aa915c8016ef92317ca'; do
    IFS='|' read -r directory repository revision <<< "$binding"
    target="/opt/comfyui/custom_nodes/$directory"
    test ! -e "$target"
    git init "$target"
    git -C "$target" remote add origin "https://github.com/$repository.git"
    git -C "$target" fetch --depth=1 origin "$revision"
    git -C "$target" checkout --detach FETCH_HEAD
    test "$(git -C "$target" rev-parse HEAD)" = "$revision"
    test -z "$(git -C "$target" status --porcelain)"
    printf '%s\n' "$revision" > "$target/UPSTREAM_REVISION"
    if [ "$directory" = GenAutomationH3LokkenUpscale ]; then
        mv "$target/__init__.py" "$target/UPSTREAM_INIT.py"
        cp /opt/i2v/bin/h3-lokken-upscale-entrypoint.py "$target/__init__.py"
    fi
    # Complete original source/license/notice files stay in the image.
    # No custom-node installer, Manager, extra package index or runtime download.
    rm -rf "$target/.git"
    find "$target" -type d -exec chmod 0755 {} +
    find "$target" -type f -exec chmod 0444 {} +
done

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
