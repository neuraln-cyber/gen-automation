"""Immutable model hydration into a persistent RunPod network volume."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import httpx2

from gen_automation.i2v_worker.artifacts import ModelBootstrapError
from gen_automation.i2v_worker.models import ModelObject
from gen_automation.i2v_worker.runpod_models import RunPodModelGrant
from gen_automation.i2v_worker.settings import I2VWorkerSettings

_CACHE_SCHEMA = "gen-automation/i2v-runpod-volume/v1"
_EXECUTION_SCHEMA = "gen-automation/i2v-runpod-execution/v1"


class RunPodVolumeBootstrapper:
    """Hydrate exact content-addressed objects once, then install safe symlinks."""

    def __init__(
        self,
        settings: I2VWorkerSettings,
        *,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self.settings = settings
        self._http_client = http_client

    def claim_execution(self, *, submission_key: str, provider_job_id: str) -> None:
        """Reject an automatic RunPod replay before any paid inference work."""

        if (
            len(submission_key) != 64
            or any(character not in "0123456789abcdef" for character in submission_key)
            or not provider_job_id
            or len(provider_job_id) > 128
        ):
            raise ModelBootstrapError("execution claim failed")
        root = (self.settings.volume_root.resolve() / "gen-automation/i2v-executions").resolve()
        if not root.is_relative_to(self.settings.volume_root.resolve()):
            raise ModelBootstrapError("execution claim failed")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = root / f"{submission_key}.json"
        payload = json.dumps(
            {
                "schema": _EXECUTION_SCHEMA,
                "submission_key": submission_key,
                "provider_job_id": provider_job_id,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(marker, flags, 0o400)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise ModelBootstrapError("execution was already claimed") from None
        except OSError:
            marker.unlink(missing_ok=True)
            raise ModelBootstrapError("execution claim failed") from None

    def bootstrap(self, grants: tuple[RunPodModelGrant, ...]) -> tuple[Path, ...]:
        models = self.settings.model_objects
        grants_by_role = {grant.role: grant for grant in grants}
        if set(grants_by_role) != {model.role for model in models}:
            raise ModelBootstrapError("model bootstrap failed")
        for model in models:
            grant = grants_by_role[model.role]
            if grant.byte_size != model.byte_size or grant.sha256 != model.sha256:
                raise ModelBootstrapError("model bootstrap failed")

        root = self._artifact_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _exclusive_lock(root / ".hydrate.lock"):
            if not self._ready(root, models):
                if self.settings.require_preseeded_volume:
                    raise ModelBootstrapError("preseeded model volume is unavailable")
                for model in models:
                    self._materialize(root, model, grants_by_role[model.role])
                self._write_marker(root, models)
            return tuple(self._install(root, model) for model in models)

    def _artifact_root(self) -> Path:
        volume = self.settings.volume_root.resolve()
        root = (volume / "gen-automation/i2v" / self.settings.artifact_identity_sha256).resolve()
        if not root.is_relative_to(volume):
            raise ModelBootstrapError("model bootstrap failed")
        return root

    def _ready(self, root: Path, models: tuple[ModelObject, ...]) -> bool:
        marker = root / "ready.json"
        try:
            metadata = marker.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                return False
            raw = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if raw != self._marker_payload(models):
            return False
        for model in models:
            path = root / f"{model.sha256}.safetensors"
            try:
                item = path.lstat()
            except OSError:
                return False
            if (
                stat.S_ISLNK(item.st_mode)
                or not stat.S_ISREG(item.st_mode)
                or item.st_size != model.byte_size
                or item.st_mode & 0o222
            ):
                return False
        return True

    def _materialize(
        self,
        root: Path,
        model: ModelObject,
        grant: RunPodModelGrant,
    ) -> None:
        target = root / f"{model.sha256}.safetensors"
        if target.exists():
            if _hash_file(target) == (model.byte_size, model.sha256):
                os.chmod(target, 0o444)
                return
            target.unlink()
        partial = root / f".{model.sha256}.partial"
        for full_attempt in range(2):
            try:
                self._download(model, grant, partial)
                if _hash_file(partial) != (model.byte_size, model.sha256):
                    raise ModelBootstrapError("model bootstrap failed")
                os.chmod(partial, 0o444)
                os.replace(partial, target)
                return
            except ModelBootstrapError:
                partial.unlink(missing_ok=True)
                if full_attempt:
                    raise
        raise ModelBootstrapError("model bootstrap failed")

    def _download(
        self,
        model: ModelObject,
        grant: RunPodModelGrant,
        partial: Path,
    ) -> None:
        if grant.expires_at.tzinfo is None or grant.expires_at.astimezone(UTC) <= datetime.now(UTC):
            raise ModelBootstrapError("model bootstrap failed")
        resume_at = partial.stat().st_size if partial.exists() else 0
        if resume_at > model.byte_size:
            raise ModelBootstrapError("model bootstrap failed")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(partial, flags, 0o600)
            with os.fdopen(descriptor, "ab", closefd=True) as output:
                for start in range(resume_at, model.byte_size, self.settings.artifact_chunk_bytes):
                    end = min(start + self.settings.artifact_chunk_bytes, model.byte_size) - 1
                    self._download_range(str(grant.url), start, end, model.byte_size, output)
                    output.flush()
                    os.fsync(output.fileno())
        except (OSError, httpx2.HTTPError):
            raise ModelBootstrapError("model bootstrap failed") from None

    def _download_range(
        self,
        url: str,
        start: int,
        end: int,
        total: int,
        output: BinaryIO,
    ) -> None:
        expected = end - start + 1
        owns_client = self._http_client is None
        client = self._http_client or httpx2.Client(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx2.Timeout(self.settings.network_timeout_seconds),
        )
        try:
            for attempt in range(self.settings.network_attempts):
                try:
                    with client.stream(
                        "GET",
                        url,
                        headers={"Range": f"bytes={start}-{end}"},
                    ) as response:
                        if (
                            response.status_code != 206
                            or response.headers.get("content-range")
                            != f"bytes {start}-{end}/{total}"
                            or int(response.headers.get("content-length", "-1")) != expected
                        ):
                            raise ModelBootstrapError("model bootstrap failed")
                        received = 0
                        for chunk in response.iter_bytes():
                            received += len(chunk)
                            if received > expected:
                                raise ModelBootstrapError("model bootstrap failed")
                            output.write(chunk)
                        if received != expected:
                            raise OSError
                    return
                except ModelBootstrapError:
                    raise
                except (OSError, httpx2.HTTPError, ValueError):
                    if attempt + 1 >= self.settings.network_attempts:
                        raise ModelBootstrapError("model bootstrap failed") from None
                    time.sleep(min(2**attempt, 16))
        finally:
            if owns_client:
                client.close()

    def _install(self, root: Path, model: ModelObject) -> Path:
        cache = root / f"{model.sha256}.safetensors"
        comfy = self.settings.comfy_root.resolve()
        unresolved_target = comfy / model.install_path
        try:
            target_parent = unresolved_target.parent.resolve(strict=False)
        except OSError:
            raise ModelBootstrapError("model bootstrap failed") from None
        if not target_parent.is_relative_to(comfy):
            raise ModelBootstrapError("model bootstrap failed")
        target = target_parent / unresolved_target.name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if target.exists() or target.is_symlink():
            try:
                if target.is_symlink() and target.resolve() == cache.resolve():
                    return target
            except OSError:
                pass
            raise ModelBootstrapError("model bootstrap failed")
        temporary = target.with_name(f".{target.name}.runpod-link")
        temporary.unlink(missing_ok=True)
        try:
            temporary.symlink_to(cache)
            os.replace(temporary, target)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise ModelBootstrapError("model bootstrap failed") from None
        return target

    def _write_marker(self, root: Path, models: tuple[ModelObject, ...]) -> None:
        temporary = root / ".ready.json.partial"
        try:
            temporary.write_text(
                json.dumps(
                    self._marker_payload(models),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o444)
            os.replace(temporary, root / "ready.json")
        except OSError:
            temporary.unlink(missing_ok=True)
            raise ModelBootstrapError("model bootstrap failed") from None

    def _marker_payload(self, models: tuple[ModelObject, ...]) -> dict[str, object]:
        return {
            "schema": _CACHE_SCHEMA,
            "artifact_identity_sha256": self.settings.artifact_identity_sha256,
            "objects": [
                {
                    "role": model.role,
                    "byte_size": model.byte_size,
                    "sha256": model.sha256,
                    "install_path": model.install_path,
                }
                for model in models
            ],
        }


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OSError
        with path.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError:
        raise ModelBootstrapError("model bootstrap failed") from None
    return size, digest.hexdigest()


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        raise ModelBootstrapError("model bootstrap failed") from None
    try:
        if os.name == "posix":
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "posix":
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
