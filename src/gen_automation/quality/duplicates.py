import hashlib
import json
from collections.abc import Iterable

from gen_automation.quality.models import (
    DEFAULT_QUALITY_CONFIG,
    MICROS,
    DuplicateCandidate,
    DuplicateCluster,
    QualityBatchError,
    QualityConfig,
)

_MAX_IDENTIFIER_LENGTH = 200
_MAX_DHASH64 = (1 << 64) - 1


def hamming_distance(left: int, right: int) -> int:
    """Return the number of differing bits between two unsigned 64-bit hashes."""

    _validate_hash(left)
    _validate_hash(right)
    return (left ^ right).bit_count()


def cluster_near_duplicates(
    candidates: Iterable[DuplicateCandidate],
    *,
    config: QualityConfig = DEFAULT_QUALITY_CONFIG,
) -> tuple[DuplicateCluster, ...]:
    """Return deterministic single-link clusters, including singleton clusters."""

    collected: list[DuplicateCandidate] = []
    for candidate in candidates:
        if len(collected) >= config.max_batch_size:
            raise QualityBatchError("duplicate batch exceeds the configured limit")
        if not isinstance(candidate, DuplicateCandidate):
            raise QualityBatchError("duplicate candidate is invalid")
        _validate_candidate(candidate)
        collected.append(candidate)

    ordered = sorted(collected, key=lambda candidate: candidate.identifier)
    identifiers = [candidate.identifier for candidate in ordered]
    if len(set(identifiers)) != len(identifiers):
        raise QualityBatchError("duplicate candidate identifiers must be unique")
    if not ordered:
        return ()

    parents = list(range(len(ordered)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left_index: int, right_index: int) -> None:
        left_root = find(left_index)
        right_root = find(right_index)
        if left_root == right_root:
            return
        if left_root < right_root:
            parents[right_root] = left_root
        else:
            parents[left_root] = right_root

    for left_index, left in enumerate(ordered):
        for right_index in range(left_index + 1, len(ordered)):
            right = ordered[right_index]
            if (
                hamming_distance(left.dhash64, right.dhash64)
                <= config.near_duplicate_hamming_threshold
            ):
                union(left_index, right_index)

    grouped: dict[int, list[DuplicateCandidate]] = {}
    for index, candidate in enumerate(ordered):
        grouped.setdefault(find(index), []).append(candidate)

    clusters = tuple(
        _build_cluster(group, config.near_duplicate_hamming_threshold)
        for _, group in sorted(
            grouped.items(),
            key=lambda item: tuple(candidate.identifier for candidate in item[1]),
        )
    )
    return clusters


def _build_cluster(
    candidates: list[DuplicateCandidate],
    threshold: int,
) -> DuplicateCluster:
    ordered = sorted(candidates, key=lambda candidate: candidate.identifier)
    representative = min(
        ordered,
        key=lambda candidate: (-candidate.quality_score_micros, candidate.identifier),
    )
    maximum_distance = 0
    for left_index, left in enumerate(ordered):
        for right in ordered[left_index + 1 :]:
            maximum_distance = max(
                maximum_distance,
                hamming_distance(left.dhash64, right.dhash64),
            )

    identity = [
        {
            "dhash64": f"{candidate.dhash64:016x}",
            "identifier": candidate.identifier,
        }
        for candidate in ordered
    ]
    canonical_identity = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return DuplicateCluster(
        cluster_id=hashlib.sha256(canonical_identity).hexdigest(),
        representative_id=representative.identifier,
        member_ids=tuple(candidate.identifier for candidate in ordered),
        link_threshold=threshold,
        max_pairwise_hamming=maximum_distance,
    )


def _validate_candidate(candidate: DuplicateCandidate) -> None:
    identifier = candidate.identifier
    if (
        not isinstance(identifier, str)
        or not identifier
        or len(identifier) > _MAX_IDENTIFIER_LENGTH
        or identifier != identifier.strip()
        or any(ord(character) < 32 for character in identifier)
    ):
        raise QualityBatchError("duplicate candidate identifier is invalid")
    _validate_hash(candidate.dhash64)
    score = candidate.quality_score_micros
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= MICROS:
        raise QualityBatchError("duplicate candidate quality score is invalid")


def _validate_hash(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_DHASH64:
        raise ValueError("dHash value must be an unsigned 64-bit integer")
