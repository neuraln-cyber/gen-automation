from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from gen_automation.db.models import Asset, GenerationJob


@dataclass(frozen=True, slots=True)
class GenerationPosition:
    """Stable, human-facing position in the frozen generation queue."""

    batch_index: int
    batch_name: str
    batch_image_number: int


def generation_position(job: GenerationJob, asset: Asset) -> GenerationPosition:
    parameters = job.parameters if isinstance(job.parameters, dict) else {}
    ordinal = generation_ordinal(job)
    batch_index = ordinal
    batch_name = f"Batch {batch_index + 1}"
    image_offset = 0

    batch = parameters.get("batch")
    if isinstance(batch, dict):
        batch_index = _nonnegative_int(batch.get("index"), default=batch_index)
        raw_name = batch.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            batch_name = raw_name.strip()[:120]
        else:
            batch_name = f"Batch {batch_index + 1}"
        image_offset = _nonnegative_int(batch.get("image_offset"), default=0)

    output_index = _nonnegative_int(asset.output_index, default=0)
    return GenerationPosition(
        batch_index=batch_index,
        batch_name=batch_name,
        batch_image_number=image_offset + output_index + 1,
    )


def generation_ordinal(job: GenerationJob) -> int:
    parameters = job.parameters if isinstance(job.parameters, dict) else {}
    return _nonnegative_int(parameters.get("ordinal"), default=0)


def generation_queue_offsets(jobs: Iterable[GenerationJob]) -> dict[UUID, int]:
    """Map each frozen job to its zero-based output offset in the release queue."""

    ordered_jobs = sorted(jobs, key=lambda job: (generation_ordinal(job), job.id))
    offsets: dict[UUID, int] = {}
    offset = 0
    for job in ordered_jobs:
        offsets[job.id] = offset
        offset += job.expected_output_count
    return offsets


def _nonnegative_int(value: object, *, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default
