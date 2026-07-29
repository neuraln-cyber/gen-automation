from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from gen_automation.domain.canonical import canonical_json_bytes
from gen_automation.integrations.patreon.driver import (
    PatreonDriverError,
    PatreonDriverOutcome,
    PatreonDriverRequest,
    PatreonDriverResult,
)

PATREON_BROWSER_SCHEMA = "gen-automation.patreon-browser-result.v1"
PATREON_BROWSER_SIGNATURE_HEADER = "X-Gen-Automation-Signature"
PATREON_BROWSER_SIGNATURE_SCHEMA = "gen-automation.patreon-browser-request.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DETAIL_CODE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_MAX_RESPONSE_BYTES = 16 * 1024
_MIN_SHARED_SECRET_BYTES = 32
_MAX_SHARED_SECRET_BYTES = 4096


class _StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _SidecarResponse(_StrictResponse):
    schema_version: str
    intent_id: str
    intent_digest: str
    package_id: str
    package_sha256: str
    outcome: PatreonDriverOutcome
    remote_identifier: str | None = Field(default=None, max_length=200)
    remote_url: str | None = Field(default=None, max_length=2048)
    detail_code: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_outcome_fields(self) -> _SidecarResponse:
        if self.outcome == PatreonDriverOutcome.PUBLISHED:
            if self.remote_identifier is None or self.remote_url is None:
                raise ValueError("published outcomes require remote identity")
        elif self.remote_identifier is not None or self.remote_url is not None:
            raise ValueError("non-published outcomes cannot contain remote identity")
        if self.detail_code is not None and _DETAIL_CODE.fullmatch(self.detail_code) is None:
            raise ValueError("detail code is invalid")
        return self


class PatreonSidecarDriver:
    """Narrow HTTP client for the separately deployed Chromium sidecar.

    There are deliberately no automatic transport retries. Once ``publish`` is
    invoked, a connection loss can mean that the irreversible UI click happened;
    the durable caller must mark the outcome unknown and reconcile it manually.
    """

    def __init__(
        self,
        *,
        http_client: httpx2.AsyncClient,
        endpoint_url: str,
        timeout_seconds: float,
        max_package_bytes: int,
        shared_secret: str,
    ) -> None:
        parsed = urlsplit(endpoint_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Patreon browser sidecar URL is invalid")
        if timeout_seconds <= 0:
            raise ValueError("Patreon browser sidecar timeout must be positive")
        if max_package_bytes <= 0:
            raise ValueError("Patreon browser package limit must be positive")
        _shared_secret_bytes(shared_secret)
        self._http_client = http_client
        self._endpoint_url = endpoint_url
        self._timeout = httpx2.Timeout(timeout_seconds)
        self._max_package_bytes = max_package_bytes
        self.__shared_secret = shared_secret

    async def publish(self, request: PatreonDriverRequest) -> PatreonDriverResult:
        package = await asyncio.to_thread(
            _read_request_package,
            request,
            self._max_package_bytes,
        )
        idempotency_key = patreon_browser_idempotency_key(
            intent_id=request.intent_id,
            intent_digest=request.intent_digest,
            package_id=request.package_id,
            package_sha256=request.package_sha256,
            profile_reference=request.browser_profile_reference,
        )
        signature = calculate_patreon_browser_signature(
            shared_secret=self.__shared_secret,
            intent_id=request.intent_id,
            intent_digest=request.intent_digest,
            package_id=request.package_id,
            package_sha256=request.package_sha256,
            profile_reference=request.browser_profile_reference,
            idempotency_key=idempotency_key,
        )
        try:
            response = await self._http_client.post(
                self._endpoint_url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/zip",
                    "Idempotency-Key": idempotency_key,
                    "X-Gen-Automation-Intent-Id": str(request.intent_id),
                    "X-Gen-Automation-Intent-Digest": request.intent_digest,
                    "X-Gen-Automation-Package-Id": str(request.package_id),
                    "X-Gen-Automation-Package-Sha256": request.package_sha256,
                    "X-Gen-Automation-Browser-Profile": request.browser_profile_reference,
                    PATREON_BROWSER_SIGNATURE_HEADER: signature,
                },
                content=package,
                timeout=self._timeout,
            )
        except (httpx2.TimeoutException, httpx2.TransportError) as error:
            raise PatreonDriverError("Patreon browser sidecar outcome is unavailable") from error
        if (
            response.status_code != 200
            or response.headers.get("content-type", "").split(";", 1)[0].strip()
            != "application/json"
            or len(response.content) > _MAX_RESPONSE_BYTES
        ):
            raise PatreonDriverError("Patreon browser sidecar returned an invalid response")
        try:
            result = _SidecarResponse.model_validate_json(response.content)
        except (ValueError, ValidationError) as error:
            raise PatreonDriverError("Patreon browser sidecar response is malformed") from error
        if (
            result.schema_version != PATREON_BROWSER_SCHEMA
            or result.intent_id != str(request.intent_id)
            or result.intent_digest != request.intent_digest
            or result.package_id != str(request.package_id)
            or result.package_sha256 != request.package_sha256
        ):
            raise PatreonDriverError("Patreon browser sidecar response identity does not match")
        return PatreonDriverResult(
            outcome=result.outcome,
            remote_identifier=result.remote_identifier,
            remote_url=result.remote_url,
            detail_code=result.detail_code,
        )


