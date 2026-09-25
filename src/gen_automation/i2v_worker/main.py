import asyncio
import logging
import os

import uvicorn

from gen_automation.i2v_worker.app import create_i2v_worker_app
from gen_automation.i2v_worker.settings import I2VWorkerSettings


def _configure_worker_logging(level: str) -> None:
    # Uvicorn configures its own loggers, not our bootstrap/download loggers.
    # Keep SDK/root logging untouched: it can contain signed URLs or credentials.
    logger = logging.getLogger("gen_automation.i2v_worker")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger.handlers = [handler]
    logger.setLevel(level.upper())
    logger.propagate = False


def main() -> None:
    os.umask(0o077)
    settings = I2VWorkerSettings()
    _configure_worker_logging(settings.log_level)
    app = create_i2v_worker_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        access_log=False,
        server_header=False,
        date_header=False,
    )
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
