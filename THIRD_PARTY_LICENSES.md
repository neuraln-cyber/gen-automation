# Third-party notices for I2V face stabilization

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
