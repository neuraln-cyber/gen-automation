"""On-demand, hash-checked H3 LoRA cache using private signed delivery only."""

import hashlib
import os
import stat
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx2

from gen_automation.i2v_worker.artifacts import _hash_file, _safe_target
from gen_automation.i2v_worker.media import MediaError, validate_grant_url
from gen_automation.i2v_worker.models import H3LoraGrant
from gen_automation.i2v_worker.settings import I2VWorkerSettings


def materialize_h3_lora(grant: H3LoraGrant, settings: I2VWorkerSettings) -> Path:
    url = str(grant.download.url)
    validate_grant_url(url, allow_http=settings.environment == "test")
    parsed = urlsplit(url)
    if settings.require_private_delivery and (
        parsed.scheme != "https"
        or parsed.netloc != settings.model_delivery_domain
        or parsed.path != f"/models/worker/managed-loras/sha256/{grant.sha256}.safetensors"
    ):
        raise MediaError("H3 LoRA private delivery is required")
    unresolved_root = settings.comfy_root / "models/loras/managed-h3"
    if unresolved_root.is_symlink():
        raise MediaError("H3 LoRA cache path is invalid")
    root = _safe_target(settings.comfy_root, "models/loras/managed-h3")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_ISLNK(root.lstat().st_mode):
        raise MediaError("H3 LoRA cache path is invalid")
    target = root / f"{grant.sha256}.safetensors"
    if target.exists():
        if _hash_file(target) != (grant.byte_size, grant.sha256):
            raise MediaError("H3 LoRA cached file failed verification")
        return target
    if grant.download.expires_at.astimezone(UTC) <= datetime.now(UTC):
        raise MediaError("H3 LoRA download grant expired")
    partial = root / f".{grant.sha256}.partial"
    digest = hashlib.sha256()
    offset = 0
    if partial.exists():
        offset, _ = _hash_file(partial)
        if offset > grant.byte_size:
            raise MediaError("H3 LoRA partial file is invalid")
        with partial.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                digest.update(chunk)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        with (
            httpx2.Client(
                follow_redirects=False, trust_env=False, timeout=settings.network_timeout_seconds
            ) as client,
            ThreadPoolExecutor(max_workers=settings.artifact_download_concurrency) as pool,
            os.fdopen(os.open(partial, flags, 0o600), "ab") as output,
        ):
            starts = iter(range(offset, grant.byte_size, settings.artifact_chunk_bytes))
            pending: deque[Future[bytes]] = deque()

            def submit() -> None:
                start = next(starts, None)
                if start is not None:
                    end = min(start + settings.artifact_chunk_bytes, grant.byte_size) - 1
                    pending.append(
                        pool.submit(_range, client, grant, start, end, settings.network_attempts)
                    )

            for _ in range(settings.artifact_download_concurrency):
                submit()
            try:
                while pending:
                    data = pending.popleft().result()
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                    digest.update(data)
                    submit()
            finally:
                for future in pending:
                    future.cancel()
        if partial.stat().st_size != grant.byte_size or digest.hexdigest() != grant.sha256:
            partial.unlink()
            raise MediaError("H3 LoRA download failed checksum verification")
        os.chmod(partial, 0o444)
        os.replace(partial, target)
        return target
    except (OSError, httpx2.HTTPError):
        raise MediaError("H3 LoRA download failed; its verified prefix can resume") from None


def _range(client: httpx2.Client, grant: H3LoraGrant, start: int, end: int, attempts: int) -> bytes:
    expected = end - start + 1
    for attempt in range(attempts):
        try:
            with client.stream(
                "GET", str(grant.download.url), headers={"Range": f"bytes={start}-{end}"}
            ) as response:
                if (
                    response.status_code != 206
                    or response.headers.get("content-range")
                    != f"bytes {start}-{end}/{grant.byte_size}"
                ):
                    raise MediaError("H3 LoRA range download failed")
                data = bytearray()
                for chunk in response.iter_bytes(1024 * 1024):
                    data.extend(chunk)
                    if len(data) > expected:
                        raise MediaError("H3 LoRA range size is invalid")
                if len(data) != expected:
                    raise MediaError("H3 LoRA range size is invalid")
                return bytes(data)
        except (MediaError, httpx2.HTTPError):
            if attempt + 1 == attempts:
                raise MediaError("H3 LoRA download failed") from None
            time.sleep(min(2**attempt, 16))
    raise MediaError("H3 LoRA download failed")
