"""Сверка расчётных остатков с остатками из отчёта брокера.

Сверка остатков — единственный механизм, отличающий «данные верные» от «данные
выглядят правдоподобно» (спека 4.8). Поэтому она сравнивает количества и деньги,
а не стоимость: на этапе 1 цен нет и не должно быть.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portfolio.domain.positions import Position, cash_balances, positions_on
from portfolio.models import Transaction

__all__ = ["Discrepancy", "ExpectedBalance", "ReconcileResult", "reconcile"]


@dataclass(frozen=True)
class ExpectedBalance:
    """Контрольный остаток из отчёта, с разрешённым `instrument_id`."""

    kind: str  # cash | security
    quantity: Decimal
    currency: str
    instrument_id: int | None = None
    label: str = ""
    as_of: date | None = None


@dataclass(frozen=True)
class Discrepancy:
    kind: str
    label: str
    expected: Decimal
    actual: Decimal

    @property
    def difference(self) -> Decimal:
        return self.actual - self.expected


@dataclass(frozen=True)
class ReconcileResult:
    as_of: date | None
    discrepancies: tuple[Discrepancy, ...]
    checked: int

    @property
    def ok(self) -> bool:
        return not self.discrepancies


def reconcile(
    transactions: Iterable[Transaction],
    expected: Sequence[ExpectedBalance],
    *,
    as_of: date | None = None,
    cash_tolerance: Decimal = Decimal("0.01"),
    quantity_tolerance: Decimal = Decimal(0),
) -> ReconcileResult:
    """Сравнивает журнал с контрольными числами отчёта.

    Деньги сверяются по `settlement_date`, бумаги — по `trade_date`
    (STAGE-1, T7). Позиция, которой нет в отчёте, но есть в журнале, — тоже
    расхождение: потерянная продажа выглядит именно так.
    """
    rows = list(transactions)
    actual_cash = cash_balances(rows, as_of)
    actual_positions = positions_on(rows, as_of)

    discrepancies: list[Discrepancy] = []
    seen_currencies: set[str] = set()
    seen_instruments: set[int] = set()

    for item in expected:
        if item.kind == "cash":
            currency = item.currency.upper()
            seen_currencies.add(currency)
            actual = actual_cash.get(currency, Decimal(0))
            if abs(actual - item.quantity) > cash_tolerance:
                discrepancies.append(
                    Discrepancy(
                        kind="cash",
                        label=item.label or currency,
                        expected=item.quantity,
                        actual=actual,
                    )
                )
        elif item.kind == "security":
            if item.instrument_id is None:
                discrepancies.append(
                    Discrepancy(
                        kind="security",
                        label=item.label or "инструмент не разрешён",
                        expected=item.quantity,
                        actual=Decimal(0),
                    )
                )
                continue
            seen_instruments.add(item.instrument_id)
            position = actual_positions.get(item.instrument_id)
            actual = position.quantity if position is not None else Decimal(0)
            if abs(actual - item.quantity) > quantity_tolerance:
                discrepancies.append(
                    Discrepancy(
                        kind="security",
                        label=item.label or str(item.instrument_id),
                        expected=item.quantity,
                        actual=actual,
                    )
                )
        else:
            raise ValueError(f"неизвестный вид остатка: {item.kind!r}")

    discrepancies.extend(
        _missing_in_report(actual_cash, seen_currencies, actual_positions, seen_instruments)
    )

    return ReconcileResult(
        as_of=as_of,
        discrepancies=tuple(discrepancies),
        checked=len(expected),
    )


def _missing_in_report(
    actual_cash: Mapping[str, Decimal],
    seen_currencies: set[str],
    actual_positions: Mapping[int, Position],
    seen_instruments: set[int],
) -> list[Discrepancy]:
    """Позиции и валюты, которых нет среди контрольных чисел отчёта.

    Ненулевой остаток, не подтверждённый отчётом, — расхождение: именно так
    выглядит лишняя или задвоенная операция.
    """
    result: list[Discrepancy] = []

    for currency, amount in actual_cash.items():
        if currency not in seen_currencies and amount != 0:
            result.append(
                Discrepancy(kind="cash", label=currency, expected=Decimal(0), actual=amount)
            )

    for instrument_id, position in actual_positions.items():
        if instrument_id not in seen_instruments and position.quantity != 0:
            result.append(
                Discrepancy(
                    kind="security",
                    label=str(instrument_id),
                    expected=Decimal(0),
                    actual=position.quantity,
                )
            )
    return result
