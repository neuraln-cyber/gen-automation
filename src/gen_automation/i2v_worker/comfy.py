from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import httpx2

_PROMPT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_HEALTH_INTERVAL_SECONDS = 10.0
_HEALTH_FAILURE_LIMIT = 3
_REQUIRED_NODES = (
    (
        "KSamplerWithNAG (Advanced)",
        "/object_info/KSamplerWithNAG%20%28Advanced%29",
    ),
)


class ComfyError(Exception):
    """A redacted ComfyUI failure."""


class ComfyLoraError(ComfyError):
    """A managed H3 loader rejected the selected file before sampling."""


class ComfyUnhealthyError(ComfyError):
    """The inference process needs recovery, not another indefinite history poll."""


class ComfyMemoryError(ComfyUnhealthyError):
    """GPU memory exhaustion reported by the inference process."""


class ComfyClient:
    def __init__(
        self,
        *,
        base_url: str,
        request_timeout_seconds: float,
        network_attempts: int,
        poll_seconds: float,
        profile: str = "wan22",
        source_resolution_enabled: bool = False,
    ) -> None:
        if base_url != "http://127.0.0.1:8188":
            raise ComfyError("ComfyUI endpoint is invalid")
        self.network_attempts = network_attempts
        self.poll_seconds = poll_seconds
        self.required_nodes = (
            _REQUIRED_NODES
            if profile == "wan22"
            else tuple(
                (name, f"/object_info/{name}")
                for name in (
                    "MiniMaxH3ImageToVideo",
                    "MiniMaxH3SigmaShift",
                    "SaveVideo",
                    "VAEDecodeAudio",
                    "ManagedH3LoraLoader",
                )
            )
        )
        self.client = httpx2.AsyncClient(
            base_url=base_url,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx2.Timeout(request_timeout_seconds, connect=5),
            limits=httpx2.Limits(max_connections=2, max_keepalive_connections=2),
        )
        if source_resolution_enabled:
            self.required_nodes += tuple(
                (name, f"/object_info/{name}")
                for name in (
                    "ManagedH3SourceUpscale",
                    "MinimaxH3LatentUpscaler3D",
                    "MMH3SplitUpscale",
                    "MMH3TemporalSplitParamsV10",
                    "MMH3SpatialSplitParamsV10",
                )
            )

    async def close(self) -> None:
        await self.client.aclose()

    async def ready(self) -> bool:
        try:
            response = await self.client.get("/system_stats")
            if response.status_code != 200:
                return False
            for node_name, node_path in self.required_nodes:
                node_response = await self.client.get(node_path)
                if node_response.status_code != 200:
                    return False
                node_info = node_response.json()
                if not isinstance(node_info, dict) or set(node_info) != {node_name}:
                    return False
            return True
        except Exception:
            return False

    async def execute(self, workflow: dict[str, Any], output_root: Path) -> tuple[Path, ...]:
        response = await self._request("POST", "/prompt", json={"prompt": workflow})
        try:
            body = response.json()
            prompt_id = body["prompt_id"]
        except (KeyError, TypeError, ValueError):
            raise ComfyError("ComfyUI rejected the workflow") from None
        if not isinstance(prompt_id, str) or _PROMPT_ID.fullmatch(prompt_id) is None:
            raise ComfyError("ComfyUI returned an invalid prompt identity")

        unhealthy_checks = 0
        next_health_check = 0.0
        while True:
            try:
                history_response = await self._request("GET", f"/history/{prompt_id}")
            except ComfyError:
                raise ComfyUnhealthyError("ComfyUI history is unavailable") from None
            try:
                history = history_response.json()
            except ValueError:
                raise ComfyError("ComfyUI returned invalid history") from None
            record = history.get(prompt_id) if isinstance(history, dict) else None
            if record is None:
                # A CUDA failure can kill ComfyUI's prompt thread while its HTTP
                # server keeps returning empty history forever. This checks
                # health, NOT elapsed sampling time: healthy slow jobs have no
                # new deadline. Require repeated failures, with bounded probes.
                now = asyncio.get_running_loop().time()
                if now >= next_health_check:
                    unhealthy_checks = 0 if await self._runtime_healthy() else unhealthy_checks + 1
                    if unhealthy_checks >= _HEALTH_FAILURE_LIMIT:
                        raise ComfyUnhealthyError("ComfyUI runtime health repeatedly failed")
                    next_health_check = asyncio.get_running_loop().time() + _HEALTH_INTERVAL_SECONDS
                await asyncio.sleep(self.poll_seconds)
                continue
            if not isinstance(record, dict):
                raise ComfyError("ComfyUI returned invalid history")
            status = record.get("status")
            if isinstance(status, dict) and status.get("status_str") in {
                "error",
                "failed",
                "cancelled",
                "canceled",
            }:
                messages = status.get("messages", [])
                if _memory_failure(messages):
                    raise ComfyMemoryError("ComfyUI GPU memory exhausted")
                if isinstance(messages, list) and any(
                    isinstance(message, list)
                    and len(message) == 2
                    and message[0] == "execution_error"
                    and isinstance(message[1], dict)
                    and message[1].get("node_type") == "ManagedH3LoraLoader"
                    for message in messages
                ):
                    raise ComfyLoraError("selected H3 LoRA could not be applied")
                raise ComfyError("ComfyUI generation failed")
            outputs = record.get("outputs")
            if isinstance(status, dict) and status.get("completed") is True:
                return _output_paths(outputs, output_root)
            if isinstance(outputs, dict) and "14" in outputs:
                return _output_paths(outputs, output_root)
            await asyncio.sleep(self.poll_seconds)

    async def _runtime_healthy(self) -> bool:
        try:
            response = await self.client.get("/system_stats", timeout=5.0)
            return response.status_code == 200
        except httpx2.HTTPError:
            return False

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        for attempt in range(self.network_attempts):
            try:
                response = await self.client.request(method, path, **kwargs)
                if response.status_code == 200:
                    return response
                if response.status_code not in {408, 425, 429, 500, 502, 503, 504}:
                    raise ComfyError("ComfyUI request failed")
            except ComfyError:
                raise
            except httpx2.HTTPError:
                if attempt + 1 >= self.network_attempts:
                    break
            if attempt + 1 < self.network_attempts:
                await asyncio.sleep(min(2**attempt, 16))
        raise ComfyError("ComfyUI is unavailable")


