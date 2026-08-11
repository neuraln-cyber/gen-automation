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
from PIL import Image, ImageOps, UnidentifiedImageError

from gen_automation.video_worker.profiles import (
    A14B_ADULT_VIDEO_PROFILE,
    A14B_ADULT_VIDEO_WORKFLOW_SHA256,
    A14B_VIDEO_PROFILE,
    A14B_VIDEO_WORKFLOW_SHA256,
    HQ_VIDEO_PROFILE,
    HQ_VIDEO_WORKFLOW_SHA256,
    PINNED_VIDEO_PROFILE,
    PINNED_VIDEO_WORKFLOW_SHA256,
    VideoProfile,
    VideoRenderSpec,
    require_video_profile_registration,
)
from gen_automation.video_worker.runtime import VideoExecutionError

_MODEL_NAME = "wan2.2_ti2v_5B_fp16.safetensors"
_TEXT_ENCODER_NAME = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
_VAE_NAME = "wan2.2_vae.safetensors"
_OUTPUT_NODE_ID = "11"
_A14B_HIGH_MODEL_NAME = "smoothMixWan22I2VV20_highQ3KM.gguf"
_A14B_LOW_MODEL_NAME = "smoothMixWan22I2VV20_lowQ3KM.gguf"
_A14B_VAE_NAME = "Wan2_1_VAE_fp32.safetensors"
_A14B_HIGH_LIGHTX_LORA_NAME = (
    "wan2.2_i2v_A14b_high_noise_lora_rank64_lightx2v_4step_1022.safetensors"
)
_A14B_LOW_LIGHTX_LORA_NAME = "wan2.2_i2v_A14b_low_noise_lora_rank64_lightx2v_4step_1022.safetensors"
_A14B_HIGH_ADULT_LORA_NAME = "NSFW-22-H-e8.safetensors"
_A14B_LOW_ADULT_LORA_NAME = "NSFW-22-L-e8.safetensors"
_A14B_OUTPUT_NODE_ID = "18"
NAG_CUSTOM_NODE_REVISION = "c6f27116a8259f5b501d498a09e51c82fa72e35f"
_PROMPT_ID = re.compile(r"^[0-9a-f-]{36}$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
_SAFE_SUBFOLDER = re.compile(r"^[A-Za-z0-9._/-]{0,512}$")


def build_wan_workflow(
    *,
    source_filename: str,
    output_prefix: str,
    prompt: str,
    negative_prompt: str,
    seed: int,
    render_spec: VideoRenderSpec,
    profile: VideoProfile = PINNED_VIDEO_PROFILE,
) -> dict[str, object]:
    registration = require_video_profile_registration(profile.profile_id)
    if registration.profile != profile:
        raise ValueError("video profile identity mismatch")
    if profile in {A14B_VIDEO_PROFILE, A14B_ADULT_VIDEO_PROFILE}:
        return _build_a14b_q4_workflow(
            source_filename=source_filename,
            output_prefix=output_prefix,
            prompt=prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            render_spec=render_spec,
            profile=profile,
        )
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
                    part
                    for part in (
                        negative_prompt.strip(),
                        registration.built_in_negative_prompt,
                    )
                    if part
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


def _build_a14b_q4_workflow(
    *,
    source_filename: str,
    output_prefix: str,
    prompt: str,
    negative_prompt: str,
    seed: int,
    render_spec: VideoRenderSpec,
    profile: VideoProfile,
) -> dict[str, object]:
    registration = require_video_profile_registration(profile.profile_id)
    is_adult = profile is A14B_ADULT_VIDEO_PROFILE
    high_model: list[object] = ["8", 0]
    low_model: list[object] = ["10", 0]
    workflow: dict[str, object] = {
        "1": {
            "class_type": "UnetLoaderGGUF",
            "inputs": {"unet_name": _A14B_HIGH_MODEL_NAME},
        },
        "2": {
            "class_type": "UnetLoaderGGUF",
            "inputs": {"unet_name": _A14B_LOW_MODEL_NAME},
        },
        "3": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": _TEXT_ENCODER_NAME,
                "type": "wan",
                "device": "default",
            },
        },
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": _A14B_VAE_NAME}},
        "5": {"class_type": "LoadImage", "inputs": {"image": source_filename}},
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["3", 0], "text": prompt},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["3", 0],
                "text": ", ".join(
                    part
                    for part in (
                        negative_prompt.strip(),
                        registration.built_in_negative_prompt,
                    )
                    if part
                ),
            },
        },
        "8": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["1", 0],
                "lora_name": _A14B_HIGH_LIGHTX_LORA_NAME,
                "strength_model": 1.0,
            },
        },
        "10": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["2", 0],
                "lora_name": _A14B_LOW_LIGHTX_LORA_NAME,
                "strength_model": 1.0,
            },
        },
    }
    if is_adult:
        workflow["9"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["8", 0],
                "lora_name": _A14B_HIGH_ADULT_LORA_NAME,
                "strength_model": 0.9,
            },
        }
        workflow["11"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["10", 0],
                "lora_name": _A14B_LOW_ADULT_LORA_NAME,
                "strength_model": 0.9,
            },
        }
        high_model = ["9", 0]
        low_model = ["11", 0]
    workflow.update(
        {
            "12": {
                "class_type": "ModelSamplingSD3",
                "inputs": {"model": high_model, "shift": 8.0},
            },
            "13": {
                "class_type": "ModelSamplingSD3",
                "inputs": {"model": low_model, "shift": 8.0},
            },
            "14": {
                "class_type": "WanImageToVideo",
                "inputs": {
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "vae": ["4", 0],
                    "start_image": ["5", 0],
                    "width": render_spec.width,
                    "height": render_spec.height,
                    "length": render_spec.native_frame_count,
                    "batch_size": 1,
                },
            },
            "15": {
                "class_type": "KSamplerAdvanced",
                "inputs": {
                    "model": ["12", 0],
                    "positive": ["14", 0],
                    "negative": ["14", 1],
                    "latent_image": ["14", 2],
                    "add_noise": "enable",
                    "noise_seed": seed,
                    "steps": 6,
                    "cfg": 1.0,
                    "sampler_name": "euler",
                    "scheduler": "simple",
                    "start_at_step": 0,
                    "end_at_step": 3,
                    "return_with_leftover_noise": "enable",
                },
            },
            "16": {
                "class_type": "KSamplerWithNAG (Advanced)",
                "inputs": {
                    "model": ["13", 0],
                    "positive": ["14", 0],
                    "negative": ["14", 1],
                    "nag_negative": ["14", 1],
                    "latent_image": ["15", 0],
                    "add_noise": "disable",
                    "noise_seed": 0,
                    "steps": 6,
                    "cfg": 1.0,
                    "nag_scale": 30.0,
                    "nag_tau": 2.5,
                    "nag_alpha": 0.25,
                    "nag_sigma_end": 1.0,
                    "sampler_name": "euler",
                    "scheduler": "simple",
                    "start_at_step": 3,
                    "end_at_step": 10_000,
                    "return_with_leftover_noise": "disable",
                },
            },
            "17": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["16", 0], "vae": ["4", 0]},
            },
            _A14B_OUTPUT_NODE_ID: {
                "class_type": "SaveImage",
                "inputs": {"images": ["17", 0], "filename_prefix": output_prefix},
            },
        }
    )
    return workflow


