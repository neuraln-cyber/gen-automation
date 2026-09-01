import hashlib
import hmac
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import Request, status
from pydantic import ValidationError

from gen_automation.config import Settings
from gen_automation.domain.lora_limits import MAX_GENERATION_LORAS
from gen_automation.services.new_sets import NewSetLoraSelection, NewSetSubmission

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_MAX_FORM_BODY_BYTES = 512 * 1024
_MAX_STOP_FORM_BODY_BYTES = 4 * 1024
_FORM_KEY = re.compile(r"web-new-set-[0-9a-f]{64}")
_HEX = frozenset("0123456789abcdefABCDEF")
_LORA_SLOTS = range(1, MAX_GENERATION_LORAS + 1)
_STOP_FIELDS = frozenset({"csrf_token", "idempotency_key"})
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
        *(f"lora_{slot}_id" for slot in _LORA_SLOTS),
        *(f"lora_{slot}_weight" for slot in _LORA_SLOTS),
    }
)
_OPTIONAL_FIELDS = frozenset(
    {
        # Accepted only for forms rendered before the automatic-final-size rollout.
        # The submitted value is ignored; the server derives the target from the plan.
        "desired_accepted_count",
        "batch_plan",
        "subject_2_id",
        "subject_3_id",
        "composition_mode",
        "duo_contract_version",
        "composition_preset_id",
        "character_a_prompt",
        "character_b_prompt",
        "character_a_pose_prompt",
        "character_b_pose_prompt",
        "character_c_prompt",
        "character_c_pose_prompt",
        "character_a_negative_prompt",
        "character_b_negative_prompt",
        "character_c_negative_prompt",
        "interaction_prompt",
        "camera_prompt",
        "duo_isolation_mode",
        "duo_quality_mode",
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


@dataclass(frozen=True, slots=True)
class BrowserGenerationStopForm:
    csrf_token: str
    idempotency_key: str


async def read_new_set_form(request: Request) -> BrowserNewSetForm:
    values = await _read_form(request)
    try:
        csrf_token = _bounded(values["csrf_token"], label="Security token", maximum=200)
        submission_id = _uuid(values["submission_id"], label="Submission")
        query_string = request.scope.get("query_string", b"")
        query = parse_qs(
            query_string.decode("ascii", "strict") if isinstance(query_string, bytes) else ""
        )
        if query.get("mode") == ["experiment"] and not values["slug"]:
            values["slug"] = _automatic_experiment_slug(
                values["title"],
                submission_id=submission_id,
            )
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
            secondary_subject_approval_id=(
                _uuid(values["subject_2_id"], label="Second subject")
                if values.get("subject_2_id")
                else None
            ),
            tertiary_subject_approval_id=(
                _uuid(values["subject_3_id"], label="Third subject")
                if values.get("subject_3_id")
                else None
            ),
            composition_mode=values.get("composition_mode", "single"),
            duo_contract_version=_integer(
                values.get("duo_contract_version", "1"),
                label="Duo contract version",
            ),
            composition_preset_id=values.get("composition_preset_id") or None,
            character_a_prompt=values.get("character_a_prompt", ""),
            character_b_prompt=values.get("character_b_prompt", ""),
            character_a_pose_prompt=values.get("character_a_pose_prompt", ""),
            character_b_pose_prompt=values.get("character_b_pose_prompt", ""),
            character_c_prompt=values.get("character_c_prompt", ""),
            character_c_pose_prompt=values.get("character_c_pose_prompt", ""),
            character_a_negative_prompt=values.get("character_a_negative_prompt", ""),
            character_b_negative_prompt=values.get("character_b_negative_prompt", ""),
            character_c_negative_prompt=values.get("character_c_negative_prompt", ""),
            interaction_prompt=values.get("interaction_prompt", ""),
            camera_prompt=values.get("camera_prompt", ""),
            duo_isolation_mode=values.get("duo_isolation_mode", "balanced"),
            duo_quality_mode=values.get("duo_quality_mode", "standard"),
            checkpoint_approval_id=_uuid(values["checkpoint_id"], label="Checkpoint"),
            loras=tuple(loras),
            workflow_approval_id=_uuid(values["workflow_id"], label="Workflow profile"),
            prompt=values["prompt"],
            negative_prompt=values["negative_prompt"],
            detailer_prompt=values["detailer_prompt"],
            detailer_negative_prompt=values["detailer_negative_prompt"],
            batches=_decode_batch_plan(values.get("batch_plan", "")),
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


def _automatic_experiment_slug(title: str, *, submission_id: UUID) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-z0-9]+", "-", ascii_title.casefold()).strip("-")[:70].rstrip("-")
    return f"{base or 'experiment'}-{submission_id.hex[:8]}"


async def read_generation_stop_form(request: Request) -> BrowserGenerationStopForm:
    values = await _read_exact_form(
        request,
        required_fields=_STOP_FIELDS,
        optional_fields=frozenset(),
        maximum_body_bytes=_MAX_STOP_FORM_BODY_BYTES,
    )
    csrf_token = _bounded(values["csrf_token"], label="Security token", maximum=200)
    idempotency_key = values["idempotency_key"]
    if _FORM_KEY.fullmatch(idempotency_key) is None:
        raise _bad_request("The stop control expired or was changed. Reload and try again.")
    return BrowserGenerationStopForm(
        csrf_token=csrf_token,
        idempotency_key=idempotency_key,
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


def generation_stop_form_key(
    settings: Settings,
    *,
    session_id: UUID,
    release_id: UUID,
) -> str:
    return _signed_key(
        settings,
        session_id=session_id,
        action="stop-generation",
        value=str(release_id),
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
    return await _read_exact_form(
        request,
        required_fields=_FIELDS,
        optional_fields=_OPTIONAL_FIELDS,
        maximum_body_bytes=_MAX_FORM_BODY_BYTES,
    )


async def _read_exact_form(
    request: Request,
    *,
    required_fields: frozenset[str],
    optional_fields: frozenset[str],
    maximum_body_bytes: int,
) -> dict[str, str]:
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
        if declared_length > maximum_body_bytes:
            raise BrowserNewSetFormError(
                "The submitted form was too large.",
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_body_bytes:
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
            max_num_fields=len(required_fields | optional_fields),
        )
    except (UnicodeDecodeError, ValueError):
        raise _bad_request("The submitted form fields were invalid.") from None
    submitted_fields = set(parsed)
    if (
        not required_fields.issubset(submitted_fields)
        or not submitted_fields.issubset(required_fields | optional_fields)
        or any(len(items) != 1 for items in parsed.values())
    ):
        raise _bad_request("The submitted form fields were invalid.")
    return {field: items[0] for field, items in parsed.items()}


def _decode_batch_plan(value: str) -> object:
    if not value:
        return ()
    if len(value) > 400_000:
        raise _unprocessable("Batch queue is too large.")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate object key")
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError):
        raise _unprocessable("Batch queue is not valid JSON.") from None
    if not isinstance(decoded, list) or not 1 <= len(decoded) <= 50:
        raise _unprocessable("Batch queue must contain between 1 and 50 batches.")
    return decoded


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
        "secondary_subject_approval_id": "Second subject",
        "tertiary_subject_approval_id": "Third subject",
        "composition_mode": "Composition",
        "duo_contract_version": "Duo contract",
        "composition_preset_id": "Composition preset",
        "character_a_prompt": "Left character prompt",
        "character_b_prompt": "Right character prompt",
        "character_a_pose_prompt": "Character A pose prompt",
        "character_b_pose_prompt": "Character B pose prompt",
        "character_c_prompt": "Character C identity prompt",
        "character_c_pose_prompt": "Character C pose prompt",
        "character_a_negative_prompt": "Left character exclusions",
        "character_b_negative_prompt": "Right character exclusions",
        "character_c_negative_prompt": "Character C exclusions",
        "interaction_prompt": "Interaction",
        "camera_prompt": "Camera",
        "duo_isolation_mode": "Duo isolation",
        "duo_quality_mode": "Duo quality",
        "loras": "LoRAs",
        "prompt": "Prompt",
        "negative_prompt": "Negative prompt",
        "detailer_prompt": "Detailer prompt",
        "detailer_negative_prompt": "Detailer negative prompt",
        "batches": "Batch queue",
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
