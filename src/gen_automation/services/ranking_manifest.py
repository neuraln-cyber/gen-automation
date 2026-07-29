import hmac
import re
from collections.abc import Sequence
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import AssetRanking, AssetScore, ScoringRun
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import AssetScoreState, ScoringRunState

_SHA256 = re.compile(r"[0-9a-f]{64}")
_FROZEN_SCORE_STATES = frozenset(
    {
        AssetScoreState.SCORED,
        AssetScoreState.FLAGGED_BLANK,
        AssetScoreState.FLAGGED_CORRUPT,
    }
)

type RankingManifestRow = tuple[AssetRanking, AssetScore]


class RankingManifestIntegrityError(Exception):
    """A completed scoring run no longer matches its committed ranking manifest."""


async def load_ranking_manifest_rows(
    session: AsyncSession,
    scoring_run_id: UUID,
) -> list[RankingManifestRow]:
    result = await session.execute(
        select(AssetRanking, AssetScore)
        .join(
            AssetScore,
            (AssetScore.id == AssetRanking.asset_score_id)
            & (AssetScore.scoring_run_id == AssetRanking.scoring_run_id)
            & (AssetScore.asset_id == AssetRanking.asset_id),
        )
        .where(AssetRanking.scoring_run_id == scoring_run_id)
        .order_by(AssetRanking.rank)
    )
    return list(result.tuples().all())


def ranking_manifest_sha256(
    run: ScoringRun,
    rows: Sequence[RankingManifestRow],
) -> str:
    ordered = sorted(rows, key=lambda row: row[0].rank)
    payload = {
        "schema": "ranking-manifest/v1",
        "scoring_run": {
            "id": str(run.id),
            "release_version_id": str(run.release_version_id),
            "asset_count": run.asset_count,
            "config_sha256": run.config_sha256,
            "input_manifest_sha256": run.input_manifest_sha256,
            "scorer_version": run.scorer_version,
            "pillow_version": run.pillow_version,
        },
        "rankings": [_ranking_entry(ranking=ranking, score=score) for ranking, score in ordered],
    }
    try:
        return canonical_sha256(payload)
    except (TypeError, ValueError):
        raise RankingManifestIntegrityError(
            "ranking manifest contains non-canonical data"
        ) from None


def validate_completed_ranking_manifest(
    run: ScoringRun,
    rows: Sequence[RankingManifestRow],
) -> str:
    if (
        run.state != ScoringRunState.COMPLETED
        or run.completed_at is None
        or not isinstance(run.ranking_manifest_sha256, str)
        or _SHA256.fullmatch(run.ranking_manifest_sha256) is None
        or len(rows) != run.asset_count
    ):
        raise RankingManifestIntegrityError("completed ranking manifest is incomplete")

    ordered = sorted(rows, key=lambda row: row[0].rank)
    if [ranking.rank for ranking, _score in ordered] != list(range(1, run.asset_count + 1)):
        raise RankingManifestIntegrityError("completed ranking order is invalid")
    if len({ranking.asset_id for ranking, _score in ordered}) != len(ordered):
        raise RankingManifestIntegrityError("completed ranking contains duplicate assets")
    if len({ranking.asset_score_id for ranking, _score in ordered}) != len(ordered):
        raise RankingManifestIntegrityError("completed ranking contains duplicate scores")

    for ranking, score in ordered:
        if (
            ranking.scoring_run_id != run.id
            or score.scoring_run_id != run.id
            or ranking.asset_score_id != score.id
            or ranking.asset_id != score.asset_id
            or score.state not in _FROZEN_SCORE_STATES
            or score.completed_at is None
            or score.aggregate_score_micros is None
            or ranking.aggregate_score_micros != score.aggregate_score_micros
            or ranking.scorer_version != run.scorer_version
            or score.scorer_version != run.scorer_version
            or ranking.pillow_version != run.pillow_version
            or score.pillow_version != run.pillow_version
            or ranking.config_sha256 != run.config_sha256
            or score.config_sha256 != run.config_sha256
            or not isinstance(ranking.explanation, dict)
            or not isinstance(score.signal_detail, dict)
        ):
            raise RankingManifestIntegrityError(
                "completed ranking rows conflict with the scoring run"
            )

    actual = ranking_manifest_sha256(run, ordered)
    if not hmac.compare_digest(actual, run.ranking_manifest_sha256):
        raise RankingManifestIntegrityError("completed ranking manifest digest changed")
    return actual


def _ranking_entry(
    *,
    ranking: AssetRanking,
    score: AssetScore,
) -> dict[str, Any]:
    return {
        "rank": ranking.rank,
        "asset_id": str(ranking.asset_id),
        "asset_score_id": str(ranking.asset_score_id),
        "aggregate_score_micros": ranking.aggregate_score_micros,
        "disposition": _enum_value(ranking.disposition),
        "explanation": ranking.explanation,
        "duplicate_cluster_id": ranking.duplicate_cluster_id,
        "duplicate_representative_asset_id": _optional_uuid(
            ranking.duplicate_representative_asset_id
        ),
        "is_duplicate_representative": ranking.is_duplicate_representative,
        "scorer_version": ranking.scorer_version,
        "pillow_version": ranking.pillow_version,
        "config_sha256": ranking.config_sha256,
        "score": {
            "state": _enum_value(score.state),
            "asset_storage_backend": score.asset_storage_backend,
            "asset_storage_bucket": score.asset_storage_bucket,
            "asset_sha256": score.asset_sha256,
            "asset_object_key": score.asset_object_key,
            "asset_object_version_id": score.asset_object_version_id,
            "asset_byte_size": score.asset_byte_size,
            "asset_image_format": score.asset_image_format,
            "asset_width": score.asset_width,
            "asset_height": score.asset_height,
            "luminance_mean_micros": score.luminance_mean_micros,
            "luminance_std_micros": score.luminance_std_micros,
            "dynamic_range_micros": score.dynamic_range_micros,
            "entropy_bits_micros": score.entropy_bits_micros,
            "entropy_normalized_micros": score.entropy_normalized_micros,
            "sharpness_micros": score.sharpness_micros,
            "dhash_hex": score.dhash_hex,
            "aggregate_score_micros": score.aggregate_score_micros,
            "score_breakdown": score.score_breakdown,
            "signal_detail": score.signal_detail,
            "scorer_version": score.scorer_version,
            "pillow_version": score.pillow_version,
            "config_sha256": score.config_sha256,
            "last_error_code": score.last_error_code,
            "last_error_detail": score.last_error_detail,
        },
    }


def _enum_value(value: Enum | str) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def _optional_uuid(value: UUID | None) -> str | None:
    return str(value) if value is not None else None
