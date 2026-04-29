from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
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


def _is_sqlite_url(database_url: str) -> bool:
    return database_url.startswith("sqlite")


def _sqlite_connect_args(database_url: str) -> dict[str, Any]:
    if not _is_sqlite_url(database_url):
        return {}
    return {
        "check_same_thread": False,
        "timeout": 30,
    }


def _configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


settings = get_settings()
_ensure_sqlite_directory(settings.database_url)

engine = create_engine(
    settings.database_url,
    future=True,
    pool_pre_ping=True,
    connect_args=_sqlite_connect_args(settings.database_url),
)
if _is_sqlite_url(settings.database_url):
    event.listen(engine, "connect", _configure_sqlite_connection)

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
    _run_sqlite_migrations()


def _run_sqlite_migrations() -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    if not inspector.has_table("marketplace_offers"):
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("marketplace_offers")
    }
    migrations = {
        "requires_buyer_input": (
            "ALTER TABLE marketplace_offers "
            "ADD COLUMN requires_buyer_input BOOLEAN NOT NULL DEFAULT 0"
        ),
        "buyer_input_label": (
            "ALTER TABLE marketplace_offers ADD COLUMN buyer_input_label VARCHAR(255)"
        ),
        "buyer_input_help": "ALTER TABLE marketplace_offers ADD COLUMN buyer_input_help TEXT",
    }

    with engine.begin() as connection:
        for column_name, statement in migrations.items():
            if column_name not in existing_columns:
                connection.execute(text(statement))

    if not inspector.has_table("product_mappings"):
        return

    mapping_columns = {
        column["name"] for column in inspector.get_columns("product_mappings")
    }
    mapping_migrations = {
        "marketplace_variant_id": (
            "ALTER TABLE product_mappings ADD COLUMN marketplace_variant_id INTEGER"
        ),
        "marketplace_variant_name": (
            "ALTER TABLE product_mappings ADD COLUMN marketplace_variant_name VARCHAR(255)"
        ),
        "marketplace_commission_percent": (
            "ALTER TABLE product_mappings ADD COLUMN marketplace_commission_percent NUMERIC(8, 4)"
        ),
        "payment_system_commission_percent": (
            "ALTER TABLE product_mappings ADD COLUMN payment_system_commission_percent NUMERIC(8, 4)"
        ),
        "manage_stock_quantity": (
            "ALTER TABLE product_mappings ADD COLUMN manage_stock_quantity BOOLEAN NOT NULL DEFAULT 0"
        ),
        "min_purchase_quantity": (
            "ALTER TABLE product_mappings ADD COLUMN min_purchase_quantity INTEGER"
        ),
    }

    with engine.begin() as connection:
        for column_name, statement in mapping_migrations.items():
            if column_name not in mapping_columns:
                connection.execute(text(statement))
