import logging
from typing import Any

from gen_automation.i2v_worker.main import _configure_worker_logging


def test_worker_progress_is_visible_without_enabling_sdk_logs(
    monkeypatch: Any, capsys: Any
) -> None:
    root = logging.getLogger()
    root_handlers = list(root.handlers)
    root_level = root.level
    logger = logging.Logger("gen_automation.i2v_worker")
    get_logger = logging.getLogger

    def worker_logger(name: str | None = None) -> logging.Logger:
        return logger if name == "gen_automation.i2v_worker" else get_logger(name)

    monkeypatch.setattr(logging, "getLogger", worker_logger)
    _configure_worker_logging("info")
    _configure_worker_logging("info")
    child = logging.Logger("gen_automation.i2v_worker.artifacts")
    child.parent = logger
    child.info("i2v_model_progress role=diffusion_model bytes=1024 total_bytes=2048")
    child.debug("debug details")

    captured = capsys.readouterr().err
    assert captured.count("i2v_model_progress") == 1
    assert "bytes=1024 total_bytes=2048" in captured
    assert "debug details" not in captured
    assert len(logger.handlers) == 1
    assert logger.propagate is False
    assert root.handlers == root_handlers
    assert root.level == root_level
