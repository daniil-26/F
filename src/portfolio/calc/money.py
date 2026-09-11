"""Тип `Money` и арифметика над ним.

Этап 1: без `convert` (курсов ещё нет) и без распределения остатка (амортизаций
ещё нет). Всё остальное — из спеки 9, раздел `calc/`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "CENT",
    "CurrencyMismatch",
    "Money",
    "is_close",
    "money_sum",
    "round_amount",
    "zero",
]

CENT = Decimal("0.01")

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class CurrencyMismatch(ValueError):
    """Операция над суммами в разных валютах. Курсов в `calc/` нет по построению."""


@dataclass(frozen=True, order=False)
class Money:
    """Сумма в валюте. `float` не принимается и не приводится."""

    amount: Decimal
    currency: str

    def __init__(self, amount: Decimal | int | str, currency: str) -> None:
        if isinstance(amount, float):
            raise TypeError("Money не принимает float: деньги только Decimal (спека 2, п.6)")
        if isinstance(amount, bool):
            raise TypeError("Money не принимает bool")
        if not isinstance(amount, Decimal | int | str):
            raise TypeError(f"Money не принимает {type(amount).__name__}")

        value = amount if isinstance(amount, Decimal) else Decimal(amount)
        if not value.is_finite():
            raise ValueError(f"неконечная сумма: {amount!r}")

        code = currency.upper()
        if not _CURRENCY_RE.match(code):
            raise ValueError(f"валюта должна быть кодом ISO-4217: {currency!r}")

        object.__setattr__(self, "amount", value)
        object.__setattr__(self, "currency", code)

    # -- арифметика ---------------------------------------------------------

    def __add__(self, other: Money) -> Money:
        self._check_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    def __mul__(self, factor: Decimal | int) -> Money:
        _reject_money(factor, "умножение Money на Money не определено")
        return Money(self.amount * _as_decimal(factor), self.currency)

    __rmul__ = __mul__

    def __truediv__(self, divisor: Decimal | int) -> Money:
        _reject_money(divisor, "деление Money на Money не определено")
        value = _as_decimal(divisor)
        if value == 0:
            raise ZeroDivisionError("деление суммы на ноль")
        return Money(self.amount / value, self.currency)

    # -- сравнение ----------------------------------------------------------

    def __lt__(self, other: Money) -> bool:
        self._check_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._check_same_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._check_same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._check_same_currency(other)
        return self.amount >= other.amount

    # -- прочее -------------------------------------------------------------

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"

    def _check_same_currency(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise TypeError(f"ожидалась Money, получено {type(other).__name__}")
        if self.currency != other.currency:
            raise CurrencyMismatch(
                f"разные валюты: {self.currency} и {other.currency}"
            )


def zero(currency: str) -> Money:
    return Money(Decimal(0), currency)


def money_sum(items: Iterable[Money], currency: str | None = None) -> Money:
    """Сумма с проверкой валют.

    `currency` обязателен, если список может оказаться пустым: нулевая сумма
    без валюты — то самое подставное значение, которое запрещено правилом 5.4.
    """
    total: Money | None = None
    for item in items:
        if currency is not None and item.currency != currency.upper():
            raise CurrencyMismatch(f"ожидалась {currency.upper()}, получена {item.currency}")
        total = item if total is None else total + item

    if total is not None:
        return total
    if currency is None:
        raise ValueError("сумма пустого списка без указания валюты не определена")
    return zero(currency)


def is_close(a: Money, b: Money, tol: Money) -> bool:
    """Сравнение с допуском — для сверки остатков."""
    a._check_same_currency(b)
    a._check_same_currency(tol)
    if tol.amount < 0:
        raise ValueError("допуск не может быть отрицательным")
    return abs(a.amount - b.amount) <= tol.amount


def round_amount(m: Money, places: Decimal = CENT) -> Money:
    """Округление до копейки, ROUND_HALF_UP. Округляется только на выводе."""
    return Money(m.amount.quantize(places, rounding=ROUND_HALF_UP), m.currency)


def _as_decimal(value: Decimal | int) -> Decimal:
    if isinstance(value, float):
        raise TypeError("множитель не может быть float")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    raise TypeError(f"ожидался Decimal или int, получено {type(value).__name__}")


def _reject_money(value: object, message: str) -> None:
    if isinstance(value, Money):
        raise TypeError(message)
