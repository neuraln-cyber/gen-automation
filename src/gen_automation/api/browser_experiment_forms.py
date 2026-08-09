import json
import math
import re
from dataclasses import dataclass
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import Request, status
from pydantic import ValidationError

from gen_automation.services.experiments import (
    ExperimentSubmission,
    ExperimentVariantSubmission,
)
from gen_automation.services.new_sets import NewSetLoraSelection, NewSetSubmission

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_MAX_FORM_BODY_BYTES = 512 * 1024
_FORM_KEY = re.compile(r"web-new-set-[0-9a-f]{64}")
_HEX = frozenset("0123456789abcdefABCDEF")
_REQUIRED_FIELDS = frozenset(
    {
        "csrf_token",
        "submission_id",
        "idempotency_key",
        "group_slug",
        "experiment_title",
        "outputs_per_variant",
        "paired_seeds",
        "base_seed",
        "variant_plan",
    }
)
_OPTIONAL_FIELDS = frozenset({"keep_warm"})
_EDITOR_FIELDS = frozenset(
    {
        "subject_id",
        "subject_2_id",
        "composition_mode",
        "duo_contract_version",
        "composition_preset_id",
        "character_a_prompt",
        "character_b_prompt",
        "character_a_negative_prompt",
        "character_b_negative_prompt",
        "interaction_prompt",
        "camera_prompt",
        "duo_isolation_mode",
        "duo_quality_mode",
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
        *(f"lora_{slot}_{suffix}" for slot in range(1, 9) for suffix in ("id", "weight")),
    }
)
_LEGACY_VARIANT_KEYS = frozenset(
    {
        "label",
        "subject_id",
        "subject_2_id",
        "composition_mode",
        "character_a_prompt",
        "character_b_prompt",
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
        "loras",
    }
)
_CONTROLLED_DUO_VARIANT_KEYS = _LEGACY_VARIANT_KEYS | {
    "duo_contract_version",
    "composition_preset_id",
    "character_a_negative_prompt",
    "character_b_negative_prompt",
    "interaction_prompt",
    "camera_prompt",
    "duo_isolation_mode",
    "duo_quality_mode",
}


class BrowserExperimentFormError(ValueError):
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
class BrowserExperimentForm:
    csrf_token: str
    submission_id: UUID
    idempotency_key: str
    command: ExperimentSubmission
    values: dict[str, str]


async def read_experiment_form(request: Request) -> BrowserExperimentForm:
    values = await _read_form(request)
    values["keep_warm"] = values.get("keep_warm", "false")
    try:
        csrf_token = _bounded(values["csrf_token"], label="Security token", maximum=200)
        submission_id = _uuid(values["submission_id"], label="Submission")
        idempotency_key = values["idempotency_key"]
        if _FORM_KEY.fullmatch(idempotency_key) is None:
            raise _bad_request("The form expired or was changed. Reload Experiment Lab.")
        outputs = _integer(values["outputs_per_variant"], label="Images per variant")
        variants = _decode_variant_plan(
            values["variant_plan"],
            group_slug=values["group_slug"],
            title=values["experiment_title"],
            outputs_per_variant=outputs,
        )
        command = ExperimentSubmission(
            group_slug=values["group_slug"],
            title=values["experiment_title"],
            outputs_per_variant=outputs,
            paired_seeds=_boolean(values["paired_seeds"], label="Paired seeds"),
            keep_warm=_boolean(values["keep_warm"], label="Keep GPU warm"),
            base_seed=_integer(values["base_seed"], label="Base seed"),
            variants=variants,
        )
    except ValidationError as error:
        first = error.errors(include_url=False, include_input=False)[0]
        detail = str(first["msg"]).removeprefix("Value error, ")
        raise BrowserExperimentFormError(
            f"Experiment plan: {detail}.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            values=values,
        ) from None
    except BrowserExperimentFormError as error:
        error.values = values
        raise
    return BrowserExperimentForm(
        csrf_token=csrf_token,
        submission_id=submission_id,
        idempotency_key=idempotency_key,
        command=command,
        values=values,
    )


