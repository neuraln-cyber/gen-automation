"""Official MEGAcmd transport used by the completed-set delivery runtime."""

from gen_automation.integrations.mega.client import (
    MegaCmdClient,
    MegaRemoteNode,
)
from gen_automation.integrations.mega.errors import (
    MegaAmbiguousError,
    MegaConfigurationError,
    MegaError,
    MegaProtocolError,
    MegaRemoteConflictError,
    MegaRetryableError,
)

__all__ = [
    "MegaAmbiguousError",
    "MegaCmdClient",
    "MegaConfigurationError",
    "MegaError",
    "MegaProtocolError",
    "MegaRemoteConflictError",
    "MegaRemoteNode",
    "MegaRetryableError",
]
