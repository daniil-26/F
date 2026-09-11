"""Облигации послойно: номинал на дату, НКД, график, YTM, YTP, дюрация.

Порядок тестов повторяет порядок слоёв (спека 9, `test_bonds.py`). Это не
косметика: если не сходится YTM, ошибка почти наверняка в графике платежей, и
найти её должен упавший тест слоем ниже, а не разбор выброса руками.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portfolio.calc.bonds import (
    Amortization,
    BondSchedule,
    CashFlow,
    CashFlowKind,
    Compounding,
    Coupon,
    DayCount,
    Offer,
    OfferKind,
    ScheduleError,
    accrued_interest,
    basis_points,
    coupon_period_at,
    current_yield,
    duration,
    duration_days,
    face_value_at,
    full_price,
    future_cashflows,
    g_spread,
    has_unknown,
    modified_duration,
    present_value,
    solve_brent,
    year_fraction,
    ytm,
    ytp,
)
from portfolio.calc.money import CurrencyMismatch, Money

RUB = "RUB"


def rub(amount: str | int) -> Money:
    return Money(Decimal(str(amount)), RUB)


def annual_bond(
    rate: str = "10",
    years: int = 2,
    face: int = 1000,
    *,
    unknown_from: int | None = None,
) -> BondSchedule:
    """Годовой купон, выплаты 1 января, погашение номиналом.

    `unknown_from` — с какого по счёту купона значения неизвестны: так приходит
    флоатер.
    """
    coupons = []
    for index in range(1, years + 1):
        value = Decimal(face) * Decimal(rate) / Decimal(100)
        known = unknown_from is None or index < unknown_from
        coupons.append(
            Coupon(
                date=date(2025 + index, 1, 1),
                period_start=date(2024 + index, 1, 1),
                value=rub(value) if known else None,
                rate_pct=Decimal(rate) if known else None,
            )
        )
    return BondSchedule(
        secid="TEST",
        currency=RUB,
        initial_face_value=rub(face),
        maturity_date=date(2025 + years, 1, 1),
        coupons=tuple(coupons),
        amortizations=(
            Amortization(date=date(2025 + years, 1, 1), value=rub(face), is_maturity=True),
        ),
    )


# -- слой 0: конвенции ------------------------------------------------------


def test_year_fraction_act_365() -> None:
    assert year_fraction(date(2025, 1, 1), date(2026, 1, 1)) == Decimal(365) / Decimal(365)
    assert year_fraction(date(2025, 1, 1), date(2025, 1, 1)) == 0
    assert year_fraction(date(2026, 1, 1), date(2025, 1, 1)) < 0


def test_year_fraction_act_act_counts_leap_year() -> None:
    """2024 високосный: год считается своей длиной, а не усреднённой."""
    whole = year_fraction(date(2024, 1, 1), date(2025, 1, 1), DayCount.ACT_ACT)
    assert whole == 1
    assert year_fraction(date(2024, 1, 1), date(2025, 1, 1)) > 1  # ACT/365: 366/365


def test_year_fraction_thirty_360() -> None:
    assert year_fraction(date(2025, 1, 31), date(2025, 2, 28), DayCount.THIRTY_360) == Decimal(
        28
    ) / Decimal(360)
    assert year_fraction(date(2025, 1, 1), date(2026, 1, 1), DayCount.THIRTY_360) == 1


# -- слой 1: номинал --------------------------------------------------------


def test_face_value_falls_with_amortization() -> None:
    schedule = BondSchedule(
        secid="AMO",
        currency=RUB,
        initial_face_value=rub(1000),
        maturity_date=date(2028, 1, 1),
        amortizations=(
            Amortization(date=date(2026, 1, 1), value=rub(300)),
            Amortization(date=date(2027, 1, 1), value=rub(300)),
            Amortization(date=date(2028, 1, 1), value=rub(400), is_maturity=True),
        ),
    )

    assert face_value_at(schedule, date(2025, 12, 31)) == rub(1000)
    assert face_value_at(schedule, date(2026, 1, 1)) == rub(700)
    assert face_value_at(schedule, date(2027, 6, 1)) == rub(400)
    assert face_value_at(schedule, date(2028, 1, 1)) == rub(0)


def test_amortizations_cannot_exceed_face() -> None:
    with pytest.raises(ScheduleError):
        BondSchedule(
            secid="BAD",
            currency=RUB,
            initial_face_value=rub(1000),
            maturity_date=date(2027, 1, 1),
            amortizations=(Amortization(date=date(2027, 1, 1), value=rub(1500)),),
        )


def test_currency_of_payments_must_match_face() -> None:
    with pytest.raises(CurrencyMismatch):
        BondSchedule(
            secid="BAD",
            currency="USD",
            initial_face_value=Money(1000, "USD"),
            maturity_date=date(2027, 1, 1),
            coupons=(
                Coupon(date=date(2026, 1, 1), period_start=date(2025, 1, 1), value=rub(50)),
            ),
        )


# -- слой 2: НКД ------------------------------------------------------------


def test_accrued_interest_is_linear_in_days() -> None:
    schedule = annual_bond()
    # Половина периода 2025-01-01..2026-01-01 (365 дней) — 182 дня.
    assert accrued_interest(schedule, date(2025, 7, 2)) == rub("49.86")
    assert accrued_interest(schedule, date(2025, 1, 1)) == rub(0)


def test_accrued_interest_resets_on_payment_date() -> None:
    """В дату выплаты купон достаётся продавцу, у покупателя НКД нулевой."""
    schedule = annual_bond()
    assert accrued_interest(schedule, date(2026, 1, 1)) == rub(0)
    assert coupon_period_at(schedule, date(2026, 1, 1)) is not None


def test_accrued_interest_is_unknown_for_unfixed_coupon() -> None:
    """Флоатер: купон текущего периода не установлен — НКД неизвестен, а не ноль."""
    schedule = annual_bond(unknown_from=1)
    assert accrued_interest(schedule, date(2025, 7, 2)) is None


def test_accrued_interest_outside_periods_is_zero() -> None:
    """После погашения начислять нечего: ноль здесь — факт, а не подстановка."""
    schedule = annual_bond()
    assert accrued_interest(schedule, date(2030, 1, 1)) == rub(0)


# -- слой 3: полная цена ----------------------------------------------------


def test_full_price_is_percent_of_face_plus_accrued() -> None:
    price = full_price(Decimal("99.5"), rub(1000), rub("12.34"))
    assert price == rub("1007.34")


def test_full_price_uses_face_on_date_not_initial() -> None:
    """У амортизируемого выпуска процент считается от текущего номинала."""
    assert full_price(Decimal(100), rub(700), rub(0)) == rub(700)


def test_full_price_of_unknown_accrued_is_unknown() -> None:
    assert full_price(Decimal(100), rub(1000), None) is None


# -- слой 4: график платежей ------------------------------------------------


def test_future_cashflows_are_strictly_after_settlement() -> None:
    """Платёж в дату расчётов достаётся продавцу и в поток покупателя не входит."""
    schedule = annual_bond(years=3)
    flows = future_cashflows(schedule, date(2026, 1, 1))
    assert [flow.date for flow in flows] == [
        date(2027, 1, 1),
        date(2028, 1, 1),
        date(2028, 1, 1),
    ]


def test_future_cashflows_marks_unknown_payments() -> None:
    schedule = annual_bond(years=3, unknown_from=2)
    flows = future_cashflows(schedule, date(2025, 6, 1))
    assert has_unknown(flows)
    assert [flow.is_known for flow in flows] == [True, False, False, True]


def test_future_cashflows_truncated_at_offer() -> None:
    schedule = annual_bond(years=3)
    flows = future_cashflows(schedule, date(2025, 6, 1), until=date(2027, 1, 1))
    assert [flow.date for flow in flows] == [date(2026, 1, 1), date(2027, 1, 1)]
    assert all(flow.kind is CashFlowKind.COUPON for flow in flows)


# -- слой 5: доходность -----------------------------------------------------


def test_ytm_of_par_bond_equals_coupon_rate() -> None:
    """Свойство спеки 5.5: бумага без амортизации по номиналу даёт купонную ставку."""
    schedule = annual_bond(rate="10", years=2)
    flows = future_cashflows(schedule, date(2025, 1, 1))
    assert ytm(flows, rub(1000), date(2025, 1, 1)) == Decimal("0.1000000000")


def test_leap_day_shifts_par_yield_slightly() -> None:
    """То же равенство через високосный год выполняется лишь приближённо.

    ACT/365 считает 2028 год длиной 366 дней, и купонные периоды перестают быть
    ровно годом. Расхождение около 1 б.п. — это конвенция, а не ошибка; именно
    такие вещи `verify-yields` и вылавливает.
    """
    schedule = annual_bond(rate="10", years=5)
    flows = future_cashflows(schedule, date(2025, 1, 1))
    rate = ytm(flows, rub(1000), date(2025, 1, 1))
    assert rate is not None
    assert rate != Decimal("0.1")
    assert abs(basis_points(rate - Decimal("0.1")) or Decimal(0)) < Decimal(5)


def test_ytm_round_trip_through_present_value() -> None:
    """Цена, посчитанная по ставке, возвращает ту же ставку. Проверка солвера."""
    schedule = annual_bond(rate="7.5", years=7)
    flows = future_cashflows(schedule, date(2025, 1, 1))
    price = present_value(flows, date(2025, 1, 1), Decimal("0.1234"))
    assert price is not None
    assert ytm(flows, price, date(2025, 1, 1)) == Decimal("0.1234000000")


def test_ytm_is_none_when_a_coupon_is_unknown() -> None:
    """Правило A-12: доходность к погашению по флоатеру не показывается."""
    schedule = annual_bond(years=5, unknown_from=3)
    flows = future_cashflows(schedule, date(2025, 1, 1))
    assert ytm(flows, rub(1000), date(2025, 1, 1)) is None
    assert present_value(flows, date(2025, 1, 1), Decimal("0.1")) is None


def test_ytm_without_future_payments_is_none() -> None:
    assert ytm((), rub(1000), date(2025, 1, 1)) is None


def test_ytm_of_nonpositive_price_is_none() -> None:
    schedule = annual_bond()
    flows = future_cashflows(schedule, date(2025, 1, 1))
    assert ytm(flows, rub(0), date(2025, 1, 1)) is None


def test_ytm_rejects_price_in_another_currency() -> None:
    schedule = annual_bond()
    flows = future_cashflows(schedule, date(2025, 1, 1))
    with pytest.raises(CurrencyMismatch):
        ytm(flows, Money(1000, "USD"), date(2025, 1, 1))


def test_simple_and_effective_compounding_differ() -> None:
    """Эффективная против номинальной — второй подозреваемый при расхождении (B1)."""
    schedule = annual_bond(rate="10", years=5)
    flows = future_cashflows(schedule, date(2025, 1, 1))
    effective = ytm(flows, rub(950), date(2025, 1, 1))
    simple = ytm(flows, rub(950), date(2025, 1, 1), compounding=Compounding.SIMPLE)
    assert effective is not None and simple is not None
    assert effective != simple


def test_ytp_is_computed_when_ytm_is_not() -> None:
    """Бумага с put-офертой: купоны за горизонтом неизвестны, YTP считается."""
    schedule = BondSchedule(
        secid="PUT",
        currency=RUB,
        initial_face_value=rub(1000),
        maturity_date=date(2030, 1, 1),
        coupons=(
            Coupon(date=date(2026, 1, 1), period_start=date(2025, 1, 1), value=rub(120)),
            Coupon(date=date(2027, 1, 1), period_start=date(2026, 1, 1), value=None),
            Coupon(date=date(2028, 1, 1), period_start=date(2027, 1, 1), value=None),
        ),
        amortizations=(
            Amortization(date=date(2030, 1, 1), value=rub(1000), is_maturity=True),
        ),
        offers=(Offer(date=date(2026, 1, 1), kind=OfferKind.PUT),),
    )
    settlement = date(2025, 1, 1)
    flows = future_cashflows(schedule, settlement)

    assert ytm(flows, rub(1000), settlement) is None

    to_offer = ytp(schedule, rub(1000), settlement, schedule.offers[0])
    assert to_offer == Decimal("0.1200000000")


def test_ytp_respects_offer_price() -> None:
    """Оферта по 98% — доходность ниже, чем по номиналу."""
    schedule = annual_bond(rate="10", years=5)
    settlement = date(2025, 1, 1)
    at_par = ytp(schedule, rub(1000), settlement, Offer(date=date(2027, 1, 1)))
    below = ytp(
        schedule,
        rub(1000),
        settlement,
        Offer(date=date(2027, 1, 1), price_pct=Decimal(98)),
    )
    assert at_par is not None and below is not None
    assert below < at_par


def test_ytp_to_past_offer_is_none() -> None:
    schedule = annual_bond()
    assert ytp(schedule, rub(1000), date(2025, 6, 1), Offer(date=date(2025, 1, 1))) is None


def test_solver_returns_none_without_a_root() -> None:
    """Отсутствие решения — `None`, а не произвольное число (спека 9)."""
    assert solve_brent(lambda x: x * x + 1, -0.99, 10) is None


# -- слой 6: дюрация и спреды ----------------------------------------------


def test_macaulay_duration_of_two_year_par_bond() -> None:
    """Эталон, считается на бумаге: 1 × 100/1.1 + 2 × 1100/1.21, делённое на 1000."""
    schedule = annual_bond(rate="10", years=2)
    flows = future_cashflows(schedule, date(2025, 1, 1))
    assert duration(flows, date(2025, 1, 1), Decimal("0.1")) == Decimal("1.90909091")
    assert duration_days(flows, date(2025, 1, 1), Decimal("0.1")) == 697


def test_modified_duration_is_macaulay_over_one_plus_yield() -> None:
    schedule = annual_bond(rate="10", years=2)
    flows = future_cashflows(schedule, date(2025, 1, 1))
    macaulay = duration(flows, date(2025, 1, 1), Decimal("0.1"))
    modified = modified_duration(flows, date(2025, 1, 1), Decimal("0.1"))
    assert macaulay is not None and modified is not None
    assert modified == (macaulay / Decimal("1.1")).quantize(Decimal("1E-8"))


def test_duration_of_unknown_flow_is_none() -> None:
    schedule = annual_bond(years=3, unknown_from=2)
    flows = future_cashflows(schedule, date(2025, 1, 1))
    assert duration(flows, date(2025, 1, 1), Decimal("0.1")) is None
    assert duration_days(flows, date(2025, 1, 1), Decimal("0.1")) is None


def test_current_yield_counts_coupons_of_the_next_year() -> None:
    schedule = annual_bond(rate="10", years=3)
    assert current_yield(schedule, date(2025, 1, 1), rub(1000)) == Decimal("0.1000000000")
    assert current_yield(schedule, date(2025, 1, 1), rub(800)) == Decimal("0.1250000000")


def test_current_yield_is_unknown_for_unfixed_coupon() -> None:
    schedule = annual_bond(years=3, unknown_from=1)
    assert current_yield(schedule, date(2025, 1, 1), rub(1000)) is None


def test_g_spread_interpolates_the_curve() -> None:
    curve = [(Decimal(1), Decimal("0.10")), (Decimal(3), Decimal("0.12"))]
    assert g_spread(Decimal("0.15"), curve, Decimal(2)) == Decimal("0.040")


def test_g_spread_does_not_extrapolate() -> None:
    """За пределами кривой спред неизвестен: экстраполяция — выдумка (спека 5.4)."""
    curve = [(Decimal(1), Decimal("0.10")), (Decimal(3), Decimal("0.12"))]
    assert g_spread(Decimal("0.15"), curve, Decimal(10)) is None
    assert g_spread(None, curve, Decimal(2)) is None


def test_basis_points_conversion() -> None:
    assert basis_points(Decimal("0.0005")) == Decimal("5.00")
    assert basis_points(None) is None


def test_cashflow_currency_mix_is_rejected() -> None:
    flows = (
        CashFlow(date=date(2026, 1, 1), amount=rub(100), kind=CashFlowKind.COUPON),
        CashFlow(date=date(2026, 1, 1), amount=Money(100, "USD"), kind=CashFlowKind.COUPON),
    )
    with pytest.raises(CurrencyMismatch):
        ytm(flows, rub(1000), date(2025, 1, 1))
