import hashlib
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
from PIL import Image, UnidentifiedImageError

from gen_automation.video_worker.profiles import VideoProfile, VideoRenderSpec
from gen_automation.video_worker.runtime import VideoExecutionError

_MODEL_NAME = "wan2.2_ti2v_5B_fp16.safetensors"
_TEXT_ENCODER_NAME = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
_VAE_NAME = "wan2.2_vae.safetensors"
_OUTPUT_NODE_ID = "11"
_PROMPT_ID = re.compile(r"^[0-9a-f-]{36}$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
_SAFE_SUBFOLDER = re.compile(r"^[A-Za-z0-9._/-]{0,512}$")
_NEGATIVE_PROMPT = (
    "overexposed, static, blurry details, subtitles, worst quality, low quality, "
    "jpeg artifacts, malformed anatomy, extra limbs, fused fingers, duplicate people, "
    "flicker, frame corruption"
)


def build_wan_workflow(
    *,
    source_filename: str,
    output_prefix: str,
    prompt: str,
    negative_prompt: str,
    seed: int,
    render_spec: VideoRenderSpec,
) -> dict[str, object]:
    return {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": _MODEL_NAME, "weight_dtype": "default"},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": _TEXT_ENCODER_NAME,
                "type": "wan",
                "device": "default",
            },
        },
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": _VAE_NAME}},
        "4": {"class_type": "LoadImage", "inputs": {"image": source_filename}},
        "5": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["2", 0], "text": prompt},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["2", 0],
                "text": ", ".join(
                    part for part in (negative_prompt.strip(), _NEGATIVE_PROMPT) if part
                ),
            },
        },
        "7": {
            "class_type": "Wan22ImageToVideoLatent",
            "inputs": {
                "vae": ["3", 0],
                "start_image": ["4", 0],
                "width": render_spec.width,
                "height": render_spec.height,
                "length": render_spec.native_frame_count,
                "batch_size": 1,
            },
        },
        "8": {
            "class_type": "ModelSamplingSD3",
            "inputs": {"model": ["1", 0], "shift": 8.0},
        },
        "9": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["8", 0],
                "positive": ["5", 0],
                "negative": ["6", 0],
                "latent_image": ["7", 0],
                "seed": seed,
                "steps": 30,
                "cfg": 5.0,
                "sampler_name": "uni_pc",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "10": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["9", 0], "vae": ["3", 0]},
        },
        _OUTPUT_NODE_ID: {
            "class_type": "SaveImage",
            "inputs": {"images": ["10", 0], "filename_prefix": output_prefix},
        },
    }


