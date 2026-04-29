from __future__ import annotations

from app.db import _sqlite_connect_args


def test_sqlite_connect_args_wait_for_busy_database() -> None:
    assert _sqlite_connect_args("sqlite:///./data/app.db") == {
        "check_same_thread": False,
        "timeout": 30,
    }


def test_non_sqlite_connect_args_are_empty() -> None:
    assert _sqlite_connect_args("postgresql+psycopg://example") == {}
