"""SQLAlchemy engine/session setup for the SQLite comp store."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import DATABASE_PATH, DATABASE_URL


class Base(DeclarativeBase):
    pass


# check_same_thread=False lets FastAPI's threadpool share the connection;
# each request still gets its own Session.
engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}, echo=False
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create the data directory and all tables if they don't exist."""
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    # Import models so their tables are registered on Base.metadata.
    from app import models  # noqa: F401

    Base.metadata.create_all(engine)


def get_session() -> Session:
    """FastAPI dependency: yield a session, always close it."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
