"""Fail closed on an unusable RTX runtime before downloading models or taking jobs."""

from __future__ import annotations

import ctypes
import logging
import os

_LOGGER = logging.getLogger(__name__)


class RTXStartupError(Exception):
    pass


def verify_rtx_runtime() -> None:
    """Exercise the pinned VFX effect's real load path, without media or a render.

    CUDA availability and Python imports alone do not establish NGX availability.
    Only the Lokken recipe calls this, on its normal owner-requested GPU startup.
    The dimensions are a small 1.2x target; quality/encoding match the real node.
    """
    check = "driver_capabilities"
    try:
        capabilities = set(os.environ.get("NVIDIA_DRIVER_CAPABILITIES", "").split(","))
        if "all" not in capabilities and not {"compute", "utility", "ngx"} <= capabilities:
            raise ValueError
        check = "ngx_library"
        # Use the provider's injected, driver-matched library. Retain the handle
        # through effect teardown; no downloads, driver installation or fallback.
        ngx_library = ctypes.CDLL("libnvidia-ngx.so.1")
        check = "vfx_import"
        import nvvfx  # type: ignore[import-not-found]

        check = "vfx_create"
        effect = nvvfx.VideoSuperRes(
            quality=nvvfx.QualityLevel.ULTRA,
            strength=1.0,
            device=0,
            image_encoding=nvvfx.ImageEncoding.RGB10A2,
        )
        try:
            check = "vfx_dimensions"
            effect.output_width = 768
            effect.output_height = 432
            check = "vfx_load"
            effect.load()
            if not effect.is_loaded:
                raise ValueError
        finally:
            effect.close()
        del ngx_library
    except Exception as error:
        # Never expose native exception strings, paths, URLs or tracebacks.
        _LOGGER.error(
            "i2v_rtx_preflight status=failed check=%s error_type=%s",
            check,
            type(error).__name__,
        )
        raise RTXStartupError("RTX enhancement runtime is unavailable") from None
    _LOGGER.info("i2v_rtx_preflight status=passed check=vfx_load")
