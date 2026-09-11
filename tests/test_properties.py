"""Property-based тесты (hypothesis) для `formats` и `money`.

На этапе 1 доступны только свойства разбора и денег: облигационные свойства из
спеки 5.5 появятся вместе с `calc/bonds.py`.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import assume, given
from hypothesis import strategies as st

from portfolio.adapters.formats import format_decimal, parse_decimal
from portfolio.calc.money import Money, money_sum, round_amount

amounts = st.decimals(
    min_value=Decimal("-1e12"),
    max_value=Decimal("1e12"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)


@given(amounts)
def test_parsing_and_formatting_are_inverse(value: Decimal) -> None:
    """Разбор числа и его форматирование взаимно обратны (спека 5.5)."""
    assert parse_decimal(format_decimal(value)) == value


@given(amounts)
def test_formatting_uses_russian_separators(value: Decimal) -> None:
    text = format_decimal(value)
    assert "." not in text
    assert parse_decimal(text.replace(" ", " ")) == value


@given(st.lists(amounts, min_size=1, max_size=20))
def test_money_sum_does_not_depend_on_order(values: list[Decimal]) -> None:
    items = [Money(value, "RUB") for value in values]
    assert money_sum(items) == money_sum(list(reversed(items)))


@given(amounts, amounts)
def test_addition_is_commutative(left: Decimal, right: Decimal) -> None:
    assert Money(left, "RUB") + Money(right, "RUB") == Money(right, "RUB") + Money(left, "RUB")


@given(amounts)
def test_rounding_is_idempotent(value: Decimal) -> None:
    once = round_amount(Money(value, "RUB"))
    assert round_amount(once) == once


@given(amounts, st.integers(min_value=1, max_value=1000))
def test_scaling_then_dividing_returns_original(value: Decimal, factor: int) -> None:
    assume(factor != 0)
    money = Money(value, "RUB")
    assert (money * Decimal(factor) / Decimal(factor)).amount == money.amount
