"""Property-based тесты (hypothesis) для `formats`, `money` и `bonds`.

Свойства облигаций из спеки 5.5: YTM бумаги без амортизации, купленной по
номиналу, равна купонной ставке; цена монотонно убывает по доходности; сумма
графика купонов не зависит от порядка обхода.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from hypothesis import assume, given
from hypothesis import strategies as st

from portfolio.adapters.formats import format_decimal, parse_decimal
from portfolio.calc.bonds import (
    Amortization,
    BondSchedule,
    Coupon,
    future_cashflows,
    present_value,
    ytm,
)
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


# -- облигации (спека 5.5) -------------------------------------------------

ISSUE_DATE = date(2025, 1, 1)

coupon_rates = st.decimals(
    min_value=Decimal("0.5"),
    max_value=Decimal("25"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
yields = st.decimals(
    min_value=Decimal("0.001"),
    max_value=Decimal("0.5"),
    allow_nan=False,
    allow_infinity=False,
    places=4,
)


def par_bond(rate: Decimal, periods: int) -> BondSchedule:
    """Годовой купон ровно по 365 дней: доля года целая при любой базе ACT/365.

    Периоды считаются от даты размещения шагом в 365 дней, а не по календарю:
    високосный день сдвинул бы доходность на доли базисного пункта и превратил
    точное свойство в приближённое (см. `test_leap_day_shifts_par_yield_slightly`).
    """
    face = Decimal(1000)
    coupons = tuple(
        Coupon(
            date=ISSUE_DATE + timedelta(days=365 * index),
            period_start=ISSUE_DATE + timedelta(days=365 * (index - 1)),
            value=Money(face * rate / Decimal(100), "RUB"),
            rate_pct=rate,
        )
        for index in range(1, periods + 1)
    )
    maturity = ISSUE_DATE + timedelta(days=365 * periods)
    return BondSchedule(
        secid="PROP",
        currency="RUB",
        initial_face_value=Money(face, "RUB"),
        maturity_date=maturity,
        coupons=coupons,
        amortizations=(Amortization(date=maturity, value=Money(face, "RUB"), is_maturity=True),),
    )


@given(coupon_rates, st.integers(min_value=1, max_value=15))
def test_par_bond_yield_equals_coupon_rate(rate: Decimal, periods: int) -> None:
    """Бумага без амортизации, купленная по номиналу, даёт YTM, равную купону."""
    flows = future_cashflows(par_bond(rate, periods), ISSUE_DATE)
    calculated = ytm(flows, Money(Decimal(1000), "RUB"), ISSUE_DATE)

    assert calculated is not None
    assert abs(calculated - rate / Decimal(100)) < Decimal("0.00000001")


@given(coupon_rates, st.integers(min_value=1, max_value=15), yields, yields)
def test_price_decreases_with_yield(
    rate: Decimal,
    periods: int,
    low: Decimal,
    high: Decimal,
) -> None:
    """Цена монотонно убывает по доходности."""
    assume(low < high)
    flows = future_cashflows(par_bond(rate, periods), ISSUE_DATE)

    cheaper = present_value(flows, ISSUE_DATE, high)
    dearer = present_value(flows, ISSUE_DATE, low)

    assert cheaper is not None and dearer is not None
    assert cheaper < dearer


@given(coupon_rates, st.integers(min_value=1, max_value=15))
def test_coupon_sum_does_not_depend_on_order(rate: Decimal, periods: int) -> None:
    """Сумма графика купонов не зависит от порядка обхода."""
    schedule = par_bond(rate, periods)
    values = [coupon.value for coupon in schedule.coupons if coupon.value is not None]
    shuffled = list(reversed(values))

    assert money_sum(values, "RUB") == money_sum(shuffled, "RUB")
