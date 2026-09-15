"""Стадия 2 парсера: таблицы → события. Версия v1.

Версионируется: при смене формата отчёта рядом появляется `mapping_v2.py`, а v1
остаётся рабочей для старых файлов (спека 4.1). Номер версии пишется в
`raw_reports.parser_version`.

Функция `parse_report` не пишет в БД и не сохраняет файл — этим занимается
`jobs/import_broker.py`. Поэтому golden-тесты работают без БД и без сети.

Предметные допущения, закреплённые здесь (см. `ASSUMPTIONS.md`):

* A-05 — знаки: `quantity` как изменение позиции, `amount` как изменение остатка;
* A-06 — комиссии отдельными событиями `FEE`, поле `fee` у сделки справочное;
* A-07 — денежный эффект сделки = сумма сделки ± НКД, комиссии не входят;
* A-04 — если gross купона не восстанавливается, `withheld_at_source = None`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal

from portfolio.adapters.broker.anchors import (
    Section,
    find_sections,
    is_total_row,
    match_direction,
    match_operation_kind,
    normalize_signature,
    resolve_columns,
)
from portfolio.adapters.broker.dto import (
    ParsedBalance,
    ParsedOperation,
    ParsedReport,
    UnparsedRow,
)
from portfolio.adapters.broker.tables import RawTable, decode_report, extract_tables
from portfolio.adapters.formats import (
    FormatError,
    is_blank,
    parse_currency,
    parse_date,
    parse_decimal,
    parse_optional_date,
    parse_optional_decimal,
)

__all__ = ["MAPPING_VERSION", "parse", "parse_report"]

MAPPING_VERSION = "v1"

# Валюта отчёта, когда колонки валюты в таблице нет (допущение A-08).
DEFAULT_CURRENCY = "RUB"

_FEE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("fee_broker", "BROKER"),
    ("fee_depositary", "DEPOSITARY"),
    ("fee_exchange", "EXCHANGE"),
)

_PERIOD_RE = re.compile(
    r"(?:за\s+период\s+)?с\s+(\d{2}\.\d{2}\.\d{4})\s+(?:по|-|—)\s+(\d{2}\.\d{2}\.\d{4})",
    re.IGNORECASE,
)
_ACCOUNT_RE = re.compile(
    r"(?:договор|соглашение|счет|счёт|код\s+клиента)[^\dA-Za-zА-Яа-я]{0,20}"
    r"(?:№|n|no)?\s*([\w\-/]{3,32})",
    re.IGNORECASE,
)

# Формулировки, по которым видно, что сумма уже за вычетом налога.
_NET_MARKERS = ("за вычетом", "после удержания", "нетто", "net", "минус налог")
_GROSS_MARKERS = ("до удержания", "брутто", "gross", "до налога")


def parse_report(
    content: bytes,
) -> tuple[list[ParsedOperation], list[ParsedBalance], list[UnparsedRow]]:
    """Контракт парсера (STAGE-1, T6): `bytes` → (операции, остатки, нераспознанное)."""
    report = parse(content)
    return list(report.operations), list(report.balances), list(report.unparsed)


def parse(content: bytes, tables: list[RawTable] | None = None) -> ParsedReport:
    """То же, плюс реквизиты отчёта: период и код счёта.

    Готовые таблицы можно передать снаружи: диспетчер версий уже разобрал
    документ, чтобы выбрать версию, и второй разбор той же выгрузки — впустую.
    """
    tables = extract_tables(content) if tables is None else tables
    sections = find_sections(tables)

    operations: list[ParsedOperation] = []
    balances: list[ParsedBalance] = []
    unparsed: list[UnparsedRow] = []

    for table in sections.get(Section.TRADES, []):
        _collect(_parse_trades(table), operations, unparsed)
    for table in sections.get(Section.CASH_FLOW, []):
        _collect(_parse_cash_flow(table), operations, unparsed)

    for table in sections.get(Section.CASH_BALANCES, []):
        _collect_balances(_parse_cash_balances(table), balances, unparsed)
    for table in sections.get(Section.SECURITY_BALANCES, []):
        _collect_balances(_parse_security_balances(table), balances, unparsed)

    unparsed.extend(_unknown_tables(tables, sections))

    period_start, period_end = _extract_period(content, tables)
    if period_end is not None:
        balances = [
            balance if balance.as_of is not None else replace(balance, as_of=period_end)
            for balance in balances
        ]

    return ParsedReport(
        operations=tuple(operations),
        balances=tuple(balances),
        unparsed=tuple(unparsed),
        mapping_version=MAPPING_VERSION,
        period_start=period_start,
        period_end=period_end,
        account_code=_extract_account(content),
    )


# -- сделки -----------------------------------------------------------------


def _parse_trades(table: RawTable) -> Iterator[ParsedOperation | UnparsedRow]:
    columns = resolve_columns(table)
    label = _table_label(table, "сделки")

    for row in table.as_dicts():
        if is_total_row(row):
            continue
        try:
            yield from _trade_row(row, columns, label)
        except (FormatError, ValueError) as error:
            yield UnparsedRow(table=label, row=row, reason=str(error))


def _trade_row(
    row: dict[str, str], columns: dict[str, str], label: str
) -> Iterator[ParsedOperation | UnparsedRow]:
    trade_date = parse_date(_require(row, columns, "trade_date"))
    settlement_date = parse_optional_date(_value(row, columns, "settlement_date")) or trade_date

    quantity = parse_decimal(_require(row, columns, "quantity"))
    price = parse_optional_decimal(_value(row, columns, "price"))
    accrued = parse_optional_decimal(_value(row, columns, "accrued_int"))
    currency = _currency(row, columns)

    direction = match_direction(_value(row, columns, "direction") or "")
    if direction is None:
        # Часть отчётов обходится без колонки вида сделки: знак количества или
        # суммы несёт направление. Если не несёт — строка уходит в нераспознанные.
        direction = _direction_from_signs(quantity, _value(row, columns, "amount"))
    if direction is None:
        raise ValueError("вид сделки не опознан и не выводится из знаков")

    gross = _trade_gross(row, columns, quantity, price, accrued)
    sign = Decimal(-1) if direction == "BUY" else Decimal(1)
    fees = list(_fee_operations(row, columns, trade_date, settlement_date, currency, label))

    yield ParsedOperation(
        kind=direction,
        trade_date=trade_date,
        settlement_date=settlement_date,
        ticker=_ticker(row, columns),
        isin=_isin(row, columns),
        instrument_name=_value(row, columns, "instrument_name") or None,
        quantity=abs(quantity) * (Decimal(1) if direction == "BUY" else Decimal(-1)),
        price=price,
        accrued_int=accrued,
        # A-06: поле `fee` справочное, в денежный остаток не входит —
        # его несут отдельные события FEE.
        fee=sum((abs(operation.amount) for operation in fees), Decimal(0)) or None,
        amount=sign * gross,
        currency=currency,
        broker_trade_no=_value(row, columns, "broker_trade_no") or None,
        raw_row=dict(row),
    )
    yield from fees


def _trade_gross(
    row: dict[str, str],
    columns: dict[str, str],
    quantity: Decimal,
    price: Decimal | None,
    accrued: Decimal | None,
) -> Decimal:
    """Сумма сделки без комиссий, по модулю (A-07).

    Предпочитается сумма из отчёта: это авторитетное число брокера, включающее
    его собственное округление. Расчёт `количество × цена` — только если
    колонки суммы нет.
    """
    explicit = parse_optional_decimal(_value(row, columns, "amount"))
    if explicit is not None:
        gross = abs(explicit)
        # НКД прибавляется, только если он не входит в сумму сделки. Признак —
        # отдельная колонка НКД: если она есть, брокер показывает сумму без него.
        if accrued is not None and "accrued_int" in columns:
            gross += abs(accrued)
        return gross

    if price is None:
        raise ValueError("нет ни суммы сделки, ни цены — сумма не восстанавливается")
    gross = abs(quantity) * price
    if accrued is not None:
        gross += abs(accrued)
    return gross


def _direction_from_signs(quantity: Decimal, amount_cell: str | None) -> str | None:
    if quantity > 0 and amount_cell and not is_blank(amount_cell):
        try:
            amount = parse_decimal(amount_cell)
        except FormatError:
            return None
        if amount < 0:
            return "BUY"
        if amount > 0:
            return "SELL"
        return None
    if quantity < 0:
        return "SELL"
    return None


def _fee_operations(
    row: dict[str, str],
    columns: dict[str, str],
    trade_date: date,
    settlement_date: date | None,
    currency: str,
    label: str,
) -> Iterator[ParsedOperation]:
    """Комиссии сделки — отдельными событиями по типам (спека 4.1, A-06)."""
    for column, fee_kind in _FEE_COLUMNS:
        value = parse_optional_decimal(_value(row, columns, column))
        if value is None or value == 0:
            continue
        yield ParsedOperation(
            kind="FEE",
            trade_date=trade_date,
            settlement_date=settlement_date,
            ticker=_ticker(row, columns),
            isin=_isin(row, columns),
            quantity=None,
            price=None,
            amount=-abs(value),
            currency=currency,
            fee_kind=fee_kind,
            broker_trade_no=_value(row, columns, "broker_trade_no") or None,
            note=label,
            raw_row=dict(row),
        )


# -- движение денежных средств ----------------------------------------------


def _parse_cash_flow(table: RawTable) -> Iterator[ParsedOperation | UnparsedRow]:
    columns = resolve_columns(table)
    label = _table_label(table, "движение денежных средств")

    for row in table.as_dicts():
        if is_total_row(row):
            continue
        try:
            yield from _cash_flow_row(row, columns, label)
        except (FormatError, ValueError) as error:
            yield UnparsedRow(table=label, row=row, reason=str(error))


def _cash_flow_row(
    row: dict[str, str], columns: dict[str, str], label: str
) -> Iterator[ParsedOperation | UnparsedRow]:
    description = _value(row, columns, "description") or ""
    matched = match_operation_kind(description)
    if matched is None:
        yield UnparsedRow(
            table=label,
            row=row,
            reason=f"тип операции не опознан: {description!r}",
        )
        return

    kind, fee_kind = matched
    operation_date = parse_date(_require(row, columns, "trade_date"))
    settlement_date = parse_optional_date(_value(row, columns, "settlement_date")) or operation_date
    currency = _currency(row, columns)
    amount = _cash_amount(row, columns, kind)

    tax = parse_optional_decimal(_value(row, columns, "tax"))
    withheld = _withheld_flag(description, kind, tax)

    if kind in {"COUPON", "DIVIDEND"} and tax is not None and tax != 0:
        # Расщепление: в журнал сумма до налога, удержание — отдельным событием.
        amount = amount + abs(tax)
        withheld = False

    yield ParsedOperation(
        kind=kind,
        trade_date=operation_date,
        settlement_date=settlement_date,
        ticker=_ticker(row, columns),
        isin=_isin(row, columns),
        instrument_name=_value(row, columns, "instrument_name") or None,
        quantity=None,
        price=None,
        amount=amount,
        currency=currency,
        fee_kind=fee_kind,
        withheld_at_source=withheld,
        note=description or None,
        raw_row=dict(row),
    )

    if kind in {"COUPON", "DIVIDEND"} and tax is not None and tax != 0:
        yield ParsedOperation(
            kind="TAX",
            trade_date=operation_date,
            settlement_date=settlement_date,
            ticker=_ticker(row, columns),
            isin=_isin(row, columns),
            quantity=None,
            price=None,
            amount=-abs(tax),
            currency=currency,
            withheld_at_source=True,
            note=description or None,
            raw_row=dict(row),
        )


def _cash_amount(row: dict[str, str], columns: dict[str, str], kind: str) -> Decimal:
    """Денежный эффект операции со знаком.

    Колонки «зачисление» и «списание» разнесены по разным полям у большинства
    брокеров; если есть единая сумма, знак берётся из типа операции.
    """
    credit = parse_optional_decimal(_value(row, columns, "credit"))
    debit = parse_optional_decimal(_value(row, columns, "debit"))
    if credit is not None or debit is not None:
        return (credit or Decimal(0)) - abs(debit or Decimal(0))

    raw = parse_optional_decimal(_value(row, columns, "amount"))
    if raw is None:
        raise ValueError("нет суммы операции")
    if raw < 0:
        return raw
    return -raw if kind in {"FEE", "TAX", "CASH_OUT"} else raw


def _withheld_flag(description: str, kind: str, tax: Decimal | None) -> bool | None:
    """Признак удержания налога у источника.

    Если из отчёта не видно, остаётся `None` — «неизвестно». Восстанавливать
    gross делением на (1 − ставка) запрещено (спека 3.2).
    """
    if kind not in {"COUPON", "DIVIDEND"}:
        return None
    if tax is not None:
        return tax != 0
    signature = normalize_signature(description)
    if any(marker in signature for marker in _NET_MARKERS):
        return True
    if any(marker in signature for marker in _GROSS_MARKERS):
        return False
    return None


# -- остатки ----------------------------------------------------------------


def _parse_cash_balances(table: RawTable) -> Iterator[ParsedBalance | UnparsedRow]:
    columns = resolve_columns(table)
    label = _table_label(table, "остатки денежных средств")

    for row in table.as_dicts():
        if is_total_row(row):
            continue
        try:
            balance = _require(row, columns, "closing_balance")
            yield ParsedBalance(
                kind="cash",
                ticker=None,
                quantity=parse_decimal(balance),
                currency=_currency(row, columns),
                raw_row=dict(row),
            )
        except (FormatError, ValueError) as error:
            yield UnparsedRow(table=label, row=row, reason=str(error))


def _parse_security_balances(table: RawTable) -> Iterator[ParsedBalance | UnparsedRow]:
    columns = resolve_columns(table)
    label = _table_label(table, "остатки по ценным бумагам")

    for row in table.as_dicts():
        if is_total_row(row):
            continue
        try:
            ticker = _ticker(row, columns)
            isin = _isin(row, columns)
            if ticker is None and isin is None:
                raise ValueError("в строке остатка нет ни тикера, ни ISIN")
            yield ParsedBalance(
                kind="security",
                ticker=ticker,
                isin=isin,
                quantity=parse_decimal(_require(row, columns, "closing_balance")),
                currency=_currency(row, columns),
                raw_row=dict(row),
            )
        except (FormatError, ValueError) as error:
            yield UnparsedRow(table=label, row=row, reason=str(error))


# -- прочее -----------------------------------------------------------------


def _unknown_tables(
    tables: list[RawTable], sections: dict[Section, list[RawTable]]
) -> list[UnparsedRow]:
    """Таблицы с данными, не попавшие ни в одну секцию.

    Верстальные обёртки и текстовые шапки пропускаются: в них нет чисел. Всё
    остальное обязано попасть во «Входящие» — молча потерянная таблица как раз и
    есть та ошибка, от которой защищает сверка.
    """
    known = {id(table) for group in sections.values() for table in group}
    result: list[UnparsedRow] = []

    for table in tables:
        if id(table) in known or table.is_empty or not _looks_like_data(table):
            continue
        label = _table_label(table, f"таблица #{table.index}")
        for row in table.as_dicts():
            if is_total_row(row):
                continue
            result.append(
                UnparsedRow(table=label, row=row, reason="секция отчёта не опознана")
            )
    return result


def _looks_like_data(table: RawTable) -> bool:
    if table.width < 2:
        return False
    for row in table.rows:
        numeric = sum(1 for cell in row if _is_number(cell))
        if numeric and len([cell for cell in row if cell]) >= 2:
            return True
    return False


def _is_number(cell: str) -> bool:
    try:
        parse_decimal(cell)
    except FormatError:
        return False
    return True


def _extract_period(content: bytes, tables: list[RawTable]) -> tuple[date | None, date | None]:
    for text in _text_candidates(content, tables):
        match = _PERIOD_RE.search(text)
        if match:
            return parse_date(match.group(1)), parse_date(match.group(2))
    return None, None


def _extract_account(content: bytes) -> str | None:
    match = _ACCOUNT_RE.search(decode_report(content))
    return match.group(1) if match else None


def _text_candidates(content: bytes, tables: list[RawTable]) -> Iterator[str]:
    for table in tables:
        yield from table.preceding_text
        yield table.caption
        for row in table.rows:
            yield " ".join(row)
    yield decode_report(content)


def _table_label(table: RawTable, default: str) -> str:
    for candidate in (table.caption, *reversed(table.preceding_text)):
        if candidate:
            return candidate
    return default


def _value(row: dict[str, str], columns: dict[str, str], logical: str) -> str | None:
    physical = columns.get(logical)
    if physical is None:
        return None
    cell = row.get(physical)
    return None if cell is None or is_blank(cell) else cell


def _require(row: dict[str, str], columns: dict[str, str], logical: str) -> str:
    value = _value(row, columns, logical)
    if value is None:
        raise ValueError(f"не заполнена обязательная колонка {logical!r}")
    return value


def _currency(row: dict[str, str], columns: dict[str, str]) -> str:
    raw = _value(row, columns, "currency")
    return parse_currency(raw) if raw else DEFAULT_CURRENCY


def _ticker(row: dict[str, str], columns: dict[str, str]) -> str | None:
    value = _value(row, columns, "ticker")
    return value.strip() if value else None


def _isin(row: dict[str, str], columns: dict[str, str]) -> str | None:
    value = _value(row, columns, "isin")
    if not value:
        return None
    candidate = value.strip().upper()
    return candidate if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", candidate) else None


def _collect(
    items: Iterator[ParsedOperation | UnparsedRow],
    operations: list[ParsedOperation],
    unparsed: list[UnparsedRow],
) -> None:
    for item in items:
        if isinstance(item, UnparsedRow):
            unparsed.append(item)
        else:
            operations.append(item)


def _collect_balances(
    items: Iterator[ParsedBalance | UnparsedRow],
    balances: list[ParsedBalance],
    unparsed: list[UnparsedRow],
) -> None:
    for item in items:
        if isinstance(item, UnparsedRow):
            unparsed.append(item)
        else:
            balances.append(item)
