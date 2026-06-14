import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from app.models import Base


@pytest.fixture(scope="session")
def postgres_container():
    with PostgresContainer("postgres:16") as pg:
        yield pg


@pytest.fixture(scope="session")
def async_dsn(postgres_container):
    c = postgres_container
    return (
        f"postgresql+asyncpg://{c.username}:{c.password}"
        f"@{c.get_container_host_ip()}:{c.get_exposed_port(5432)}/{c.dbname}"
    )


@pytest_asyncio.fixture
async def engine(async_dsn):
    eng = create_async_engine(async_dsn, pool_size=16, max_overflow=4)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest.fixture
def session_factory(engine):
    # expire_on_commit=False mirrors the production worker factory: the rows
    # claim_deliveries returns must stay usable after its short transaction commits.
    return async_sessionmaker(engine, expire_on_commit=False)