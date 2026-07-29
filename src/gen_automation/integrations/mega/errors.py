"""Redacted error taxonomy for the isolated MEGAcmd boundary."""


class MegaError(Exception):
    """Base error for completed-set delivery to MEGA."""


class MegaConfigurationError(MegaError):
    """The pre-authenticated MEGAcmd runtime is unavailable or invalid."""


class MegaRetryableError(MegaError):
    """A command failed without evidence of a conflicting remote object."""


class MegaAmbiguousError(MegaRetryableError):
    """A mutation may have completed before its command result was lost."""


class MegaProtocolError(MegaError):
    """MEGAcmd returned output that could not be interpreted safely."""


class MegaRemoteConflictError(MegaError):
    """The content-addressed remote identity resolves to conflicting bytes."""
