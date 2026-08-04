from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import SemanticIssueCode, SemanticVerdict

SEMANTIC_SCHEMA_VERSION = "semantic-anatomy-assessment/v1"
MAX_SEMANTIC_ISSUES = 20
ANATOMY_IMAGE_NORMALIZATION_VERSION = "semantic-anatomy-image-normalization/v1"
ANATOMY_IMAGE_MAX_LONG_EDGE = 1536

ANATOMY_ASSESSMENT_PROMPT = f"""\
Inspect only visible anatomy and rendering integrity. Ignore sexual content, clothing, identity,
and artistic style. Look for obvious extra or missing fingers, toes, or limbs; malformed hands or
feet; duplicated body parts; impossible joints; implausible body proportions; and severe face
deformation. Do not penalize foreshortening, occlusion, stylization, or anatomy that is uncertain.
Use pass when no listed defect is visible, review when evidence is uncertain or moderate, and
severe only for an obvious defect. Input normalization version
{ANATOMY_IMAGE_NORMALIZATION_VERSION} preserves images with a long edge of
{ANATOMY_IMAGE_MAX_LONG_EDGE} pixels or less; larger images are resized without cropping to that
maximum long edge using deterministic high-quality Lanczos resampling and supplied as PNG. Return
only the requested structured result."""

ANATOMY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "request_id",
        "model",
        "model_revision",
        "asset_sha256",
        "assessment",
    ],
    "properties": {
        "schema_version": {"const": SEMANTIC_SCHEMA_VERSION},
        "request_id": {"type": "string", "minLength": 64, "maxLength": 64},
        "model": {"type": "string", "minLength": 1, "maxLength": 200},
        "model_revision": {"type": "string", "minLength": 1, "maxLength": 200},
        "asset_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "assessment": {
            "type": "object",
            "additionalProperties": False,
            "required": ["verdict", "confidence", "issues"],
            "properties": {
                "verdict": {"enum": [item.value for item in SemanticVerdict]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "issues": {
                    "type": "array",
                    "maxItems": MAX_SEMANTIC_ISSUES,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["code", "confidence"],
                        "properties": {
                            "code": {
                                "enum": [item.value for item in SemanticIssueCode],
                            },
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                            "box": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["x_min", "y_min", "x_max", "y_max"],
                                "properties": {
                                    "x_min": {"type": "number", "minimum": 0, "maximum": 1},
                                    "y_min": {"type": "number", "minimum": 0, "maximum": 1},
                                    "x_max": {"type": "number", "minimum": 0, "maximum": 1},
                                    "y_max": {"type": "number", "minimum": 0, "maximum": 1},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


@dataclass(frozen=True, slots=True)
class SemanticNormalizedBox:
    x_min_micros: int
    y_min_micros: int
    x_max_micros: int
    y_max_micros: int

    def __post_init__(self) -> None:
        values = (
            self.x_min_micros,
            self.y_min_micros,
            self.x_max_micros,
            self.y_max_micros,
        )
        if any(isinstance(value, bool) or not 0 <= value <= 1_000_000 for value in values):
            raise ValueError("semantic issue box coordinates must be normalized")
        if self.x_min_micros >= self.x_max_micros or self.y_min_micros >= self.y_max_micros:
            raise ValueError("semantic issue box must have positive area")

    def to_wire(self) -> dict[str, int]:
        return {
            "x_min_micros": self.x_min_micros,
            "y_min_micros": self.y_min_micros,
            "x_max_micros": self.x_max_micros,
            "y_max_micros": self.y_max_micros,
        }


@dataclass(frozen=True, slots=True)
class SemanticIssue:
    code: SemanticIssueCode
    confidence_micros: int
    box: SemanticNormalizedBox | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, SemanticIssueCode):
            raise ValueError("semantic issue code is invalid")
        if isinstance(self.confidence_micros, bool) or not 0 <= self.confidence_micros <= 1_000_000:
            raise ValueError("semantic issue confidence must be normalized")

    def to_wire(self) -> dict[str, object]:
        value: dict[str, object] = {
            "code": self.code.value,
            "confidence_micros": self.confidence_micros,
        }
        if self.box is not None:
            value["box"] = self.box.to_wire()
        return value


@dataclass(frozen=True, slots=True)
class SemanticAssessmentResult:
    verdict: SemanticVerdict
    confidence_micros: int
    issues: tuple[SemanticIssue, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, SemanticVerdict):
            raise ValueError("semantic verdict is invalid")
        if isinstance(self.confidence_micros, bool) or not 0 <= self.confidence_micros <= 1_000_000:
            raise ValueError("semantic assessment confidence must be normalized")
        if len(self.issues) > MAX_SEMANTIC_ISSUES:
            raise ValueError("semantic assessment has too many issues")
        if self.verdict == SemanticVerdict.PASS and self.issues:
            raise ValueError("passing semantic assessment cannot contain issues")
        if self.verdict != SemanticVerdict.PASS and not self.issues:
            raise ValueError("non-passing semantic assessment must contain an issue")

    def to_wire(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "confidence_micros": self.confidence_micros,
            "issues": [issue.to_wire() for issue in self.issues],
        }


def prompt_sha256() -> str:
    return hashlib.sha256(ANATOMY_ASSESSMENT_PROMPT.encode("utf-8")).hexdigest()


def schema_sha256() -> str:
    return canonical_sha256(ANATOMY_OUTPUT_SCHEMA)


def assessment_profile_sha256(*, model: str, model_revision: str) -> str:
    return canonical_sha256(
        {
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "model": model,
            "model_revision": model_revision,
            "prompt_sha256": prompt_sha256(),
            "schema_sha256": schema_sha256(),
        }
    )
