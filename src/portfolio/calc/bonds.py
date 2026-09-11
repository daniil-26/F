"""Облигации: график платежей, НКД, доходности, дюрация.

Слой `calc/` (спека 9): чистые функции, ни БД, ни сети, ни текущей даты. Дата
расчётов всегда приходит аргументом — иначе тест на снапшоте перестал бы быть
воспроизводимым на следующий день.

Порядок слоёв снизу вверх, каждый тестируется до перехода к следующему
(`docs/PARALLEL-TRACK.md`, трек B):

    face_value_at      номинал с учётом амортизации
    accrued_interest   НКД
    full_price         полная цена
    future_cashflows   построение будущего потока
    ytm / ytp          солвер
    duration / modified_duration / g_spread

Вся сложность — в построении графика, а не в солвере: расхождение с биржевой
доходностью почти всегда означает ошибку в `future_cashflows` (спека 9).

**Неизвестное не равно нулю** (спека 5.4). У флоатера и у бумаги за горизонтом
оферты будущие купоны неизвестны; такой купон приходит сюда как `None`, и
доходность к погашению по нему **не считается** — возвращается `None`, а не
правдоподобное число (A-12).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from itertools import pairwise

from portfolio.calc.money import CENT, CurrencyMismatch, Money, zero

__all__ = [
    "Amortization",
    "BondSchedule",
    "CashFlow",
    "CashFlowKind",
    "Compounding",
    "Coupon",
    "DayCount",
    "Offer",
    "OfferKind",
    "ScheduleError",
    "accrued_interest",
    "basis_points",
    "coupon_period_at",
    "current_yield",
    "duration",
    "duration_days",
    "face_value_at",
    "full_price",
    "future_cashflows",
    "g_spread",
    "has_unknown",
    "modified_duration",
    "present_value",
    "solve_brent",
    "year_fraction",
    "ytm",
    "ytp",
]

# Границы брекетинга солвера (спека 9): ниже −99% и выше 1000% решение не ищется.
RATE_LOWER = Decimal("-0.99")
RATE_UPPER = Decimal("10")

RATE_PRECISION = Decimal("1E-10")

DAYS_IN_YEAR = Decimal(365)

# Поля датаклассов ниже называются `date`, и внутри тела класса это имя
# перекрывает тип. Псевдоним нужен только для аннотаций.
_Date = date


class ScheduleError(ValueError):
    """График платежей противоречив: такой выпуск не существует."""


# --- конвенции ------------------------------------------------------------


class DayCount(Enum):
    """База начисления дней. Первый подозреваемый при расхождении с биржей (A-16)."""

    ACT_365 = "ACT/365"
    ACT_ACT = "ACT/ACT"
    THIRTY_360 = "30/360"


class Compounding(Enum):
    """Способ приведения ставки к сроку.

    `EFFECTIVE` — эффективная годовая: `(1 + y) ** t`. Так считает Мосбиржа.
    `SIMPLE` — простая: `1 + y * t`. Держится здесь ради разбора выбросов в
    `verify-yields`: расхождение, исчезающее при смене способа, — методология,
    а не ошибка графика.
    """

    EFFECTIVE = "effective"
    SIMPLE = "simple"


def year_fraction(start: date, end: date, basis: DayCount = DayCount.ACT_365) -> Decimal:
    """Доля года между датами. Отрицательная, если `end` раньше `start`."""
    if basis is DayCount.ACT_365:
        return Decimal((end - start).days) / DAYS_IN_YEAR
    if basis is DayCount.ACT_ACT:
        return _act_act(start, end)
    return _thirty_360(start, end)


def _act_act(start: date, end: date) -> Decimal:
    """ACT/ACT (ISDA): каждый календарный год делится на свою длину."""
    if end < start:
        return -_act_act(end, start)

    total = Decimal(0)
    year = start.year
    cursor = start
    while year < end.year:
        boundary = date(year + 1, 1, 1)
        total += Decimal((boundary - cursor).days) / Decimal(_days_in_year(year))
        cursor = boundary
        year += 1
    total += Decimal((end - cursor).days) / Decimal(_days_in_year(year))
    return total


def _days_in_year(year: int) -> int:
    return (date(year + 1, 1, 1) - date(year, 1, 1)).days


def _thirty_360(start: date, end: date) -> Decimal:
    """30/360 (US NASD): месяц ровно 30 дней, год ровно 360."""
    d1 = min(start.day, 30)
    d2 = end.day
    if d1 == 30 and d2 == 31:
        d2 = 30
    days = (end.year - start.year) * 360 + (end.month - start.month) * 30 + (d2 - d1)
    return Decimal(days) / Decimal(360)


# --- элементы графика -----------------------------------------------------


@dataclass(frozen=True)
class Coupon:
    """Купон из `bondization`.

    `value is None` означает «купон неизвестен», а не «купон равен нулю»
    (A-12). Флоатер отдаёт плейсхолдер на все периоды за горизонтом
    установления ставки; взятый как ноль, он даёт систематически заниженную
    доходность, неотличимую от верной.
    """

    date: _Date
    period_start: _Date
    value: Money | None
    rate_pct: Decimal | None = None

    def __post_init__(self) -> None:
        if self.period_start >= self.date:
            raise ScheduleError(
                f"купонный период {self.period_start}..{self.date} пуст или обращён"
            )

    @property
    def is_known(self) -> bool:
        return self.value is not None

    @property
    def period_days(self) -> int:
        return (self.date - self.period_start).days


@dataclass(frozen=True)
class Amortization:
    """Погашение части номинала. Финальное погашение — тоже амортизация."""

    date: _Date
    value: Money
    is_maturity: bool = False


class OfferKind(Enum):
    """Put — право владельца, call — право эмитента (спека, «Прогноз и облигации»).

    Единой «доходности к оферте» не бывает: по put решение принимаете вы и
    платите комиссию за предъявление, по call решение принимает эмитент.
    """

    PUT = "put"
    CALL = "call"


@dataclass(frozen=True)
class Offer:
    """Оферта. `kind is None` — тип не распознан источником, а не «обычная»."""

    date: _Date
    kind: OfferKind | None = None
    price_pct: Decimal | None = None


class CashFlowKind(Enum):
    COUPON = "coupon"
    AMORTIZATION = "amortization"
    REDEMPTION = "redemption"


@dataclass(frozen=True)
class CashFlow:
    """Элемент будущего потока. `amount is None` — платёж неизвестен."""

    date: _Date
    amount: Money | None
    kind: CashFlowKind

    @property
    def is_known(self) -> bool:
        return self.amount is not None


@dataclass(frozen=True)
class BondSchedule:
    """График платежей одного выпуска на одну облигацию.

    Все суммы — в валюте номинала. Валюта расчётов у замещающих и юаневых
    выпусков другая (A-18); конвертация живёт выше слоя `calc/`.
    """

    secid: str
    currency: str
    initial_face_value: Money
    maturity_date: date
    coupons: tuple[Coupon, ...] = ()
    amortizations: tuple[Amortization, ...] = ()
    offers: tuple[Offer, ...] = ()
    name: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        currency = self.currency.upper()
        object.__setattr__(self, "currency", currency)

        if self.initial_face_value.currency != currency:
            raise CurrencyMismatch(
                f"номинал в {self.initial_face_value.currency}, выпуск в {currency}"
            )
        if self.initial_face_value.amount <= 0:
            raise ScheduleError(f"номинал должен быть положительным: {self.initial_face_value}")

        for coupon in self.coupons:
            if coupon.value is not None and coupon.value.currency != currency:
                raise CurrencyMismatch(
                    f"купон {coupon.date} в {coupon.value.currency}, выпуск в {currency}"
                )
        for amortization in self.amortizations:
            if amortization.value.currency != currency:
                raise CurrencyMismatch(
                    f"амортизация {amortization.date} в {amortization.value.currency}, "
                    f"выпуск в {currency}"
                )

        object.__setattr__(self, "coupons", tuple(sorted(self.coupons, key=lambda c: c.date)))
        object.__setattr__(
            self, "amortizations", tuple(sorted(self.amortizations, key=lambda a: a.date))
        )
        object.__setattr__(self, "offers", tuple(sorted(self.offers, key=lambda o: o.date)))

        repaid = sum((a.value.amount for a in self.amortizations), Decimal(0))
        if repaid > self.initial_face_value.amount:
            raise ScheduleError(
                f"сумма амортизаций {repaid} больше номинала {self.initial_face_value.amount}"
            )

    @property
    def has_unknown_coupons(self) -> bool:
        return any(not coupon.is_known for coupon in self.coupons)

    def offers_after(self, on: date, *, kind: OfferKind | None = None) -> tuple[Offer, ...]:
        """Оферты строго после даты. Прошедшая оферта на расчёт не влияет."""
        return tuple(
            offer
            for offer in self.offers
            if offer.date > on and (kind is None or offer.kind is kind)
        )


# --- слой 1: номинал ------------------------------------------------------


def face_value_at(schedule: BondSchedule, on: date) -> Money:
    """Номинал на дату с учётом амортизации (A-14).

    Амортизация с датой `d` считается выплаченной **на** `d`: начиная с этой
    даты номинал уменьшен. Купонный период, заканчивающийся в `d`, начислялся
    на прежний номинал — поэтому НКД смотрит на купон, а не на номинал.
    """
    repaid = sum(
        (item.value.amount for item in schedule.amortizations if item.date <= on),
        Decimal(0),
    )
    return Money(schedule.initial_face_value.amount - repaid, schedule.currency)


# --- слой 2: НКД ----------------------------------------------------------


def coupon_period_at(schedule: BondSchedule, on: date) -> Coupon | None:
    """Купон, в периоде которого лежит дата.

    В дату выплаты период уже закрыт: купон достаётся продавцу, НКД у
    покупателя обнуляется.
    """
    for coupon in schedule.coupons:
        if coupon.period_start <= on < coupon.date:
            return coupon
    return None


def accrued_interest(schedule: BondSchedule, on: date) -> Money | None:
    """НКД на дату расчётов (A-15).

    Линейно по фактическим дням периода, с округлением до копейки на одну
    облигацию — так печатает `ACCRUEDINT` доска торгов. Сравнение своего НКД с
    этим полем проверяет график платежей лучше любого другого теста: поле есть
    по каждой бумаге каждый день.

    `None` — купон текущего периода неизвестен (флоатер за горизонтом
    установления ставки). Вне купонных периодов (до размещения, после
    погашения, дисконтный выпуск) НКД равен нулю — это факт, а не подстановка.
    """
    coupon = coupon_period_at(schedule, on)
    if coupon is None:
        return zero(schedule.currency)
    if coupon.value is None:
        return None

    accrued_days = Decimal((on - coupon.period_start).days)
    share = accrued_days / Decimal(coupon.period_days)
    return Money(
        (coupon.value.amount * share).quantize(CENT, rounding=ROUND_HALF_UP),
        schedule.currency,
    )


# --- слой 3: полная цена --------------------------------------------------


def full_price(clean_pct: Decimal, face: Money, accrued: Money | None) -> Money | None:
    """Полная цена = `цена_% × номинал_на_дату / 100 + НКД` (спека 4.3).

    Цена всегда в процентах от номинала на дату, а не от исходного: у
    амортизируемого выпуска это разные числа. `None` на входе НКД даёт `None`
    на выходе — цена бумаги с неизвестным НКД неизвестна.
    """
    if accrued is None:
        return None
    if accrued.currency != face.currency:
        raise CurrencyMismatch(f"НКД в {accrued.currency}, номинал в {face.currency}")
    return Money(face.amount * clean_pct / Decimal(100), face.currency) + accrued


# --- слой 4: будущий поток ------------------------------------------------


def future_cashflows(
    schedule: BondSchedule,
    from_date: date,
    *,
    until: date | None = None,
) -> tuple[CashFlow, ...]:
    """Будущий поток на одну облигацию: платежи строго после `from_date`.

    Строго — потому что платёж в дату расчётов достаётся продавцу.

    `until` усекает поток (используется `ytp`); погашение остатка номинала на
    эту дату сюда **не** добавляется, этим занимается `ytp`.
    """
    flows: list[CashFlow] = []

    for coupon in schedule.coupons:
        if coupon.date <= from_date:
            continue
        if until is not None and coupon.date > until:
            continue
        flows.append(CashFlow(date=coupon.date, amount=coupon.value, kind=CashFlowKind.COUPON))

    for amortization in schedule.amortizations:
        if amortization.date <= from_date:
            continue
        if until is not None and amortization.date > until:
            continue
        kind = CashFlowKind.REDEMPTION if amortization.is_maturity else CashFlowKind.AMORTIZATION
        flows.append(CashFlow(date=amortization.date, amount=amortization.value, kind=kind))

    flows.sort(key=lambda flow: (flow.date, flow.kind.value))
    return tuple(flows)


def has_unknown(flows: Iterable[CashFlow]) -> bool:
    return any(not flow.is_known for flow in flows)


# --- слой 5: солвер -------------------------------------------------------


def present_value(
    flows: Sequence[CashFlow],
    settlement: date,
    rate: Decimal,
    *,
    day_count: DayCount = DayCount.ACT_365,
    compounding: Compounding = Compounding.EFFECTIVE,
) -> Money | None:
    """Приведённая стоимость потока. `None`, если в потоке есть неизвестный платёж."""
    if has_unknown(flows):
        return None
    if not flows:
        return None

    currency = _single_currency(flows)
    total = _discount(_as_pairs(flows, settlement, day_count), float(rate), compounding)
    if total is None:
        return None
    return Money(Decimal(repr(total)), currency)


def ytm(
    flows: Sequence[CashFlow],
    price: Money,
    settlement: date,
    *,
    day_count: DayCount = DayCount.ACT_365,
    compounding: Compounding = Compounding.EFFECTIVE,
) -> Decimal | None:
    """Доходность к погашению по полной цене.

    По умолчанию — эффективная годовая ставка с базой ACT/365 (A-16).

    `None` возвращается в трёх случаях, и все три означают «неизвестно», а не
    «ноль» (спека 5.4):

    * в потоке есть неизвестный платёж — флоатер или бумага за горизонтом
      оферты; для такой бумаги считается только `ytp` (A-12);
    * поток пуст;
    * решения на `[-0.99, 10]` нет.
    """
    if has_unknown(flows) or not flows:
        return None
    if price.amount <= 0:
        return None
    if price.currency != _single_currency(flows):
        raise CurrencyMismatch(f"цена в {price.currency}, поток в {_single_currency(flows)}")

    pairs = _as_pairs(flows, settlement, day_count)
    if all(years <= 0 for years, _ in pairs):
        return None

    target = float(price.amount)

    def mismatch(rate: float) -> float | None:
        value = _discount(pairs, rate, compounding)
        return None if value is None else value - target

    root = solve_brent(mismatch, _lower_bound(pairs, compounding), float(RATE_UPPER))
    if root is None:
        return None
    return Decimal(repr(root)).quantize(RATE_PRECISION)


def ytp(
    schedule: BondSchedule,
    price: Money,
    settlement: date,
    offer: Offer,
    *,
    day_count: DayCount = DayCount.ACT_365,
    compounding: Compounding = Compounding.EFFECTIVE,
) -> Decimal | None:
    """Доходность к оферте: поток усекается на дате оферты.

    В дату оферты бумага предъявляется по цене `price_pct` от номинала на эту
    дату (по умолчанию 100%). Купоны и амортизации после оферты в расчёт не
    входят — именно поэтому доходность к put-оферте считается и тогда, когда
    купоны за её горизонтом неизвестны.

    Тип оферты на арифметику не влияет и обязан учитываться при принятии
    решения: put требует вашего действия и комиссии брокера, call — право
    эмитента (A-17).
    """
    if offer.date <= settlement:
        return None

    flows = list(future_cashflows(schedule, settlement, until=offer.date))
    remaining = face_value_at(schedule, offer.date)
    if remaining.amount > 0:
        price_pct = Decimal(100) if offer.price_pct is None else offer.price_pct
        flows.append(
            CashFlow(
                date=offer.date,
                amount=Money(remaining.amount * price_pct / Decimal(100), schedule.currency),
                kind=CashFlowKind.REDEMPTION,
            )
        )

    return ytm(
        tuple(flows),
        price,
        settlement,
        day_count=day_count,
        compounding=compounding,
    )


def solve_brent(
    function: Callable[[float], float | None],
    lower: float,
    upper: float,
    *,
    tolerance: float = 1e-12,
    max_iterations: int = 200,
) -> float | None:
    """Метод Брента на отрезке с брекетингом (спека 9).

    `None` — решения на отрезке нет: концы одного знака или функция там не
    определена. Произвольное число вместо `None` было бы утверждением о
    доходности, которого никто не проверял.
    """
    a, b = lower, upper
    fa = function(a)
    fb = function(b)
    if fa is None or fb is None or not math.isfinite(fa) or not math.isfinite(fb):
        return None
    if fa == 0.0:
        return a
    if fb == 0.0:
        return b
    if fa * fb > 0:
        return None

    c, fc = a, fa
    d = e = b - a

    for _ in range(max_iterations):
        if fb * fc > 0:
            c, fc = a, fa
            d = e = b - a
        if abs(fc) < abs(fb):
            a, b, c = b, c, b
            fa, fb, fc = fb, fc, fb

        tol = 2.0 * 1e-16 * abs(b) + 0.5 * tolerance
        midpoint = 0.5 * (c - b)
        if abs(midpoint) <= tol or fb == 0.0:
            return b

        if abs(e) >= tol and abs(fa) > abs(fb):
            s = fb / fa
            if a == c:
                p = 2.0 * midpoint * s
                q = 1.0 - s
            else:
                q = fa / fc
                r = fb / fc
                p = s * (2.0 * midpoint * q * (q - r) - (b - a) * (r - 1.0))
                q = (q - 1.0) * (r - 1.0) * (s - 1.0)
            if p > 0:
                q = -q
            p = abs(p)
            if 2.0 * p < min(3.0 * midpoint * q - abs(tol * q), abs(e * q)):
                e, d = d, p / q
            else:
                d = e = midpoint
        else:
            d = e = midpoint

        a, fa = b, fb
        b += d if abs(d) > tol else math.copysign(tol, midpoint)
        value = function(b)
        if value is None or not math.isfinite(value):
            return None
        fb = value

    return None


# --- слой 6: дюрация и спреды ---------------------------------------------


def duration(
    flows: Sequence[CashFlow],
    settlement: date,
    rate: Decimal,
    *,
    day_count: DayCount = DayCount.ACT_365,
) -> Decimal | None:
    """Дюрация Маколея в годах. `None`, если поток неизвестен или пуст."""
    if has_unknown(flows) or not flows:
        return None

    weighted = 0.0
    total = 0.0
    for years, amount in _as_pairs(flows, settlement, day_count):
        if years <= 0:
            continue
        discounted = amount / (1.0 + float(rate)) ** years
        weighted += years * discounted
        total += discounted

    if total <= 0:
        return None
    return Decimal(repr(weighted / total)).quantize(Decimal("1E-8"))


def duration_days(
    flows: Sequence[CashFlow],
    settlement: date,
    rate: Decimal,
    *,
    day_count: DayCount = DayCount.ACT_365,
) -> int | None:
    """Дюрация в днях — в этих единицах её печатает доска торгов ISS."""
    years = duration(flows, settlement, rate, day_count=day_count)
    if years is None:
        return None
    return int((years * DAYS_IN_YEAR).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def modified_duration(
    flows: Sequence[CashFlow],
    settlement: date,
    rate: Decimal,
    *,
    day_count: DayCount = DayCount.ACT_365,
) -> Decimal | None:
    """Модифицированная дюрация: Маколея, делённая на `1 + y`."""
    macaulay = duration(flows, settlement, rate, day_count=day_count)
    if macaulay is None:
        return None
    denominator = Decimal(1) + rate
    if denominator <= 0:
        return None
    return (macaulay / denominator).quantize(Decimal("1E-8"))


def current_yield(
    schedule: BondSchedule,
    on: date,
    price: Money,
    *,
    horizon_days: int = 365,
) -> Decimal | None:
    """Текущая доходность: купоны ближайших `horizon_days` к цене.

    Какую цену подставлять — полную или чистую — решает вызывающий: обе
    конвенции встречаются, и функция не выбирает за него. `None`, если хотя бы
    один купон горизонта неизвестен.
    """
    if price.amount <= 0:
        return None
    if price.currency != schedule.currency:
        raise CurrencyMismatch(f"цена в {price.currency}, выпуск в {schedule.currency}")

    total = Decimal(0)
    for coupon in schedule.coupons:
        if not on < coupon.date <= _add_days(on, horizon_days):
            continue
        if coupon.value is None:
            return None
        total += coupon.value.amount

    return (total / price.amount).quantize(RATE_PRECISION)


def g_spread(
    bond_yield: Decimal | None,
    curve: Sequence[tuple[Decimal, Decimal]],
    years: Decimal | None,
) -> Decimal | None:
    """YTM минус кривая ОФЗ той же дюрации (спека 4.3).

    Кривая — пары «срок в годах, доходность в долях», интерполяция линейная.
    За пределами кривой спред **не** экстраполируется: `None`. Экстраполяция
    на длинном конце — самый дешёвый способ получить уверенное неверное число.
    """
    if bond_yield is None or years is None or not curve:
        return None

    points = sorted(curve)
    if years < points[0][0] or years > points[-1][0]:
        return None

    for (left_years, left_rate), (right_years, right_rate) in pairwise(points):
        if left_years <= years <= right_years:
            if right_years == left_years:
                return bond_yield - left_rate
            weight = (years - left_years) / (right_years - left_years)
            return bond_yield - (left_rate + (right_rate - left_rate) * weight)

    return bond_yield - points[-1][1]


def basis_points(value: Decimal | None) -> Decimal | None:
    """Доли в базисные пункты: пороги сверки в спеке 5.6 заданы в б.п."""
    if value is None:
        return None
    return (value * Decimal(10_000)).quantize(Decimal("0.01"))


# --- вспомогательное ------------------------------------------------------


def _single_currency(flows: Sequence[CashFlow]) -> str:
    currencies = {flow.amount.currency for flow in flows if flow.amount is not None}
    if len(currencies) > 1:
        raise CurrencyMismatch(f"поток в нескольких валютах: {sorted(currencies)}")
    if not currencies:
        raise ValueError("валюта пустого потока не определена")
    return currencies.pop()


def _as_pairs(
    flows: Sequence[CashFlow],
    settlement: date,
    day_count: DayCount,
) -> tuple[tuple[float, float], ...]:
    """Поток в пары «годы до платежа, сумма».

    Здесь кончается `Decimal` и начинается `float`: солвер работает с
    непрерывной функцией, точности double хватает с запасом на порог в 1 б.п.
    """
    pairs: list[tuple[float, float]] = []
    for flow in flows:
        assert flow.amount is not None  # проверено в has_unknown
        pairs.append(
            (
                float(year_fraction(settlement, flow.date, day_count)),
                float(flow.amount.amount),
            )
        )
    return tuple(pairs)


def _discount(
    pairs: Sequence[tuple[float, float]],
    rate: float,
    compounding: Compounding,
) -> float | None:
    total = 0.0
    for years, amount in pairs:
        if compounding is Compounding.EFFECTIVE:
            base = 1.0 + rate
            if base <= 0:
                return None
            factor = base**years
        else:
            factor = 1.0 + rate * years
            if factor <= 0:
                return None
        total += amount / factor
    return total


def _lower_bound(pairs: Sequence[tuple[float, float]], compounding: Compounding) -> float:
    """Нижняя граница брекетинга.

    При простом начислении множитель `1 + y * t` обращается в ноль на
    `y = -1/t`: левее самого дальнего платежа функция не определена, и
    брекетинг на `[-0.99, 10]` там развалился бы. Граница поднимается ровно
    настолько, насколько нужно.
    """
    lower = float(RATE_LOWER)
    if compounding is not Compounding.SIMPLE:
        return lower
    horizon = max((years for years, _ in pairs), default=0.0)
    if horizon <= 0:
        return lower
    return max(lower, -1.0 / horizon + 1e-9)


def _add_days(start: date, days: int) -> date:
    return start + timedelta(days=days)
