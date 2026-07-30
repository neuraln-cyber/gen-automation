import hashlib
import hmac
import math
import re
from dataclasses import dataclass
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import Request, status
from pydantic import ValidationError

from gen_automation.config import Settings
from gen_automation.services.new_sets import NewSetLoraSelection, NewSetSubmission

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_MAX_FORM_BODY_BYTES = 128 * 1024
_FORM_KEY = re.compile(r"web-new-set-[0-9a-f]{64}")
_HEX = frozenset("0123456789abcdefABCDEF")
_LORA_SLOTS = range(1, 9)
_FIELDS = frozenset(
    {
        "csrf_token",
        "submission_id",
        "idempotency_key",
        "slug",
        "title",
        "subject_id",
        "checkpoint_id",
        "workflow_id",
        "prompt",
        "negative_prompt",
        "detailer_prompt",
        "detailer_negative_prompt",
        "seed",
        "width",
        "height",
        "cfg",
        "steps",
        "sampler",
        "scheduler",
        "clip_skip",
        "outputs_per_job",
        "hires_scale",
        "hires_denoise",
        "hires_upscale_method",
        "detailer_guide_size",
        "detailer_max_size",
        "detailer_denoise",
        "detailer_bbox_threshold",
        "detailer_bbox_dilation",
        "detailer_bbox_crop_factor",
        "detailer_feather",
        "planned_job_count",
        "desired_accepted_count",
        *(f"lora_{slot}_id" for slot in _LORA_SLOTS),
        *(f"lora_{slot}_weight" for slot in _LORA_SLOTS),
    }
)


class BrowserNewSetFormError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        values: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.values = values


@dataclass(frozen=True, slots=True)
class BrowserNewSetForm:
    csrf_token: str
    submission_id: UUID
    idempotency_key: str
    command: NewSetSubmission
    values: dict[str, str]


async def read_new_set_form(request: Request) -> BrowserNewSetForm:
    values = await _read_form(request)
    try:
        csrf_token = _bounded(values["csrf_token"], label="Security token", maximum=200)
        submission_id = _uuid(values["submission_id"], label="Submission")
        idempotency_key = values["idempotency_key"]
        if _FORM_KEY.fullmatch(idempotency_key) is None:
            raise _bad_request("The form expired or was changed. Reload New Set and try again.")

        loras: list[NewSetLoraSelection] = []
        for slot in _LORA_SLOTS:
            approval_value = values[f"lora_{slot}_id"]
            weight_value = values[f"lora_{slot}_weight"]
            if not approval_value and not weight_value:
                continue
            if not approval_value:
                raise _unprocessable(f"LoRA slot {slot} needs a selected LoRA.")
            if not weight_value:
                raise _unprocessable(f"LoRA slot {slot} needs a weight.")
            loras.append(
                NewSetLoraSelection(
                    approval_id=_uuid(approval_value, label=f"LoRA slot {slot}"),
                    weight=_float(weight_value, label=f"LoRA slot {slot} weight"),
                )
            )
        command = NewSetSubmission(
            slug=values["slug"],
            title=values["title"],
            subject_approval_id=_uuid(values["subject_id"], label="Subject"),
            checkpoint_approval_id=_uuid(values["checkpoint_id"], label="Checkpoint"),
            loras=tuple(loras),
            workflow_approval_id=_uuid(values["workflow_id"], label="Workflow profile"),
            prompt=values["prompt"],
            negative_prompt=values["negative_prompt"],
            detailer_prompt=values["detailer_prompt"],
            detailer_negative_prompt=values["detailer_negative_prompt"],
            seed=_integer(values["seed"], label="Seed"),
            width=_integer(values["width"], label="Width"),
            height=_integer(values["height"], label="Height"),
            cfg=_float(values["cfg"], label="CFG"),
            steps=_integer(values["steps"], label="Steps"),
            sampler=values["sampler"],
            scheduler=values["scheduler"],
            clip_skip=_integer(values["clip_skip"], label="Clip skip"),
            outputs_per_job=_integer(values["outputs_per_job"], label="Outputs per job"),
            hires_scale=_float(values["hires_scale"], label="Hires scale"),
            hires_denoise=_float(values["hires_denoise"], label="Hires denoise"),
            hires_upscale_method=values["hires_upscale_method"],
            detailer_guide_size=_integer(
                values["detailer_guide_size"],
                label="Detailer guide size",
            ),
            detailer_max_size=_integer(
                values["detailer_max_size"],
                label="Detailer maximum size",
            ),
            detailer_denoise=_float(
                values["detailer_denoise"],
                label="Detailer denoise",
            ),
            detailer_bbox_threshold=_float(
                values["detailer_bbox_threshold"],
                label="Detailer face threshold",
            ),
            detailer_bbox_dilation=_integer(
                values["detailer_bbox_dilation"],
                label="Detailer face dilation",
            ),
            detailer_bbox_crop_factor=_float(
                values["detailer_bbox_crop_factor"],
                label="Detailer crop factor",
            ),
            detailer_feather=_integer(
                values["detailer_feather"],
                label="Detailer feather",
            ),
            planned_job_count=_integer(values["planned_job_count"], label="Planned jobs"),
            desired_accepted_count=_integer(
                values["desired_accepted_count"],
                label="Desired accepted images",
            ),
        )
    except ValidationError as error:
        raise BrowserNewSetFormError(
            _validation_message(error),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            values=values,
        ) from None
    except BrowserNewSetFormError as error:
        error.values = values
        raise
    return BrowserNewSetForm(
        csrf_token=csrf_token,
        submission_id=submission_id,
        idempotency_key=idempotency_key,
        command=command,
        values=values,
    )


