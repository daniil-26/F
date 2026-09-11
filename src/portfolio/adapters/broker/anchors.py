"""Соответствие «текст заголовка секции → логическое имя».

Таблицы якорятся по подписи, а не по индексу (спека 4.1): брокер переставляет
секции между версиями отчёта, а формулировки меняет редко. При смене
формулировки правится одна строка этого файла.
"""

from __future__ import annotations

import enum
import re

from portfolio.adapters.broker.tables import RawTable
from portfolio.adapters.formats import normalize_text

__all__ = [
    "COLUMNS",
    "OPERATION_SIGNATURES",
    "SECTION_SIGNATURES",
    "TOTAL_ROW_MARKERS",
    "Section",
    "find_sections",
    "is_total_row",
    "match_direction",
    "match_operation_kind",
    "normalize_signature",
    "resolve_columns",
]


class Section(enum.Enum):
    """Логические секции отчёта, нужные этапу 1."""

    TRADES = "trades"
    CASH_FLOW = "cash_flow"
    CASH_BALANCES = "cash_balances"
    SECURITY_BALANCES = "security_balances"
    REPORT_HEADER = "report_header"


# Подписи секций. Сравнение идёт по нормализованному вхождению подстроки, поэтому
# достаточно устойчивого ядра формулировки без хвостов вида «за период с ... по ...».
SECTION_SIGNATURES: dict[Section, tuple[str, ...]] = {
    Section.TRADES: (
        "сделки",
        "заключенные в отчетном периоде сделки",
        "сделки с ценными бумагами",
        "совершенные сделки",
    ),
    Section.CASH_FLOW: (
        "движение денежных средств",
        "операции с денежными средствами",
        "прочие операции",
        "неторговые операции",
    ),
    Section.CASH_BALANCES: (
        "остатки денежных средств",
        "денежные средства",
        "остаток денежных средств",
    ),
    Section.SECURITY_BALANCES: (
        "остатки по ценным бумагам",
        "состояние портфеля",
        "портфель по ценным бумагам",
        "активы",
    ),
    Section.REPORT_HEADER: (
        "отчет о сделках и операциях",
        "отчет брокера",
        "период",
    ),
}

# Логическое имя колонки → варианты заголовка. Сопоставление по вхождению.
COLUMNS: dict[str, tuple[str, ...]] = {
    "broker_trade_no": ("номер сделки", "номер заявки", "no сделки", "№ сделки", "id сделки"),
    "trade_date": ("дата заключения", "дата сделки", "дата операции", "дата"),
    "settlement_date": ("дата расчетов", "дата поставки", "дата исполнения"),
    "ticker": ("код инструмента", "код бумаги", "тикер", "код", "инструмент", "бумага"),
    "isin": ("isin",),
    "instrument_name": ("наименование", "название бумаги", "эмитент"),
    "direction": ("вид сделки", "операция", "тип операции", "вид", "направление", "b/s"),
    "quantity": ("количество", "кол-во", "шт"),
    "price": ("цена", "курс"),
    "amount": ("сумма сделки", "сумма", "объем", "итого"),
    "accrued_int": ("нкд", "накопленный купонный доход"),
    "fee_broker": ("комиссия брокера", "вознаграждение брокера", "брокерская комиссия"),
    "fee_depositary": ("комиссия депозитария", "депозитарная комиссия", "депозитарий"),
    "fee_exchange": ("комиссия биржи", "биржевой сбор", "комиссия тс"),
    "currency": ("валюта",),
    "tax": ("сумма налога", "удержанный налог", "налог", "ндфл"),
    "description": ("описание операции", "содержание операции", "описание", "комментарий"),
    # Основы слов, а не полные формы: в заголовках встречается и «Сумма
    # зачисления», и «Зачислено».
    "credit": ("зачислен", "приход", "кредит", "поступлен"),
    "debit": ("списан", "расход", "дебет", "выплат"),
    "closing_balance": (
        "исходящий остаток",
        "остаток на конец",
        "остаток на конец периода",
        "конечный остаток",
        "плановый исходящий остаток",
    ),
    "opening_balance": ("входящий остаток", "остаток на начало", "начальный остаток"),
}

