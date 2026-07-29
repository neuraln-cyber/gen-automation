import logging
import sys

import structlog

_SECRET_BEARING_LIBRARY_LOGGERS = (
    "boto3",
    "botocore",
    "httpcore",
    "httpx",
    "httpx2",
    "urllib3",
)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        format="%(message)s",
        level=level.upper(),
        stream=sys.stdout,
        force=True,
    )
    # SDK debug logs can include request headers or request parameters.  OAuth
    # and secret-store transports therefore stay above DEBUG even when the
    # application's own structured logging is temporarily verbose.
    for logger_name in _SECRET_BEARING_LIBRARY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
