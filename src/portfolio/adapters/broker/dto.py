"""DTO парсера отчёта. Тикер и валюта строками, без `instrument_id` и `natural_key`.

Разрешение идентификаторов — дело `domain/instruments.py`, построение ключа —
`domain/events.py`. Адаптер ничего не знает про БД (спека 9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

__all__ = ["ParsedBalance", "ParsedOperation", "ParsedReport", "UnparsedRow"]


@dataclass(frozen=True)
class ParsedOperation:
    """Одна операция из отчёта.

    Знаки: `quantity` — изменение позиции (покупка +, продажа −), `amount` —
    изменение денежного остатка (приход +, расход −). Допущение A-05.
    """

    kind: str
    trade_date: date
    settlement_date: date | None
    ticker: str | None
    quantity: Decimal | None
    price: Decimal | None
    amount: Decimal
    currency: str
    fee_kind: str | None = None
    broker_trade_no: str | None = None
    withheld_at_source: bool | None = None
    isin: str | None = None
    accrued_int: Decimal | None = None
    fee: Decimal | None = None
    instrument_name: str | None = None
    note: str | None = None
    raw_row: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedBalance:
    """Контрольный остаток из отчёта — то, с чем сверяется журнал."""

    kind: str  # cash | security
    ticker: str | None
    quantity: Decimal
    currency: str
    isin: str | None = None
    as_of: date | None = None
    raw_row: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class UnparsedRow:
    """Строка, которую парсер не понял. Попадает во «Входящие», а не в журнал."""

    table: str
    row: dict[str, str]
    reason: str


@dataclass(frozen=True)
class ParsedReport:
    """Полный результат разбора, включая реквизиты самого отчёта."""

    operations: tuple[ParsedOperation, ...]
    balances: tuple[ParsedBalance, ...]
    unparsed: tuple[UnparsedRow, ...]
    mapping_version: str
    period_start: date | None = None
    period_end: date | None = None
    account_code: str | None = None
    # В отчёте были строки займа бумаг. Мягкий допуск сверки применяется только
    # к таким отчётам (A-27): расчёты по займу ложатся на границу месяца, и
    # копеечная неточность здесь стоит дороже, чем стоит.
    has_loan_section: bool = False
