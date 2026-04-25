from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_directory(database_url: str) -> None:
    sqlite_prefixes = ("sqlite:///", "sqlite+pysqlite:///")
    if not database_url.startswith(sqlite_prefixes):
        return

    db_path_str = database_url.split("///", maxsplit=1)[1]
    db_path = Path(db_path_str).expanduser()
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)


def _sqlite_connect_args(database_url: str) -> dict[str, bool]:
    return {"check_same_thread": False} if database_url.startswith("sqlite") else {}


settings = get_settings()
_ensure_sqlite_directory(settings.database_url)

engine = create_engine(
    settings.database_url,
    future=True,
    pool_pre_ping=True,
    connect_args=_sqlite_connect_args(settings.database_url),
)

SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
