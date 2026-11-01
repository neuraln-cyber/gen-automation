# SPDX-License-Identifier: AGPL-3.0-only
"""Expose only the pinned 3D node; preserve its unmodified network and math."""

# Relocated into the pinned upstream package by the image installer.
from .nodes.minimax_h3_latent_upscaler_3d import (  # type: ignore[import-untyped]
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
)

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
