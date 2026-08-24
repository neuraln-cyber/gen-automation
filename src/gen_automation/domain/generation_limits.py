import json
from collections.abc import Iterable

from gen_automation.domain.controlled_duo import LEGACY_REGIONAL_PROMPT_NODE_CLASSES

MAX_OUTPUTS_PER_GENERATION_JOB = 25

# Salad queue messages have a hard 256 KiB boundary. Small jobs keep their
# complete payload inline, while larger jobs carry a signed, content-addressed
# reference to the same bounded payload in private object storage.
MAX_INLINE_OUTPUTS_PER_SIGNED_GENERATION_JOB = 8
MAX_SAFE_OUTPUTS_PER_SIGNED_GENERATION_JOB = MAX_OUTPUTS_PER_GENERATION_JOB

# Backward-compatible explicit name used by Controlled Trio contract tests and
# documentation. All compositions now share the same signed-envelope boundary.
MAX_CONTROLLED_TRIO_OUTPUTS_PER_GENERATION_JOB = MAX_SAFE_OUTPUTS_PER_SIGNED_GENERATION_JOB

# Multi-output execution duplicates prompt-bearing workflow branches. Keep a
# conservative prompt budget even when the full payload is referenced from
# object storage; this is checked both on frozen source prompts and again after
# wildcard expansion.
MAX_PROMPT_TEXT_BYTES_PER_GENERATION_JOB = 96 * 1024

# This budget also keeps referenced payloads below their independent 1 MiB
# parsing limit with maximum filenames, eight LoRAs, and upload grants.
MAX_SIGNED_PROMPT_BUDGET_BYTES_PER_GENERATION_JOB = 90 * 1024

# Some approved workflows bind the same source prompt into the shared scene,
# per-character lanes, and a refinement/detailer lane. Budget new work against
# that worst supported amplification before a provider job is frozen. Legacy
# frozen jobs retain the v1 calculation and are protected by the exact,
# transactional envelope check at dispatch.
SIGNED_WORKER_PROMPT_AMPLIFICATION = 3


def json_encoded_prompt_bytes(values: Iterable[str]) -> int:
    """Return the exact UTF-8 size of prompt strings when encoded as JSON values."""

    return sum(
        len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        for value in values
    )


def utf8_prompt_bytes(values: Iterable[str]) -> int:
    """Return legacy raw UTF-8 prompt bytes for frozen v1 job compatibility."""

    return sum(len(value.encode("utf-8")) for value in values)


def signed_worker_prompt_budget_bytes(
    values: Iterable[str],
    *,
    outputs_per_job: int = 1,
) -> int:
    """Return the conservative prompt share of a current signed worker request."""

    return json_encoded_prompt_bytes(values) * outputs_per_job * SIGNED_WORKER_PROMPT_AMPLIFICATION


def referenced_worker_prompt_budget_bytes(
    values: Iterable[str],
    *,
    outputs_per_job: int = 1,
) -> int:
    """Return the prompt share of a content-addressed worker payload."""

    return json_encoded_prompt_bytes(values) * outputs_per_job


def effective_outputs_per_generation_job(
    *,
    composition_mode: str,
    requested: int,
) -> int:
    """Bound internal fan-out without reducing the user's requested image count."""

    del composition_mode
    return min(requested, MAX_SAFE_OUTPUTS_PER_SIGNED_GENERATION_JOB)


# Backward-compatible public name used by the v1 workflow and its tests.
REGIONAL_PROMPT_NODE_CLASSES = LEGACY_REGIONAL_PROMPT_NODE_CLASSES