def _workflow_contract_sha256(profile: VideoProfile = PINNED_VIDEO_PROFILE) -> str:
    representative = build_wan_workflow(
        source_filename="SOURCE.png",
        output_prefix="OUTPUT/frame",
        prompt="PROMPT",
        negative_prompt="NEGATIVE_PROMPT",
        seed=42,
        render_spec=VideoRenderSpec(
            native_frame_count=profile.default_native_frame_count,
            fps=profile.fps,
            width=profile.landscape_width,
            height=profile.landscape_height,
            loop_mode=profile.loop_mode,
        ),
        profile=profile,
    )
    encoded = json.dumps(
        representative,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


WAN_COMFY_WORKFLOW_SHA256 = PINNED_VIDEO_WORKFLOW_SHA256
if _workflow_contract_sha256() != WAN_COMFY_WORKFLOW_SHA256:
    raise RuntimeError("pinned Wan Comfy workflow changed")

WAN_COMFY_HQ_WORKFLOW_SHA256 = HQ_VIDEO_WORKFLOW_SHA256
if _workflow_contract_sha256(HQ_VIDEO_PROFILE) != WAN_COMFY_HQ_WORKFLOW_SHA256:
    raise RuntimeError("pinned HQ Wan Comfy workflow changed")

WAN_COMFY_A14B_WORKFLOW_SHA256 = A14B_VIDEO_WORKFLOW_SHA256
if _workflow_contract_sha256(A14B_VIDEO_PROFILE) != WAN_COMFY_A14B_WORKFLOW_SHA256:
    raise RuntimeError("pinned A14B Wan Comfy workflow changed")

WAN_COMFY_A14B_ADULT_WORKFLOW_SHA256 = A14B_ADULT_VIDEO_WORKFLOW_SHA256
if _workflow_contract_sha256(A14B_ADULT_VIDEO_PROFILE) != WAN_COMFY_A14B_ADULT_WORKFLOW_SHA256:
    raise RuntimeError("pinned adult A14B Wan Comfy workflow changed")


def _workflow_registry_sha256() -> str:
    encoded = json.dumps(
        {
            PINNED_VIDEO_PROFILE.profile_id: WAN_COMFY_WORKFLOW_SHA256,
            HQ_VIDEO_PROFILE.profile_id: WAN_COMFY_HQ_WORKFLOW_SHA256,
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


WAN_COMFY_WORKFLOW_REGISTRY_SHA256 = (
    "19e4426429def8dc731b2b8802f714fa85a9e3ec61b5fcc79609c26363783f69"
)
if _workflow_registry_sha256() != WAN_COMFY_WORKFLOW_REGISTRY_SHA256:
    raise RuntimeError("pinned Wan Comfy workflow registry changed")


def _a14b_workflow_registry_sha256() -> str:
    encoded = json.dumps(
        {
            A14B_VIDEO_PROFILE.profile_id: WAN_COMFY_A14B_WORKFLOW_SHA256,
            A14B_ADULT_VIDEO_PROFILE.profile_id: WAN_COMFY_A14B_ADULT_WORKFLOW_SHA256,
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


WAN_COMFY_A14B_WORKFLOW_REGISTRY_SHA256 = (
    "ceee10e914a13dc601a7f64d97237a2736e82c68aefb85c59a5cd9d34cb90d83"
)
if _a14b_workflow_registry_sha256() != WAN_COMFY_A14B_WORKFLOW_REGISTRY_SHA256:
    raise RuntimeError("pinned A14B Wan Comfy workflow registry changed")


@dataclass(slots=True)
class NativeComfyWanExecutor:
    client: httpx2.Client = field(repr=False)
    input_directory: Path
    output_directory: Path
    # A non-None value is a bounded test/operator override. Production resolves
    # the immutable timeout from the selected profile registration.
    execution_timeout_seconds: float | None = None
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
        if profile.adapter not in {"wan-native-comfy", "wan-native-comfy-gguf"}:
            raise VideoExecutionError("video generation failed")
        registration = require_video_profile_registration(profile.profile_id)
        if registration.profile != profile:
            raise VideoExecutionError("video generation failed")
        token = uuid.uuid4().hex
        is_a14b = profile in {A14B_VIDEO_PROFILE, A14B_ADULT_VIDEO_PROFILE}
        source_filename = (
            f"video-worker-{token}.png"
            if is_a14b
            else f"video-worker-{token}{source_path.suffix.lower()}"
        )
        comfy_source = self.input_directory / source_filename
        output_subfolder = f"video-worker/{token}"
        output_prefix = f"{output_subfolder}/frame"
        comfy_output_directory = self.output_directory / "video-worker" / token
        try:
            self.input_directory.mkdir(parents=True, exist_ok=True)
            self.output_directory.mkdir(parents=True, exist_ok=True)
            if is_a14b:
                self._prepare_a14b_source(
                    source_path=source_path,
                    target_path=comfy_source,
                    render_spec=render_spec,
                )
            else:
                shutil.copyfile(source_path, comfy_source)
            workflow = build_wan_workflow(
                source_filename=source_filename,
                output_prefix=output_prefix,
                prompt=prompt,
                negative_prompt=negative_prompt,
                seed=seed,
                render_spec=render_spec,
                profile=profile,
            )
            prompt_id = self._submit(workflow)
            timeout_seconds = (
                self.execution_timeout_seconds
                if self.execution_timeout_seconds is not None
                else float(registration.execution_timeout_seconds)
            )
            output_node_id = (
                _A14B_OUTPUT_NODE_ID
                if profile in {A14B_VIDEO_PROFILE, A14B_ADULT_VIDEO_PROFILE}
                else _OUTPUT_NODE_ID
            )
            images = self._wait_for_images(
                prompt_id,
                output_node_id=output_node_id,
                timeout_seconds=timeout_seconds,
            )
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

    @staticmethod
    def _prepare_a14b_source(
        *,
        source_path: Path,
        target_path: Path,
        render_spec: VideoRenderSpec,
    ) -> None:
        # The dimensions are derived from source aspect before this call. A
        # centered crop removes only the rounding remainder and never stretches
        # the subject to the model canvas.
        with Image.open(source_path) as raw_image:
            transposed = ImageOps.exif_transpose(raw_image)
            converted = transposed.convert("RGB")
            fitted = ImageOps.fit(
                converted,
                (render_spec.width, render_spec.height),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            fitted.save(target_path, format="PNG", optimize=False)

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

    def _wait_for_images(
        self,
        prompt_id: str,
        *,
        output_node_id: str,
        timeout_seconds: float,
    ) -> list[dict[str, object]]:
        deadline = time.monotonic() + timeout_seconds
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
                raw_images = record["outputs"][output_node_id]["images"]
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