def _workflow_contract_sha256() -> str:
    representative = build_wan_workflow(
        source_filename="SOURCE.png",
        output_prefix="OUTPUT/frame",
        prompt="PROMPT",
        negative_prompt="NEGATIVE_PROMPT",
        seed=42,
        render_spec=VideoRenderSpec(
            native_frame_count=73,
            fps=24,
            width=832,
            height=480,
            loop_mode="ping_pong",
        ),
    )
    encoded = json.dumps(
        representative,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


WAN_COMFY_WORKFLOW_SHA256 = "ecba3eef1c14abcd4d0d2ba3cec1f53042767f1a177d833dcfd3fd6b11f09ab3"
if _workflow_contract_sha256() != WAN_COMFY_WORKFLOW_SHA256:
    raise RuntimeError("pinned Wan Comfy workflow changed")


@dataclass(slots=True)
class NativeComfyWanExecutor:
    client: httpx2.Client = field(repr=False)
    input_directory: Path
    output_directory: Path
    execution_timeout_seconds: float = 1800.0
    poll_interval_seconds: float = 1.0

    def is_ready(self) -> bool:
        try:
            response = self.client.get("/system_stats", timeout=2.0, follow_redirects=False)
            return response.status_code == 200
        except (httpx2.RequestError, httpx2.TimeoutException):
            return False

    def render(
        self,
        *,
        profile: VideoProfile,
        render_spec: VideoRenderSpec,
        source_path: Path,
        native_frames_path: Path,
        prompt: str,
        negative_prompt: str,
        seed: int,
    ) -> None:
        if profile.adapter != "wan-native-comfy":
            raise VideoExecutionError("video generation failed")
        token = uuid.uuid4().hex
        source_filename = f"video-worker-{token}{source_path.suffix.lower()}"
        comfy_source = self.input_directory / source_filename
        output_subfolder = f"video-worker/{token}"
        output_prefix = f"{output_subfolder}/frame"
        comfy_output_directory = self.output_directory / "video-worker" / token
        try:
            self.input_directory.mkdir(parents=True, exist_ok=True)
            self.output_directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, comfy_source)
            workflow = build_wan_workflow(
                source_filename=source_filename,
                output_prefix=output_prefix,
                prompt=prompt,
                negative_prompt=negative_prompt,
                seed=seed,
                render_spec=render_spec,
            )
            prompt_id = self._submit(workflow)
            images = self._wait_for_images(prompt_id)
            if len(images) != render_spec.native_frame_count:
                raise VideoExecutionError("video generation failed")
            native_frames_path.mkdir(parents=True, exist_ok=True)
            for index, metadata in enumerate(images):
                source_frame = self._resolve_output(metadata, expected_subfolder=output_subfolder)
                target_frame = native_frames_path / f"frame-{index:06d}.png"
                self._copy_verified_frame(
                    source_frame,
                    target_frame,
                    expected_width=render_spec.width,
                    expected_height=render_spec.height,
                )
        except VideoExecutionError:
            raise
        except (OSError, httpx2.RequestError, httpx2.TimeoutException, ValueError):
            raise VideoExecutionError("video generation failed") from None
        finally:
            try:
                comfy_source.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                shutil.rmtree(comfy_output_directory)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _submit(self, workflow: dict[str, object]) -> str:
        response = self.client.post(
            "/prompt",
            json={"prompt": workflow, "client_id": "gen-automation-video-worker"},
            timeout=30.0,
            follow_redirects=False,
        )
        if response.status_code != 200 or len(response.content) > 64 * 1024:
            raise VideoExecutionError("video generation failed")
        try:
            payload: Any = response.json()
            prompt_id = payload["prompt_id"]
        except (KeyError, TypeError, ValueError):
            raise VideoExecutionError("video generation failed") from None
        if not isinstance(prompt_id, str) or _PROMPT_ID.fullmatch(prompt_id) is None:
            raise VideoExecutionError("video generation failed")
        return prompt_id

    def _wait_for_images(self, prompt_id: str) -> list[dict[str, object]]:
        deadline = time.monotonic() + self.execution_timeout_seconds
        while time.monotonic() < deadline:
            response = self.client.get(
                f"/history/{prompt_id}",
                timeout=30.0,
                follow_redirects=False,
            )
            if response.status_code != 200 or len(response.content) > 2 * 1024 * 1024:
                raise VideoExecutionError("video generation failed")
            try:
                history: Any = response.json()
                if prompt_id not in history:
                    time.sleep(self.poll_interval_seconds)
                    continue
                record = history[prompt_id]
                status = record.get("status", {})
                if status.get("status_str") == "error" or status.get("completed") is False:
                    raise VideoExecutionError("video generation failed")
                raw_images = record["outputs"][_OUTPUT_NODE_ID]["images"]
                if not isinstance(raw_images, list):
                    raise ValueError
                images = [item for item in raw_images if isinstance(item, dict)]
                if len(images) != len(raw_images):
                    raise ValueError
                return sorted(images, key=lambda item: str(item.get("filename", "")))
            except VideoExecutionError:
                raise
            except (KeyError, TypeError, ValueError):
                raise VideoExecutionError("video generation failed") from None
        raise VideoExecutionError("video generation failed")

    def _resolve_output(
        self,
        metadata: dict[str, object],
        *,
        expected_subfolder: str,
    ) -> Path:
        filename = metadata.get("filename")
        subfolder = metadata.get("subfolder", "")
        output_type = metadata.get("type")
        if (
            not isinstance(filename, str)
            or _SAFE_FILENAME.fullmatch(filename) is None
            or not isinstance(subfolder, str)
            or _SAFE_SUBFOLDER.fullmatch(subfolder) is None
            or subfolder != expected_subfolder
            or output_type != "output"
        ):
            raise VideoExecutionError("video generation failed")
        root = self.output_directory.resolve(strict=True)
        candidate = (root / subfolder / filename).resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise VideoExecutionError("video generation failed")
        return candidate

    @staticmethod
    def _copy_verified_frame(
        source: Path,
        target: Path,
        *,
        expected_width: int,
        expected_height: int,
    ) -> None:
        try:
            with Image.open(source) as image:
                if (
                    image.format != "PNG"
                    or image.width != expected_width
                    or image.height != expected_height
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise VideoExecutionError("video generation failed")
                image.verify()
            shutil.copyfile(source, target)
        except VideoExecutionError:
            raise
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
            raise VideoExecutionError("video generation failed") from None
