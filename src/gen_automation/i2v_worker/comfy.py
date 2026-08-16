from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import httpx2

_PROMPT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_REQUIRED_NODES = (
    (
        "KSamplerWithNAG (Advanced)",
        "/object_info/KSamplerWithNAG%20%28Advanced%29",
    ),
)


class ComfyError(Exception):
    """A redacted ComfyUI failure."""


class ComfyClient:
    def __init__(
        self,
        *,
        base_url: str,
        request_timeout_seconds: float,
        network_attempts: int,
        poll_seconds: float,
    ) -> None:
        if base_url != "http://127.0.0.1:8188":
            raise ComfyError("ComfyUI endpoint is invalid")
        self.network_attempts = network_attempts
        self.poll_seconds = poll_seconds
        self.client = httpx2.AsyncClient(
            base_url=base_url,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx2.Timeout(request_timeout_seconds, connect=5),
            limits=httpx2.Limits(max_connections=2, max_keepalive_connections=2),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def ready(self) -> bool:
        try:
            response = await self.client.get("/system_stats")
            if response.status_code != 200:
                return False
            for node_name, node_path in _REQUIRED_NODES:
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

        while True:
            history_response = await self._request("GET", f"/history/{prompt_id}")
            try:
                history = history_response.json()
            except ValueError:
                raise ComfyError("ComfyUI returned invalid history") from None
            record = history.get(prompt_id) if isinstance(history, dict) else None
            if record is None:
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
                raise ComfyError("ComfyUI generation failed")
            outputs = record.get("outputs")
            if isinstance(status, dict) and status.get("completed") is True:
                return _output_paths(outputs, output_root)
            if isinstance(outputs, dict) and "14" in outputs:
                return _output_paths(outputs, output_root)
            await asyncio.sleep(self.poll_seconds)

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
