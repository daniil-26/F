"""Каждый инвариант срабатывает на подготовленном нарушении (спека 5.5).

Жёсткие проверки этапа 1 — те, что не требуют цен. «Сумма позиций по ценам равна
nav» появится на этапе 4 вместе с `daily_nav`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio.domain.events import EXTERNAL_EVENT_TYPES, is_external
from portfolio.domain.invariants import (
    check_all,
    check_currency_filled,
    check_natural_keys_unique,
    check_no_negative_positions,
    check_reversals_net_to_zero,
    external_flow_by_day,
)
from portfolio.models import EventType, Transaction


def _row(
    row_id: int,
    event_type: EventType,
    amount: str = "0",
    *,
    quantity: str | None = None,
    day: date = date(2025, 8, 1),
    instrument_id: int | None = 1,
    currency: str = "RUB",
    natural_key: str | None = None,
    reverses_id: int | None = None,
    settlement_date: date | None = None,
) -> Transaction:
    row = Transaction(
        natural_key=natural_key or f"key-{row_id}",
        event_type=event_type,
        trade_date=day,
        settlement_date=settlement_date or day,
        account_id=1,
        instrument_id=instrument_id,
        quantity=None if quantity is None else Decimal(quantity),
        amount=Decimal(amount),
        currency=currency,
        reverses_id=reverses_id,
    )
    row.id = row_id
    return row


def test_clean_ledger_has_no_violations() -> None:
    rows = [
        _row(1, EventType.OPENING_BALANCE, "0", quantity="100"),
        _row(2, EventType.BUY, "-15000", quantity="50", day=date(2025, 8, 5)),
        _row(3, EventType.SELL, "9315", quantity="-30", day=date(2025, 8, 12)),
    ]
    assert check_all(rows) == []


def test_negative_position_is_caught() -> None:
    """Чаще всего это незаполненные начальные остатки (STAGE-1, T9)."""
    rows = [_row(1, EventType.SELL, "9315", quantity="-30")]

    violations = check_no_negative_positions(rows)

    assert [item.check for item in violations] == ["quantity_not_negative"]
    assert "-30" in violations[0].message


def test_reversal_must_net_to_zero() -> None:
    original = _row(1, EventType.BUY, "-15000", quantity="50")
    broken = _row(2, EventType.BUY, "14000", quantity="-50", reverses_id=1)

    violations = check_reversals_net_to_zero([original, broken])

    assert [item.check for item in violations] == ["reversal_pairs"]
    assert "не ноль" in violations[0].message


def test_correct_reversal_passes() -> None:
    original = _row(1, EventType.BUY, "-15000", quantity="50")
    reversal = _row(2, EventType.BUY, "15000", quantity="-50", reverses_id=1)

    assert check_reversals_net_to_zero([original, reversal]) == []


def test_duplicate_natural_key_is_caught() -> None:
    rows = [
        _row(1, EventType.BUY, "-100", quantity="1", natural_key="same"),
        _row(2, EventType.BUY, "-100", quantity="1", natural_key="same"),
    ]

    violations = check_natural_keys_unique(rows)

    assert [item.check for item in violations] == ["natural_key_unique"]


def test_missing_currency_is_caught() -> None:
    rows = [_row(1, EventType.FEE, "-45", currency="")]

    violations = check_currency_filled(rows)

    assert [item.check for item in violations] == ["currency_filled"]


def test_external_flow_counts_only_external_events() -> None:
    """Классификация — свойство типа события, а не решение на лету (спека 3.2)."""
    rows = [
        _row(1, EventType.CASH_IN, "100000", day=date(2025, 8, 1)),
        _row(2, EventType.BUY, "-15000", quantity="50", day=date(2025, 8, 5)),
        _row(3, EventType.COUPON, "282.50", day=date(2025, 8, 15)),
        _row(4, EventType.CASH_OUT, "-20000", day=date(2025, 8, 29)),
    ]

    flow = external_flow_by_day(rows)

    assert flow == {date(2025, 8, 1): Decimal("100000"), date(2025, 8, 29): Decimal("-20000")}


def test_external_flow_uses_settlement_date() -> None:
    rows = [
        _row(
            1,
            EventType.CASH_IN,
            "1000",
            day=date(2025, 8, 29),
            settlement_date=date(2025, 9, 1),
        )
    ]
    assert external_flow_by_day(rows) == {date(2025, 9, 1): Decimal("1000")}


def test_external_classification_matches_spec() -> None:
    assert {
        EventType.OPENING_BALANCE,
        EventType.CASH_IN,
        EventType.CASH_OUT,
        EventType.TRANSFER,
    } == EXTERNAL_EVENT_TYPES
    assert is_external(EventType.CASH_IN)
    assert not is_external(EventType.COUPON)
    assert not is_external(EventType.FEE)
