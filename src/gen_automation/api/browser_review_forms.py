import hashlib
import hmac
import re
from dataclasses import dataclass
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import Request, status

from gen_automation.config import Settings
from gen_automation.domain.enums import (
    ReviewBulkAction,
    ReviewDecisionValue,
    SemanticGroundTruth,
    SemanticIssueCode,
)

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_MAX_FORM_BODY_BYTES = 64 * 1024
_MAX_LOCK_VERSION = 2_147_483_647
_MAX_BULK_ASSET_COUNT = 500
_FORM_KEY = re.compile(r"web-review-[0-9a-f]{64}")
_HEX = frozenset("0123456789abcdefABCDEF")

CREATE_FIELDS = frozenset({"csrf_token", "idempotency_key"})
DECISION_FIELDS = frozenset(
    {
        "csrf_token",
        "idempotency_key",
        "expected_lock_version",
        "asset_id",
        "decision",
        "reason_code",
        "note",
    }
)
TRANSITION_FIELDS = frozenset({"csrf_token", "idempotency_key", "expected_lock_version"})
X_SELECTION_FIELDS = frozenset(
    {
        "csrf_token",
        "idempotency_key",
        "expected_lock_version",
        "asset_id",
        "selected",
    }
)
BULK_ACTION_FIELDS = frozenset(
    {
        "csrf_token",
        "idempotency_key",
        "expected_lock_version",
        "asset_id",
        "action",
        "reason_code",
        "note",
    }
)
ANATOMY_FEEDBACK_FIELDS = frozenset(
    {
        "csrf_token",
        "assessment_id",
        "ground_truth",
        "issue_code",
        "note",
    }
)


class BrowserReviewFormError(ValueError):
    """A review form did not match its bounded, exact browser contract."""

    def __init__(self, *, status_code: int) -> None:
        super().__init__("review form is invalid")
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class DecisionForm:
    csrf_token: str
    idempotency_key: str
    expected_lock_version: int
    asset_id: UUID
    decision: ReviewDecisionValue
    reason_code: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class TransitionForm:
    csrf_token: str
    idempotency_key: str
    expected_lock_version: int


@dataclass(frozen=True, slots=True)
class XSelectionForm:
    csrf_token: str
    idempotency_key: str
    expected_lock_version: int
    asset_id: UUID
    selected: bool


@dataclass(frozen=True, slots=True)
class BulkActionForm:
    csrf_token: str
    idempotency_key: str
    expected_lock_version: int
    asset_ids: tuple[UUID, ...]
    action: ReviewBulkAction
    reason_code: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class AnatomyFeedbackForm:
    csrf_token: str
    assessment_id: UUID
    ground_truth: SemanticGroundTruth
    issue_code: SemanticIssueCode | None
    note: str | None


async def read_create_form(request: Request) -> tuple[str, str]:
    values = await _read_form(request, expected_fields=CREATE_FIELDS)
    return (
        _bounded_nonempty(values["csrf_token"], maximum=200),
        _idempotency_key(values["idempotency_key"]),
    )


async def read_decision_form(request: Request) -> DecisionForm:
    values = await _read_form(request, expected_fields=DECISION_FIELDS)
    try:
        asset_id = UUID(values["asset_id"])
        decision = ReviewDecisionValue(values["decision"])
    except ValueError:
        raise _bad_request() from None
    if str(asset_id) != values["asset_id"].lower():
        raise _bad_request()
    reason_code = values["reason_code"] or None
    note = values["note"] or None
    if reason_code is not None and len(reason_code) > 100:
        raise _bad_request()
    if note is not None and len(note) > 4_000:
        raise _bad_request()
    return DecisionForm(
        csrf_token=_bounded_nonempty(values["csrf_token"], maximum=200),
        idempotency_key=_idempotency_key(values["idempotency_key"]),
        expected_lock_version=_lock_version(values["expected_lock_version"]),
        asset_id=asset_id,
        decision=decision,
        reason_code=reason_code,
        note=note,
    )


async def read_transition_form(request: Request) -> TransitionForm:
    values = await _read_form(request, expected_fields=TRANSITION_FIELDS)
    return TransitionForm(
        csrf_token=_bounded_nonempty(values["csrf_token"], maximum=200),
        idempotency_key=_idempotency_key(values["idempotency_key"]),
        expected_lock_version=_lock_version(values["expected_lock_version"]),
    )


async def read_x_selection_form(request: Request) -> XSelectionForm:
    values = await _read_form(request, expected_fields=X_SELECTION_FIELDS)
    try:
        asset_id = UUID(values["asset_id"])
    except ValueError:
        raise _bad_request() from None
    if str(asset_id) != values["asset_id"].lower() or values["selected"] not in {
        "true",
        "false",
    }:
        raise _bad_request()
    return XSelectionForm(
        csrf_token=_bounded_nonempty(values["csrf_token"], maximum=200),
        idempotency_key=_idempotency_key(values["idempotency_key"]),
        expected_lock_version=_lock_version(values["expected_lock_version"]),
        asset_id=asset_id,
        selected=values["selected"] == "true",
    )


