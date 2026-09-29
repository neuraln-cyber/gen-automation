"""Checkpoint identities and recipe boundaries; legacy snapshots mean Turbo V2."""

from collections.abc import Mapping
from typing import Literal

H3Variant = Literal["turbo_v2", "hybrid_v2"]
H3_NORMAL_V2_SHA256 = "4cb8e1eaa9c3e5c664822760890bcbe6078455401cfa43205baf322e856d25f8"
H3_NORMAL_V2_BYTES = 20967669160
H3_NORMAL_V2_FILENAME = "DasiwaMinimaxH3_dasiwaHybridV2_3203130.safetensors"


def h3_job_matches_variant(settings: Mapping[str, object], variant: str) -> bool:
    return (
        settings.get("profile", "wan22") != "minimax_h3"
        or (settings.get("h3_model_variant") or "turbo_v2") == variant
    )


def h3_manifest_matches_variant(model: Mapping[str, object], variant: str) -> bool:
    if variant == "hybrid_v2":
        return (
            model.get("sha256") == H3_NORMAL_V2_SHA256
            and model.get("bytes") == H3_NORMAL_V2_BYTES
            and model.get("target_filename") == H3_NORMAL_V2_FILENAME
        )
    # Keep pre-existing manifests compatible, but never mistake normal V2 for Turbo.
    return variant == "turbo_v2" and model.get("sha256") != H3_NORMAL_V2_SHA256
