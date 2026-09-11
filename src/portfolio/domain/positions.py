"""Проекции журнала: количества, лоты, денежные остатки.

Этап 1 — **только количества, без оценки**. Цен на этапе нет, рыночная стоимость
не считается. Дата и цена каждой партии при этом сохраняются: они понадобятся
для FIFO и для срока владения по ЛДВ (спека 9), а задним числом не
восстанавливаются.

Две даты (спека 3.2): **позиции считаются по `trade_date`, деньги — по
`settlement_date`**. Без этого сверка остатка на конец месяца не сойдётся ровно
в тех случаях, когда сделка прошла в последний торговый день периода.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from portfolio.models import EventType, Transaction

__all__ = ["Lot", "Position", "cash_balances", "positions_on", "settled_cash"]

# Типы, меняющие количество бумаг. Остальные на позицию не влияют.
_QUANTITY_EVENTS = frozenset(
    {
        EventType.OPENING_BALANCE,
        EventType.BUY,
        EventType.SELL,
        EventType.MATURITY,
        EventType.CONVERSION,
        EventType.SPIN_OFF,
        EventType.SPLIT,
        EventType.PARTIAL_REDEMPTION,
    }
)


@dataclass(frozen=True)
class Lot:
    """Партия: дата, количество, цена приобретения и качество этой цены."""

    trade_date: date
    quantity: Decimal
    price: Decimal | None
    basis_quality: str | None = None
    transaction_id: int | None = None


@dataclass
class Position:
    """Позиция по инструменту: агрегат и список лотов, а не только агрегат."""

    instrument_id: int
    quantity: Decimal = Decimal(0)
    lots: list[Lot] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.quantity == 0


def positions_on(
    transactions: Iterable[Transaction],
    on_date: date | None = None,
    *,
    keep_empty: bool = False,
) -> dict[int, Position]:
    """Позиции на дату по `trade_date`.

    Продажа списывает лоты по FIFO — это же правило понадобится для налоговой
    базы, и держать два разных порядка списания нельзя.
    """
    result: dict[int, Position] = {}

    for row in _sorted(transactions):
        if row.event_type not in _QUANTITY_EVENTS:
            continue
        if row.instrument_id is None or row.quantity is None:
            continue
        if on_date is not None and row.trade_date > on_date:
            continue

        position = result.setdefault(row.instrument_id, Position(instrument_id=row.instrument_id))
        position.quantity += row.quantity

        if row.quantity > 0:
            position.lots.append(
                Lot(
                    trade_date=row.trade_date,
                    quantity=row.quantity,
                    price=row.price if row.price is not None else row.cost_basis,
                    basis_quality=(
                        row.basis_quality.value if row.basis_quality is not None else None
                    ),
                    transaction_id=row.id,
                )
            )
        elif row.quantity < 0:
            _consume_fifo(position, -row.quantity)

    if keep_empty:
        return result
    return {key: value for key, value in result.items() if not value.is_empty}


def cash_balances(
    transactions: Iterable[Transaction],
    on_date: date | None = None,
) -> dict[str, Decimal]:
    """Денежные остатки по валютам на дату — по `settlement_date`.

    Событие без даты расчётов считается рассчитанным в дату сделки: у событий
    без расчётного лага обе даты совпадают (спека 3.2).
    """
    result: dict[str, Decimal] = {}

    for row in _sorted(transactions):
        effective = row.settlement_date or row.trade_date
        if on_date is not None and effective > on_date:
            continue
        if row.amount is None:
            continue
        result[row.currency] = result.get(row.currency, Decimal(0)) + row.amount
    return result


def settled_cash(
    transactions: Iterable[Transaction],
    currency: str,
    on_date: date | None = None,
) -> Decimal:
    """Остаток одной валюты. Отсутствие операций даёт ноль, отсутствие данных — нет.

    Разница существенна: ноль здесь — это «событий не было», а не «сумма
    неизвестна». Неизвестных сумм в журнале не бывает: `amount` не NULL.
    """
    return cash_balances(transactions, on_date).get(currency.upper(), Decimal(0))


def _consume_fifo(position: Position, quantity: Decimal) -> None:
    remaining = quantity
    while remaining > 0 and position.lots:
        lot = position.lots[0]
        if lot.quantity > remaining:
            position.lots[0] = Lot(
                trade_date=lot.trade_date,
                quantity=lot.quantity - remaining,
                price=lot.price,
                basis_quality=lot.basis_quality,
                transaction_id=lot.transaction_id,
            )
            return
        remaining -= lot.quantity
        position.lots.pop(0)
    # Остаток списания сверх известных лотов не «съедается молча»: количество
    # позиции уже ушло в минус, и это поймает жёсткий инвариант.


def _sorted(transactions: Iterable[Transaction]) -> Sequence[Transaction]:
    return sorted(transactions, key=lambda row: (row.trade_date, row.id or 0))