async def read_bulk_action_form(request: Request) -> BulkActionForm:
    parsed = await _read_form_values(
        request,
        max_num_fields=len(BULK_ACTION_FIELDS) + _MAX_BULK_ASSET_COUNT - 1,
    )
    if set(parsed) != BULK_ACTION_FIELDS:
        raise _bad_request()
    single_fields = BULK_ACTION_FIELDS - {"asset_id"}
    if any(len(parsed[field]) != 1 for field in single_fields):
        raise _bad_request()
    raw_asset_ids = parsed["asset_id"]
    if not 1 <= len(raw_asset_ids) <= _MAX_BULK_ASSET_COUNT:
        raise _bad_request()
    try:
        asset_ids = tuple(UUID(value) for value in raw_asset_ids)
        action = ReviewBulkAction(parsed["action"][0])
    except ValueError:
        raise _bad_request() from None
    if any(
        str(asset_id) != value.lower()
        for asset_id, value in zip(asset_ids, raw_asset_ids, strict=True)
    ):
        raise _bad_request()
    if len(set(asset_ids)) != len(asset_ids):
        raise _bad_request()
    reason_code = parsed["reason_code"][0] or None
    note = parsed["note"][0] or None
    if reason_code is not None and len(reason_code) > 100:
        raise _bad_request()
    if note is not None and len(note) > 4_000:
        raise _bad_request()
    return BulkActionForm(
        csrf_token=_bounded_nonempty(parsed["csrf_token"][0], maximum=200),
        idempotency_key=_idempotency_key(parsed["idempotency_key"][0]),
        expected_lock_version=_lock_version(parsed["expected_lock_version"][0]),
        asset_ids=asset_ids,
        action=action,
        reason_code=reason_code,
        note=note,
    )


async def read_anatomy_feedback_form(request: Request) -> AnatomyFeedbackForm:
    values = await _read_form(request, expected_fields=ANATOMY_FEEDBACK_FIELDS)
    try:
        assessment_id = UUID(values["assessment_id"])
        ground_truth = SemanticGroundTruth(values["ground_truth"])
        issue_code = SemanticIssueCode(values["issue_code"]) if values["issue_code"] else None
    except ValueError:
        raise _bad_request() from None
    if str(assessment_id) != values["assessment_id"].lower():
        raise _bad_request()
    note = values["note"].strip() or None
    if note is not None and len(note) > 1_000:
        raise _bad_request()
    if ground_truth != SemanticGroundTruth.ANATOMY_DEFECT:
        issue_code = None
    return AnatomyFeedbackForm(
        csrf_token=_bounded_nonempty(values["csrf_token"], maximum=200),
        assessment_id=assessment_id,
        ground_truth=ground_truth,
        issue_code=issue_code,
        note=note,
    )


def review_form_idempotency_key(
    settings: Settings,
    *,
    session_id: UUID,
    action: str,
    parts: tuple[str, ...],
) -> str:
    context = "\x1f".join(("gen-automation-browser-review-v1", str(session_id), action, *parts))
    digest = hmac.new(
        settings.session_secret.get_secret_value().encode("utf-8"),
        context.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"web-review-{digest}"


def form_key_matches(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(supplied, expected)


async def _read_form(
    request: Request,
    *,
    expected_fields: frozenset[str],
) -> dict[str, str]:
    parsed = await _read_form_values(request, max_num_fields=len(expected_fields))
    if set(parsed) != expected_fields or any(len(parsed[field]) != 1 for field in expected_fields):
        raise _bad_request()
    return {field: parsed[field][0] for field in expected_fields}


async def _read_form_values(
    request: Request,
    *,
    max_num_fields: int,
) -> dict[str, list[str]]:
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != _FORM_CONTENT_TYPE:
        raise BrowserReviewFormError(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise _bad_request() from None
        if declared_length < 0:
            raise _bad_request()
        if declared_length > _MAX_FORM_BODY_BYTES:
            raise BrowserReviewFormError(status_code=status.HTTP_413_CONTENT_TOO_LARGE)

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_FORM_BODY_BYTES:
            raise BrowserReviewFormError(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
        body.extend(chunk)
    try:
        encoded = bytes(body).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _bad_request() from None
    if not _valid_percent_encoding(encoded):
        raise _bad_request()
    try:
        parsed = parse_qs(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=max_num_fields,
        )
    except (UnicodeDecodeError, ValueError):
        raise _bad_request() from None
    return parsed


def _valid_percent_encoding(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value) or value[index + 1] not in _HEX or value[index + 2] not in _HEX:
            return False
        index += 3
    return True


def _bounded_nonempty(value: str, *, maximum: int) -> str:
    if not 1 <= len(value) <= maximum:
        raise _bad_request()
    return value


def _idempotency_key(value: str) -> str:
    if _FORM_KEY.fullmatch(value) is None:
        raise _bad_request()
    return value


def _lock_version(value: str) -> int:
    if not value or len(value) > 10 or not value.isascii() or not value.isdecimal():
        raise _bad_request()
    parsed = int(value)
    if not 1 <= parsed <= _MAX_LOCK_VERSION or str(parsed) != value:
        raise _bad_request()
    return parsed


def _bad_request() -> BrowserReviewFormError:
    return BrowserReviewFormError(status_code=status.HTTP_400_BAD_REQUEST)
