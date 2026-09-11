"""Типы событий, признак «внешний / внутренний» и построение `natural_key`.

Классификация потоков — свойство типа события, а не решение на лету (спека 3.2):
от неё зависит корректность всех доходностей, поэтому она задана таблицей.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portfolio.models import EventType

__all__ = [
    "EXTERNAL_EVENT_TYPES",
    "EventType",
    "KeyInput",
    "assign_natural_keys",
    "is_external",
    "natural_key",
    "to_event_type",
]

# Внешние потоки: движение денег между портфелем и внешним миром. Всё остальное —
# внутренние события, не влияющие на `external_flow` (спека 3, 5.2).
EXTERNAL_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.OPENING_BALANCE,
        EventType.CASH_IN,
        EventType.CASH_OUT,
        EventType.TRANSFER,
    }
)

_KEY_VERSION = "k1"


def to_event_type(kind: str) -> EventType:
    """Строка парсера → тип события. Незнакомый тип — ошибка, а не `OTHER`."""
    try:
        return EventType(kind)
    except ValueError as error:
        raise ValueError(f"неизвестный тип события: {kind!r}") from error


def is_external(event_type: EventType | str) -> bool:
    resolved = event_type if isinstance(event_type, EventType) else to_event_type(event_type)
    return resolved in EXTERNAL_EVENT_TYPES


@dataclass(frozen=True)
class KeyInput:
    """Всё, из чего строится ключ идемпотентности.

    `instrument_ref` — стабильная строка инструмента (ISIN либо тикер), а не
    `instrument_id`: id зависит от порядка создания справочника и ключ бы поехал
    при пересоздании базы.
    """

    account_code: str
    kind: str
    trade_date: date
    instrument_ref: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    amount: Decimal | None = None
    fee_kind: str | None = None
    broker_trade_no: str | None = None
    source_row_id: str | None = None


def natural_key(item: KeyInput, index: int = 0) -> str:
    """Ключ идемпотентности в порядке предпочтения из спеки 3.2.

    1. номер сделки брокера — единственный устойчивый вариант;
    2. хеш от реквизитов плюс порядковый индекс внутри группы одинаковых
       операций **в пределах календарного дня**, а не отчёта: иначе перекрытие
       периодов между отчётами ломает ключ;
    3. для `OPENING_BALANCE` и строк CSV — хеш с `id` строки источника.
    """
    if item.source_row_id is not None:
        return _digest(
            "csv",
            item.account_code,
            item.trade_date.isoformat(),
            item.kind,
            item.instrument_ref or "",
            item.source_row_id,
        )

    if item.broker_trade_no:
        return _digest(
            "trade",
            item.account_code,
            item.trade_date.isoformat(),
            item.kind,
            item.fee_kind or "",
            item.broker_trade_no,
        )

    return _digest(
        "hash",
        item.account_code,
        item.trade_date.isoformat(),
        item.kind,
        item.instrument_ref or "",
        _num(item.quantity),
        _num(item.price),
        _num(item.amount),
        item.fee_kind or "",
        str(index),
    )


def assign_natural_keys(items: Sequence[KeyInput]) -> list[str]:
    """Ключи для набора операций одного импорта.

    Индекс внутри группы одинаковых операций считается по календарному дню.
    Известный отказ варианта 2 (спека 3.2): отмена брокером одной сделки из
    группы сдвигает индексы последующих. Поэтому номер сделки предпочтителен.
    """
    counters: dict[tuple[str, ...], int] = {}
    keys: list[str] = []

    for item in items:
        group = _group(item)
        index = counters.get(group, 0)
        counters[group] = index + 1
        keys.append(natural_key(item, index))
    return keys


def _group(item: KeyInput) -> tuple[str, ...]:
    return (
        item.account_code,
        item.trade_date.isoformat(),
        item.kind,
        item.instrument_ref or "",
        _num(item.quantity),
        _num(item.price),
        _num(item.amount),
        item.fee_kind or "",
    )


def _num(value: Decimal | None) -> str:
    if value is None:
        return ""
    # Нормализация, чтобы 10 и 10.00 давали один ключ.
    normalized = value.normalize()
    return format(normalized, "f")


def _digest(*parts: str) -> str:
    payload = "|".join((_KEY_VERSION, *parts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]
