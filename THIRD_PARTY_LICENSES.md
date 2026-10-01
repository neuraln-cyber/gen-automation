# Third-party notices for I2V extensions

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
