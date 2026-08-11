import hashlib
import hmac
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final


class ModelIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ModelRuntimeContract:
    manifest: ModelArtifact
    artifacts: tuple[ModelArtifact, ...]


MODEL_MANIFEST: Final = ModelArtifact(
    path=Path("/opt/video-worker/model-manifest.json"),
    size_bytes=1472,
    sha256="83fe998813b2f16662064b4f11e327bb4608efa00b991616cc46df7d996ef65e",
)
MODEL_ARTIFACTS: Final = (
    ModelArtifact(
        path=Path("/opt/comfyui/models/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors"),
        size_bytes=9_999_658_848,
        sha256="456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e",
    ),
    ModelArtifact(
        path=Path("/opt/comfyui/models/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"),
        size_bytes=6_735_906_897,
        sha256="c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
    ),
    ModelArtifact(
        path=Path("/opt/comfyui/models/vae/wan2.2_vae.safetensors"),
        size_bytes=1_409_400_960,
        sha256="e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156",
    ),
)

BASE_VIDEO_PROFILE_IDS: Final = frozenset(
    {
        "wan2.2-ti2v-5b-comfy-v1",
        "wan2.2-ti2v-5b-comfy-hq-v1",
    }
)
A14B_VIDEO_PROFILE_IDS: Final = frozenset(
    {
        "wan2.2-smoothmix-i2v-a14b-q3-v1",
        "wan2.2-smoothmix-i2v-a14b-q3-adult-v1",
    }
)
A14B_MODEL_MANIFEST: Final = ModelArtifact(
    path=Path("/opt/video-worker/model-manifest.json"),
    size_bytes=7_137,
    sha256="f7d9aa4fc7783ca76b605d1dc520e3ef812257c40c4aef9803e635d28c37bbbb",
)
A14B_MODEL_ARTIFACTS: Final = (
    ModelArtifact(
        path=Path("/opt/comfyui/models/diffusion_models/smoothMixWan22I2VV20_highQ3KM.gguf"),
        size_bytes=7_176_106_496,
        sha256="f9ead6cc6183a02bfb8827c852a9f2c71289acb10086993c9495b5c242ee096b",
    ),
    ModelArtifact(
        path=Path("/opt/comfyui/models/diffusion_models/smoothMixWan22I2VV20_lowQ3KM.gguf"),
        size_bytes=7_176_106_496,
        sha256="8ce49c8a9b272a69cddab5042a4424b6689cc067db17707263f3ef2795564c17",
    ),
    ModelArtifact(
        path=Path("/opt/comfyui/models/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"),
        size_bytes=6_735_906_897,
        sha256="c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
    ),
    ModelArtifact(
        path=Path("/opt/comfyui/models/vae/Wan2_1_VAE_fp32.safetensors"),
        size_bytes=507_591_244,
        sha256="fd1531e31cdd20af005a9fb66e69afe4ea481a6db9b07cd889ece2e1ca2e67b9",
    ),
    ModelArtifact(
        path=Path(
            "/opt/comfyui/models/loras/"
            "wan2.2_i2v_A14b_high_noise_lora_rank64_lightx2v_4step_1022.safetensors"
        ),
        size_bytes=634_645_944,
        sha256="887c3bdeb74e83859c920438e16ca31f39ab18ce189abc5f0e36f8348c5bbb19",
    ),
    ModelArtifact(
        path=Path("/opt/comfyui/models/loras/NSFW-22-H-e8.safetensors"),
        size_bytes=613_516_752,
        sha256="34e2144d3cd65360f97d09ccbe03e1c39a096df6c9234af5fe3899d1b63cda39",
    ),
    ModelArtifact(
        path=Path(
            "/opt/comfyui/models/loras/"
            "wan2.2_i2v_A14b_low_noise_lora_rank64_lightx2v_4step_1022.safetensors"
        ),
        size_bytes=739_472_104,
        sha256="8833bd4fd7c8eabebf0bc8ee5cfaf47f4f310ce116928a02c1adf8941dd4b0f1",
    ),
    ModelArtifact(
        path=Path("/opt/comfyui/models/loras/NSFW-22-L-e8.safetensors"),
        size_bytes=613_516_752,
        sha256="d6b783742f4d5fd63a0223ae1d5bf64fc995a6b408480ac2a00528ae0d4146db",
    ),
)

MODEL_RUNTIME_CONTRACTS: Final = {
    BASE_VIDEO_PROFILE_IDS: ModelRuntimeContract(MODEL_MANIFEST, MODEL_ARTIFACTS),
    A14B_VIDEO_PROFILE_IDS: ModelRuntimeContract(A14B_MODEL_MANIFEST, A14B_MODEL_ARTIFACTS),
}


def verify_model_artifact(artifact: ModelArtifact) -> None:
    digest = hashlib.sha256()
    try:
        status = artifact.path.lstat()
        if not stat.S_ISREG(status.st_mode) or status.st_size != artifact.size_bytes:
            raise ModelIntegrityError("video model integrity check failed")
        with artifact.path.open("rb") as source:
            while chunk := source.read(8 * 1024 * 1024):
                digest.update(chunk)
    except ModelIntegrityError:
        raise
    except OSError:
        raise ModelIntegrityError("video model integrity check failed") from None
    if not hmac.compare_digest(digest.hexdigest(), artifact.sha256):
        raise ModelIntegrityError("video model integrity check failed")


def verify_model_runtime(*, profile_ids: frozenset[str] | None = None) -> None:
    selected_profile_ids = profile_ids or BASE_VIDEO_PROFILE_IDS
    try:
        contract = MODEL_RUNTIME_CONTRACTS[selected_profile_ids]
    except KeyError:
        raise ModelIntegrityError("video model integrity check failed") from None
    verify_model_artifact(contract.manifest)
    for artifact in contract.artifacts:
        verify_model_artifact(artifact)
