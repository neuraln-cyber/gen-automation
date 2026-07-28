from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from pydantic import SecretStr

from gen_automation.config import Environment, Settings
from gen_automation.domain.runtime_bindings import (
    SALAD_WORKER_ALLOWED_RUNTIME_BINDINGS,
    SALAD_WORKER_REQUIRED_RUNTIME_BINDINGS,
    SALAD_WORKER_RUNTIME_BINDING_REFERENCES,
    WORKER_ALLOWED_UPLOAD_ORIGIN_BINDING,
    WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING,
    WORKER_ARTIFACT_BUCKET_BINDING,
    WORKER_ARTIFACT_ENDPOINT_URL_BINDING,
    WORKER_ARTIFACT_REGION_BINDING,
    WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING,
    WORKER_ARTIFACT_SESSION_TOKEN_BINDING,
    WORKER_ENVIRONMENT_BINDING,
    WORKER_MODEL_MANIFEST_JSON_BINDING,
    WORKER_MODEL_MANIFEST_SHA256_BINDING,
    WORKER_VERIFICATION_KEYS_BINDING,
)


class RuntimeSecretResolutionError(Exception):
    """A redacted runtime-binding failure safe for controller error handling."""


class RuntimeSecretResolver(Protocol):
    """Resolve approved target/reference pairs only for one provider request."""

    async def resolve_many(
        self,
        bindings: Mapping[str, str],
    ) -> Mapping[str, str]: ...


_SETTINGS_BINDINGS = {
    WORKER_ALLOWED_UPLOAD_ORIGIN_BINDING: "salad_worker_allowed_upload_origin",
    WORKER_MODEL_MANIFEST_JSON_BINDING: "salad_worker_model_manifest_json",
    WORKER_MODEL_MANIFEST_SHA256_BINDING: "salad_worker_model_manifest_sha256",
    WORKER_ARTIFACT_BUCKET_BINDING: "salad_worker_artifact_bucket",
    WORKER_ARTIFACT_REGION_BINDING: "salad_worker_artifact_region",
    WORKER_ARTIFACT_ENDPOINT_URL_BINDING: "salad_worker_artifact_endpoint_url",
    WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING: "salad_worker_artifact_access_key_id",
    WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING: ("salad_worker_artifact_secret_access_key"),
    WORKER_ARTIFACT_SESSION_TOKEN_BINDING: "salad_worker_artifact_session_token",
}


class ConfiguredRuntimeSecretResolver:
    """In-memory resolver backed only by deployment-injected ``SecretStr`` values."""

    __slots__ = ("_closed", "_require_complete", "_values")

    def __init__(
        self,
        values: Mapping[str, SecretStr],
        *,
        require_complete: bool,
    ) -> None:
        copied = dict(values)
        if not set(copied).issubset(SALAD_WORKER_ALLOWED_RUNTIME_BINDINGS):
            raise RuntimeSecretResolutionError("runtime secret configuration is invalid")
        if any(not value.get_secret_value().strip() for value in copied.values()):
            raise RuntimeSecretResolutionError("runtime secret configuration is invalid")
        if require_complete and not SALAD_WORKER_REQUIRED_RUNTIME_BINDINGS.issubset(copied):
            raise RuntimeSecretResolutionError("runtime secret configuration is incomplete")
        self._values = copied
        self._require_complete = require_complete
        self._closed = False

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"{type(self).__name__}(state={state}, values=<redacted>)"

    @property
    def closed(self) -> bool:
        return self._closed

    async def resolve_many(
        self,
        bindings: Mapping[str, str],
    ) -> Mapping[str, str]:
        if self._closed:
            raise RuntimeSecretResolutionError("runtime secret resolver is unavailable")
        requested = dict(bindings)
        if not set(requested).issubset(SALAD_WORKER_ALLOWED_RUNTIME_BINDINGS):
            raise RuntimeSecretResolutionError("runtime binding is not allowed")
        if any(
            SALAD_WORKER_RUNTIME_BINDING_REFERENCES[name] != reference
            for name, reference in requested.items()
        ):
            raise RuntimeSecretResolutionError("runtime binding reference is invalid")
        if self._require_complete and set(requested) != set(self._values):
            raise RuntimeSecretResolutionError("runtime binding set is incomplete")

        resolved: dict[str, str] = {}
        for name in requested:
            value = self._values.get(name)
            if value is None:
                raise RuntimeSecretResolutionError("runtime binding could not be resolved")
            raw_value = value.get_secret_value()
            if not raw_value:
                raise RuntimeSecretResolutionError("runtime binding could not be resolved")
            resolved[name] = raw_value
        return resolved

    async def aclose(self) -> None:
        self._values.clear()
        self._closed = True


def build_runtime_secret_resolver(
    settings: Settings,
) -> ConfiguredRuntimeSecretResolver | None:
    """Build an environment-backed resolver without copying values into durable state."""

    if not settings.salad_enabled:
        return None

    values: dict[str, SecretStr] = {
        WORKER_ENVIRONMENT_BINDING: SecretStr("production"),
    }
    public_key = settings.worker_verification_public_key
    if settings.worker_signing_key_id is not None and public_key is not None:
        values[WORKER_VERIFICATION_KEYS_BINDING] = SecretStr(
            json.dumps(
                {settings.worker_signing_key_id: public_key},
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    for binding_name, setting_name in _SETTINGS_BINDINGS.items():
        configured = getattr(settings, setting_name)
        if isinstance(configured, SecretStr) and configured.get_secret_value().strip():
            values[binding_name] = configured

    protected_gpu = settings.gpu_allocation_enabled and settings.environment in {
        Environment.STAGING,
        Environment.PRODUCTION,
    }
    return ConfiguredRuntimeSecretResolver(
        values,
        require_complete=protected_gpu,
    )
