from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from gen_automation.domain.enums import SemanticIssueCode, SemanticVerdict
from gen_automation.semantic import (
    ANATOMY_ASSESSMENT_PROMPT,
    ANATOMY_OUTPUT_SCHEMA,
    MAX_SEMANTIC_ISSUES,
    SEMANTIC_SCHEMA_VERSION,
    SemanticAssessmentResult,
    SemanticIssue,
    SemanticNormalizedBox,
    prompt_sha256,
    schema_sha256,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESPONSE_BYTES = 64 * 1024


class SemanticVlmError(Exception):
    """Base error for the private semantic-anatomy service."""


class SemanticVlmUnavailableError(SemanticVlmError):
    """The service could not return an assessment and may be retried."""


class SemanticVlmProtocolError(SemanticVlmError):
    """The service returned data outside the bounded assessment contract."""


class _StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _BoxResponse(_StrictResponse):
    x_min: float = Field(ge=0, le=1)
    y_min: float = Field(ge=0, le=1)
    x_max: float = Field(ge=0, le=1)
    y_max: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_area(self) -> _BoxResponse:
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("box must have positive area")
        return self


class _IssueResponse(_StrictResponse):
    code: SemanticIssueCode
    confidence: float = Field(ge=0, le=1)
    box: _BoxResponse | None = None


class _AssessmentResponse(_StrictResponse):
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


class _EnvelopeResponse(_StrictResponse):
    schema_version: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=64, max_length=64)
    model: str = Field(min_length=1, max_length=200)
    model_revision: str = Field(min_length=1, max_length=200)
    asset_sha256: str = Field(min_length=64, max_length=64)
    assessment: _AssessmentResponse


class SemanticVlmClient:
    """Client for a private, scale-to-zero semantic VLM gateway.

    The caller owns ``http_client``. The gateway is intentionally model-agnostic:
    it receives a bounded image plus a fixed prompt/schema and may run a pinned
    Qwen3-VL-class model or another compatible vision-language model.
    """

    def __init__(
        self,
        *,
        http_client: httpx2.AsyncClient,
        endpoint_url: str,
        model: str,
        model_revision: str,
        timeout_seconds: float,
    ) -> None:
        if not endpoint_url:
            raise ValueError("semantic VLM endpoint must not be empty")
        if not model or len(model) > 200:
            raise ValueError("semantic VLM model is invalid")
        if not model_revision or len(model_revision) > 200:
            raise ValueError("semantic VLM model revision is invalid")
        if timeout_seconds <= 0:
            raise ValueError("semantic VLM timeout must be positive")
        self._http_client = http_client
        self.endpoint_url = endpoint_url
        self.model = model
        self.model_revision = model_revision
        self._timeout = httpx2.Timeout(timeout_seconds)

    async def assess(
        self,
        payload: bytes,
        *,
        content_type: str,
        asset_sha256: str,
    ) -> SemanticAssessmentResult:
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("semantic VLM image payload must be non-empty bytes")
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("semantic VLM image content type is unsupported")
        if _SHA256.fullmatch(asset_sha256) is None:
            raise ValueError("semantic VLM asset digest is invalid")
        if hashlib.sha256(payload).hexdigest() != asset_sha256:
            raise ValueError("semantic VLM payload does not match its digest")
        request_id = hashlib.sha256(
            (
                f"{asset_sha256}:{self.model}:{self.model_revision}:"
                f"{prompt_sha256()}:{schema_sha256()}"
            ).encode()
        ).hexdigest()
        body: dict[str, Any] = {
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "request_id": request_id,
            "model": self.model,
            "model_revision": self.model_revision,
            "image": {
                "content_type": content_type,
                "sha256": asset_sha256,
                "base64": base64.b64encode(payload).decode("ascii"),
            },
            "task": {
                "prompt": ANATOMY_ASSESSMENT_PROMPT,
                "prompt_sha256": prompt_sha256(),
                "output_schema": ANATOMY_OUTPUT_SCHEMA,
                "schema_sha256": schema_sha256(),
            },
        }
        try:
            response = await self._http_client.post(
                self.endpoint_url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Idempotency-Key": request_id,
                },
                json=body,
                timeout=self._timeout,
            )
        except (httpx2.TimeoutException, httpx2.TransportError) as error:
            raise SemanticVlmUnavailableError("semantic VLM request failed") from error
        if response.status_code != 200:
            raise SemanticVlmUnavailableError(f"semantic VLM returned HTTP {response.status_code}")
        content_type_header = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type_header != "application/json" or len(response.content) > _MAX_RESPONSE_BYTES:
            raise SemanticVlmProtocolError("semantic VLM response envelope is invalid")
        try:
            envelope = _EnvelopeResponse.model_validate_json(response.content)
        except (ValueError, ValidationError) as error:
            raise SemanticVlmProtocolError("semantic VLM response is malformed") from error
        if (
            envelope.schema_version != SEMANTIC_SCHEMA_VERSION
            or envelope.request_id != request_id
            or envelope.model != self.model
            or envelope.model_revision != self.model_revision
            or envelope.asset_sha256 != asset_sha256
            or _SHA256.fullmatch(envelope.asset_sha256) is None
        ):
            raise SemanticVlmProtocolError("semantic VLM response identity does not match")
        return _assessment_result(envelope.assessment)


def _assessment_result(value: _AssessmentResponse) -> SemanticAssessmentResult:
    issues = tuple(
        SemanticIssue(
            code=issue.code,
            confidence_micros=_micros(issue.confidence),
            box=(
                SemanticNormalizedBox(
                    x_min_micros=_micros(issue.box.x_min),
                    y_min_micros=_micros(issue.box.y_min),
                    x_max_micros=_micros(issue.box.x_max),
                    y_max_micros=_micros(issue.box.y_max),
                )
                if issue.box is not None
                else None
            ),
        )
        for issue in value.issues
    )
    return SemanticAssessmentResult(
        verdict=value.verdict,
        confidence_micros=_micros(value.confidence),
        issues=issues,
    )


def _micros(value: float) -> int:
    return round(value * 1_000_000)
