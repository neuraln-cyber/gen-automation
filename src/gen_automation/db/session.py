from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from gen_automation.db.base import Base


class Database:
    def __init__(self, url: str) -> None:
        engine_options: dict[str, object] = {
            "pool_pre_ping": True,
        }
        if url.startswith("sqlite") and ":memory:" in url:
            engine_options["poolclass"] = StaticPool

        self.engine: AsyncEngine = create_async_engine(url, **engine_options)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def enable_sqlite_foreign_keys(
                dbapi_connection: Any,
                _connection_record: Any,
            ) -> None:
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA foreign_keys=ON")
                finally:
                    cursor.close()

        self.sessions = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    database: Database = request.app.state.database
    async with database.sessions() as session:
        yield session