async def _read_form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != _FORM_CONTENT_TYPE:
        raise BrowserExperimentFormError(
            "The submitted form type is not supported.",
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            raise _bad_request("The submitted form length was invalid.") from None
        if declared < 0:
            raise _bad_request("The submitted form length was invalid.")
        if declared > _MAX_FORM_BODY_BYTES:
            raise BrowserExperimentFormError(
                "The submitted form was too large.",
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_FORM_BODY_BYTES:
            raise BrowserExperimentFormError(
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
    allowed = _REQUIRED_FIELDS | _OPTIONAL_FIELDS | _EDITOR_FIELDS
    try:
        parsed = parse_qs(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=len(allowed),
        )
    except (UnicodeDecodeError, ValueError):
        raise _bad_request("The submitted form fields were invalid.") from None
    fields = set(parsed)
    if (
        not _REQUIRED_FIELDS.issubset(fields)
        or not fields.issubset(allowed)
        or any(len(items) != 1 for items in parsed.values())
    ):
        raise _bad_request("The submitted form fields were invalid.")
    return {field: items[0] for field, items in parsed.items()}


def _decode_variant_plan(
    value: str,
    *,
    group_slug: str,
    title: str,
    outputs_per_variant: int,
) -> tuple[ExperimentVariantSubmission, ...]:
    if not value or len(value) > 480_000:
        raise _unprocessable("Add between 2 and 12 variants before starting the experiment.")

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
        raise _unprocessable("The variant queue is not valid JSON.") from None
    if not isinstance(decoded, list) or not 2 <= len(decoded) <= 12:
        raise _unprocessable("Variant queue must contain between 2 and 12 variants.")
    variants: list[ExperimentVariantSubmission] = []
    for index, item in enumerate(decoded):
        if not isinstance(item, dict) or frozenset(item) not in {
            _LEGACY_VARIANT_KEYS,
            _CONTROLLED_DUO_VARIANT_KEYS,
        }:
            raise _unprocessable(f"Variant {index + 1} has invalid fields.")
        loras_value = item["loras"]
        if not isinstance(loras_value, list) or len(loras_value) > 8:
            raise _unprocessable(f"Variant {index + 1} has an invalid LoRA stack.")
        loras: list[NewSetLoraSelection] = []
        for lora_index, lora in enumerate(loras_value):
            if not isinstance(lora, dict) or set(lora) != {"approval_id", "weight"}:
                raise _unprocessable(
                    f"Variant {index + 1} LoRA {lora_index + 1} has invalid fields."
                )
            loras.append(
                NewSetLoraSelection(
                    approval_id=_uuid_string(
                        lora["approval_id"],
                        label=f"Variant {index + 1} LoRA {lora_index + 1}",
                    ),
                    weight=_number(lora["weight"], label="LoRA weight"),
                )
            )
        profile = NewSetSubmission(
            slug=f"{group_slug}-{index + 1:02d}-variant",
            title=f"{title} · Variant {index + 1}",
            subject_approval_id=_uuid_string(item["subject_id"], label="Subject"),
            secondary_subject_approval_id=(
                _uuid_string(item["subject_2_id"], label="Second subject")
                if item["subject_2_id"]
                else None
            ),
            composition_mode=_string(item["composition_mode"], label="Composition"),
            duo_contract_version=_whole(
                item.get("duo_contract_version", 1),
                label="Duo contract version",
            ),
            composition_preset_id=(
                _string(item["composition_preset_id"], label="Composition preset")
                if item.get("composition_preset_id")
                else None
            ),
            character_a_prompt=_string(item["character_a_prompt"], label="Character prompt"),
            character_b_prompt=_string(item["character_b_prompt"], label="Character prompt"),
            character_a_negative_prompt=_string(
                item.get("character_a_negative_prompt", ""),
                label="Character exclusions",
            ),
            character_b_negative_prompt=_string(
                item.get("character_b_negative_prompt", ""),
                label="Character exclusions",
            ),
            interaction_prompt=_string(
                item.get("interaction_prompt", ""),
                label="Interaction",
            ),
            camera_prompt=_string(item.get("camera_prompt", ""), label="Camera"),
            duo_isolation_mode=_string(
                item.get("duo_isolation_mode", "balanced"),
                label="Duo isolation",
            ),
            duo_quality_mode=_string(
                item.get("duo_quality_mode", "standard"),
                label="Duo quality",
            ),
            checkpoint_approval_id=_uuid_string(item["checkpoint_id"], label="Checkpoint"),
            loras=tuple(loras),
            workflow_approval_id=_uuid_string(item["workflow_id"], label="Workflow"),
            prompt=_string(item["prompt"], label="Prompt"),
            negative_prompt=_string(item["negative_prompt"], label="Negative prompt"),
            detailer_prompt=_string(item["detailer_prompt"], label="Detailer prompt"),
            detailer_negative_prompt=_string(
                item["detailer_negative_prompt"], label="Detailer negative prompt"
            ),
            batches=(),
            seed=_whole(item["seed"], label="Seed"),
            width=_whole(item["width"], label="Width"),
            height=_whole(item["height"], label="Height"),
            cfg=_number(item["cfg"], label="CFG"),
            steps=_whole(item["steps"], label="Steps"),
            sampler=_string(item["sampler"], label="Sampler"),
            scheduler=_string(item["scheduler"], label="Scheduler"),
            clip_skip=_whole(item["clip_skip"], label="Clip skip"),
            outputs_per_job=outputs_per_variant,
            hires_scale=_number(item["hires_scale"], label="Hires scale"),
            hires_denoise=_number(item["hires_denoise"], label="Hires denoise"),
            hires_upscale_method=_string(item["hires_upscale_method"], label="Upscale method"),
            detailer_guide_size=_whole(item["detailer_guide_size"], label="Guide size"),
            detailer_max_size=_whole(item["detailer_max_size"], label="Detailer max size"),
            detailer_denoise=_number(item["detailer_denoise"], label="Detailer denoise"),
            detailer_bbox_threshold=_number(
                item["detailer_bbox_threshold"], label="Face threshold"
            ),
            detailer_bbox_dilation=_whole(item["detailer_bbox_dilation"], label="Face dilation"),
            detailer_bbox_crop_factor=_number(
                item["detailer_bbox_crop_factor"], label="Crop factor"
            ),
            detailer_feather=_whole(item["detailer_feather"], label="Feather"),
            planned_job_count=1,
            desired_accepted_count=outputs_per_variant,
        )
        variants.append(
            ExperimentVariantSubmission(
                label=_string(item["label"], label=f"Variant {index + 1} label"),
                profile=profile,
            )
        )
    return tuple(variants)


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
    parsed = _uuid_string(value, label=label)
    return parsed


def _uuid_string(value: object, *, label: str) -> UUID:
    if not isinstance(value, str):
        raise _unprocessable(f"{label} selection is invalid.")
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


def _whole(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _unprocessable(f"{label} must be a whole number.")
    return value


def _number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _unprocessable(f"{label} must be a number.")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _unprocessable(f"{label} must be a finite number.")
    return parsed


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise _unprocessable(f"{label} is invalid.")
    return value


def _boolean(value: str, *, label: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise _unprocessable(f"{label} is invalid.")


def _bounded(value: str, *, label: str, maximum: int) -> str:
    if not 1 <= len(value) <= maximum:
        raise _bad_request(f"{label} is invalid.")
    return value


def _bad_request(message: str) -> BrowserExperimentFormError:
    return BrowserExperimentFormError(message, status_code=status.HTTP_400_BAD_REQUEST)


def _unprocessable(message: str) -> BrowserExperimentFormError:
    return BrowserExperimentFormError(
        message,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )
