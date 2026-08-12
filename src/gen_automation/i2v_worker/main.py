import asyncio
import os

import uvicorn

from gen_automation.i2v_worker.app import create_i2v_worker_app
from gen_automation.i2v_worker.settings import I2VWorkerSettings


def main() -> None:
    os.umask(0o077)
    settings = I2VWorkerSettings()
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