def new_set_form_key(
    settings: Settings,
    *,
    session_id: UUID,
    submission_id: UUID,
) -> str:
    return _signed_key(
        settings,
        session_id=session_id,
        action="submit",
        value=str(submission_id),
    )


def new_set_csrf_token(settings: Settings, *, session_id: UUID) -> str:
    return _signed_key(
        settings,
        session_id=session_id,
        action="csrf",
        value="",
    )


def form_key_matches(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(supplied, expected)


def _signed_key(
    settings: Settings,
    *,
    session_id: UUID,
    action: str,
    value: str,
) -> str:
    context = "\x1f".join(("gen-automation-browser-new-set-v1", str(session_id), action, value))
    digest = hmac.new(
        settings.session_secret.get_secret_value().encode("utf-8"),
        context.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"web-new-set-{digest}"


async def _read_form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != _FORM_CONTENT_TYPE:
        raise BrowserNewSetFormError(
            "The submitted form type is not supported.",
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise _bad_request("The submitted form length was invalid.") from None
        if declared_length < 0:
            raise _bad_request("The submitted form length was invalid.")
        if declared_length > _MAX_FORM_BODY_BYTES:
            raise BrowserNewSetFormError(
                "The submitted form was too large.",
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_FORM_BODY_BYTES:
            raise BrowserNewSetFormError(
                "The submitted form was too large.",
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )
        body.extend(chunk)
    try:
        encoded = bytes(body).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _bad_request("The submitted form was not valid UTF-8.") from None
    if not _valid_percent_encoding(encoded):
        raise _bad_request("The submitted form encoding was invalid.")
    try:
        parsed = parse_qs(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=len(_FIELDS),
        )
    except (UnicodeDecodeError, ValueError):
        raise _bad_request("The submitted form fields were invalid.") from None
    if set(parsed) != _FIELDS or any(len(parsed[field]) != 1 for field in _FIELDS):
        raise _bad_request("The submitted form fields were invalid.")
    return {field: parsed[field][0] for field in _FIELDS}


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


def _uuid(value: str, *, label: str) -> UUID:
    try:
        parsed = UUID(value)
    except ValueError:
        raise _unprocessable(f"{label} selection is invalid.") from None
    if str(parsed) != value.lower():
        raise _unprocessable(f"{label} selection is invalid.")
    return parsed


def _integer(value: str, *, label: str) -> int:
    if not value or not value.isascii():
        raise _unprocessable(f"{label} must be a whole number.")
    try:
        return int(value)
    except ValueError:
        raise _unprocessable(f"{label} must be a whole number.") from None


def _float(value: str, *, label: str) -> float:
    if not value or not value.isascii():
        raise _unprocessable(f"{label} must be a number.")
    try:
        parsed = float(value)
    except ValueError:
        raise _unprocessable(f"{label} must be a number.") from None
    if not math.isfinite(parsed):
        raise _unprocessable(f"{label} must be a finite number.")
    return parsed


def _bounded(value: str, *, label: str, maximum: int) -> str:
    if not 1 <= len(value) <= maximum:
        raise _bad_request(f"{label} is invalid.")
    return value


def _validation_message(error: ValidationError) -> str:
    first = error.errors(include_url=False, include_input=False)[0]
    location = first["loc"]
    labels = {
        "slug": "Set slug",
        "title": "Set title",
        "loras": "LoRAs",
        "prompt": "Prompt",
        "negative_prompt": "Negative prompt",
        "detailer_prompt": "Detailer prompt",
        "detailer_negative_prompt": "Detailer negative prompt",
        "seed": "Seed",
        "width": "Width",
        "height": "Height",
        "cfg": "CFG",
        "steps": "Steps",
        "sampler": "Sampler",
        "scheduler": "Scheduler",
        "clip_skip": "Clip skip",
        "outputs_per_job": "Outputs per job",
        "hires_scale": "Hires scale",
        "hires_denoise": "Hires denoise",
        "hires_upscale_method": "Hires upscale method",
        "detailer_guide_size": "Detailer guide size",
        "detailer_max_size": "Detailer maximum size",
        "detailer_denoise": "Detailer denoise",
        "detailer_bbox_threshold": "Detailer face threshold",
        "detailer_bbox_dilation": "Detailer face dilation",
        "detailer_bbox_crop_factor": "Detailer crop factor",
        "detailer_feather": "Detailer feather",
        "planned_job_count": "Planned jobs",
        "desired_accepted_count": "Desired accepted images",
    }
    label = labels.get(str(location[0]), "Generation plan") if location else "Generation plan"
    detail = str(first["msg"]).removeprefix("Value error, ")
    return f"{label}: {detail}."


def _bad_request(message: str) -> BrowserNewSetFormError:
    return BrowserNewSetFormError(message, status_code=status.HTTP_400_BAD_REQUEST)


def _unprocessable(message: str) -> BrowserNewSetFormError:
    return BrowserNewSetFormError(
        message,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )
