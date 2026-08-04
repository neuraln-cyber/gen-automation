from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import hashlib
import io
import json
import re
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from gen_automation.domain.enums import SemanticIssueCode, SemanticVerdict
from gen_automation.semantic import (
    ANATOMY_ASSESSMENT_PROMPT,
    ANATOMY_IMAGE_MAX_LONG_EDGE,
    ANATOMY_IMAGE_MAX_SOURCE_PIXELS,
    ANATOMY_OUTPUT_SCHEMA,
    MAX_SEMANTIC_ISSUES,
    SEMANTIC_SCHEMA_VERSION,
    prompt_sha256,
    schema_sha256,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SHA256 = re.compile(_SHA256_PATTERN)
_SUPPORTED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_IMAGE_FORMAT_CONTENT_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_ABSOLUTE_MAX_IMAGE_BYTES = 64 * 1024 * 1024
_REQUEST_OVERHEAD_BYTES = 256 * 1024


class SemanticGatewaySettings(BaseSettings):
    """Environment-only settings for the isolated semantic gateway."""

    model_config = SettingsConfigDict(
        env_prefix="GEN_AUTOMATION_SEMANTIC_GATEWAY_",
        extra="ignore",
        frozen=True,
    )

    upstream_chat_completions_url: AnyHttpUrl
    model: str = Field(min_length=1, max_length=200)
    model_revision: str = Field(min_length=7, max_length=200)
    upstream_api_key: SecretStr | None = None
    upstream_timeout_seconds: float = Field(default=120, gt=0, le=900)
    upstream_max_tokens: int = Field(default=2_048, ge=256, le=8_192)
    max_image_bytes: int = Field(
        default=20 * 1024 * 1024,
        ge=1,
        le=_ABSOLUTE_MAX_IMAGE_BYTES,
    )
    max_upstream_response_bytes: int = Field(
        default=128 * 1024,
        ge=4 * 1024,
        le=1024 * 1024,
    )

    @field_validator("model", "model_revision")
    @classmethod
    def validate_pinned_identifier(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("model identifiers must not contain whitespace")
        if value.lower() in {"head", "latest", "main", "master"}:
            raise ValueError("model revision must be immutable")
        return value

    @field_validator("upstream_chat_completions_url")
    @classmethod
    def validate_upstream_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("upstream credentials must not be embedded in the URL")
        if value.query is not None or value.fragment is not None:
            raise ValueError("upstream URL must not contain a query or fragment")
        return value

    @property
    def max_request_bytes(self) -> int:
        encoded_image_bytes = 4 * ((self.max_image_bytes + 2) // 3)
        return encoded_image_bytes + _REQUEST_OVERHEAD_BYTES


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ImageRequest(_StrictModel):
    content_type: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    base64: str


class _TaskRequest(_StrictModel):
    prompt: str
    prompt_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_schema: dict[str, Any]
    schema_sha256: str = Field(pattern=_SHA256_PATTERN)


class _AssessmentRequest(_StrictModel):
    schema_version: str
    request_id: str = Field(pattern=_SHA256_PATTERN)
    model: str = Field(min_length=1, max_length=200)
    model_revision: str = Field(min_length=1, max_length=200)
    image: _ImageRequest
    task: _TaskRequest


class _BoxResponse(_StrictModel):
    x_min: float = Field(ge=0, le=1)
    y_min: float = Field(ge=0, le=1)
    x_max: float = Field(ge=0, le=1)
    y_max: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_area(self) -> _BoxResponse:
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("box must have positive area")
        return self


class _IssueResponse(_StrictModel):
    code: SemanticIssueCode
    confidence: float = Field(ge=0, le=1)
    box: _BoxResponse | None = None


class _AssessmentResponse(_StrictModel):
    verdict: SemanticVerdict
    confidence: float = Field(ge=0, le=1)
    issues: list[_IssueResponse] = Field(max_length=MAX_SEMANTIC_ISSUES)

    @model_validator(mode="after")
    def validate_verdict(self) -> _AssessmentResponse:
        if self.verdict == SemanticVerdict.PASS and self.issues:
            raise ValueError("pass cannot include issues")
        if self.verdict != SemanticVerdict.PASS and not self.issues:
            raise ValueError("review and severe verdicts require an issue")
        return self


class _AssessmentEnvelope(_StrictModel):
    schema_version: str
    request_id: str = Field(pattern=_SHA256_PATTERN)
    model: str = Field(min_length=1, max_length=200)
    model_revision: str = Field(min_length=1, max_length=200)
    asset_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessment: _AssessmentResponse


class _UpstreamMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    content: str


class _UpstreamChoice(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    message: _UpstreamMessage
    finish_reason: str | None = None


class _UpstreamCompletion(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    choices: list[_UpstreamChoice] = Field(min_length=1, max_length=16)


class _UpstreamUnavailableError(Exception):
    pass


class _UpstreamProtocolError(Exception):
    pass


class _ImageNormalizationError(Exception):
    pass


def create_app(
    settings: SemanticGatewaySettings | None = None,
    *,
    upstream_client: httpx2.AsyncClient | None = None,
) -> FastAPI:
    """Create the private gateway.

    Uvicorn calls this as an application factory. Tests may inject an HTTP
    client with a fake transport; injected clients remain caller-owned.
    """

    resolved = settings or SemanticGatewaySettings()
    owns_client = upstream_client is None
    http_client = upstream_client or httpx2.AsyncClient(
        timeout=httpx2.Timeout(resolved.upstream_timeout_seconds),
        limits=httpx2.Limits(max_connections=2, max_keepalive_connections=1),
        follow_redirects=False,
        trust_env=False,
    )
    normalization_slots = asyncio.Semaphore(1)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_client:
                await http_client.aclose()

    app = FastAPI(
        title="Semantic anatomy gateway",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/v1/anatomy/assess")
    async def assess(request: Request) -> JSONResponse:
        contract, image = await _validated_request(
            request,
            settings=resolved,
        )
        try:
            async with normalization_slots:
                upstream_content_type, upstream_image = await asyncio.to_thread(
                    _normalize_image_for_upstream,
                    image,
                    content_type=contract.image.content_type,
                )
            envelope = await _request_assessment(
                http_client,
                settings=resolved,
                contract=contract,
                encoded_image=base64.b64encode(upstream_image).decode("ascii"),
                image_content_type=upstream_content_type,
            )
        except _ImageNormalizationError as error:
            raise HTTPException(status_code=422, detail="invalid image payload") from error
        except _UpstreamUnavailableError as error:
            raise HTTPException(status_code=503, detail="semantic model unavailable") from error
        except _UpstreamProtocolError as error:
            raise HTTPException(
                status_code=502,
                detail="semantic model response invalid",
            ) from error
        finally:
            # Keep the decoded image scoped to this request and never place it
            # in exception messages, logs, or application state.
            del image
        return JSONResponse(content=envelope.model_dump(mode="json"))

    return app


async def _validated_request(
    request: Request,
    *,
    settings: SemanticGatewaySettings,
) -> tuple[_AssessmentRequest, bytes]:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="application/json is required")

    raw_body = await _read_bounded_body(request, max_bytes=settings.max_request_bytes)
    try:
        decoded_json = json.loads(raw_body)
        contract = _AssessmentRequest.model_validate(decoded_json)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise HTTPException(
            status_code=422,
            detail="invalid semantic assessment contract",
        ) from error

    if request.headers.get("idempotency-key") != contract.request_id:
        raise HTTPException(status_code=422, detail="invalid semantic assessment identity")
    if (
        contract.schema_version != SEMANTIC_SCHEMA_VERSION
        or contract.model != settings.model
        or contract.model_revision != settings.model_revision
    ):
        raise HTTPException(status_code=422, detail="unsupported semantic assessment profile")
    if (
        contract.task.prompt != ANATOMY_ASSESSMENT_PROMPT
        or contract.task.prompt_sha256 != prompt_sha256()
        or hashlib.sha256(contract.task.prompt.encode("utf-8")).hexdigest()
        != contract.task.prompt_sha256
        or contract.task.output_schema != ANATOMY_OUTPUT_SCHEMA
        or contract.task.schema_sha256 != schema_sha256()
    ):
        raise HTTPException(status_code=422, detail="unsupported semantic assessment contract")
    if contract.image.content_type not in _SUPPORTED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="unsupported image content type")

    max_encoded_length = 4 * ((settings.max_image_bytes + 2) // 3)
    if len(contract.image.base64) > max_encoded_length:
        raise HTTPException(status_code=413, detail="image payload exceeds configured limit")
    try:
        image = base64.b64decode(contract.image.base64, validate=True)
    except (ValueError, binascii.Error) as error:
        raise HTTPException(status_code=422, detail="invalid image encoding") from error
    if not image:
        raise HTTPException(status_code=422, detail="image payload must not be empty")
    if len(image) > settings.max_image_bytes:
        raise HTTPException(status_code=413, detail="image payload exceeds configured limit")
    if hashlib.sha256(image).hexdigest() != contract.image.sha256:
        raise HTTPException(status_code=422, detail="image digest does not match")

    expected_request_id = hashlib.sha256(
        (
            f"{contract.image.sha256}:{settings.model}:{settings.model_revision}:"
            f"{prompt_sha256()}:{schema_sha256()}"
        ).encode()
    ).hexdigest()
    if contract.request_id != expected_request_id:
        raise HTTPException(status_code=422, detail="invalid semantic assessment identity")
    return contract, image


def _normalize_image_for_upstream(image: bytes, *, content_type: str) -> tuple[str, bytes]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image)) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > ANATOMY_IMAGE_MAX_SOURCE_PIXELS:
                    raise _ImageNormalizationError
                expected_content_type = _IMAGE_FORMAT_CONTENT_TYPES.get(source.format or "")
                if expected_content_type != content_type:
                    raise _ImageNormalizationError
                orientation = source.getexif().get(274, 1)
                if not isinstance(orientation, int) or not 1 <= orientation <= 8:
                    raise _ImageNormalizationError
                source.load()
                if orientation == 1 and max(width, height) <= ANATOMY_IMAGE_MAX_LONG_EDGE:
                    return content_type, image

                with ImageOps.exif_transpose(source) as oriented:
                    width, height = oriented.size
                    if max(width, height) > ANATOMY_IMAGE_MAX_LONG_EDGE:
                        if width >= height:
                            normalized_size = (
                                ANATOMY_IMAGE_MAX_LONG_EDGE,
                                max(
                                    1,
                                    (height * ANATOMY_IMAGE_MAX_LONG_EDGE + width // 2) // width,
                                ),
                            )
                        else:
                            normalized_size = (
                                max(
                                    1,
                                    (width * ANATOMY_IMAGE_MAX_LONG_EDGE + height // 2) // height,
                                ),
                                ANATOMY_IMAGE_MAX_LONG_EDGE,
                            )
                    else:
                        normalized_size = oriented.size

                    has_alpha = oriented.mode in {"RGBA", "LA"} or (
                        oriented.mode == "P" and "transparency" in oriented.info
                    )
                    with oriented.convert("RGBA" if has_alpha else "RGB") as converted:
                        with converted.resize(
                            normalized_size,
                            resample=Image.Resampling.LANCZOS,
                        ) as normalized:
                            output = io.BytesIO()
                            normalized.save(output, format="PNG", optimize=False, compress_level=6)
                            return "image/png", output.getvalue()
    except _ImageNormalizationError:
        raise
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
        raise _ImageNormalizationError from error
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError) as error:
        raise _ImageNormalizationError from error


async def _read_bounded_body(request: Request, *, max_bytes: int) -> bytes:
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            parsed_length = int(declared_length)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="invalid content length") from error
        if parsed_length < 0:
            raise HTTPException(status_code=400, detail="invalid content length")
        if parsed_length > max_bytes:
            raise HTTPException(status_code=413, detail="request payload exceeds configured limit")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail="request payload exceeds configured limit")
    if not body:
        raise HTTPException(status_code=422, detail="request payload must not be empty")
    return bytes(body)


async def _request_assessment(
    http_client: httpx2.AsyncClient,
    *,
    settings: SemanticGatewaySettings,
    contract: _AssessmentRequest,
    encoded_image: str,
    image_content_type: str,
) -> _AssessmentEnvelope:
    bound_schema = copy.deepcopy(ANATOMY_OUTPUT_SCHEMA)
    properties = bound_schema["properties"]
    properties["request_id"] = {"const": contract.request_id}
    properties["model"] = {"const": settings.model}
    properties["model_revision"] = {"const": settings.model_revision}
    properties["asset_sha256"] = {"const": contract.image.sha256}

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Gen-Automation-Model-Revision": settings.model_revision,
    }
    if settings.upstream_api_key is not None:
        headers["Authorization"] = f"Bearer {settings.upstream_api_key.get_secret_value()}"

    upstream_body = {
        "model": settings.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{image_content_type};base64,{encoded_image}"},
                    },
                    {"type": "text", "text": contract.task.prompt},
                    {
                        "type": "text",
                        "text": (
                            "Return the identity fields exactly as constrained by the supplied "
                            "JSON schema."
                        ),
                    },
                ],
            }
        ],
        "temperature": 0,
        "seed": 0,
        "max_tokens": settings.upstream_max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "semantic_anatomy_assessment",
                "strict": True,
                "schema": bound_schema,
            },
        },
    }

    try:
        async with http_client.stream(
            "POST",
            str(settings.upstream_chat_completions_url),
            headers=headers,
            json=upstream_body,
        ) as response:
            if response.status_code in {408, 429} or response.status_code >= 500:
                raise _UpstreamUnavailableError
            if response.status_code != 200:
                raise _UpstreamProtocolError
            media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
            if media_type != "application/json":
                raise _UpstreamProtocolError
            response_body = bytearray()
            async for chunk in response.aiter_bytes():
                response_body.extend(chunk)
                if len(response_body) > settings.max_upstream_response_bytes:
                    raise _UpstreamProtocolError
    except _UpstreamUnavailableError:
        raise
    except _UpstreamProtocolError:
        raise
    except (httpx2.TimeoutException, httpx2.TransportError) as error:
        raise _UpstreamUnavailableError from error

    try:
        completion = _UpstreamCompletion.model_validate_json(response_body)
        choice = completion.choices[0]
        if choice.finish_reason not in {None, "stop"}:
            raise ValueError("upstream generation did not finish")
        envelope = _AssessmentEnvelope.model_validate_json(choice.message.content)
    except (ValueError, ValidationError) as error:
        raise _UpstreamProtocolError from error
    if (
        envelope.schema_version != SEMANTIC_SCHEMA_VERSION
        or envelope.request_id != contract.request_id
        or envelope.model != settings.model
        or envelope.model_revision != settings.model_revision
        or envelope.asset_sha256 != contract.image.sha256
        or _SHA256.fullmatch(envelope.asset_sha256) is None
    ):
        raise _UpstreamProtocolError
    return envelope
