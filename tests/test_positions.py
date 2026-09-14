"""Проекции и сверка: две даты, лоты, расхождения."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio.domain.positions import cash_balances, positions_on, settled_cash
from portfolio.domain.reconcile import ExpectedBalance, reconcile
from portfolio.models import BasisQuality, EventType, Transaction


def _row(
    row_id: int,
    event_type: EventType,
    *,
    trade_date: date,
    settlement_date: date | None = None,
    amount: str = "0",
    quantity: str | None = None,
    price: str | None = None,
    instrument_id: int | None = 1,
    cost_basis: str | None = None,
    basis_quality: BasisQuality | None = None,
) -> Transaction:
    row = Transaction(
        natural_key=f"key-{row_id}",
        event_type=event_type,
        trade_date=trade_date,
        settlement_date=settlement_date or trade_date,
        account_id=1,
        instrument_id=instrument_id,
        quantity=None if quantity is None else Decimal(quantity),
        price=None if price is None else Decimal(price),
        amount=Decimal(amount),
        currency="RUB",
        cost_basis=None if cost_basis is None else Decimal(cost_basis),
        basis_quality=basis_quality,
    )
    row.id = row_id
    return row


def test_positions_use_trade_date_and_cash_uses_settlement_date() -> None:
    """Сделка в последний день периода: бумаги уже здесь, деньги ещё нет.

    Ровно этот случай и не сходится, если не различать две даты (спека 3.2).
    """
    rows = [
        _row(
            1,
            EventType.BUY,
            trade_date=date(2025, 8, 31),
            settlement_date=date(2025, 9, 1),
            amount="-15000",
            quantity="50",
            price="300",
        )
    ]
    end_of_month = date(2025, 8, 31)

    assert positions_on(rows, end_of_month)[1].quantity == Decimal(50)
    assert settled_cash(rows, "RUB", end_of_month) == Decimal(0)
    assert settled_cash(rows, "RUB", date(2025, 9, 1)) == Decimal("-15000")


def test_lots_keep_date_and_price() -> None:
    """Лоты нужны для FIFO и для срока владения по ЛДВ — агрегата мало."""
    rows = [
        _row(
            1,
            EventType.OPENING_BALANCE,
            trade_date=date(2025, 7, 31),
            quantity="100",
            cost_basis="255.40",
            basis_quality=BasisQuality.ESTIMATED,
        ),
        _row(
            2,
            EventType.BUY,
            trade_date=date(2025, 8, 5),
            quantity="50",
            price="300.00",
            amount="-15000",
        ),
    ]

    lots = positions_on(rows)[1].lots

    assert [(lot.trade_date, lot.quantity, lot.price) for lot in lots] == [
        (date(2025, 7, 31), Decimal(100), Decimal("255.40")),
        (date(2025, 8, 5), Decimal(50), Decimal("300.00")),
    ]
    assert lots[0].basis_quality == "estimated"


def test_sale_consumes_lots_fifo() -> None:
    rows = [
        _row(1, EventType.BUY, trade_date=date(2025, 8, 1), quantity="10", price="100"),
        _row(2, EventType.BUY, trade_date=date(2025, 8, 2), quantity="10", price="200"),
        _row(3, EventType.SELL, trade_date=date(2025, 8, 3), quantity="-15", amount="3000"),
    ]

    position = positions_on(rows)[1]

    assert position.quantity == Decimal(5)
    assert [(lot.quantity, lot.price) for lot in position.lots] == [(Decimal(5), Decimal(200))]


def test_cash_balances_by_currency() -> None:
    rows = [
        _row(1, EventType.CASH_IN, trade_date=date(2025, 8, 1), amount="1000", instrument_id=None),
        _row(2, EventType.FEE, trade_date=date(2025, 8, 2), amount="-45", instrument_id=None),
    ]
    assert cash_balances(rows) == {"RUB": Decimal("955")}


def test_reconcile_reports_both_directions() -> None:
    rows = [
        _row(1, EventType.CASH_IN, trade_date=date(2025, 8, 1), amount="1000", instrument_id=None),
        _row(
            2,
            EventType.BUY,
            trade_date=date(2025, 8, 2),
            quantity="10",
            price="10",
            amount="-100",
        ),
    ]

    result = reconcile(
        rows,
        [
            ExpectedBalance(kind="cash", quantity=Decimal("900"), currency="RUB", label="RUB"),
            ExpectedBalance(
                kind="security", quantity=Decimal(12), currency="RUB", instrument_id=1, label="SBER"
            ),
        ],
    )

    assert not result.ok
    assert [(item.label, item.difference) for item in result.discrepancies] == [
        ("SBER", Decimal(-2))
    ]


def test_position_absent_from_report_is_a_discrepancy() -> None:
    """Лишняя или задвоенная операция выглядит именно так."""
    rows = [_row(1, EventType.BUY, trade_date=date(2025, 8, 2), quantity="10", amount="-100")]

    result = reconcile(
        rows,
        [ExpectedBalance(kind="cash", quantity=Decimal("-100"), currency="RUB", label="RUB")],
    )

    # Без справочника подписей остаётся номер записи: `domain/` в БД не ходит.
    assert [item.label for item in result.discrepancies] == ["#1"]


def test_position_absent_from_report_is_named_when_labels_are_given() -> None:
    """Голый номер записи не называет бумагу никак, а именно эту строку и
    приходится разбирать. Номер при этом остаётся: когда одна бумага завелась
    в справочнике дважды, различить две строки можно только по нему."""
    rows = [_row(1, EventType.BUY, trade_date=date(2025, 8, 2), quantity="10", amount="-100")]

    result = reconcile(
        rows,
        [ExpectedBalance(kind="cash", quantity=Decimal("-100"), currency="RUB", label="RUB")],
        labels={1: "SBER"},
    )

    assert [item.label for item in result.discrepancies] == ["SBER (#1)"]


def test_cash_tolerance_is_a_kopeck() -> None:
    rows = [
        _row(
            1,
            EventType.CASH_IN,
            trade_date=date(2025, 8, 1),
            amount="1000.01",
            instrument_id=None,
        )
    ]

    result = reconcile(
        rows,
        [ExpectedBalance(kind="cash", quantity=Decimal("1000.00"), currency="RUB", label="RUB")],
        cash_tolerance=Decimal("0.01"),
    )
    assert result.ok


def test_transfer_moves_the_position() -> None:
    """«Ввод ЦБ» и «Вывод ЦБ» из раздела 8.2 меняют количество, а не деньги.

    Перевод бумаг извне — единственный источник позиции, у которого нет
    денежного эффекта. Пропущенный проекцией, он даёт расхождение, выглядящее
    как потерянная операция: строка в журнале есть, в количестве её нет.
    """
    rows = [
        _row(1, EventType.TRANSFER, trade_date=date(2024, 3, 5), quantity="4"),
        _row(2, EventType.TRANSFER, trade_date=date(2024, 3, 20), quantity="-1"),
    ]

    positions = positions_on(rows, date(2024, 3, 31))

    assert positions[1].quantity == Decimal(3)


def test_cash_transfer_without_instrument_is_not_a_position() -> None:
    """Тот же тип без бумаги позицией не становится: строка без инструмента
    пропускается, а не заводит позицию с пустым ключом."""
    rows = [_row(1, EventType.TRANSFER, trade_date=date(2024, 3, 5), instrument_id=None)]

    assert positions_on(rows, date(2024, 3, 31)) == {}
