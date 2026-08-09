import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import Release
from gen_automation.domain.controlled_duo import (
    WorkflowCapability,
    require_controlled_duo_capabilities,
)
from gen_automation.services.gpu_billing import (
    GpuBillingSnapshot,
    gpu_billing_payload,
    gpu_billing_poll_after_ms,
    load_shared_gpu_billing_snapshot,
)
from gen_automation.services.new_sets import (
    NewSetOptions,
    NewSetResult,
    NewSetStatus,
    NewSetSubmission,
    create_and_approve_new_set,
    list_new_set_options,
    load_new_set_status,
    new_set_progress_payload,
)

EXPERIMENT_GROUP_PATTERN = re.compile(r"experiment-[0-9a-f]{12}")
MAX_EXPERIMENT_VARIANTS = 12
MAX_EXPERIMENT_OUTPUTS_PER_VARIANT = 4


class ExperimentVariantSubmission(BaseModel):
    """A named, fully validated snapshot of the ordinary New Set form."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1, max_length=80)
    profile: NewSetSubmission

    @field_validator("label")
    @classmethod
    def require_trimmed_label(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("variant label must be trimmed")
        return value


class ExperimentSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    group_slug: str
    title: str = Field(min_length=1, max_length=240)
    outputs_per_variant: int = Field(ge=1, le=MAX_EXPERIMENT_OUTPUTS_PER_VARIANT)
    paired_seeds: bool = True
    keep_warm: bool = True
    base_seed: int = Field(ge=0, le=(2**63) - 1)
    variants: tuple[ExperimentVariantSubmission, ...] = Field(
        min_length=2,
        max_length=MAX_EXPERIMENT_VARIANTS,
    )

    @field_validator("group_slug")
    @classmethod
    def require_experiment_group_slug(cls, value: str) -> str:
        if EXPERIMENT_GROUP_PATTERN.fullmatch(value) is None:
            raise ValueError("experiment group identifier is invalid")
        return value

    @field_validator("title")
    @classmethod
    def require_trimmed_title(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("experiment title must be trimmed")
        return value

    @model_validator(mode="after")
    def require_unique_labels(self) -> "ExperimentSubmission":
        labels = [variant.label.casefold() for variant in self.variants]
        if len(labels) != len(set(labels)):
            raise ValueError("variant labels must be unique")
        return self


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    group_slug: str
    releases: tuple[NewSetResult, ...]


@dataclass(frozen=True, slots=True)
class ExperimentVariantStatus:
    index: int
    label: str
    release_id: UUID
    release_slug: str
    status: NewSetStatus


@dataclass(frozen=True, slots=True)
class ExperimentStatus:
    group_slug: str
    title: str
    variants: tuple[ExperimentVariantStatus, ...]
    gpu_billing: GpuBillingSnapshot

    @property
    def expected_outputs(self) -> int:
        return sum(variant.status.expected_outputs for variant in self.variants)

    @property
    def generated_outputs(self) -> int:
        return sum(variant.status.images.generated for variant in self.variants)


class ExperimentNotFoundError(LookupError):
    pass


class ExperimentInputError(ValueError):
    pass


async def create_experiment(
    session: AsyncSession,
    *,
    command: ExperimentSubmission,
    idempotency_key: str,
    actor: str,
) -> ExperimentResult:
    """Create one ordinary release per variant and queue them in a warm-friendly order."""

    options = await list_new_set_options(session)
    _preflight(command, options)

    # Keep profiles that need the same model stack adjacent. The logical index remains
    # in every release slug, so comparison order is deterministic in the UI.
    execution_order = sorted(
        enumerate(command.variants),
        key=lambda item: (_model_stack_key(item[1].profile), item[0]),
    )
    results: list[tuple[int, NewSetResult]] = []
    for logical_index, variant in execution_order:
        seed = (
            command.base_seed
            if command.paired_seeds
            else (variant.profile.seed + logical_index * command.outputs_per_variant) % (2**63)
        )
        release_slug = experiment_release_slug(
            command.group_slug,
            index=logical_index,
            label=variant.label,
        )
        profile = NewSetSubmission.model_validate(
            {
                **variant.profile.model_dump(),
                "slug": release_slug,
                "title": f"{command.title} · {variant.label}",
                "seed": seed,
                "outputs_per_job": command.outputs_per_variant,
                "planned_job_count": 1,
                "desired_accepted_count": command.outputs_per_variant,
                "batches": (),
            }
        )
        result = await create_and_approve_new_set(
            session,
            command=profile,
            idempotency_key=f"{idempotency_key}:v{logical_index + 1:02d}",
            actor=actor,
        )
        results.append((logical_index, result))
    results.sort(key=lambda item: item[0])
    return ExperimentResult(
        group_slug=command.group_slug,
        releases=tuple(result for _index, result in results),
    )


async def load_experiment_status(
    session: AsyncSession,
    *,
    group_slug: str,
) -> ExperimentStatus:
    if EXPERIMENT_GROUP_PATTERN.fullmatch(group_slug) is None:
        raise ExperimentNotFoundError("experiment was not found")
    releases = list(
        (
            await session.scalars(
                select(Release)
                .where(Release.slug.like(f"{group_slug}-%"))
                .order_by(Release.slug, Release.id)
            )
        ).all()
    )
    if not releases:
        raise ExperimentNotFoundError("experiment was not found")
    variant_gpu_billing = await load_shared_gpu_billing_snapshot(
        session,
        now=datetime.now(UTC),
    )
    variants: list[ExperimentVariantStatus] = []
    for index, release in enumerate(releases):
        current = await load_new_set_status(
            session,
            release_id=release.id,
            gpu_billing_snapshot=variant_gpu_billing,
        )
        variants.append(
            ExperimentVariantStatus(
                index=index,
                label=_variant_label(release.title, fallback=f"Variant {index + 1}"),
                release_id=release.id,
                release_slug=release.slug,
                status=current,
            )
        )
    title = releases[0].title.rsplit(" · ", 1)[0]
    gpu_billing = await load_shared_gpu_billing_snapshot(
        session,
        now=datetime.now(UTC),
    )
    return ExperimentStatus(
        group_slug=group_slug,
        title=title,
        variants=tuple(variants),
        gpu_billing=gpu_billing,
    )


def experiment_progress_payload(current: ExperimentStatus) -> dict[str, object]:
    generated = current.generated_outputs
    expected = current.expected_outputs
    complete = bool(expected and generated >= expected)
    settled_poll_after_ms = (
        3_000
        if not complete or any(variant.status.poll_after_ms > 0 for variant in current.variants)
        else 0
    )
    return {
        "schema_version": 2,
        "group_slug": current.group_slug,
        "title": current.title,
        "generated": generated,
        "expected": expected,
        "percent": round(generated * 100 / expected, 1) if expected else 0.0,
        "complete": complete,
        "gpu_billing": gpu_billing_payload(current.gpu_billing),
        "poll_after_ms": gpu_billing_poll_after_ms(
            current.gpu_billing,
            settled_poll_after_ms=settled_poll_after_ms,
        ),
        "variants": [
            {
                "index": variant.index,
                "label": variant.label,
                "release_id": str(variant.release_id),
                "release_slug": variant.release_slug,
                **new_set_progress_payload(variant.status, include_gpu_billing=False),
            }
            for variant in current.variants
        ],
    }


def experiment_release_slug(group_slug: str, *, index: int, label: str) -> str:
    suffix = _slugify(label)[:45].strip("-") or "variant"
    prefix = f"{group_slug}-{index + 1:02d}-"
    return f"{prefix}{suffix[: 80 - len(prefix)]}"


def _slugify(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")


def _variant_label(title: str, *, fallback: str) -> str:
    if " · " not in title:
        return fallback
    label = title.rsplit(" · ", 1)[1].strip()
    return label or fallback


def _model_stack_key(profile: NewSetSubmission) -> tuple[str, str, tuple[tuple[str, float], ...]]:
    return (
        str(profile.workflow_approval_id),
        str(profile.checkpoint_approval_id),
        tuple((str(lora.approval_id), lora.weight) for lora in profile.loras),
    )


def _preflight(command: ExperimentSubmission, options: NewSetOptions) -> None:
    subjects = {option.approval_id for option in options.subjects}
    checkpoints = {option.approval_id for option in options.checkpoints}
    loras = {option.approval_id for option in options.loras}
    workflows = {option.approval_id: option for option in options.workflows}
    for variant in command.variants:
        profile = variant.profile
        if profile.subject_approval_id not in subjects or (
            profile.secondary_subject_approval_id is not None
            and profile.secondary_subject_approval_id not in subjects
        ):
            raise ExperimentInputError(f"{variant.label}: a subject is no longer approved")
        if profile.checkpoint_approval_id not in checkpoints:
            raise ExperimentInputError(f"{variant.label}: the checkpoint is no longer approved")
        if any(selection.approval_id not in loras for selection in profile.loras):
            raise ExperimentInputError(f"{variant.label}: a LoRA is no longer approved")
        workflow = workflows.get(profile.workflow_approval_id)
        if workflow is None:
            raise ExperimentInputError(f"{variant.label}: the workflow is no longer approved")
        if profile.composition_mode == "duo" and profile.duo_contract_version == 1:
            if not workflow.has_regional_prompting:
                raise ExperimentInputError(
                    f"{variant.label}: two characters require a regional v1 workflow"
                )
        if profile.composition_mode == "duo" and profile.duo_contract_version == 2:
            try:
                require_controlled_duo_capabilities(
                    frozenset(workflow.capabilities),
                    isolation_mode=profile.duo_isolation_mode,
                    quality_mode=profile.duo_quality_mode,
                )
            except ValueError as error:
                raise ExperimentInputError(f"{variant.label}: {error}") from error
        if profile.composition_mode == "single" and (
            workflow.has_regional_prompting
            or WorkflowCapability.CONTROLLED_DUO_V2 in workflow.capabilities
        ):
            raise ExperimentInputError(
                f"{variant.label}: one character requires a standard workflow"
            )
