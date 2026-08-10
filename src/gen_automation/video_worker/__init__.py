"""Isolated one-image-to-video worker contract.

The package deliberately contains no model downloader and no controller or
Salad integration.  A runtime adapter must supply the pinned profile's model
execution implementation.
"""

from gen_automation.video_worker.profiles import PINNED_VIDEO_PROFILE

__all__ = ["PINNED_VIDEO_PROFILE"]
