"""Генератор синтетических снапшотов ISS для `tests/fixtures/iss/`.

Запускается руками и осознанно:

    python tests/make_iss_fixtures.py

**Эти снапшоты синтетические.** Они написаны по форме ответов ISS и покрывают
структурные случаи из эталонного набора (`docs/PARALLEL-TRACK.md`, A4):
фиксированный купон, амортизация, флоатер с неизвестными купонами, put-оферта,
валюта номинала, дисконтный выпуск.

Чего они **не** проверяют: конвенций Мосбиржи. Поля `ACCRUEDINT`,
`YIELDATPREVWAPRICE` и `DURATION` здесь посчитаны тем же кодом, что их потом
сверяет, поэтому совпадение проверяет проводку данных, а не правильность базы
начисления дней. Конвенции проверяются только против реальных ответов ISS и
калькулятора Мосбиржи (спека 5.6, `docs/BOND-REFERENCES.md`).

Замена синтетики реальными снапшотами — задача A1 трека; форма файлов при этом
не меняется, меняется только содержимое.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from portfolio.calc.bonds import (  # noqa: E402
    Amortization,
    BondSchedule,
    Coupon,
    Offer,
    OfferKind,
    accrued_interest,
    duration_days,
    face_value_at,
    full_price,
    future_cashflows,
    ytm,
    ytp,
)
from portfolio.calc.money import Money  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "iss"
# Форма каталога та же, что у боевого хранилища трека (`data/bondlab/`):
# фикстура — это снапшот, а не его пересказ.

# Дата снимка и дата расчётов T+1: доска отдаёт НКД на дату расчётов.
TRADE_DATE = date(2025, 9, 10)
SETTLEMENT = date(2025, 9, 11)

CENT = Decimal("0.01")


@dataclass
class Issue:
    """Описание синтетического выпуска в терминах, близких к ISS."""

    secid: str
    isin: str
    shortname: str
    board: str
    face: Decimal
    face_unit: str
    settle_currency: str
    issue_date: date
    maturity: date
    period_days: int
    rate_pct: Decimal | None
    known_until: date | None = None
    amortization: tuple[tuple[date, Decimal], ...] = ()
    offer: tuple[date, str, Decimal] | None = None
    price_pct: Decimal = Decimal("100")
    quote_to_offer: bool = False
    comment: str = ""
    coupons: list[dict[str, object]] = field(default_factory=list)


def build_coupons(issue: Issue) -> list[dict[str, object]]:
    """Купонные периоды от даты размещения до погашения.

    Купон начисляется на номинал, действующий в периоде: у амортизируемого
    выпуска он падает вместе с номиналом. Купон за горизонтом установления
    ставки — `None`, а не ноль (A-12, A-13).
    """
    if issue.rate_pct is None:
        return []

    periods: list[dict[str, object]] = []
    start = issue.issue_date
    while start < issue.maturity:
        end = min(start + timedelta(days=issue.period_days), issue.maturity)
        face = face_at(issue, start)
        known = issue.known_until is None or end <= issue.known_until
        value = (
            face * issue.rate_pct / Decimal(100) * Decimal((end - start).days) / Decimal(365)
        ).quantize(CENT, rounding=ROUND_HALF_UP)
        periods.append(
            {
                "startdate": start,
                "coupondate": end,
                "facevalue": face,
                "value": value if known else None,
                "valueprc": issue.rate_pct if known else None,
            }
        )
        start = end
    return periods


def face_at(issue: Issue, on: date) -> Decimal:
    repaid = sum((value for when, value in issue.amortization if when <= on), Decimal(0))
    return issue.face - repaid


def amortizations_of(issue: Issue) -> tuple[tuple[date, Decimal], ...]:
    """Амортизации плюс финальное погашение остатка номинала."""
    rows = list(issue.amortization)
    rows.append((issue.maturity, face_at(issue, issue.maturity - timedelta(days=1))))
    return tuple(sorted(rows))


def schedule_of(issue: Issue, *, extend_unknown: bool = False) -> BondSchedule:
    """График выпуска.

    `extend_unknown` продлевает неизвестные купоны последним известным — так
    доходность флоатера считает биржа. В нашем ядре такого режима нет и не
    будет: продлённый купон — это допущение о будущем, выданное за факт.
    """
    currency = "RUB" if issue.face_unit == "SUR" else issue.face_unit
    if extend_unknown:
        last_known = next(
            (row["value"] for row in reversed(issue.coupons) if row["value"] is not None), None
        )
        rows = [
            {**row, "value": row["value"] if row["value"] is not None else last_known}
            for row in issue.coupons
        ]
    else:
        rows = list(issue.coupons)
    coupons = tuple(
        Coupon(
            date=row["coupondate"],  # type: ignore[arg-type]
            period_start=row["startdate"],  # type: ignore[arg-type]
            value=Money(row["value"], currency) if row["value"] is not None else None,  # type: ignore[arg-type]
            rate_pct=row["valueprc"],  # type: ignore[arg-type]
        )
        for row in rows
    )
    amortizations = tuple(
        Amortization(date=when, value=Money(value, currency), is_maturity=when == issue.maturity)
        for when, value in amortizations_of(issue)
    )
    offers = ()
    if issue.offer is not None:
        when, kind, price = issue.offer
        offers = (
            Offer(
                date=when,
                kind=OfferKind.PUT if kind == "put" else OfferKind.CALL,
                price_pct=price,
            ),
        )
    return BondSchedule(
        secid=issue.secid,
        currency=currency,
        initial_face_value=Money(issue.face, currency),
        maturity_date=issue.maturity,
        coupons=coupons,
        amortizations=amortizations,
        offers=offers,
        name=issue.shortname,
    )


# -- выпуски эталонного набора (A4) ----------------------------------------


def issues() -> list[Issue]:
    return [
        Issue(
            secid="SU26238RMFS4",
            isin="RU000A1038V6",
            shortname="ОФЗ 26238",
            board="TQOB",
            face=Decimal(1000),
            face_unit="SUR",
            settle_currency="SUR",
            issue_date=date(2021, 9, 15),
            maturity=date(2041, 5, 15),
            period_days=182,
            rate_pct=Decimal("7.10"),
            price_pct=Decimal("54.321"),
            comment="ОФЗ с фиксированным купоном, без оферты: базовый случай",
        ),
        Issue(
            secid="SU46020RMFS2",
            isin="RU000A0GN9A7",
            shortname="ОФЗ 46020",
            board="TQOB",
            face=Decimal(1000),
            face_unit="SUR",
            settle_currency="SUR",
            issue_date=date(2021, 2, 10),
            maturity=date(2036, 2, 6),
            period_days=182,
            rate_pct=Decimal("6.90"),
            amortization=((date(2026, 2, 11), Decimal(200)), (date(2031, 2, 12), Decimal(300))),
            price_pct=Decimal("61.5"),
            comment="ОФЗ с амортизацией: номинал меняется во времени",
        ),
        Issue(
            secid="SU29014RMFS6",
            isin="RU000A101GD3",
            shortname="ОФЗ 29014",
            board="TQOB",
            face=Decimal(1000),
            face_unit="SUR",
            settle_currency="SUR",
            issue_date=date(2022, 3, 16),
            maturity=date(2026, 3, 25),
            period_days=91,
            rate_pct=Decimal("16.40"),
            known_until=date(2025, 12, 10),
            price_pct=Decimal("99.7"),
            comment="ОФЗ-флоатер RUONIA: купоны за горизонтом установления неизвестны",
        ),
        Issue(
            secid="RU000A105SD9",
            isin="RU000A105SD9",
            shortname="Корп-Пут 01",
            board="TQCB",
            face=Decimal(1000),
            face_unit="SUR",
            settle_currency="SUR",
            issue_date=date(2023, 1, 20),
            maturity=date(2033, 1, 14),
            period_days=91,
            rate_pct=Decimal("12.50"),
            known_until=date(2026, 4, 17),
            offer=(date(2026, 4, 17), "put", Decimal(100)),
            price_pct=Decimal("98.4"),
            quote_to_offer=True,
            comment="корпоративная с put-офертой: YTP вместо YTM",
        ),
        Issue(
            secid="RU000A106XY7",
            isin="RU000A106XY7",
            shortname="Корп-Амо 02",
            board="TQCB",
            face=Decimal(1000),
            face_unit="SUR",
            settle_currency="SUR",
            issue_date=date(2023, 8, 25),
            maturity=date(2028, 8, 18),
            period_days=91,
            rate_pct=Decimal("13.75"),
            known_until=date(2026, 8, 21),
            amortization=((date(2026, 8, 21), Decimal(250)), (date(2027, 8, 20), Decimal(250))),
            offer=(date(2026, 8, 21), "put", Decimal(100)),
            price_pct=Decimal("97.05"),
            quote_to_offer=True,
            comment="корпоративная с амортизацией и офертой: комбинация",
        ),
        Issue(
            secid="RU000A107LS1",
            isin="RU000A107LS1",
            shortname="Флоатер КС 03",
            board="TQCB",
            face=Decimal(1000),
            face_unit="SUR",
            settle_currency="SUR",
            issue_date=date(2024, 1, 12),
            maturity=date(2029, 1, 5),
            period_days=30,
            rate_pct=Decimal("18.00"),
            known_until=date(2025, 10, 9),
            price_pct=Decimal("100.85"),
            comment="флоатер на ключевую ставку: другая база расчёта купона",
        ),
        Issue(
            secid="RU000A105RH2",
            isin="RU000A105RH2",
            shortname="Замещ USD 04",
            board="TQCB",
            face=Decimal(1000),
            face_unit="USD",
            settle_currency="SUR",
            issue_date=date(2023, 1, 13),
            maturity=date(2030, 1, 11),
            period_days=182,
            rate_pct=Decimal("5.50"),
            price_pct=Decimal("92.3"),
            comment="замещающая: валюта номинала USD, расчёты в рублях",
        ),
        Issue(
            secid="RU000A105NH9",
            isin="RU000A105NH9",
            shortname="Юань 05",
            board="TQCB",
            face=Decimal(1000),
            face_unit="CNY",
            settle_currency="CNY",
            issue_date=date(2022, 12, 9),
            maturity=date(2027, 12, 3),
            period_days=182,
            rate_pct=Decimal("3.95"),
            price_pct=Decimal("95.6"),
            comment="юаневая: третья валюта номинала",
        ),
        Issue(
            secid="RU000A106H62",
            isin="RU000A106H62",
            shortname="Дисконт 06",
            board="TQCB",
            face=Decimal(1000),
            face_unit="SUR",
            settle_currency="SUR",
            issue_date=date(2023, 6, 30),
            maturity=date(2026, 6, 30),
            period_days=0,
            rate_pct=None,
            price_pct=Decimal("88.2"),
            comment="дисконтный выпуск: купонов нет вообще",
        ),
    ]


# -- сериализация в форму ISS ----------------------------------------------


def _block(columns: list[str], data: list[list[object]]) -> dict[str, object]:
    return {"columns": columns, "data": data}


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def security_payload(issue: Issue) -> dict[str, object]:
    frequency = str(round(365 / issue.period_days)) if issue.period_days else "0"
    fields: list[tuple[str, str, object]] = [
        ("SECID", "Код инструмента", issue.secid),
        ("NAME", "Полное наименование", f"{issue.shortname} (синтетическая фикстура)"),
        ("SHORTNAME", "Краткое наименование", issue.shortname),
        ("ISIN", "ISIN код", issue.isin),
        ("ISSUEDATE", "Дата начала торгов", issue.issue_date.isoformat()),
        ("MATDATE", "Дата погашения", issue.maturity.isoformat()),
        ("INITIALFACEVALUE", "Первоначальная номинальная стоимость", str(issue.face)),
        ("FACEVALUE", "Номинальная стоимость", str(face_at(issue, TRADE_DATE))),
        ("FACEUNIT", "Валюта номинала", issue.face_unit),
        ("COUPONFREQUENCY", "Периодичность выплаты купона в год", frequency),
        ("TYPE", "Тип бумаги", "ofz_bond" if issue.board == "TQOB" else "corporate_bond"),
        ("GROUP", "Группа инструментов", "stock_bonds"),
        ("ISQUALIFIEDINVESTORS", "Бумага для квалифицированных инвесторов", "0"),
    ]
    description = _block(
        ["name", "title", "value", "type", "sort_order", "is_hidden", "precision"],
        [
            [name, title, value, "string", index + 1, 0, None]
            for index, (name, title, value) in enumerate(fields)
        ],
    )
    boards = _block(
        ["secid", "boardid", "title", "is_primary", "currencyid", "market", "engine"],
        [[issue.secid, issue.board, "Т+: Облигации", 1, issue.settle_currency, "bonds", "stock"]],
    )
    return {"description": description, "boards": boards}


def bondization_payload(issue: Issue) -> dict[str, object]:
    coupon_columns = [
        "isin", "name", "issuevalue", "coupondate", "recorddate", "startdate",
        "initialfacevalue", "facevalue", "faceunit", "value", "valueprc", "value_rub",
    ]
    coupon_rows: list[list[object]] = []
    for row in issue.coupons:
        payment: date = row["coupondate"]  # type: ignore[assignment]
        start: date = row["startdate"]  # type: ignore[assignment]
        value: Decimal | None = row["value"]  # type: ignore[assignment]
        rate: Decimal | None = row["valueprc"]  # type: ignore[assignment]
        coupon_rows.append(
            [
                issue.isin,
                issue.shortname,
                None,
                payment.isoformat(),
                (payment - timedelta(days=1)).isoformat(),
                start.isoformat(),
                _number(issue.face),
                _number(row["facevalue"]),  # type: ignore[arg-type]
                issue.face_unit,
                _number(value),
                _number(rate),
                _number(value),
            ]
        )

    amortization_columns = [
        "isin", "name", "issuevalue", "amortdate", "facevalue", "initialfacevalue",
        "faceunit", "valueprc", "value", "value_rub", "data_source",
    ]
    amortization_rows = [
        [
            issue.isin,
            issue.shortname,
            None,
            when.isoformat(),
            _number(face_at(issue, when)),
            _number(issue.face),
            issue.face_unit,
            _number((value / issue.face * Decimal(100)).quantize(CENT)),
            _number(value),
            _number(value),
            "amortization",
        ]
        for when, value in amortizations_of(issue)
    ]

    offer_columns = [
        "isin", "name", "issuevalue", "offerdate", "offerdatestart", "offerdateend",
        "price", "value", "agent", "fixdate", "faceunit", "offertype",
    ]
    offer_rows: list[list[object]] = []
    if issue.offer is not None:
        when, kind, price = issue.offer
        offer_rows.append(
            [
                issue.isin,
                issue.shortname,
                None,
                when.isoformat(),
                (when - timedelta(days=14)).isoformat(),
                (when - timedelta(days=7)).isoformat(),
                _number(price),
                None,
                "Синтетический агент",
                (when - timedelta(days=3)).isoformat(),
                issue.face_unit,
                "Оферта" if kind == "put" else "Досрочное погашение",
            ]
        )

    return {
        "coupons": _block(coupon_columns, coupon_rows),
        "amortizations": _block(amortization_columns, amortization_rows),
        "offers": _block(offer_columns, offer_rows),
    }


def board_payload(board: str, board_issues: list[Issue]) -> dict[str, object]:
    """Доска: `securities` и `marketdata`, как отдаёт ISS.

    `ACCRUEDINT`, `YIELDATPREVWAPRICE` и `DURATION` посчитаны тем же ядром —
    см. предупреждение в заголовке модуля.
    """
    securities_columns = [
        "SECID", "BOARDID", "SHORTNAME", "PREVWAPRICE", "YIELDATPREVWAPRICE", "COUPONVALUE",
        "NEXTCOUPON", "ACCRUEDINT", "PREVPRICE", "LOTSIZE", "FACEVALUE", "STATUS", "MATDATE",
        "COUPONPERIOD", "FACEUNIT", "CURRENCYID", "OFFERDATE",
    ]
    marketdata_columns = [
        "SECID", "BOARDID", "LAST", "YIELD", "DURATION", "WAPRICE", "YIELDATWAPRICE",
        "NUMTRADES", "VOLTODAY",
    ]

    securities_rows: list[list[object]] = []
    marketdata_rows: list[list[object]] = []

    for issue in board_issues:
        schedule = schedule_of(issue)
        accrued = accrued_interest(schedule, SETTLEMENT)
        face = face_value_at(schedule, SETTLEMENT)
        price = full_price(issue.price_pct, face, accrued)
        flows = future_cashflows(schedule, SETTLEMENT)

        offer = next(iter(schedule.offers_after(SETTLEMENT)), None)
        if issue.quote_to_offer and offer is not None and price is not None:
            # Биржа по бумаге с офертой печатает доходность к оферте.
            own = ytp(schedule, price, SETTLEMENT, offer)
            flows = tuple(future_cashflows(schedule, SETTLEMENT, until=offer.date))
        elif issue.known_until is not None and price is not None:
            # По флоатеру биржа доходность печатает: неизвестные купоны она
            # продлевает последним известным. Наше правило другое (A-12), и
            # расхождение здесь ожидаемое, а не дефект.
            naive = schedule_of(issue, extend_unknown=True)
            flows = future_cashflows(naive, SETTLEMENT)
            own = ytm(flows, price, SETTLEMENT)
        else:
            own = None if price is None else ytm(flows, price, SETTLEMENT)

        horizon = duration_days(flows, SETTLEMENT, own) if own is not None else None
        next_coupon = next((c for c in schedule.coupons if c.date > SETTLEMENT), None)

        securities_rows.append(
            [
                issue.secid,
                issue.board,
                issue.shortname,
                _number(issue.price_pct),
                _number(None if own is None else (own * Decimal(100)).quantize(CENT)),
                _number(next_coupon.value.amount if next_coupon and next_coupon.value else None),
                next_coupon.date.isoformat() if next_coupon else None,
                _number(None if accrued is None else accrued.amount),
                _number(issue.price_pct),
                1,
                _number(face.amount),
                "A",
                issue.maturity.isoformat(),
                issue.period_days or None,
                issue.face_unit,
                issue.settle_currency,
                issue.offer[0].isoformat() if issue.offer else None,
            ]
        )
        marketdata_rows.append(
            [
                issue.secid,
                issue.board,
                _number(issue.price_pct),
                _number(None if own is None else (own * Decimal(100)).quantize(CENT)),
                horizon,
                _number(issue.price_pct),
                _number(None if own is None else (own * Decimal(100)).quantize(CENT)),
                42,
                1_000_000,
            ]
        )

    return {
        "securities": _block(securities_columns, securities_rows),
        "marketdata": _block(marketdata_columns, marketdata_rows),
    }


def boards_payload(board_codes: list[str]) -> dict[str, object]:
    titles = {"TQOB": "Т+: Гособлигации - безадрес.", "TQCB": "Т+: Облигации - безадрес."}
    return {
        "boards": _block(
            [
                "id", "board_group_id", "engine", "market",
                "boardid", "boardtitle", "title", "is_traded",
            ],
            [
                [index + 1, 58, "stock", "bonds", code, titles[code], titles[code], 1]
                for index, code in enumerate(board_codes)
            ],
        )
    }


def write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    catalogue = issues()
    for issue in catalogue:
        issue.coupons = build_coupons(issue)

    day = FIXTURES / "snapshots" / TRADE_DATE.isoformat()
    for issue in catalogue:
        write(day / "securities" / f"{issue.secid}.json", security_payload(issue))
        write(day / "bondization" / f"{issue.secid}.json", bondization_payload(issue))

    board_codes = sorted({issue.board for issue in catalogue})
    write(day / "boards" / "boards.json", boards_payload(board_codes))
    for board in board_codes:
        members = [issue for issue in catalogue if issue.board == board]
        write(day / "board-securities" / f"{board}.json", board_payload(board, members))

    print(f"снапшоты записаны в {day}")
    for issue in catalogue:
        print(f"  {issue.secid:<14} {issue.comment}")


if __name__ == "__main__":
    main()
