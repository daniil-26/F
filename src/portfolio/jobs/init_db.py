"""Создание схемы журнала для локального прогона на SQLite.

Боевая схема живёт в миграциях alembic и пишется под PostgreSQL: только там
воспроизводимая история изменений, без которой боевую БД нельзя обновлять. Для
SQLite миграции неприменимы — в нём нет ни `ALTER … DROP CONSTRAINT`, ни
типов из 0001, — поэтому схема собирается из тех же метаданных, что и в тестах.

Это не второй источник правды: метаданные `models.py` — единственный, а
миграции лишь переносят их изменения на боевую БД.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import inspect

from portfolio.config import Settings, get_settings
from portfolio.db import get_engine
from portfolio.models import Base

__all__ = ["InitDbResult", "init_db"]


@dataclass(frozen=True)
class InitDbResult:
    """Что получилось: адрес БД, созданные и уже существовавшие таблицы."""

    url: str
    created: tuple[str, ...]
    existing: tuple[str, ...]

    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")


def init_db(settings: Settings | None = None) -> InitDbResult:
    """Создаёт недостающие таблицы. Существующие не трогает и не удаляет.

    Для PostgreSQL поднимает ошибку: там схема обязана приезжать миграцией,
    иначе боевая БД и история изменений разъезжаются с первого же выпуска.
    """
    config = settings or get_settings()
    url = config.database_url
    if not url.startswith("sqlite"):
        raise ValueError(
            "схема PostgreSQL создаётся миграциями: alembic upgrade head. "
            "init-db существует для локального прогона на SQLite"
        )

    engine = get_engine()
    before = set(inspect(engine).get_table_names())
    Base.metadata.create_all(engine)
    after = set(inspect(engine).get_table_names())

    return InitDbResult(
        url=url,
        created=tuple(sorted(after - before)),
        existing=tuple(sorted(before & after)),
    )