def _read_request_package(request: PatreonDriverRequest, maximum: int) -> bytes:
    if _SHA256.fullmatch(request.intent_digest) is None:
        raise ValueError("Patreon intent digest is invalid")
    if _SHA256.fullmatch(request.package_sha256) is None:
        raise ValueError("Patreon package digest is invalid")
    if _PROFILE_REFERENCE.fullmatch(request.browser_profile_reference) is None:
        raise ValueError("Patreon browser profile reference is invalid")
    if not isinstance(request.package_path, Path) or not request.package_path.is_file():
        raise ValueError("Patreon package path is unavailable")
    size = request.package_path.stat().st_size
    if size <= 0 or size > maximum:
        raise ValueError("Patreon package size is invalid")
    package = request.package_path.read_bytes()
    if len(package) != size or hashlib.sha256(package).hexdigest() != request.package_sha256:
        raise ValueError("Patreon package bytes do not match")
    return package


def patreon_browser_idempotency_key(
    *,
    intent_id: UUID,
    intent_digest: str,
    package_id: UUID,
    package_sha256: str,
    profile_reference: str,
) -> str:
    """Bind one replay key to the complete immutable browser request identity."""

    return hashlib.sha256(
        _request_identity_bytes(
            intent_id=intent_id,
            intent_digest=intent_digest,
            package_id=package_id,
            package_sha256=package_sha256,
            profile_reference=profile_reference,
        )
    ).hexdigest()


def calculate_patreon_browser_signature(
    *,
    shared_secret: str,
    intent_id: UUID,
    intent_digest: str,
    package_id: UUID,
    package_sha256: str,
    profile_reference: str,
    idempotency_key: str,
) -> str:
    """Authenticate the complete sidecar request without exposing the shared secret."""

    secret = _shared_secret_bytes(shared_secret)
    payload = canonical_json_bytes(
        {
            "schema": PATREON_BROWSER_SIGNATURE_SCHEMA,
            "method": "POST",
            "path": "/v1/publish",
            "intent_id": str(intent_id),
            "intent_digest": intent_digest,
            "package_id": str(package_id),
            "package_sha256": package_sha256,
            "profile_reference": profile_reference,
            "idempotency_key": idempotency_key,
        }
    )
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _request_identity_bytes(
    *,
    intent_id: UUID,
    intent_digest: str,
    package_id: UUID,
    package_sha256: str,
    profile_reference: str,
) -> bytes:
    return canonical_json_bytes(
        {
            "schema": PATREON_BROWSER_SIGNATURE_SCHEMA,
            "intent_id": str(intent_id),
            "intent_digest": intent_digest,
            "package_id": str(package_id),
            "package_sha256": package_sha256,
            "profile_reference": profile_reference,
        }
    )


def _shared_secret_bytes(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError("Patreon browser shared secret must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("Patreon browser shared secret must be valid UTF-8") from error
    if not _MIN_SHARED_SECRET_BYTES <= len(encoded) <= _MAX_SHARED_SECRET_BYTES:
        raise ValueError(
            "Patreon browser shared secret must contain between "
            f"{_MIN_SHARED_SECRET_BYTES} and {_MAX_SHARED_SECRET_BYTES} UTF-8 bytes"
        )
    return encoded
