import logging
import json
from sqlalchemy import text
from tenacity import after_log, before_log, retry, stop_after_attempt, wait_fixed
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.core import limiter
from app.core.db import engine


logger = logging.getLogger(__name__)

max_tries = 60 * 5  # 5 minutes
wait_seconds = 1

@retry(
    stop=stop_after_attempt(max_tries),
    wait=wait_fixed(wait_seconds),
    before=before_log(logger, logging.INFO),
    after=after_log(logger, logging.WARN),
)
def wait_for_db(db_engine=engine) -> None:
    """Wait for the database to become available.

    Uses Tenacity to retry the health check for up to `max_tries` with a fixed
    `wait_seconds` delay between attempts. The check executes a trivial SQL
    statement (SELECT 1) using a short-lived connection.
    """
    try:
        with db_engine.connect() as conn:
            # simple query to ensure the DB will respond to requests
            conn.execute(text("SELECT 1"))
            logger.info("Database is ready")
            return
    except Exception as e:
        logger.error("Database not ready: %s", e)
        raise


app = FastAPI(title="Stinger", version="0.0.1")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception: %s %s", request.method, request.url)
    return JSONResponse(
        status_code=500,
        content={"detail": f"An unexpected error occurred: {exc}"}
    )

@app.get("/")
async def main():
    payload = {
        "message": "Welcome to Stinger! 🐝",
        "docs": "/docs",
        "redoc": "/redoc"
        }
    # ensures_ascii=True forces emoji conversion to \uD83D\uDC1D to ensure compatibility with legacy systems
    json_bytes = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    return JSONResponse(content=json.loads(json_bytes))


@app.get("/health")
async def health():
    return {"status": "healthy"}