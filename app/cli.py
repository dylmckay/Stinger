import asyncio, sys
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.auth import create_api_key
from app.config import get_settings

async def _issue(application_id: str, name: str | None):
    engine = create_async_engine(get_settings().DATABASE_URL)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        full, row = await create_api_key(s, application_id=application_id, name=name)
    print(f"key id : {row.id}\nprefix : {row.prefix}\nSECRET : {full}   # shown once - store it now")
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(_issue(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))