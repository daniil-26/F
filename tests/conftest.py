"""Общие фикстуры.

Тесты ходят в SQLite во временном каталоге: весь этап 1 детерминирован и не
требует ни сети, ни поднятого PostgreSQL. Боевая БД — PostgreSQL, миграции
пишутся под неё; схема для тестов создаётся из тех же метаданных.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from portfolio.config import get_settings
from portfolio.db import get_engine, get_session_factory
from portfolio.models import Account, AccountKind, Base

FIXTURES = Path(__file__).parent / "fixtures"


def _clear_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'portfolio.db'}")
    monkeypatch.setenv("DATA_RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("DATA_INPUT_DIR", str(tmp_path / "input"))
    _clear_caches()

    Base.metadata.create_all(get_engine())
    try:
        yield tmp_path
    finally:
        get_engine().dispose()
        _clear_caches()


@pytest.fixture
def session(database: Path) -> Iterator[Session]:
    factory = get_session_factory()
    with factory() as session:
        yield session


@pytest.fixture
def account(session: Session) -> Account:
    account = Account(
        code="12345-АБВ",
        name="Брокерский счёт",
        kind=AccountKind.BROKER,
        currency="RUB",
    )
    session.add(account)
    session.commit()
    return account
