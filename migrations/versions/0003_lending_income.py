"""вознаграждение за заём бумаг — отдельный тип события

Раздел «5.4 Сделки займа ценных бумаг» несёт вознаграждение в колонке «% по
сделке» строки возврата. Прогон по реальному архиву показал, что это сумма в
рублях, а не ставка: разность «% по сделке» и брокерской комиссии по разделу
совпала с расхождением сверки до копейки (A-21).

Записывать его `CASH_IN` нельзя: тот числится внешним потоком, а заём —
внутренний доход портфеля. Подмена завысила бы внешний приток и занизила
доходность, причём инвариант «external_flow равен сумме внешних событий»
остался бы выполненным и считал бы неверно.

`event_type` хранится как VARCHAR + CHECK (не нативный enum), поэтому миграция
пересоздаёт ограничение, а не меняет тип — как в 0002.

Revision ID: b7f4a2e90c13
Revises: 8c21f0a4d1e7
Create Date: 2026-09-14 18:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'b7f4a2e90c13'
down_revision: str | None = '8c21f0a4d1e7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = (
    'OPENING_BALANCE', 'CASH_IN', 'CASH_OUT', 'TRANSFER', 'BUY', 'SELL', 'COUPON',
    'DIVIDEND', 'AMORTIZATION', 'MATURITY', 'FX_CONVERT', 'FEE', 'TAX', 'CONVERSION',
    'SPIN_OFF', 'SPLIT', 'DEFAULT', 'PARTIAL_REDEMPTION', 'DEPOSIT_OPEN',
    'DEPOSIT_INTEREST', 'DEPOSIT_CLOSE',
)
_NEW = (
    'OPENING_BALANCE', 'CASH_IN', 'CASH_OUT', 'TRANSFER', 'BUY', 'SELL', 'COUPON',
    'DIVIDEND', 'LENDING_INCOME', 'AMORTIZATION', 'MATURITY', 'FX_CONVERT', 'FEE',
    'TAX', 'CONVERSION', 'SPIN_OFF', 'SPLIT', 'DEFAULT', 'PARTIAL_REDEMPTION',
    'DEPOSIT_OPEN', 'DEPOSIT_INTEREST', 'DEPOSIT_CLOSE',
)


def _check(values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{value}'" for value in values)
    return f"event_type IN ({listed})"


def upgrade() -> None:
    op.drop_constraint('event_type', 'transactions', type_='check')
    op.create_check_constraint('event_type', 'transactions', _check(_NEW))


def downgrade() -> None:
    # Откат возможен, только если вознаграждений в журнале ещё нет: строку с
    # LENDING_INCOME старое ограничение не примет. Удаляем явно — молчаливый
    # перевод в CASH_IN сделал бы внутренний доход внешним притоком, и отличить
    # его потом было бы нельзя.
    op.execute("DELETE FROM transactions WHERE event_type = 'LENDING_INCOME'")
    op.drop_constraint('event_type', 'transactions', type_='check')
    op.create_check_constraint('event_type', 'transactions', _check(_OLD))
