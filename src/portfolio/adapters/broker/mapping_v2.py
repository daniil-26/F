"""Стадия 2 парсера: выгрузка Excel → события. Версия v2.

Формат реального архива, в отличие от v1: **весь отчёт — одна таблица**, секции
пронумерованы («5.1 Биржевые сделки…»), инструмент сделки стоит не колонкой, а
строкой-подзаголовком над группой её сделок, а шапки двухъярусные («Дата
оплаты» → «Плановая | Фактическая»). Нарезка на секции — в `sections.py`.

Что здесь решено предметно (см. `ASSUMPTIONS.md`):

* A-22 — сделка опознаётся по номеру брокера, поэтому повтор одной и той же
  сделки в разделах 5.1 и 5.10 схлопывается ключом идемпотентности;
* A-23 — бумага сделки восстанавливается по справочнику из раздела 2: в
  подзаголовке группы есть наименование и номер гос. регистрации, но нет ISIN;
* A-21 — заём бумаг брокером не меняет позицию, но двигает деньги: в разделе
  5.4 вознаграждение стоит в колонке «% по сделке» строки возврата, и это сумма
  в рублях, а не ставка. Раздел 5.9 (незавершённые сделки) пропускается: расчётов
  по ним ещё не было, деньги придут в 5.4 следующего отчёта (A-24);
* A-25 — контрольный денежный остаток берётся из строки «Исходящий остаток
  (всего)», а не «плановый»: плановый включает неисполненные обязательства.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from portfolio.adapters.broker.anchors import (
    COLUMNS_V2,
    is_known_column,
    is_total_row,
    match_direction,
    normalize_signature,
    resolve_columns,
)
from portfolio.adapters.broker.dto import (
    ParsedBalance,
    ParsedOperation,
    ParsedReport,
    UnparsedRow,
)
from portfolio.adapters.broker.sections import ReportSection, split_sections
from portfolio.adapters.broker.tables import RawTable, extract_tables
from portfolio.adapters.formats import (
    FormatError,
    is_blank,
    parse_currency,
    parse_date,
    parse_decimal,
    parse_optional_date,
    parse_optional_decimal,
)

__all__ = ["MAPPING_VERSION", "looks_like_v2", "parse", "parse_report"]

MAPPING_VERSION = "v2"

DEFAULT_CURRENCY = "RUB"

# Разделы и их роль. Номер устойчивее подписи: «5. Сделки с ценными бумагами» и
# «5. Сделки с ценными бумагами и валютными инструментами» — один и тот же
# раздел в отчётах разных лет.
CASH_STATE_SECTIONS = ("1", "1.1")
SECURITY_BALANCE_SECTIONS = ("2",)
TRADE_SECTIONS = ("5.1", "5.10")
CASH_OPERATION_SECTIONS = ("8.1", "8.1.1")
LOAN_SECTIONS = ("5.4",)
SECURITY_OPERATION_SECTIONS = ("8.2",)

# Разделы, которые пропускаются осознанно, с причиной. Молча пропущенный раздел —
# это потерянные операции, поэтому список явный и проверяется тестом.
SKIPPED_SECTIONS: dict[str, str] = {
    "4": "оценка активов — производные числа, а не операции",
    "5": "заголовок группы разделов, строк не содержит",
    "5.9": "незавершённые сделки займа: расчётов ещё не было, деньги придут в 5.4 (A-24)",
    "5.11": "заём ценных бумаг: не меняет ни позицию, ни остаток (A-24)",
    "8": "заголовок группы разделов, строк не содержит",
}

# Итоговые строки внутри группы сделок. «Итого» ловится общим правилом, а
# «изменение» — нет: это вторая строка итога по выпуску, без подписи.
SUMMARY_MARKERS = ("оборот", "изменение", "итого", "общий итог", "всего")

# «Тип операции» раздела 8.1 → тип события журнала.
CASH_OPERATION_TYPES: dict[str, str] = {
    "ввод дс": "CASH_IN",
    "вывод дс": "CASH_OUT",
    "погашение купона": "COUPON",
    "погашение номинала": "MATURITY",
    "налог": "TAX",
}

# «Тип операции» раздела 8.2 → (тип события, знак количества). Знак `0` значит
# «взять из отчёта»: у конвертации списание и зачисление идут двумя строками, и
# минус в количестве — единственное, что их различает.
SECURITY_OPERATION_TYPES: dict[str, tuple[str, int]] = {
    "ввод цб": ("TRANSFER", 1),
    "вывод цб": ("TRANSFER", -1),
    "погашение облигации": ("MATURITY", -1),
    "конвертация цб": ("CONVERSION", 0),
}

# Виды сделок раздела 5.4. Позицию заём не меняет: бумаги остаются в
# собственности (A-21). Деньги приносит только возврат — вознаграждение стоит в
# колонке «% по сделке» его строки, у выдачи она пуста.
LOAN_RETURN = "возврат займа ценных бумаг"
LOAN_ISSUE = "выдача займа ценных бумаг"

# Строка-маркер: погашение облигации в денежном разделе идёт с нулевой суммой,
# деньги приходят отдельной строкой «Погашение номинала» (подтверждено владельцем
# архива). Нулевую строку нужно именно пропустить, а не записать нулевым событием.
ZERO_MARKER_TYPES = ("погашение облигации",)

# Доход по финансовым инструментам — это дивиденд; вид виден только в комментарии.
_DIVIDEND_MARKERS = ("выплата дивидендов", "дивиденд")
_INCOME_TYPE = "доход по финансовым инструментам"

_ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b")
_PERIOD_RE = re.compile(
    r"за\s+период\s+с\s+(\d{2}\.\d{2}\.\d{4})\s+(?:по|-|—)\s+(\d{2}\.\d{2}\.\d{4})",
    re.IGNORECASE,
)
_ACCOUNT_LABELS = ("номер счета клиента", "номер счёта клиента", "номер счета")
_REPORT_CURRENCY_LABELS = ("валюта отчета", "валюта отчёта")
_CLOSING_TOTAL = "исходящий остаток всего"
_CLOSING_ANY = "исходящий остаток"
_PLANNED = "плановый"

_FEE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("fee_broker", "BROKER", "fee_currency"),
    ("fee_exchange", "EXCHANGE", "amount_currency"),
    ("fee_stamp", "STAMP", "amount_currency"),
    ("fee_depositary", "DEPOSITARY", "amount_currency"),
)


def looks_like_v2(content: bytes, tables: list[RawTable] | None = None) -> bool:
    """Отчёт в формате выгрузки Excel: одна таблица с нумерованными разделами.

    Признак — подписи разделов «5.1 …» строками таблицы. Он устойчивее имени
    брокера в шапке: имя меняется при ребрендинге, нумерация разделов — нет.
    """
    sections = _sections_of(extract_tables(content) if tables is None else tables)
    numbers = {section.number for section in sections if section.number}
    return bool(numbers & {"2", "5.1", "8.1", "8.1.1", "8.2"})


def parse_report(
    content: bytes,
) -> tuple[list[ParsedOperation], list[ParsedBalance], list[UnparsedRow]]:
    """Контракт парсера (STAGE-1, T6): `bytes` → (операции, остатки, нераспознанное)."""
    report = parse(content)
    return list(report.operations), list(report.balances), list(report.unparsed)


def parse(content: bytes, tables: list[RawTable] | None = None) -> ParsedReport:
    sections = _sections_of(extract_tables(content) if tables is None else tables)
    header = _read_header(sections)

    operations: list[ParsedOperation] = []
    balances: list[ParsedBalance] = []
    unparsed: list[UnparsedRow] = []

    # Справочник бумаг строится до разбора сделок: в подзаголовке группы сделок
    # ISIN нет, и без раздела 2 сделку не к чему привязать (A-23).
    instruments = _Instruments()
    for section in sections:
        if section.number in SECURITY_BALANCE_SECTIONS:
            _collect(
                _security_balances(section, header, instruments), balances, unparsed
            )

    for section in sections:
        number = section.number
        if number in SECURITY_BALANCE_SECTIONS or number in SKIPPED_SECTIONS:
            continue
        if number in CASH_STATE_SECTIONS:
            _collect(_cash_balances(section, header), balances, unparsed)
        elif number in TRADE_SECTIONS:
            _collect(_trades(section, header, instruments), operations, unparsed)
        elif number in LOAN_SECTIONS:
            _collect(_loan_operations(section, header, instruments), operations, unparsed)
        elif number in CASH_OPERATION_SECTIONS:
            _collect(_cash_operations(section, header, instruments), operations, unparsed)
        elif number in SECURITY_OPERATION_SECTIONS:
            _collect(_security_operations(section, header, instruments), operations, unparsed)
        elif number:
            unparsed.extend(_unknown_section(section))

    if header.period_end is not None:
        balances = [
            item if item.as_of is not None else replace(item, as_of=header.period_end)
            for item in balances
        ]

    return ParsedReport(
        operations=tuple(operations),
        balances=tuple(balances),
        unparsed=tuple(unparsed),
        mapping_version=MAPPING_VERSION,
        period_start=header.period_start,
        period_end=header.period_end,
        account_code=header.account_code,
    )


# -- реквизиты отчёта --------------------------------------------------------


@dataclass(frozen=True)
class _Header:
    """Реквизиты из преамбулы: период, счёт, валюта отчёта."""

    period_start: date | None = None
    period_end: date | None = None
    account_code: str | None = None
    currency: str = DEFAULT_CURRENCY


def _read_header(sections: list[ReportSection]) -> _Header:
    period_start: date | None = None
    period_end: date | None = None
    account: str | None = None
    currency = DEFAULT_CURRENCY

    for section in sections:
        if section.number:
            break
        for row in section.table.row_cells:
            texts = [cell.text for cell in row if cell.text]
            if not texts:
                continue
            label = normalize_signature(texts[0])
            value = texts[1] if len(texts) > 1 else ""
            if any(marker in label for marker in _ACCOUNT_LABELS) and value:
                account = value.strip()
            elif any(marker in label for marker in _REPORT_CURRENCY_LABELS) and value:
                with suppress(FormatError):
                    currency = parse_currency(value)
        for text in section.groups:
            match = _PERIOD_RE.search(text or "")
            if match:
                period_start = parse_date(match.group(1))
                period_end = parse_date(match.group(2))

    return _Header(
        period_start=period_start,
        period_end=period_end,
        account_code=account,
        currency=currency,
    )


# -- справочник бумаг --------------------------------------------------------


@dataclass
class _Instruments:
    """Наименование и номер гос. регистрации → (тикер, ISIN).

    Нужен, потому что подзаголовок группы сделок — это одна склеенная строка
    «эмитент наименование регномер валюта», и опознать бумагу в ней можно только
    поиском известного токена (A-23).
    """

    keys: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)

    def add(self, *, name: str | None, regnum: str | None, isin: str | None) -> None:
        identity = (name, isin)
        for token in (regnum, isin, name):
            signature = normalize_signature(token or "")
            # Токен короче четырёх знаков не опознаёт бумагу, а ловит случайные
            # совпадения внутри чужих названий.
            if len(signature) >= 4:
                self.keys.setdefault(signature, identity)

    def find_in(self, text: str) -> tuple[str | None, str | None] | None:
        """Самое длинное известное вхождение: «НКНХ 1Р-01» важнее «НКНХ»."""
        signature = normalize_signature(text)
        if not signature:
            return None
        best: tuple[int, tuple[str | None, str | None]] | None = None
        for token, identity in self.keys.items():
            if token in signature and (best is None or len(token) > best[0]):
                best = (len(token), identity)
        return best[1] if best else None


# -- раздел 2: остатки по бумагам -------------------------------------------


def _security_balances(
    section: ReportSection, header: _Header, instruments: _Instruments
) -> Iterator[ParsedBalance | UnparsedRow]:
    columns = resolve_columns(section.table, COLUMNS_V2)
    for row in section.table.as_dicts():
        if _is_service_row(row) or is_total_row(row):
            continue
        name = _value(row, columns, "instrument_name")
        isin = _isin(_value(row, columns, "isin"))
        instruments.add(name=name, regnum=_value(row, columns, "regnum"), isin=isin)

        closing = _value(row, columns, "closing_balance")
        if closing is None:
            yield UnparsedRow(
                table=section.title, row=row, reason="нет количества на конец периода"
            )
            continue
        if name is None and isin is None:
            yield UnparsedRow(
                table=section.title, row=row, reason="в строке остатка нет ни бумаги, ни ISIN"
            )
            continue
        try:
            quantity = parse_decimal(closing)
        except FormatError as error:
            yield UnparsedRow(table=section.title, row=row, reason=str(error))
            continue

        yield ParsedBalance(
            kind="security",
            ticker=name,
            isin=isin,
            quantity=quantity,
            currency=header.currency,
            raw_row=dict(row),
        )


# -- разделы 1 и 1.1: остаток денег -----------------------------------------


def _cash_balances(
    section: ReportSection, header: _Header
) -> Iterator[ParsedBalance | UnparsedRow]:
    """Денежный остаток из блока «метка → значение».

    Таблицы с шапкой здесь нет: слева подпись строки, справа по колонке на
    валюту. Валюта колонки объявлена строкой «Входящий остаток (всего): | RUR».
    """
    currencies = _currency_columns(section, header)
    found: dict[int, ParsedBalance] = {}
    fallback: dict[int, ParsedBalance] = {}

    for row in section.table.row_cells:
        cells = [cell for cell in row if cell.text and not is_blank(cell.text)]
        if not cells:
            continue
        label = normalize_signature(cells[0].text)
        if _CLOSING_ANY not in label or _PLANNED in label:
            continue
        target = found if _CLOSING_TOTAL in label else fallback

        for cell in cells[1:]:
            try:
                amount = parse_decimal(cell.text)
            except FormatError:
                continue
            target[cell.column] = ParsedBalance(
                kind="cash",
                ticker=None,
                quantity=amount,
                currency=currencies.get(cell.column, header.currency),
                raw_row={"строка": cells[0].text, "значение": cell.text},
            )

    yield from (found or fallback).values()


def _currency_columns(section: ReportSection, header: _Header) -> dict[int, str]:
    """Колонка значений → валюта. Объявляется ячейкой с кодом валюты вместо суммы."""
    result: dict[int, str] = {}
    for row in section.table.row_cells:
        for cell in row:
            text = cell.text.strip()
            if len(text) != 3 or not text.isalpha():
                continue
            try:
                result.setdefault(cell.column, parse_currency(text))
            except FormatError:
                continue
    if not result:
        return result
    return result


# -- разделы 5.1 и 5.10: сделки ---------------------------------------------


def _trades(
    section: ReportSection, header: _Header, instruments: _Instruments
) -> Iterator[ParsedOperation | UnparsedRow]:
    columns = resolve_columns(section.table, COLUMNS_V2)
    rows = section.table.as_dicts()

    for index, row in enumerate(rows):
        group = section.group_of(index) or ""
        if _is_summary_row(row) or _is_service_row(row) or _is_summary_group(group):
            continue
        try:
            yield from _trade_row(row, columns, header, instruments, group, section.title)
        except (FormatError, ValueError) as error:
            yield UnparsedRow(table=section.title, row=row, reason=str(error))


def _trade_row(
    row: dict[str, str],
    columns: dict[str, str],
    header: _Header,
    instruments: _Instruments,
    group: str,
    label: str,
) -> Iterator[ParsedOperation | UnparsedRow]:
    direction = match_direction(_value(row, columns, "direction") or "")
    if direction is None:
        raise ValueError(f"вид сделки не опознан: {_value(row, columns, 'direction')!r}")

    repo = _value(row, columns, "repo_kind")
    if repo:
        yield UnparsedRow(
            table=label, row=row, reason=f"сделка РЕПО ({repo}) — разбор не определён"
        )
        return

    trade_date = parse_date(_require(row, columns, "trade_date"))
    settlement_date = (
        parse_optional_date(_value(row, columns, "settlement_date"))
        or parse_optional_date(_value(row, columns, "settlement_planned"))
        or trade_date
    )
    quantity = parse_decimal(_require(row, columns, "quantity"))
    price = parse_optional_decimal(_value(row, columns, "price"))
    accrued = parse_optional_decimal(_value(row, columns, "accrued_int"))
    currency = _currency(row, columns, "amount_currency", header)

    ticker, isin = _instrument_of(group, instruments)
    if ticker is None and isin is None:
        yield UnparsedRow(
            table=label,
            row=row,
            reason=f"бумага не опознана по подзаголовку группы: {group!r}",
        )
        return

    gross = abs(parse_decimal(_require(row, columns, "amount")))
    if accrued is not None:
        # НКД стоит отдельной колонкой, значит в сумму сделки он не входит (A-07).
        gross += abs(accrued)

    sign = Decimal(-1) if direction == "BUY" else Decimal(1)
    trade_no = _value(row, columns, "broker_trade_no")
    fees = list(
        _fee_operations(row, columns, header, trade_date, settlement_date, ticker, isin, label)
    )

    yield ParsedOperation(
        kind=direction,
        trade_date=trade_date,
        settlement_date=settlement_date,
        ticker=ticker,
        isin=isin,
        instrument_name=ticker,
        quantity=abs(quantity) * (Decimal(1) if direction == "BUY" else Decimal(-1)),
        price=price,
        accrued_int=accrued,
        fee=sum((abs(item.amount) for item in fees), Decimal(0)) or None,
        amount=sign * gross,
        currency=currency,
        broker_trade_no=trade_no,
        note=group or None,
        raw_row=dict(row),
    )
    yield from fees


def _fee_operations(
    row: dict[str, str],
    columns: dict[str, str],
    header: _Header,
    trade_date: date,
    settlement_date: date | None,
    ticker: str | None,
    isin: str | None,
    label: str,
) -> Iterator[ParsedOperation]:
    """Комиссии сделки — отдельными событиями по видам (спека 4.1, A-06)."""
    for column, fee_kind, currency_column in _FEE_COLUMNS:
        value = parse_optional_decimal(_value(row, columns, column))
        if value is None or value == 0:
            continue
        yield ParsedOperation(
            kind="FEE",
            trade_date=trade_date,
            settlement_date=settlement_date,
            ticker=ticker,
            isin=isin,
            quantity=None,
            price=None,
            amount=-abs(value),
            currency=_currency(row, columns, currency_column, header),
            fee_kind=fee_kind,
            broker_trade_no=_value(row, columns, "broker_trade_no"),
            note=label,
            raw_row=dict(row),
        )


# -- раздел 5.4: заём ценных бумаг ------------------------------------------


def _loan_operations(
    section: ReportSection, header: _Header, instruments: _Instruments
) -> Iterator[ParsedOperation | UnparsedRow]:
    """Заём бумаг брокером: позиции не меняет, но деньги двигает (A-21).

    Бумаги остаются в собственности, поэтому количества здесь нет ни у выдачи,
    ни у возврата. В журнал идут только деньги: вознаграждение из колонки «% по
    сделке» строки возврата и брокерская комиссия строки, если она ненулевая.
    """
    columns = resolve_columns(section.table, COLUMNS_V2)
    rows = section.table.as_dicts()

    for index, row in enumerate(rows):
        group = section.group_of(index) or ""
        if _is_summary_row(row) or _is_service_row(row) or _is_summary_group(group):
            continue
        try:
            yield from _loan_row(row, columns, header, instruments, group, section.title)
        except (FormatError, ValueError) as error:
            yield UnparsedRow(table=section.title, row=row, reason=str(error))


def _loan_row(
    row: dict[str, str],
    columns: dict[str, str],
    header: _Header,
    instruments: _Instruments,
    group: str,
    label: str,
) -> Iterator[ParsedOperation | UnparsedRow]:
    direction = normalize_signature(_value(row, columns, "direction") or "")
    if direction not in (LOAN_RETURN, LOAN_ISSUE):
        yield UnparsedRow(
            table=label, row=row, reason=f"вид сделки займа не опознан: {direction!r}"
        )
        return

    trade_date = parse_date(_require(row, columns, "trade_date"))
    settlement_date = (
        parse_optional_date(_value(row, columns, "settlement_date"))
        or parse_optional_date(_value(row, columns, "settlement_planned"))
        or trade_date
    )
    ticker, isin = _instrument_of(group, instruments)
    currency = _currency(row, columns, "amount_currency", header)
    trade_no = _value(row, columns, "broker_trade_no")
    note = _value(row, columns, "loan_kind") or group or None

    if direction == LOAN_RETURN:
        # «% по сделке» — сумма вознаграждения в валюте расчётов, а не ставка:
        # разность с комиссией сошлась с расхождением сверки до копейки (A-21).
        reward = parse_optional_decimal(_value(row, columns, "rate"))
        if reward is not None and reward != 0:
            yield ParsedOperation(
                kind="LENDING_INCOME",
                trade_date=trade_date,
                settlement_date=settlement_date,
                ticker=ticker,
                isin=isin,
                instrument_name=ticker,
                # Количества нет: заём не меняет позицию, бумаги остаются
                # в собственности (A-21).
                quantity=None,
                price=None,
                amount=abs(reward),
                currency=currency,
                broker_trade_no=trade_no,
                note=note,
                raw_row=dict(row),
            )

    fee = parse_optional_decimal(_value(row, columns, "fee_broker"))
    if fee is not None and fee != 0:
        yield ParsedOperation(
            kind="FEE",
            trade_date=trade_date,
            settlement_date=settlement_date,
            ticker=ticker,
            isin=isin,
            quantity=None,
            price=None,
            amount=-abs(fee),
            currency=_currency(row, columns, "fee_currency", header),
            fee_kind="BROKER",
            broker_trade_no=trade_no,
            note=note,
            raw_row=dict(row),
        )


# -- разделы 8.1 и 8.1.1: неторговые операции с деньгами ---------------------


def _cash_operations(
    section: ReportSection, header: _Header, instruments: _Instruments
) -> Iterator[ParsedOperation | UnparsedRow]:
    columns = resolve_columns(section.table, COLUMNS_V2)
    for row in section.table.as_dicts():
        if _is_service_row(row) or is_total_row(row):
            continue
        try:
            yield from _cash_operation_row(row, columns, header, instruments, section.title)
        except (FormatError, ValueError) as error:
            yield UnparsedRow(table=section.title, row=row, reason=str(error))


def _cash_operation_row(
    row: dict[str, str],
    columns: dict[str, str],
    header: _Header,
    instruments: _Instruments,
    label: str,
) -> Iterator[ParsedOperation | UnparsedRow]:
    operation = normalize_signature(_value(row, columns, "operation_type") or "")
    comment = _value(row, columns, "comment") or ""
    amount = parse_decimal(_require(row, columns, "amount"))

    if operation in ZERO_MARKER_TYPES:
        if amount != 0:
            yield UnparsedRow(
                table=label,
                row=row,
                reason=(
                    f"{operation!r} ожидалась с нулевой суммой — "
                    "деньги приходят строкой «Погашение номинала» (A-20)"
                ),
            )
        return

    kind = CASH_OPERATION_TYPES.get(operation)
    if kind is None and operation == _INCOME_TYPE:
        signature = normalize_signature(comment)
        if any(marker in signature for marker in _DIVIDEND_MARKERS):
            kind = "DIVIDEND"
    if kind is None:
        yield UnparsedRow(
            table=label, row=row, reason=f"тип операции не опознан: {operation!r}"
        )
        return

    operation_date = parse_date(_require(row, columns, "trade_date"))
    ticker, isin = _instrument_of(comment, instruments)

    # Знак берётся из отчёта: он и есть изменение остатка. Тип операции служит
    # только защитой от выгрузки, где расход напечатан без минуса.
    if kind in {"TAX", "CASH_OUT"}:
        amount = -abs(amount)
    elif kind in {"CASH_IN", "COUPON", "DIVIDEND", "MATURITY"}:
        amount = abs(amount)

    yield ParsedOperation(
        kind=kind,
        trade_date=operation_date,
        settlement_date=operation_date,
        ticker=ticker,
        isin=isin,
        quantity=None,
        price=None,
        amount=amount,
        currency=_currency(row, columns, "currency", header),
        # Налог у этого брокера приходит отдельной строкой, значит купон и
        # дивиденд показаны до удержания (A-04).
        withheld_at_source=False if kind in {"COUPON", "DIVIDEND"} else None,
        note=comment or None,
        raw_row=dict(row),
    )


# -- раздел 8.2: неторговые операции с бумагами ------------------------------


def _security_operations(
    section: ReportSection, header: _Header, instruments: _Instruments
) -> Iterator[ParsedOperation | UnparsedRow]:
    columns = resolve_columns(section.table, COLUMNS_V2)
    for row in section.table.as_dicts():
        if _is_service_row(row) or is_total_row(row):
            continue
        try:
            yield from _security_operation_row(row, columns, header, instruments, section.title)
        except (FormatError, ValueError) as error:
            yield UnparsedRow(table=section.title, row=row, reason=str(error))


def _security_operation_row(
    row: dict[str, str],
    columns: dict[str, str],
    header: _Header,
    instruments: _Instruments,
    label: str,
) -> Iterator[ParsedOperation | UnparsedRow]:
    operation = normalize_signature(_value(row, columns, "operation_type") or "")
    matched = SECURITY_OPERATION_TYPES.get(operation)
    if matched is None:
        yield UnparsedRow(
            table=label,
            row=row,
            reason=f"неторговая операция с бумагами не опознана: {operation!r}",
        )
        return

    kind, sign = matched
    operation_date = parse_date(_require(row, columns, "trade_date"))
    name = _value(row, columns, "instrument_name")
    isin = _isin(_value(row, columns, "isin"))
    instruments.add(name=name, regnum=_value(row, columns, "regnum"), isin=isin)
    quantity = parse_decimal(_require(row, columns, "quantity"))

    yield ParsedOperation(
        kind=kind,
        trade_date=operation_date,
        settlement_date=operation_date,
        ticker=name,
        isin=isin,
        instrument_name=name,
        quantity=quantity if sign == 0 else abs(quantity) * sign,
        price=None,
        # Денег в этом разделе нет: они приходят своей строкой в 8.1.
        amount=Decimal(0),
        currency=header.currency,
        note=_value(row, columns, "comment"),
        raw_row=dict(row),
    )


# -- вспомогательное ---------------------------------------------------------


def _sections_of(tables: list[RawTable]) -> list[ReportSection]:
    return split_sections(tables, known_header=lambda text: is_known_column(text, COLUMNS_V2))


def _unknown_section(section: ReportSection) -> list[UnparsedRow]:
    """Нумерованный раздел, которого парсер не знает, — это потерянные операции."""
    rows: list[UnparsedRow] = []
    for row in section.table.as_dicts():
        if _is_service_row(row) or is_total_row(row):
            continue
        rows.append(
            UnparsedRow(
                table=section.title,
                row=row,
                reason=f"раздел {section.number} не разбирается версией {MAPPING_VERSION}",
            )
        )
    return rows


def _instrument_of(text: str, instruments: _Instruments) -> tuple[str | None, str | None]:
    """Бумага по тексту: сначала ISIN прямо в тексте, затем справочник раздела 2."""
    match = _ISIN_RE.search(text.upper())
    if match:
        found = instruments.find_in(match.group(1))
        if found is not None:
            return found

    # Токен формы ISIN, которого нет в разделе 2, — ещё не ISIN этой бумаги.
    # В подзаголовке группы сделок первым стоит внутренний код выпуска той же
    # формы («MC» плюс десять цифр), и раньше разбор брал его и останавливался:
    # бумага заводилась в справочнике второй раз, сделки уходили на неё, а
    # контрольный остаток — на запись из раздела 2. Раздел 2 авторитетнее
    # (A-23), поэтому спрашиваем его по всему подзаголовку.
    found = instruments.find_in(text)
    if found is not None:
        return found

    # Ничего не known: токен формы ISIN всё же лучше, чем ничего — по нему
    # бумагу хотя бы видно в «Входящих».
    return (None, match.group(1)) if match else (None, None)


def _is_summary_row(row: dict[str, str]) -> bool:
    values = [normalize_signature(value) for value in row.values() if value]
    return any(
        value == marker or value.startswith(marker + " ") or value.startswith(marker + ",")
        for value in values
        for marker in SUMMARY_MARKERS
    )


def _is_summary_group(group: str) -> bool:
    """Хвост раздела сделок — обороты по площадкам под своим подзаголовком.

    Строки там стоят в колонках сделок, но сделками не являются: «Московская
    биржа | 1 234.56» разбиралось бы как сделка с видом «1 234.56».
    """
    signature = normalize_signature(group)
    return any(signature.startswith(marker) for marker in SUMMARY_MARKERS)


def _is_service_row(row: dict[str, str]) -> bool:
    """Подписи, реквизиты и прочий текст без единого числа — не строка данных."""
    filled = [value for value in row.values() if value and not is_blank(value)]
    if not filled:
        return True
    return not any(_has_number(value) for value in filled)


def _has_number(text: str) -> bool:
    try:
        parse_decimal(text)
    except FormatError:
        return False
    return True


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


def _currency(
    row: dict[str, str], columns: dict[str, str], logical: str, header: _Header
) -> str:
    raw = _value(row, columns, logical)
    if raw is None:
        return header.currency
    try:
        return parse_currency(raw)
    except FormatError:
        return header.currency


def _isin(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip().upper()
    return candidate if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", candidate) else None


def _collect(items: Iterator[object], target: list, unparsed: list[UnparsedRow]) -> None:
    for item in items:
        if isinstance(item, UnparsedRow):
            unparsed.append(item)
        else:
            target.append(item)
