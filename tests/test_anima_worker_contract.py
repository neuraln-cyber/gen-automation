import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

from gen_automation.gpu_worker.artifacts import (
    ArtifactKind,
    ModelArtifactSpec,
    bootstrap_artifacts,
    create_artifact_manifest,
)
from gen_automation.gpu_worker.models import (
    DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES,
    validate_approved_workflow,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / "workflows" / "anima-base-v1.json"
DETAILER_WORKFLOW_PATH = ROOT / "workflows" / "anima-base-detailer-v1.json"


def _safetensors(label: str) -> bytes:
    body = label.encode("ascii")
    header = json.dumps(
        {"weight": {"data_offsets": [0, len(body)], "dtype": "U8", "shape": [len(body)]}},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return len(header).to_bytes(8, "little") + header + body


def _artifact(kind: ArtifactKind, filename: str) -> tuple[ModelArtifactSpec, bytes]:
    content = _safetensors(kind.value)
    return (
        ModelArtifactSpec(
            logical_name=f"anima-{kind.value}",
            kind=kind,
            source_object_id=f"anima/{filename}",
            source_object_version_id=f"version-{kind.value}",
            sha256=hashlib.sha256(content).hexdigest(),
            exact_size_bytes=len(content),
            max_size_bytes=len(content),
            target_filename=filename,
        ),
        content,
    )


class _Downloader:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    async def stream(self, artifact: ModelArtifactSpec) -> AsyncIterator[bytes]:
        yield self.blobs[artifact.logical_name]


async def test_anima_artifacts_materialize_into_their_native_comfy_roots(
    tmp_path: Path,
) -> None:
    entries = (
        _artifact(ArtifactKind.DIFFUSION_MODEL, "miaomiao-anima-base.safetensors"),
        _artifact(ArtifactKind.TEXT_ENCODER, "qwen_3_06b_base.safetensors"),
        _artifact(ArtifactKind.VAE, "qwen_image_vae.safetensors"),
    )
    artifacts = tuple(entry[0] for entry in entries)
    manifest = create_artifact_manifest(artifacts)
    model_root = tmp_path / "models"
    roots = {
        ArtifactKind.CHECKPOINT: model_root / "checkpoints",
        ArtifactKind.DIFFUSION_MODEL: model_root / "diffusion_models",
        ArtifactKind.LORA: model_root / "loras",
        ArtifactKind.TEXT_ENCODER: model_root / "text_encoders",
        ArtifactKind.VAE: model_root / "vae",
    }
    for root in roots.values():
        root.mkdir(parents=True)

    result = await bootstrap_artifacts(
        manifest,
        _Downloader({artifact.logical_name: content for artifact, content in entries}),
        expected_manifest_sha256=manifest.manifest_sha256,
        checkpoint_root=roots[ArtifactKind.CHECKPOINT],
        diffusion_model_root=roots[ArtifactKind.DIFFUSION_MODEL],
        lora_root=roots[ArtifactKind.LORA],
        text_encoder_root=roots[ArtifactKind.TEXT_ENCODER],
        vae_root=roots[ArtifactKind.VAE],
    )

    assert {artifact.kind for artifact in result.artifacts} == {
        ArtifactKind.DIFFUSION_MODEL,
        ArtifactKind.TEXT_ENCODER,
        ArtifactKind.VAE,
    }
    for artifact in artifacts:
        assert (roots[artifact.kind] / artifact.target_filename).is_file()
    assert not tuple(roots[ArtifactKind.CHECKPOINT].iterdir())
    assert not tuple(roots[ArtifactKind.LORA].iterdir())


def test_anima_workflow_uses_native_loaders_and_a_model_only_lora_chain() -> None:
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))

    assert workflow["1"] == {
        "class_type": "UNETLoader",
        "inputs": {
            "unet_name": {"$gen": "diffusion_model.runtime_filename"},
            "weight_dtype": "default",
        },
    }
    assert workflow["2"] == {
        "class_type": "GenAutomationLoraChain",
        "inputs": {"mode": "model_only", "model": ["1", 0]},
    }
    assert workflow["3"]["class_type"] == "CLIPLoader"
    assert workflow["3"]["inputs"] == {
        "clip_name": {"$gen": "text_encoder.runtime_filename"},
        "device": "default",
        "type": "stable_diffusion",
    }
    assert workflow["4"] == {
        "class_type": "VAELoader",
        "inputs": {"vae_name": {"$gen": "vae.runtime_filename"}},
    }
    assert workflow["8"]["inputs"]["model"] == ["2", 0]
    assert workflow["9"]["inputs"]["vae"] == ["4", 0]
    assert not {
        "CheckpointLoaderSimple",
        "CLIPSetLastLayer",
        "LoraLoader",
    }.intersection(node["class_type"] for node in workflow.values())

    workflow["2"] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {
            "lora_name": "style.safetensors",
            "model": ["1", 0],
            "strength_model": 0.5,
        },
    }
    validate_approved_workflow(workflow, DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES)


def test_anima_detailer_workflow_preserves_native_loaders_and_refines_the_decoded_face() -> None:
    workflow = json.loads(DETAILER_WORKFLOW_PATH.read_text(encoding="utf-8"))

    assert workflow["1"] == {
        "class_type": "UNETLoader",
        "inputs": {
            "unet_name": {"$gen": "diffusion_model.runtime_filename"},
            "weight_dtype": "default",
        },
    }
    assert workflow["2"] == {
        "class_type": "GenAutomationLoraChain",
        "inputs": {"mode": "model_only", "model": ["1", 0]},
    }
    assert workflow["3"]["class_type"] == "CLIPLoader"
    assert workflow["4"]["class_type"] == "VAELoader"
    assert workflow["13"] == {
        "class_type": "UltralyticsDetectorProvider",
        "inputs": {"model_name": {"$gen": "detector.comfy_name"}},
    }
    assert workflow["14"]["class_type"] == "FaceDetailer"
    assert workflow["14"]["inputs"]["image"] == ["9", 0]
    assert workflow["14"]["inputs"]["model"] == ["2", 0]
    assert workflow["14"]["inputs"]["clip"] == ["3", 0]
    assert workflow["14"]["inputs"]["vae"] == ["4", 0]
    assert workflow["15"]["inputs"]["images"] == ["14", 0]

    workflow["2"] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {
            "lora_name": "style.safetensors",
            "model": ["1", 0],
            "strength_model": 0.5,
        },
    }
    validate_approved_workflow(workflow, DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES)


def test_worker_image_precreates_all_anima_model_directories() -> None:
    dockerfile = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")

    assert "test -d /opt/comfyui/comfy/ldm/anima" in dockerfile
    assert "test -f /opt/comfyui/comfy/text_encoders/anima.py" in dockerfile
    for directory in (
        "/opt/comfyui/models/diffusion_models",
        "/opt/comfyui/models/text_encoders",
        "/opt/comfyui/models/vae",
    ):
        assert dockerfile.count(directory) == 2