def _memory_failure(messages: object) -> bool:
    if not isinstance(messages, list):
        return False
    for message in messages:
        if (
            isinstance(message, list)
            and len(message) == 2
            and message[0] == "execution_error"
            and isinstance(message[1], dict)
        ):
            # Inspect only the exception summary, never log it or its inputs.
            kind = str(message[1].get("exception_type", "")).lower()
            detail = str(message[1].get("exception_message", "")).lower()
            if "outofmemoryerror" in kind or ("cuda" in detail and "out of memory" in detail):
                return True
    return False


def _output_paths(value: object, output_root: Path) -> tuple[Path, ...]:
    if not isinstance(value, dict):
        raise ComfyError("ComfyUI output is missing")
    node = value.get("14")
    images = node.get("images") if isinstance(node, dict) else None
    if not isinstance(images, list) or not images:
        raise ComfyError("ComfyUI output is missing")
    resolved_root = output_root.resolve()
    paths: list[Path] = []
    for image in images:
        if not isinstance(image, dict) or image.get("type") != "output":
            raise ComfyError("ComfyUI output is invalid")
        filename = image.get("filename")
        subfolder = image.get("subfolder", "")
        if not isinstance(filename, str) or not isinstance(subfolder, str):
            raise ComfyError("ComfyUI output is invalid")
        candidate = (resolved_root / subfolder / filename).resolve()
        if not candidate.is_relative_to(resolved_root) or not candidate.is_file():
            raise ComfyError("ComfyUI output is invalid")
        paths.append(candidate)
    return tuple(paths)
