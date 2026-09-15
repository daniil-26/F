"""гербовый сбор как отдельный вид комиссии

Формат v2 отдаёт гербовый сбор отдельной колонкой рядом с брокерской комиссией и
комиссией торговой системы. Свести его к `OTHER` значило бы потерять
возможность сверить его отдельно, а комиссии по видам — требование спеки 4.1.

`fee_kind` хранится как VARCHAR + CHECK (не нативный enum), поэтому миграция
пересоздаёт ограничение, а не меняет тип.

Revision ID: 8c21f0a4d1e7
Revises: 1295064538cb
Create Date: 2026-09-12 15:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = '8c21f0a4d1e7'
down_revision: str | None = '1295064538cb'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = ('BROKER', 'DEPOSITARY', 'EXCHANGE', 'WITHDRAWAL', 'OTHER')
_NEW = ('BROKER', 'DEPOSITARY', 'EXCHANGE', 'STAMP', 'WITHDRAWAL', 'OTHER')


def _check(values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{value}'" for value in values)
    return f"fee_kind IN ({listed})"


def upgrade() -> None:
    op.drop_constraint('fee_kind', 'transactions', type_='check')
    op.create_check_constraint('fee_kind', 'transactions', _check(_NEW))


def downgrade() -> None:
    # Откат возможен, только если гербового сбора в журнале ещё нет: строку с
    # `STAMP` ограничение старой версии не примет. Это честнее молчаливого
    # перевода таких записей в OTHER — они перестали бы отличаться от прочих.
    op.execute("DELETE FROM transactions WHERE fee_kind = 'STAMP'")
    op.drop_constraint('fee_kind', 'transactions', type_='check')
    op.create_check_constraint('fee_kind', 'transactions', _check(_OLD))
