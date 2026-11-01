# Third-party notices for I2V extensions

## Lokken two-pass workflow (new jobs only)

- Workflow and HQ weights: <https://huggingface.co/LokkenJP/TenEros_Max_optimized_learned/tree/f2dd2b315ea27c0a340ba7f3e03ef078e4753c28>.
  The simple-v5 graph is adapted into the application's API graph. It is not the
  experimental continuation/refmods workflow and is not a `beta7fixed` release.
  MiniMax's model terms remain separate from the code licenses.
- 3D latent upscaler code: LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler,
  revision `40316cf008b2fd8663263270669eb4da23f89d2c`, MIT. Complete original
  source and LICENSE remain in `GenAutomationH3LokkenUpscale`; our entrypoint
  exposes only the unchanged 3D implementation. FP16 weights are delivered by
  the existing private manifest/CloudFront path, not embedded in the image.
- RTX nodes: Comfy-Org/ComfyUI_Nvidia_VFX_Nodes,
  revision `38d790ded00650681fa3645362efe28c8560cf40`, Apache-2.0.
  The separate `nvidia-vfx==0.2.0.0` runtime is NVIDIA proprietary software,
  NOT Apache/MIT-licensed. Its software, AI-product, open-model and third-party
  license texts remain in the wheel's installed distribution metadata.
- Post-processing: LordLocksley/ComfyUI-Lokken-VideoTools,
  revision `0f2eeb3c15e9bf04dff57aa915c8016ef92317ca`. The backend is unmodified.
  GPL-3.0-or-later applies to Elvax-derived/Lokken code; AGPL-3.0-only applies
  to the Fast Film Grain-derived portion. These are combined obligations,
  not a choice of permissive license. Full original sources, `LICENSE`,
  `LICENSE-AGPL-3.0.txt`, `NOTICE.md` and attribution are retained in the image.
  No RIFE code/weights are bundled or requested: interpolation is disabled.

Corresponding upstream source and notices are available at
<https://github.com/LordLocksley/ComfyUI-Lokken-VideoTools/tree/0f2eeb3c15e9bf04dff57aa915c8016ef92317ca>.
The integration, installation scripts and exact dependency locks are available
in this public repository at the worker's immutable source revision. The studio
links both sources. Preserve access to these exact sources and all applicable
license notices when redistributing or hosting this integration. This notice
does not relicense NVIDIA binaries or model weights under the node licenses.

The new Lokken recipe adapter (`i2v_worker/h3_lokken.py`) and narrow 3D-node
entrypoint (`i2v_worker/comfy_h3_lokken_upscale_entrypoint.py`) are provided under
AGPL-3.0-only; the complete terms are in `licenses/AGPL-3.0-only.txt`. They are
provided without warranty. The upstream grain/encoder implementation is not
modified. Its original combined GPL/AGPL terms and attribution remain intact.
This scoped grant does not silently relicense unrelated application code.
The source link in the studio binds to the configured immutable worker revision,
including the installation scripts and locks needed to reproduce that build.

The optional `stable_expression` face stabilizer includes the following pinned
components in the immutable I2V worker image. The complete license texts shipped
by `anime-face-detector` are preserved in `/opt/i2v/vendor` inside that image.

## anime-face-detector

- Project: <https://github.com/hysts/anime-face-detector>
- Revision: `7db835de7a3a052eb4d68d241ae9f2cf28a0b509`
- Package SHA-256: `9a6a8c1384b7a57fab8ce9988f814271ff88bac52a9dd871490a28b61dff7692`
- License: MIT
- Copyright: 2021 hysts

The vendored MMDetection and MMPose portions retain their upstream Apache-2.0
license texts and copyright notices in
`anime_face_detector/_vendor/LICENSE.mmdetection` and
`anime_face_detector/_vendor/LICENSE.mmpose`.

## anime-face-detector weights

- YOLOv3 revision: `afdd4226a79ae8bb81f334dbcffd34f8cc000c38`
- YOLOv3 SHA-256: `23bbc708146bcbc1c910f00fe152adbc70d7658d875a0121eaf4ee61d978b2c4`
- HRNetV2 revision: `9b3435248b26aeb82e2a8578fe9d86d5d57158af`
- HRNetV2 SHA-256: `e71271376406a743c01528a0460637fcc06e72aeeea583f85007cc72dc8b7a4a`
- License: MIT

## OpenCV Python headless

- Distribution: `opencv-python-headless==4.14.0.94`
- Linux wheel SHA-256: `211e581f5a4670acbbe08fff36a35e9946039d2eea28b80394632d036d1be527`
- License and bundled third-party notices: preserved by the installed wheel in
  its distribution metadata.

## Community MiniMax H3 latent upscaler

- Code: <https://github.com/bbaudio-2025/Comfyui-MMH3-UltimateUpscale>
- Revision: `fe6658f6d144066f14150d3526247b417683ff2b`
- Code license: MIT; complete upstream text and sources are retained in
  `/opt/comfyui/custom_nodes/Comfyui-MMH3-UltimateUpscale` in the worker image.
  This upstream package includes its credited LBH H3 upscaler implementation.
- Separate weights: <https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler>
- Model revision: `3f941d5d182014dd5c0a5e16330420ee2d4aa0c6`
- Model repository license declaration: Apache-2.0.
- BF16 3D v1 checkpoint SHA-256:
  `4f57821f5837f32f7142b67d815606dbd7550f194e5c769f7d6c3f83b146a5e6`.

The image includes code only, not model weights. The private manifest and existing
CloudFront delivery path provide the checksum-verified checkpoint separately.

## MiniMax H3 latent contrast (output-only)

- Code: <https://github.com/Tr1dae/ComfyUI-MiniMaxH3_LatentUpscaler>
- Revision: `895e3c471164423f0ea0e8eaf45eb701efe641ae`.
- No LICENSE file or explicit license declaration was found at this revision;
  do not describe this dependency as MIT/Apache-licensed.
- Upstream sources are retained in `GenAutomationH3Contrast`. Only the unchanged
  `MiniMaxH3LatentContrast` class is registered, using our narrow entrypoint;
  the original entrypoint is retained as `UPSTREAM_INIT.py` for audit.
- No new weights, alternative upscaling, stash HTTP routes or web UI are enabled.

## Historical rollback images only — DaSiWa and KJNodes

The native FL2VA image no longer installs or imports either package below. These
notices describe retained, immutable rollback images, not the active native build.

- Code: <https://github.com/darksidewalker/ComfyUI-DaSiWa-Nodes>
- Revision: `9f5aef4a2748bba9486dda0a7efec7689462d7e0`
- License: GPL-3.0; full upstream LICENSE and unmodified Python sources are
  included in `/opt/comfyui/custom_nodes/ComfyUI-DaSiWa-Nodes` in the worker image.
- The complete upstream package is installed at the exact commit, with its own
  registration code unchanged. No extracted registration shim is used.
- Loader SHA-256: `26954c67c71a547226fdc523566783a33d03ffb2e667fb1d5930386d4cefd2cf`.

### KJNodes (historical)

- Code: <https://github.com/kijai/ComfyUI-KJNodes>
- Revision: `d3cfe21625e5170126ce06fbfcfe1d88108688c3`
- License: GPL-3.0; complete upstream LICENSE and unmodified source package are
  included in `/opt/comfyui/custom_nodes/ComfyUI-KJNodes` in the worker image.
