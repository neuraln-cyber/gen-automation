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


def verify_model_runtime() -> None:
    verify_model_artifact(MODEL_MANIFEST)
    for artifact in MODEL_ARTIFACTS:
        verify_model_artifact(artifact)
