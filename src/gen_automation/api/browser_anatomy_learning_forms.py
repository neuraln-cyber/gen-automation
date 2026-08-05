"""Bounded browser-form contracts for owner anatomy-learning controls."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import Request, status

from gen_automation.config import Settings

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_MAX_FORM_BODY_BYTES = 4096
_POLICY_REQUIRED_FIELDS = frozenset(
    {
        "csrf_token",
        "idempotency_key",
        "expected_lock_version",
        "minimum_new_labels_for_retrain",
        "max_visual_run_usd",
    }
)
_POLICY_BOOLEAN_FIELDS = frozenset(
    {
        "learning_enabled",
        "auto_train_meta",
        "auto_train_visual",
        "auto_promote_validated",
    }
)
_TRAIN_FIELDS = frozenset(
    {"csrf_token", "idempotency_key", "profile_sha256", "dataset_sha256"}
)


class AnatomyLearningFormError(ValueError):
    """A browser anatomy-learning form did not satisfy its frozen contract."""

    def __init__(self, *, status_code: int = status.HTTP_400_BAD_REQUEST) -> None:
        self.status_code = status_code
        super().__init__("invalid anatomy-learning form")


@dataclass(frozen=True, slots=True)
class AnatomyLearningPolicyForm:
    csrf_token: str
    idempotency_key: str
    expected_lock_version: int
    learning_enabled: bool
    auto_train_meta: bool
    auto_train_visual: bool
    auto_promote_validated: bool
    minimum_new_labels_for_retrain: int
    max_visual_run_microusd: int


@dataclass(frozen=True, slots=True)
class AnatomyLearningTrainForm:
    csrf_token: str
    idempotency_key: str
    profile_sha256: str
    dataset_sha256: str


async def read_anatomy_learning_policy_form(
    request: Request,
) -> AnatomyLearningPolicyForm:
    allowed = _POLICY_REQUIRED_FIELDS | _POLICY_BOOLEAN_FIELDS
    values = await _read_form(request, allowed_fields=allowed)
    if not _POLICY_REQUIRED_FIELDS.issubset(values):
        raise AnatomyLearningFormError()
    for field in _POLICY_BOOLEAN_FIELDS & values.keys():
        if values[field] != "on":
            raise AnatomyLearningFormError()
    try:
        expected_lock_version = int(values["expected_lock_version"])
        minimum_new_labels = int(values["minimum_new_labels_for_retrain"])
        visual_usd = Decimal(values["max_visual_run_usd"])
    except (InvalidOperation, ValueError):
        raise AnatomyLearningFormError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        ) from None
    visual_microusd = visual_usd * Decimal(1_000_000)
    if (
        expected_lock_version < 1
        or not 1 <= minimum_new_labels <= 10_000
        or not Decimal("0.01") <= visual_usd <= Decimal("25.00")
        or visual_microusd != visual_microusd.to_integral_value()
    ):
        raise AnatomyLearningFormError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    return AnatomyLearningPolicyForm(
        csrf_token=_bounded(values["csrf_token"], maximum=200),
        idempotency_key=_bounded(values["idempotency_key"], maximum=200),
        expected_lock_version=expected_lock_version,
        learning_enabled="learning_enabled" in values,
        auto_train_meta="auto_train_meta" in values,
        auto_train_visual="auto_train_visual" in values,
        auto_promote_validated="auto_promote_validated" in values,
        minimum_new_labels_for_retrain=minimum_new_labels,
        max_visual_run_microusd=int(visual_microusd),
    )


async def read_anatomy_learning_train_form(
    request: Request,
) -> AnatomyLearningTrainForm:
    values = await _read_form(request, allowed_fields=_TRAIN_FIELDS)
    if set(values) != _TRAIN_FIELDS:
        raise AnatomyLearningFormError()
    profile_sha256 = _sha256(values["profile_sha256"])
    dataset_sha256 = _sha256(values["dataset_sha256"])
    return AnatomyLearningTrainForm(
        csrf_token=_bounded(values["csrf_token"], maximum=200),
        idempotency_key=_bounded(values["idempotency_key"], maximum=200),
        profile_sha256=profile_sha256,
        dataset_sha256=dataset_sha256,
    )


def anatomy_learning_form_token(
    settings: Settings,
    *,
    session_id: UUID,
    action: str,
    parts: tuple[str, ...],
) -> str:
    context = "\x1f".join(
        ("gen-automation-browser-anatomy-learning-v1", str(session_id), action, *parts)
    )
    digest = hmac.new(
        settings.session_secret.get_secret_value().encode("utf-8"),
        context.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"web-anatomy-learning-{digest}"


def form_token_matches(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(supplied, expected)


async def _read_form(
    request: Request,
    *,
    allowed_fields: frozenset[str],
) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != _FORM_CONTENT_TYPE:
        raise AnatomyLearningFormError(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise AnatomyLearningFormError() from None
        if declared_length < 0:
            raise AnatomyLearningFormError()
        if declared_length > _MAX_FORM_BODY_BYTES:
            raise AnatomyLearningFormError(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE
            )
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_FORM_BODY_BYTES:
            raise AnatomyLearningFormError(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE
            )
        body.extend(chunk)
    try:
        encoded = bytes(body).decode("utf-8", errors="strict")
        parsed = parse_qs(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=len(allowed_fields),
        )
    except (UnicodeDecodeError, ValueError):
        raise AnatomyLearningFormError() from None
    if not set(parsed).issubset(allowed_fields) or any(
        len(items) != 1 for items in parsed.values()
    ):
        raise AnatomyLearningFormError()
    return {field: items[0] for field, items in parsed.items()}


def _bounded(value: str, *, maximum: int) -> str:
    if not value or value != value.strip() or len(value) > maximum:
        raise AnatomyLearningFormError()
    return value


def _sha256(value: str) -> str:
    bounded = _bounded(value, maximum=64)
    if len(bounded) != 64 or any(character not in "0123456789abcdef" for character in bounded):
        raise AnatomyLearningFormError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    return bounded


__all__ = [
    "AnatomyLearningFormError",
    "AnatomyLearningPolicyForm",
    "AnatomyLearningTrainForm",
    "anatomy_learning_form_token",
    "form_token_matches",
    "read_anatomy_learning_policy_form",
    "read_anatomy_learning_train_form",
]
