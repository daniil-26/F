"""наименование бумаги не уникально

ISIN идентифицирует бумагу, наименование её только подписывает. Брокер
переиспользует наименования: после конвертации старый и новый выпуски
печатаются под одним именем с разными ISIN, и `UNIQUE(ticker)` делает такую
пару непредставимой — импорт падает на ограничении вместо того, чтобы завести
вторую бумагу (A-28).

Уникальность ISIN сохраняется: она и есть идентичность.

Revision ID: c1d8e5b3a027
Revises: b7f4a2e90c13
Create Date: 2026-09-15 11:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'c1d8e5b3a027'
down_revision: str | None = 'b7f4a2e90c13'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint('uq_instruments_ticker', 'instruments', type_='unique')


def downgrade() -> None:
    # Откат возможен, только если наименования в справочнике не повторяются.
    # Схлопывать записи автоматически нельзя: это разные бумаги, и слияние
    # переписало бы историю количеств.
    op.create_unique_constraint('uq_instruments_ticker', 'instruments', ['ticker'])
