from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gen_automation.domain.deliverability import PATREON_MAX_ARCHIVE_BYTES
from gen_automation.integrations.patreon import (
    PATREON_BROWSER_SCHEMA,
    PatreonDriverOutcome,
    PatreonDriverResult,
)
from gen_automation.integrations.patreon.sidecar import (
    PATREON_BROWSER_SIGNATURE_HEADER,
    calculate_patreon_browser_signature,
    patreon_browser_idempotency_key,
)
from gen_automation.patreon_browser.ledger import (
    PatreonBrowserClaimState,
    PatreonBrowserIdempotencyLedger,
    PatreonBrowserLedgerConflictError,
    PatreonBrowserLedgerError,
    PatreonBrowserRequestIdentity,
)
from gen_automation.patreon_browser.package import (
    PatreonBrowserPackage,
    PatreonBrowserPackageError,
    load_patreon_browser_package,
)
from gen_automation.patreon_browser.publisher import PlaywrightPatreonPublisher

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DETAIL_CODE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")


class PatreonBrowserSettings(BaseSettings):
    """Environment-only configuration; the signed-in profile is a mounted volume."""

    model_config = SettingsConfigDict(
        env_prefix="GEN_AUTOMATION_PATREON_BROWSER_",
        extra="ignore",
        frozen=True,
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    profile_root: Path = Path("/profiles")
    spool_root: Path = Path("/var/lib/patreon-browser")
    state_path: Path = Path("/state/idempotency.sqlite3")
    profile_reference: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
        max_length=64,
    )
    shared_secret: SecretStr
    editor_url: str = "https://www.patreon.com/posts/new"
    headless: bool = True
    action_timeout_seconds: float = Field(default=180, ge=10, le=600)
    max_package_bytes: int = Field(
        default=PATREON_MAX_ARCHIVE_BYTES,
        ge=PATREON_MAX_ARCHIVE_BYTES,
        le=PATREON_MAX_ARCHIVE_BYTES,
    )

    @model_validator(mode="after")
    def validate_paths_and_url(self) -> PatreonBrowserSettings:
        for label, path in (
            ("profile root", self.profile_root),
            ("spool root", self.spool_root),
            ("state path", self.state_path),
        ):
            if not path.is_absolute() or path == Path(path.anchor):
                raise ValueError(f"Patreon browser {label} must be an absolute non-root path")
        secret = self.shared_secret.get_secret_value()
        try:
            secret_bytes = secret.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("Patreon browser shared secret must be valid UTF-8") from error
        if not 32 <= len(secret_bytes) <= 4096:
            raise ValueError(
                "Patreon browser shared secret must contain between 32 and 4096 UTF-8 bytes"
            )
        parsed = urlsplit(self.editor_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"patreon.com", "www.patreon.com"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Patreon browser editor URL must be an exact HTTPS Patreon URL")
        return self


class PatreonBrowserPublisher(Protocol):
    async def publish(
        self,
        package: PatreonBrowserPackage,
        *,
        profile_reference: str,
    ) -> PatreonDriverResult: ...


def create_app(
    settings: PatreonBrowserSettings | None = None,
    *,
    publisher: PatreonBrowserPublisher | None = None,
) -> FastAPI:
    resolved = settings or PatreonBrowserSettings()
    resolved.spool_root.mkdir(parents=True, exist_ok=True)
    ledger = PatreonBrowserIdempotencyLedger(resolved.state_path)
    ledger.initialize()
    active_publisher = publisher or PlaywrightPatreonPublisher(
        profile_root=resolved.profile_root,
        editor_url=resolved.editor_url,
        headless=resolved.headless,
        action_timeout_seconds=resolved.action_timeout_seconds,
    )
    browser_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(
        title="gen-automation Patreon browser",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/publish")
    async def publish(request: Request) -> JSONResponse:
        identity = _request_identity(
            request,
            shared_secret=resolved.shared_secret.get_secret_value(),
            allowed_profile_reference=resolved.profile_reference,
        )
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != "application/zip":
            raise HTTPException(status_code=415, detail="application/zip is required")
        with (
            TemporaryDirectory(
                prefix="request-",
                dir=resolved.spool_root,
            ) as request_directory,
            TemporaryDirectory(
                prefix="extract-",
                dir=resolved.spool_root,
            ) as extraction_directory,
        ):
            archive_path = Path(request_directory) / "handoff.zip"
            digest = hashlib.sha256()
            size = 0
            with archive_path.open("xb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > resolved.max_package_bytes:
                        raise HTTPException(status_code=413, detail="package is too large")
                    digest.update(chunk)
                    await asyncio.to_thread(output.write, chunk)
            if size == 0 or digest.hexdigest() != identity.package_sha256:
                raise HTTPException(status_code=422, detail="package digest does not match")
            try:
                package = await asyncio.to_thread(
                    load_patreon_browser_package,
                    archive_path,
                    Path(extraction_directory),
                    max_package_bytes=resolved.max_package_bytes,
                )
            except PatreonBrowserPackageError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            try:
                claim = await asyncio.to_thread(ledger.claim, identity)
            except PatreonBrowserLedgerConflictError as error:
                raise HTTPException(
                    status_code=409,
                    detail="idempotency identity conflicts",
                ) from error
            except PatreonBrowserLedgerError as error:
                raise HTTPException(
                    status_code=503,
                    detail="idempotency state is unavailable",
                ) from error
            replayed = claim.state != PatreonBrowserClaimState.NEW
            if claim.state == PatreonBrowserClaimState.TERMINAL:
                if claim.result is None:
                    raise HTTPException(status_code=503, detail="idempotency state is unavailable")
                result = claim.result
            elif claim.state == PatreonBrowserClaimState.UNRESOLVED:
                result = PatreonDriverResult(
                    outcome=PatreonDriverOutcome.UNKNOWN,
                    detail_code="idempotency_outcome_unresolved",
                )
            else:
                async with browser_lock:
                    result = await active_publisher.publish(
                        package,
                        profile_reference=identity.profile_reference,
                    )
                _validate_result(result)
                try:
                    await asyncio.to_thread(ledger.complete, identity, result)
                except PatreonBrowserLedgerError as error:
                    raise HTTPException(
                        status_code=503,
                        detail="idempotency state is unavailable",
                    ) from error
        _validate_result(result)
        return JSONResponse(
            _response_body(identity, result),
            headers={"Idempotency-Replayed": str(replayed).lower()},
        )

    return app


def _request_identity(
    request: Request,
    *,
    shared_secret: str,
    allowed_profile_reference: str,
) -> PatreonBrowserRequestIdentity:
    try:
        intent_id = UUID(request.headers["x-gen-automation-intent-id"])
        intent_digest = request.headers["x-gen-automation-intent-digest"]
        package_id = UUID(request.headers["x-gen-automation-package-id"])
        package_sha256 = request.headers["x-gen-automation-package-sha256"]
        profile_reference = request.headers["x-gen-automation-browser-profile"]
        idempotency_key = request.headers["idempotency-key"]
        provided_signature = request.headers[PATREON_BROWSER_SIGNATURE_HEADER]
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=401, detail="request authentication is invalid") from error
    expected_key = patreon_browser_idempotency_key(
        intent_id=intent_id,
        intent_digest=intent_digest,
        package_id=package_id,
        package_sha256=package_sha256,
        profile_reference=profile_reference,
    )
    if (
        _SHA256.fullmatch(intent_digest) is None
        or _SHA256.fullmatch(package_sha256) is None
        or _PROFILE_REFERENCE.fullmatch(profile_reference) is None
        or idempotency_key != expected_key
    ):
        raise HTTPException(status_code=422, detail="request identity is invalid")
    expected_signature = calculate_patreon_browser_signature(
        shared_secret=shared_secret,
        intent_id=intent_id,
        intent_digest=intent_digest,
        package_id=package_id,
        package_sha256=package_sha256,
        profile_reference=profile_reference,
        idempotency_key=idempotency_key,
    )
    signature_matches = hmac.compare_digest(
        provided_signature.encode("utf-8"),
        expected_signature.encode("ascii"),
    )
    profile_matches = hmac.compare_digest(profile_reference, allowed_profile_reference)
    if not signature_matches or not profile_matches:
        raise HTTPException(status_code=401, detail="request authentication is invalid")
    return PatreonBrowserRequestIdentity(
        idempotency_key=idempotency_key,
        intent_id=intent_id,
        intent_digest=intent_digest,
        package_id=package_id,
        package_sha256=package_sha256,
        profile_reference=profile_reference,
    )


def _response_body(
    identity: PatreonBrowserRequestIdentity,
    result: PatreonDriverResult,
) -> dict[str, str | None]:
    return {
        "schema_version": PATREON_BROWSER_SCHEMA,
        "intent_id": str(identity.intent_id),
        "intent_digest": identity.intent_digest,
        "package_id": str(identity.package_id),
        "package_sha256": identity.package_sha256,
        "outcome": result.outcome.value,
        "remote_identifier": result.remote_identifier,
        "remote_url": result.remote_url,
        "detail_code": result.detail_code,
    }


def _validate_result(result: PatreonDriverResult) -> None:
    if not isinstance(result, PatreonDriverResult):
        raise HTTPException(status_code=502, detail="browser publisher returned an invalid result")
    if result.outcome == PatreonDriverOutcome.PUBLISHED:
        if result.remote_identifier is None or result.remote_url is None:
            raise HTTPException(status_code=502, detail="published post identity is unavailable")
    elif result.remote_identifier is not None or result.remote_url is not None:
        raise HTTPException(status_code=502, detail="browser publisher result is invalid")
    if result.detail_code is not None and _DETAIL_CODE.fullmatch(result.detail_code) is None:
        raise HTTPException(status_code=502, detail="browser publisher detail code is invalid")
