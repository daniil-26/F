"""Разбор ответов ISS в график платежей.

Здесь живёт единственная содержательная часть трека A: превращение
`bondization` в `BondSchedule`. Всё остальное — транспорт.

Главное правило разбора: **«купон равен нулю» и «купон неизвестен» — разные
вещи** (`docs/PARALLEL-TRACK.md`, A3). У флоатера и у бумаги за горизонтом
оферты будущие купоны приходят плейсхолдером; взятые как есть, они дают
систематически мусорную доходность. Такой купон становится `None` (A-12).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from portfolio.calc.bonds import (
    Amortization,
    BondSchedule,
    Coupon,
    Offer,
    OfferKind,
    ScheduleError,
)
from portfolio.calc.money import Money

JsonDict = dict[str, Any]

# ISS обозначает рубль кодом `SUR`, ISO-4217 — `RUB`. Валюта номинала берётся
# из описания выпуска, а не с доски: у замещающих они различаются (A-18).
CURRENCY_ALIASES = {"SUR": "RUB", "RUB": "RUB"}

# Тип оферты в `bondization` приходит текстом, набор формулировок на реальных
# данных не подтверждён. Нераспознанный тип остаётся `None` — «неизвестно», а
# не «put» (A-17).
OFFER_KINDS: dict[str, OfferKind] = {
    "put": OfferKind.PUT,
    "оферта": OfferKind.PUT,
    "безотзывная оферта": OfferKind.PUT,
    "call": OfferKind.CALL,
    "call-опцион": OfferKind.CALL,
    "досрочное погашение": OfferKind.CALL,
    "отзывная оферта": OfferKind.CALL,
}


class ParseError(ValueError):
    """Ответ ISS не той формы, что ожидалась."""


# -- примитивы -------------------------------------------------------------


def rows(payload: JsonDict, block: str) -> list[dict[str, Any]]:
    """Блок ISS `{"columns": [...], "data": [[...]]}` в список словарей."""
    chunk = payload.get(block)
    if not isinstance(chunk, dict):
        raise ParseError(f"в ответе нет блока {block!r}: {sorted(payload)}")
    columns = list(chunk.get("columns", []))
    return [dict(zip(columns, row, strict=False)) for row in chunk.get("data", [])]


def description(payload: JsonDict) -> dict[str, str]:
    """Блок `description` — пары «имя поля → значение»."""
    result: dict[str, str] = {}
    for row in rows(payload, "description"):
        name = row.get("name")
        value = row.get("value")
        if isinstance(name, str) and value is not None:
            result[name] = str(value)
    return result


def parse_date(value: Any) -> date | None:
    """Дата ISS (`YYYY-MM-DD`). Пустая строка и `0000-00-00` — отсутствие даты."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("0000"):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_decimal(value: Any) -> Decimal | None:
    """Число ISS. `None` и пустая строка остаются `None`, а не нулём (спека 5.4)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ParseError(f"ожидалось число, получено {value!r}")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # ISS отдаёт числа JSON-овым double. Через repr, чтобы не тащить
        # двоичную погрешность в Decimal.
        return Decimal(repr(value))
    text = str(value).strip().replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def currency_of(code: Any) -> str:
    """Код валюты номинала в ISO-4217."""
    text = str(code or "").strip().upper()
    if not text:
        raise ParseError("валюта номинала не указана")
    return CURRENCY_ALIASES.get(text, text)


# -- купоны, амортизации, оферты -------------------------------------------


def coupon_from_row(row: dict[str, Any], currency: str) -> Coupon | None:
    """Строка блока `coupons` в купон.

    Неизвестный купон (`value` пустой; либо ноль без явного нулевого
    `valueprc`) даёт `Coupon.value = None`. Отличить настоящий нулевой купон от
    плейсхолдера можно только так: у настоящего ставка тоже указана нулём
    (A-13, требует подтверждения на реальных данных).
    """
    payment_date = parse_date(row.get("coupondate"))
    period_start = parse_date(row.get("startdate"))
    if payment_date is None or period_start is None:
        return None

    value = parse_decimal(row.get("value"))
    rate = parse_decimal(row.get("valueprc"))
    known = value is not None and (value != 0 or rate == 0)

    return Coupon(
        date=payment_date,
        period_start=period_start,
        value=Money(value, currency) if known and value is not None else None,
        rate_pct=rate,
    )


def amortization_from_row(
    row: dict[str, Any],
    currency: str,
    maturity_date: date | None,
) -> Amortization | None:
    """Строка блока `amortizations`. Финальное погашение — тоже амортизация."""
    when = parse_date(row.get("amortdate"))
    value = parse_decimal(row.get("value"))
    if when is None or value is None:
        return None
    return Amortization(
        date=when,
        value=Money(value, currency),
        is_maturity=maturity_date is not None and when == maturity_date,
    )


def offer_from_row(row: dict[str, Any]) -> Offer | None:
    """Строка блока `offers`. Тип оферты — put или call, это разные вещи (A-17)."""
    when = parse_date(row.get("offerdate"))
    if when is None:
        return None
    raw_kind = str(row.get("offertype") or "").strip().lower()
    return Offer(
        date=when,
        kind=OFFER_KINDS.get(raw_kind),
        price_pct=parse_decimal(row.get("price")),
    )


# -- сборка ----------------------------------------------------------------


def build_schedule(security_payload: JsonDict, bondization_payload: JsonDict) -> BondSchedule:
    """Описание выпуска плюс `bondization` в график платежей.

    Номинал берётся исходный (`INITIALFACEVALUE`), текущий восстанавливается
    амортизациями: `face_value_at` обязан работать на любую дату истории, а не
    только на сегодня. Номинал не всегда 1000 и меняется во времени, при этом
    цена всегда в процентах от него (A-14).
    """
    fields = description(security_payload)

    secid = fields.get("SECID") or fields.get("ISIN")
    if not secid:
        raise ParseError("в описании выпуска нет SECID")

    currency = currency_of(fields.get("FACEUNIT"))
    maturity_date = parse_date(fields.get("MATDATE"))

    face = parse_decimal(fields.get("INITIALFACEVALUE")) or parse_decimal(fields.get("FACEVALUE"))
    if face is None:
        raise ParseError(f"{secid}: не указан номинал")

    coupons = tuple(
        coupon
        for row in rows(bondization_payload, "coupons")
        if (coupon := coupon_from_row(row, currency)) is not None
    )
    amortizations = [
        item
        for row in rows(bondization_payload, "amortizations")
        if (item := amortization_from_row(row, currency, maturity_date)) is not None
    ]
    offers = tuple(
        offer
        for row in rows(bondization_payload, "offers")
        if (offer := offer_from_row(row)) is not None
    )

    if not amortizations:
        # Выпуск без амортизации: `bondization` может не отдать финальное
        # погашение отдельной строкой, но погашение существует всегда.
        if maturity_date is None:
            raise ParseError(f"{secid}: нет ни амортизаций, ни даты погашения")
        amortizations = [
            Amortization(date=maturity_date, value=Money(face, currency), is_maturity=True)
        ]

    if maturity_date is None:
        maturity_date = max(item.date for item in amortizations)

    try:
        return BondSchedule(
            secid=secid,
            currency=currency,
            initial_face_value=Money(face, currency),
            maturity_date=maturity_date,
            coupons=coupons,
            amortizations=tuple(amortizations),
            offers=offers,
            name=fields.get("SHORTNAME") or fields.get("NAME"),
        )
    except ScheduleError as error:
        raise ParseError(f"{secid}: {error}") from error


def board_quotes(payload: JsonDict) -> list[dict[str, Any]]:
    """Строки доски: `securities` и `marketdata` склеены по SECID.

    Доска отдаёт доходность в нескольких полях с разным смыслом. Сверять свой
    расчёт можно только с тем полем, для которого известна использованная цена
    (`docs/PARALLEL-TRACK.md`, A3), поэтому оба блока нужны целиком.
    """
    static = {row.get("SECID"): row for row in rows(payload, "securities")}
    try:
        live = {row.get("SECID"): row for row in rows(payload, "marketdata")}
    except ParseError:
        live = {}

    merged: list[dict[str, Any]] = []
    for secid, row in static.items():
        combined = dict(row)
        extra = live.get(secid, {})
        combined.update({key: value for key, value in extra.items() if key != "SECID"})
        merged.append(combined)
    return merged
