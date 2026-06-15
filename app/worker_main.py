import asyncio
import signal

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.delivery.worker import Worker, listen_for_notifications

CHANNEL = "stinger_deliveries"


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=10, max_overflow=5)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)) as client:
        worker = Worker(session_factory, client, max_concurrency=50, allow_private=settings.allow_private_targets)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.stop)

        # raw libpq DSN for the dedicated LISTEN connection (strip +asyncpg)
        raw_dsn = settings.DATABASE_URL.replace("+asyncpg", "")
        listener = asyncio.create_task(
            listen_for_notifications(raw_dsn, CHANNEL, worker, worker._stop)
        )
        try:
            await worker.run()
        finally:
            listener.cancel()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())