# Маркеры строк, которые не являются операциями (спека 4.1).
TOTAL_ROW_MARKERS: tuple[str, ...] = (
    "итого",
    "итог",
    "всего",
    "в том числе",
    "подытог",
    "оборот",
)

# Распознавание типа операции по тексту описания. Выигрывает самая длинная
# совпавшая подпись, поэтому «комиссия депозитария» не перехватывается
# «комиссией», а «пополнение счета, перевод из банка» — «переводом».
OPERATION_SIGNATURES: tuple[tuple[str, str, str | None], ...] = (
    ("налог на доходы", "TAX", None),
    ("ндфл", "TAX", None),
    ("налог", "TAX", None),
    ("комиссия депозитария", "FEE", "DEPOSITARY"),
    ("депозитарная комиссия", "FEE", "DEPOSITARY"),
    ("комиссия за вывод", "FEE", "WITHDRAWAL"),
    ("комиссия за перевод", "FEE", "WITHDRAWAL"),
    ("комиссия биржи", "FEE", "EXCHANGE"),
    ("биржевой сбор", "FEE", "EXCHANGE"),
    ("комиссия брокера", "FEE", "BROKER"),
    ("вознаграждение брокера", "FEE", "BROKER"),
    ("комиссия", "FEE", "OTHER"),
    ("выплата купона", "COUPON", None),
    ("купон", "COUPON", None),
    ("дивиденд", "DIVIDEND", None),
    ("частичное досрочное погашение", "AMORTIZATION", None),
    ("амортизация", "AMORTIZATION", None),
    ("погашение ценных бумаг", "MATURITY", None),
    ("погашение номинала", "MATURITY", None),
    ("погашение", "MATURITY", None),
    ("конвертация", "CONVERSION", None),
    ("перевод между счетами", "TRANSFER", None),
    ("перевод", "TRANSFER", None),
    ("вывод денежных средств", "CASH_OUT", None),
    ("снятие", "CASH_OUT", None),
    ("зачисление денежных средств", "CASH_IN", None),
    ("пополнение", "CASH_IN", None),
    ("внесение", "CASH_IN", None),
    ("покупка", "BUY", None),
    ("продажа", "SELL", None),
)

_BUY_WORDS = ("покупка", "покупк", "buy", "приобретение", "к покупке", "b")
_SELL_WORDS = ("продажа", "продаж", "sell", "к продаже", "s")

_PUNCT_RE = re.compile(r"[«»\"'()\[\]:;,.]+")


def normalize_signature(text: str) -> str:
    """Приводит подпись к сравнимому виду: регистр, пробелы, пунктуация, ё."""
    cleaned = normalize_text(text).casefold().replace("ё", "е")
    cleaned = _PUNCT_RE.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def find_sections(tables: list[RawTable]) -> dict[Section, list[RawTable]]:
    """Раскладывает таблицы по секциям по подписи, а не по порядку следования.

    Подпись ищется в `caption`, в тексте перед таблицей и в заголовках колонок.
    Таблица, подпись которой не опознана, в результат не попадает — её строки
    уйдут в `UnparsedRow`, и это видно в отчёте сверки.
    """
    result: dict[Section, list[RawTable]] = {}
    for table in tables:
        if table.is_empty:
            continue
        section = _section_of(table)
        if section is not None:
            result.setdefault(section, []).append(table)
    return result


def _section_of(table: RawTable) -> Section | None:
    candidates = [table.caption, *reversed(table.preceding_text)]
    for candidate in candidates:
        signature = normalize_signature(candidate)
        if not signature:
            continue
        match = _match_section(signature)
        if match is not None:
            return match
    return _section_by_headers(table)


def _match_section(signature: str) -> Section | None:
    best: tuple[int, Section] | None = None
    for section, variants in SECTION_SIGNATURES.items():
        for variant in variants:
            if variant in signature:
                score = len(variant)
                if best is None or score > best[0]:
                    best = (score, section)
    return best[1] if best else None


