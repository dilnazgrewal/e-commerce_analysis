"""Database engine and session helpers (SQLite locally, Postgres in the cloud)."""

from collections.abc import Iterator

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  (registers tables)
from app.config import get_settings

_engine: Engine | None = None


def normalise_url(url: str) -> str:
    """Hosted Postgres hands out plain postgres:// URLs; use the installed psycopg (v3) driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def make_engine(url: str) -> Engine:
    url = normalise_url(url)
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    # Neon scales to zero and drops idle connections: test each one before use.
    return create_engine(url, pool_pre_ping=True, pool_recycle=300)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine(get_settings().database_url)
    return _engine


def init_db(engine: Engine | None = None, drop: bool = False) -> None:
    engine = engine or get_engine()
    if drop:
        SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
