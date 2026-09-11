"""Прототип `verify-yields`: свой расчёт против биржевого.

Смысл прогона — не «сойтись», а получить список выбросов и разобрать их
(`docs/PARALLEL-TRACK.md`, B1). Первый прогон даёт расхождения на десятках
бумаг, и почти всегда это конвенция, а не арифметика.

Две вещи, без которых сравнение бессмысленно:

1. **Цена из той же строки ISS.** Доска отдаёт доходность в нескольких полях с
   разным смыслом; сравнивать свой расчёт можно только с тем полем, для
   которого известна использованная цена (спека 5.6).
2. **Дата расчётов, на которой сошёлся НКД.** `ACCRUEDINT` есть по каждой
   бумаге каждый день и проверяет график платежей лучше всего остального.
   Поэтому дата расчётов не угадывается, а подбирается по совпадению НКД — и
   если не подобралась, расхождение по доходности уже объяснено (A-19).
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from bondlab.store import CheckRow
from portfolio.calc.bonds import (
    BondSchedule,
    DayCount,
    accrued_interest,
    basis_points,
    duration_days,
    face_value_at,
    full_price,
    future_cashflows,
    has_unknown,
    ytm,
    ytp,
)

# Пары «цена → доходность, посчитанная биржей по этой цене». Поля из разных
# блоков доски: `securities` и `marketdata`.
PRICE_SOURCES: tuple[tuple[str, str], ...] = (
    ("PREVWAPRICE", "YIELDATPREVWAPRICE"),
    ("WAPRICE", "YIELDATWAPRICE"),
    ("LAST", "YIELD"),
)

# Порог спеки 5.6: расхождение более 5 б.п. означает баг в графике платежей.
THRESHOLD_BP = Decimal(5)

KOPECK = Decimal("0.01")
MAX_SETTLEMENT_OFFSET = 4


@dataclass(frozen=True)
class Comparison:
    """Результат сверки одной бумаги по одному источнику цены."""

    secid: str
    price_source: str
    price: Decimal | None
    own_yield: Decimal | None
    iss_yield: Decimal | None
    own_nkd: Decimal | None
    iss_nkd: Decimal | None
    settlement: date | None
    own_duration: int | None
    iss_duration: int | None
    own_yield_to_offer: Decimal | None
    note: str

    @property
    def diff_bp(self) -> Decimal | None:
        if self.own_yield is None or self.iss_yield is None:
            return None
        return basis_points(self.own_yield - self.iss_yield)

    @property
    def diff_to_offer_bp(self) -> Decimal | None:
        if self.own_yield_to_offer is None or self.iss_yield is None:
            return None
        return basis_points(self.own_yield_to_offer - self.iss_yield)

    @property
    def is_outlier(self) -> bool:
        diff = self.diff_bp
        return diff is None or abs(diff) > THRESHOLD_BP

    def to_row(self) -> CheckRow:
        return CheckRow(
            secid=self.secid,
            price_source=self.price_source,
            price=_text(self.price),
            own_yield=_text(self.own_yield),
            iss_yield=_text(self.iss_yield),
            diff_bp=_text(self.diff_bp),
            own_nkd=_text(self.own_nkd),
            iss_nkd=_text(self.iss_nkd),
            nkd_settled=self.settlement.isoformat() if self.settlement else None,
            own_duration=self.own_duration,
            iss_duration=self.iss_duration,
            note=self.note,
        )


@dataclass(frozen=True)
class Distribution:
    """Распределение расхождений по одному источнику цены."""

    price_source: str
    compared: int
    skipped: int
    median_bp: Decimal | None
    p90_bp: Decimal | None
    max_bp: Decimal | None
    within_threshold: int


def settlement_candidates(
    schedule: BondSchedule,
    trade_date: date,
    iss_accrued: Decimal | None,
    *,
    max_offset: int = MAX_SETTLEMENT_OFFSET,
) -> list[date]:
    """Даты расчётов, на которых свой НКД совпал с `ACCRUEDINT`.

    Режим расчётов у досок разный (T+0, T+1), торговый календарь в этом треке
    не заводится — вместо угадывания перебираются ближайшие даты. Ровно одно
    совпадение подтверждает график платежей. Ни одного — дальше сравнивать
    доходности незачем, ошибка уже найдена. Несколько (нулевой НКД у
    дисконтного выпуска или в дату выплаты купона) — НКД в этот день ничего не
    различает, и дату расчётов приходится задавать снаружи (A-19).
    """
    if iss_accrued is None:
        return []
    matched: list[date] = []
    for offset in range(max_offset + 1):
        candidate = trade_date + timedelta(days=offset)
        own = accrued_interest(schedule, candidate)
        if own is not None and abs(own.amount - iss_accrued) <= KOPECK:
            matched.append(candidate)
    return matched


def compare_security(
    schedule: BondSchedule,
    quote: dict[str, Any],
    trade_date: date,
    *,
    day_count: DayCount = DayCount.ACT_365,
    fallback_offset: int = 1,
) -> list[Comparison]:
    """Сверка одной бумаги по всем доступным парам «цена → доходность»."""
    iss_accrued = _decimal(quote.get("ACCRUEDINT"))
    candidates = settlement_candidates(schedule, trade_date, iss_accrued)
    settled = candidates[0] if len(candidates) == 1 else None
    settlement = settled or trade_date + timedelta(days=fallback_offset)

    own_accrued = accrued_interest(schedule, settlement)
    flows = future_cashflows(schedule, settlement)
    unknown = has_unknown(flows)
    next_offer = next(iter(schedule.offers_after(settlement)), None)
    iss_duration = _int(quote.get("DURATION"))

    structure = _structure_notes(schedule, candidates, unknown, next_offer is not None)

    results: list[Comparison] = []
    for price_field, yield_field in PRICE_SOURCES:
        clean_pct = _decimal(quote.get(price_field))
        iss_yield = _decimal(quote.get(yield_field))
        if clean_pct is None or clean_pct <= 0:
            continue

        face = face_value_at(schedule, settlement)
        price = full_price(clean_pct, face, own_accrued)

        own_yield = (
            None
            if price is None or unknown
            else ytm(flows, price, settlement, day_count=day_count)
        )
        own_offer_yield = (
            None
            if price is None or next_offer is None
            else ytp(schedule, price, settlement, next_offer, day_count=day_count)
        )
        own_duration = (
            None
            if own_yield is None
            else duration_days(flows, settlement, own_yield, day_count=day_count)
        )

        comparison = Comparison(
            secid=schedule.secid,
            price_source=price_field,
            price=clean_pct,
            own_yield=own_yield,
            iss_yield=None if iss_yield is None else iss_yield / Decimal(100),
            own_nkd=None if own_accrued is None else own_accrued.amount,
            iss_nkd=iss_accrued,
            settlement=settled,
            own_duration=own_duration,
            iss_duration=iss_duration,
            own_yield_to_offer=own_offer_yield,
            note="",
        )
        results.append(_with_note(comparison, structure))

    return results


def summarize(comparisons: Iterable[Comparison]) -> list[Distribution]:
    """Распределение расхождений по источникам цены — первая таблица прогона."""
    by_source: dict[str, list[Comparison]] = {}
    for comparison in comparisons:
        by_source.setdefault(comparison.price_source, []).append(comparison)

    summaries: list[Distribution] = []
    for price_field, _ in PRICE_SOURCES:
        items = by_source.get(price_field, [])
        diffs = sorted(abs(item.diff_bp) for item in items if item.diff_bp is not None)
        summaries.append(
            Distribution(
                price_source=price_field,
                compared=len(diffs),
                skipped=len(items) - len(diffs),
                median_bp=_quantile(diffs, 0.5),
                p90_bp=_quantile(diffs, 0.9),
                max_bp=diffs[-1] if diffs else None,
                within_threshold=sum(1 for diff in diffs if diff <= THRESHOLD_BP),
            )
        )
    return summaries


def outliers(comparisons: Iterable[Comparison]) -> list[Comparison]:
    """Выбросы, отсортированные по величине расхождения. Разбор их — и есть работа."""
    return sorted(
        (item for item in comparisons if item.is_outlier),
        key=lambda item: abs(item.diff_bp or Decimal(10**6)),
        reverse=True,
    )


# -- подсказки по выбросам --------------------------------------------------


def _structure_notes(
    schedule: BondSchedule,
    candidates: Sequence[date],
    unknown: bool,
    has_offer: bool,
) -> list[str]:
    """Структурные признаки выпуска — в порядке вероятности из B1.

    Пометки короткие: колонку причин читают глазами по всему списку выбросов,
    а не по одной строке.
    """
    notes: list[str] = []
    if not candidates:
        notes.append("НКД не сошёлся: ошибка в графике")
    elif len(candidates) > 1:
        notes.append("НКД нулевой: дата расчётов не определяется")
    if unknown:
        notes.append("купоны неизвестны, только YTP (A-12)")
    if has_offer:
        notes.append("оферта раньше погашения")
    if len(schedule.amortizations) > 1:
        notes.append("амортизация")
    if schedule.currency != "RUB":
        notes.append(f"номинал в {schedule.currency}")
    return notes


def _with_note(comparison: Comparison, structure: Sequence[str]) -> Comparison:
    notes = list(structure)

    if comparison.iss_yield is None:
        notes.append("биржа не отдала доходность")
    elif comparison.own_yield is None and comparison.own_yield_to_offer is None:
        notes.append("решения нет")

    offer_diff = comparison.diff_to_offer_bp
    if comparison.is_outlier and offer_diff is not None and abs(offer_diff) <= THRESHOLD_BP:
        notes.append("сходится к оферте")

    note = ", ".join(notes) if notes else "ok"
    return Comparison(**{**comparison.__dict__, "note": note})


# -- мелочи -----------------------------------------------------------------


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    try:
        return Decimal(str(value).strip())
    except (ArithmeticError, ValueError):
        return None


def _int(value: Any) -> int | None:
    parsed = _decimal(value)
    return None if parsed is None else int(parsed)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _quantile(values: Sequence[Decimal], share: float) -> Decimal | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = share * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = Decimal(repr(position - lower))
    return (values[lower] + (values[upper] - values[lower]) * weight).quantize(KOPECK)


def mean_bp(comparisons: Iterable[Comparison]) -> Decimal | None:
    """Средний знаковый сдвиг: систематический сдвиг указывает на конвенцию."""
    diffs = [item.diff_bp for item in comparisons if item.diff_bp is not None]
    if not diffs:
        return None
    return Decimal(repr(statistics.fmean(float(diff) for diff in diffs))).quantize(KOPECK)
