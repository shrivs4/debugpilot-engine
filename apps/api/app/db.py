import logging
import time
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def ensure_schema(retries: int = 30, delay_seconds: float = 1.0) -> None:
    """Apply the idempotent schema script, waiting for Postgres to accept connections."""
    schema_sql = get_settings().schema_sql_path.read_text(encoding="utf-8")

    for attempt in range(1, retries + 1):
        try:
            with engine.begin() as connection:
                # Serialize concurrent starters; the script itself is idempotent.
                connection.execute(text("SELECT pg_advisory_xact_lock(814201)"))
                connection.exec_driver_sql(schema_sql)
            return
        except Exception as error:  # noqa: BLE001 - surfaced after the final attempt
            if attempt == retries:
                raise
            logger.warning("Database not ready (attempt %s/%s): %s", attempt, retries, error)
            time.sleep(delay_seconds)


def check_database() -> dict:
    """Report connectivity and the installed pgvector version."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            pgvector_version = connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one_or_none()
        return {"ok": True, "pgvector": pgvector_version, "error": None}
    except Exception as error:  # noqa: BLE001 - health reporting must not raise
        return {"ok": False, "pgvector": None, "error": str(error)}
