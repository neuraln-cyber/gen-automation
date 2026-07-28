from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gen_automation.app import create_app
from gen_automation.config import Environment, Settings


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    database_path = tmp_path / "test.db"
    settings = Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        auto_create_schema=True,
        auth_development_bypass_enabled=True,
        session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client
