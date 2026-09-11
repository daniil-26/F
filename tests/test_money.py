"""Арифметика денег: валюты, запрет float, округления."""

from __future__ import annotations

from decimal import Decimal

import pytest

from portfolio.calc.money import (
    CurrencyMismatch,
    Money,
    is_close,
    money_sum,
    round_amount,
    zero,
)


def test_money_refuses_float() -> None:
    """Деньги — только Decimal (спека 2, п.6): float не приводится, а отвергается."""
    with pytest.raises(TypeError):
        Money(1.5, "RUB")  # type: ignore[arg-type]


def test_money_accepts_decimal_int_and_string() -> None:
    assert Money(Decimal("1.5"), "RUB").amount == Decimal("1.5")
    assert Money(2, "rub").currency == "RUB"
    assert Money("0.10", "USD").amount == Decimal("0.10")


def test_addition_requires_same_currency() -> None:
    with pytest.raises(CurrencyMismatch):
        Money(1, "RUB") + Money(1, "USD")
    with pytest.raises(CurrencyMismatch):
        Money(1, "RUB") - Money(1, "USD")
    with pytest.raises(CurrencyMismatch):
        _ = Money(1, "RUB") < Money(1, "USD")


def test_money_times_money_is_undefined() -> None:
    with pytest.raises(TypeError):
        Money(2, "RUB") * Money(3, "RUB")  # type: ignore[operator]
    with pytest.raises(TypeError):
        Money(2, "RUB") / Money(3, "RUB")  # type: ignore[operator]


def test_scaling_by_decimal() -> None:
    assert (Money("10.00", "RUB") * Decimal("1.5")).amount == Decimal("15.000")
    assert (Money("10.00", "RUB") / Decimal(4)).amount == Decimal("2.5")


def test_round_amount_half_up() -> None:
    assert round_amount(Money("10.005", "RUB")).amount == Decimal("10.01")
    assert round_amount(Money("-10.005", "RUB")).amount == Decimal("-10.01")
    assert round_amount(Money("10.004", "RUB")).amount == Decimal("10.00")


def test_money_sum_checks_currencies() -> None:
    assert money_sum([Money(1, "RUB"), Money("2.5", "RUB")]) == Money("3.5", "RUB")
    with pytest.raises(CurrencyMismatch):
        money_sum([Money(1, "RUB"), Money(1, "USD")])


def test_empty_sum_needs_explicit_currency() -> None:
    """Нулевая сумма без валюты — подставное значение, а не результат."""
    with pytest.raises(ValueError, match="пустого списка"):
        money_sum([])
    assert money_sum([], "RUB") == zero("RUB")


def test_is_close_uses_tolerance() -> None:
    tol = Money("0.01", "RUB")
    assert is_close(Money("100.00", "RUB"), Money("100.01", "RUB"), tol)
    assert not is_close(Money("100.00", "RUB"), Money("100.02", "RUB"), tol)
