"""Жёсткие проверки журнала, доступные без цен.

Два уровня по спеке 5.2. На этапе 1 доступны только жёсткие проверки, не
требующие цен; «сумма позиций по ценам равна nav» появится на этапе 4.

Функции возвращают список нарушений. Падает вызывающая сторона — джоба с
ненулевым кодом возврата.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portfolio.domain.events import is_external
from portfolio.domain.positions import positions_on
from portfolio.models import Transaction

__all__ = ["Violation", "check_all", "external_flow_by_day"]


@dataclass(frozen=True)
class Violation:
    check: str
    message: str
    level: str = "hard"


def check_all(transactions: Iterable[Transaction]) -> list[Violation]:
    """Все жёсткие инварианты этапа 1 разом."""
    rows = list(transactions)
    return [
        *check_no_negative_positions(rows),
        *check_reversals_net_to_zero(rows),
        *check_natural_keys_unique(rows),
        *check_currency_filled(rows),
    ]


def check_no_negative_positions(transactions: Iterable[Transaction]) -> list[Violation]:
    """Количество бумаг ни по одной позиции не отрицательно.

    Самая частая причина — незаполненные начальные остатки: продажа бумаги,
    купленной до начала архива, уводит позицию в минус (STAGE-1, T9).
    """
    rows = list(transactions)
    dates = sorted({row.trade_date for row in rows})
    violations: list[Violation] = []
    reported: set[int] = set()

    for on_date in dates:
        for instrument_id, position in positions_on(rows, on_date, keep_empty=True).items():
            if position.quantity < 0 and instrument_id not in reported:
                reported.add(instrument_id)
                violations.append(
                    Violation(
                        check="quantity_not_negative",
                        message=(
                            f"инструмент {instrument_id}: количество "
                            f"{position.quantity} на {on_date.isoformat()}"
                        ),
                    )
                )
    return violations


def check_reversals_net_to_zero(transactions: Iterable[Transaction]) -> list[Violation]:
    """Сумма сторнирующих записей и оригиналов равна нулю."""
    rows = list(transactions)
    by_id = {row.id: row for row in rows if row.id is not None}
    violations: list[Violation] = []

    for row in rows:
        if row.reverses_id is None:
            continue
        original = by_id.get(row.reverses_id)
        if original is None:
            violations.append(
                Violation(
                    check="reversal_pairs",
                    message=f"сторно #{row.id} ссылается на запись вне выборки",
                )
            )
            continue
        if original.amount + row.amount != 0:
            violations.append(
                Violation(
                    check="reversal_pairs",
                    message=(
                        f"сторно #{row.id} и оригинал #{original.id} дают "
                        f"{original.amount + row.amount}, а не ноль"
                    ),
                )
            )
        left = original.quantity or Decimal(0)
        right = row.quantity or Decimal(0)
        if left + right != 0:
            violations.append(
                Violation(
                    check="reversal_pairs",
                    message=(
                        f"сторно #{row.id}: количества дают {left + right}, а не ноль"
                    ),
                )
            )
    return violations


def check_natural_keys_unique(transactions: Iterable[Transaction]) -> list[Violation]:
    """Дубль `natural_key`. В БД это UNIQUE-индекс; проверка ловит выборки без БД."""
    seen: dict[str, int] = {}
    violations: list[Violation] = []

    for row in transactions:
        count = seen.get(row.natural_key, 0) + 1
        seen[row.natural_key] = count
        if count == 2:
            violations.append(
                Violation(
                    check="natural_key_unique",
                    message=f"ключ {row.natural_key} встречается больше одного раза",
                )
            )
    return violations


def check_currency_filled(transactions: Iterable[Transaction]) -> list[Violation]:
    """Валюта заполнена и является кодом из трёх букв.

    Пустая валюта складывает рубли с долларами в одну сумму — расхождение,
    которое потом объясняют «ошибкой округления».
    """
    violations: list[Violation] = []
    for row in transactions:
        if not row.currency or len(row.currency) != 3:
            violations.append(
                Violation(
                    check="currency_filled",
                    message=f"запись #{row.id}: валюта {row.currency!r}",
                )
            )
    return violations


def external_flow_by_day(transactions: Iterable[Transaction]) -> dict[date, Decimal]:
    """`external_flow` за день — сумма событий, помеченных как внешние.

    Без исключений и ручных поправок (спека 5.2): классификация берётся из типа
    события, а не решается на месте.
    """
    result: dict[date, Decimal] = {}
    for row in transactions:
        if not is_external(row.event_type):
            continue
        day = row.settlement_date or row.trade_date
        result[day] = result.get(day, Decimal(0)) + row.amount
    return result