def _section_by_headers(table: RawTable) -> Section | None:
    """Резервное опознание по набору колонок, если подписи не нашлось."""
    columns = set(resolve_columns(table))
    has_trade_marker = "direction" in columns or "broker_trade_no" in columns
    if {"quantity", "price"} <= columns and has_trade_marker:
        return Section.TRADES
    if "description" in columns and ({"credit", "debit"} & columns or "amount" in columns):
        return Section.CASH_FLOW
    if "closing_balance" in columns and "currency" in columns and "ticker" not in columns:
        return Section.CASH_BALANCES
    if "closing_balance" in columns and {"ticker", "isin"} & columns:
        return Section.SECURITY_BALANCES
    return None


def resolve_columns(table: RawTable) -> dict[str, str]:
    """Логическое имя колонки → фактическое имя в `RawTable.as_dicts()`.

    Колонка выбирается по самому длинному совпавшему варианту: «дата расчетов»
    должна выиграть у «дата», иначе обе даты съедет в одну.
    """
    names = table._column_names()
    scored: dict[str, tuple[int, str]] = {}

    for name in names:
        signature = normalize_signature(name)
        if not signature:
            continue
        for logical, variants in COLUMNS.items():
            for variant in variants:
                if variant in signature:
                    score = len(variant)
                    current = scored.get(logical)
                    if current is None or score > current[0]:
                        scored[logical] = (score, name)

    resolved = {logical: name for logical, (_, name) in scored.items()}
    return _drop_duplicate_targets(resolved, names)


def _drop_duplicate_targets(resolved: dict[str, str], names: list[str]) -> dict[str, str]:
    """Одна физическая колонка не может обслуживать два логических имени.

    Побеждает более специфичное имя (длиннее совпавший вариант), второе
    отбрасывается: лучше `UnparsedRow`, чем сумма, подставленная из чужой колонки.
    """
    by_physical: dict[str, list[str]] = {}
    for logical, physical in resolved.items():
        by_physical.setdefault(physical, []).append(logical)

    final: dict[str, str] = {}
    for physical, logicals in by_physical.items():
        if len(logicals) == 1:
            final[logicals[0]] = physical
            continue
        signature = normalize_signature(physical)
        best = max(
            logicals,
            key=lambda logical: max(
                (len(variant) for variant in COLUMNS[logical] if variant in signature),
                default=0,
            ),
        )
        final[best] = physical
    _ = names
    return final


def is_total_row(row: dict[str, str]) -> bool:
    """Строка «Итого», «Всего» и подзаголовки секций — не операции."""
    values = [normalize_signature(value) for value in row.values()]
    filled = [value for value in values if value]
    if not filled:
        return True

    for value in filled:
        for marker in TOTAL_ROW_MARKERS:
            if value == marker or value.startswith(marker + " ") or value.startswith(marker + ":"):
                return True

    # Подзаголовок: заполнена ровно одна ячейка, и в ней нет цифр.
    return len(filled) == 1 and not any(char.isdigit() for char in filled[0])


def match_operation_kind(text: str) -> tuple[str, str | None] | None:
    """Текст описания → (тип события, вид комиссии). `None`, если не опознано."""
    signature = normalize_signature(text)
    if not signature:
        return None

    best: tuple[int, str, str | None] | None = None
    for variant, kind, fee_kind in OPERATION_SIGNATURES:
        if variant in signature and (best is None or len(variant) > best[0]):
            best = (len(variant), kind, fee_kind)
    return (best[1], best[2]) if best else None


def match_direction(text: str) -> str | None:
    """Вид сделки: `BUY`, `SELL` или `None`, если формулировка незнакома."""
    signature = normalize_signature(text)
    if not signature:
        return None
    for word in _BUY_WORDS:
        if signature == word or word in signature:
            return "BUY"
    for word in _SELL_WORDS:
        if signature == word or word in signature:
            return "SELL"
    return None
