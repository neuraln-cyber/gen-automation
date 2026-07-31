from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import uuid4

import boto3
from botocore.config import Config
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

    async def aclose(self) -> None: ...


class StsClient(Protocol):
    """The synchronous boto3 surface used to mint one worker credential lease."""

    def assume_role(self, **kwargs: object) -> Mapping[str, object]: ...

    def close(self) -> None: ...


_ARTIFACT_READER_SESSION_DURATION_SECONDS = 10_800


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


class AwsAssumeRoleRuntimeSecretResolver:
    """Resolve worker artifact credentials from a fresh, three-hour STS lease."""

    __slots__ = ("_closed", "_require_complete", "_role_arn", "_sts_client", "_values")

    def __init__(
        self,
        values: Mapping[str, SecretStr],
        *,
        role_arn: str,
        sts_client: StsClient,
        require_complete: bool,
    ) -> None:
        copied = dict(values)
        if not set(copied).issubset(SALAD_WORKER_ALLOWED_RUNTIME_BINDINGS):
            raise RuntimeSecretResolutionError("runtime secret configuration is invalid")
        if any(not value.get_secret_value().strip() for value in copied.values()):
            raise RuntimeSecretResolutionError("runtime secret configuration is invalid")
        if set(copied) & _ARTIFACT_CREDENTIAL_BINDINGS:
            raise RuntimeSecretResolutionError("runtime secret configuration is invalid")
        available = set(copied) | _ARTIFACT_CREDENTIAL_BINDINGS
        required = SALAD_WORKER_REQUIRED_RUNTIME_BINDINGS | {WORKER_ARTIFACT_SESSION_TOKEN_BINDING}
        if require_complete and not required.issubset(available):
            raise RuntimeSecretResolutionError("runtime secret configuration is incomplete")
        self._values = copied
        self._role_arn = role_arn
        self._sts_client = sts_client
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
        _validate_requested_bindings(requested)
        available = set(self._values) | _ARTIFACT_CREDENTIAL_BINDINGS
        if self._require_complete and set(requested) != available:
            raise RuntimeSecretResolutionError("runtime binding set is incomplete")
        if not set(requested).issubset(available):
            raise RuntimeSecretResolutionError("runtime binding could not be resolved")
        requested_credentials = set(requested) & _ARTIFACT_CREDENTIAL_BINDINGS
        if requested_credentials and requested_credentials != _ARTIFACT_CREDENTIAL_BINDINGS:
            raise RuntimeSecretResolutionError("runtime artifact credential set is incomplete")

        resolved = {
            name: value.get_secret_value()
            for name, value in self._values.items()
            if name in requested
        }
        if requested_credentials:
            credentials = await self._assume_role()
            resolved.update(
                {
                    name: credentials[name]
                    for name in _ARTIFACT_CREDENTIAL_BINDINGS
                    if name in requested
                }
            )
        if set(resolved) != set(requested):
            raise RuntimeSecretResolutionError("runtime binding could not be resolved")
        return resolved

    async def _assume_role(self) -> dict[str, str]:
        try:
            response = await asyncio.to_thread(
                self._sts_client.assume_role,
                RoleArn=self._role_arn,
                RoleSessionName=f"gen-automation-salad-{uuid4().hex[:20]}",
                DurationSeconds=_ARTIFACT_READER_SESSION_DURATION_SECONDS,
            )
            raw_credentials = response.get("Credentials")
            if not isinstance(raw_credentials, Mapping):
                raise ValueError
            access_key = raw_credentials.get("AccessKeyId")
            secret_key = raw_credentials.get("SecretAccessKey")
            session_token = raw_credentials.get("SessionToken")
            expiration = raw_credentials.get("Expiration")
            if (
                not isinstance(access_key, str)
                or not access_key.strip()
                or not isinstance(secret_key, str)
                or not secret_key.strip()
                or not isinstance(session_token, str)
                or not session_token.strip()
                or not isinstance(expiration, datetime)
                or expiration.tzinfo is None
                or expiration.astimezone(UTC) <= datetime.now(UTC) + timedelta(minutes=5)
            ):
                raise ValueError
        except Exception:
            raise RuntimeSecretResolutionError("runtime binding could not be resolved") from None
        return {
            WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING: access_key,
            WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING: secret_key,
            WORKER_ARTIFACT_SESSION_TOKEN_BINDING: session_token,
        }

    async def aclose(self) -> None:
        self._values.clear()
        self._closed = True
        await asyncio.to_thread(self._sts_client.close)


_ARTIFACT_CREDENTIAL_BINDINGS = frozenset(
    {
        WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING,
        WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING,
        WORKER_ARTIFACT_SESSION_TOKEN_BINDING,
    }
)


def configured_runtime_binding_references(settings: Settings) -> dict[str, str]:
    """Return deterministic, non-secret references for configured worker values."""

    if not settings.salad_enabled:
        return {}
    configured_names = set(_configured_runtime_secret_values(settings))
    if settings.salad_worker_artifact_role_arn is not None:
        configured_names.update(_ARTIFACT_CREDENTIAL_BINDINGS)
    return {
        name: SALAD_WORKER_RUNTIME_BINDING_REFERENCES[name] for name in sorted(configured_names)
    }


def build_runtime_secret_resolver(
    settings: Settings,
    *,
    sts_client: StsClient | None = None,
) -> RuntimeSecretResolver | None:
    """Build an environment-backed resolver without copying values into durable state."""

    if not settings.salad_enabled:
        return None

    values = _configured_runtime_secret_values(settings)
    protected_gpu = settings.gpu_allocation_enabled and settings.environment in {
        Environment.STAGING,
        Environment.PRODUCTION,
    }
    role_arn = settings.salad_worker_artifact_role_arn
    if role_arn is not None:
        if sts_client is None:
            boto_config = Config(
                connect_timeout=5,
                read_timeout=15,
                retries={"mode": "standard", "max_attempts": 3},
                max_pool_connections=2,
                user_agent_extra="gen-automation-salad-artifacts/1",
            )
            sts_client = cast(
                StsClient,
                boto3.client(
                    "sts",
                    region_name=_required_secret_value(settings.salad_worker_artifact_region),
                    config=boto_config,
                ),
            )
        return AwsAssumeRoleRuntimeSecretResolver(
            values,
            role_arn=role_arn,
            sts_client=sts_client,
            require_complete=protected_gpu,
        )
    return ConfiguredRuntimeSecretResolver(
        values,
        require_complete=protected_gpu,
    )


def _configured_runtime_secret_values(settings: Settings) -> dict[str, SecretStr]:
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
    return values


def _validate_requested_bindings(requested: Mapping[str, str]) -> None:
    if not set(requested).issubset(SALAD_WORKER_ALLOWED_RUNTIME_BINDINGS):
        raise RuntimeSecretResolutionError("runtime binding is not allowed")
    if any(
        SALAD_WORKER_RUNTIME_BINDING_REFERENCES[name] != reference
        for name, reference in requested.items()
    ):
        raise RuntimeSecretResolutionError("runtime binding reference is invalid")


def _required_secret_value(value: SecretStr | None) -> str:
    if value is None or not value.get_secret_value().strip():
        raise RuntimeSecretResolutionError("runtime secret configuration is incomplete")
    return value.get_secret_value()
