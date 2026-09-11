"""Импорт: идемпотентность, перекрытие периодов, частичное исполнение.

Регрессия на весь контур целиком: импорт эталонного отчёта обязан давать нулевое
расхождение остатков (спека 5.5).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from conftest import FIXTURES
from portfolio.db import get_session_factory
from portfolio.domain.positions import cash_balances, positions_on
from portfolio.jobs.import_broker import import_broker_report
from portfolio.jobs.import_csv import import_csv_file
from portfolio.models import Instrument, RawReport, Transaction

AUGUST = FIXTURES / "report_2025-08.html"
OVERLAPPING = FIXTURES / "report_2025-08-16_09-15.html"


@pytest.fixture
def opening_balances(tmp_path: Path) -> Path:
    """Начальные остатки: без них позиции уйдут в минус на первой же продаже."""
    path = tmp_path / "opening_balances.csv"
    path.write_text(
        "id,account,date,ticker,isin,quantity,cost_basis,basis_quality,currency,note\n"
        "ob-001,12345-АБВ,2025-07-31,,,50000.00,,known,RUB,остаток денег\n"
        "ob-002,12345-АБВ,2025-07-31,SBER,RU0009029540,100,255.40,estimated,RUB,средняя\n"
        "ob-003,12345-АБВ,2025-07-31,SU26238RMFS4,RU000A1038V6,10,,unknown,RUB,нет цены\n",
        encoding="utf-8",
    )
    return path


def _count(kind: type[Transaction] | type[Instrument]) -> int:
    with get_session_factory()() as session:
        return session.scalar(select(func.count()).select_from(kind)) or 0


def test_import_reconciles_against_report(database: Path, opening_balances: Path) -> None:
    import_csv_file(opening_balances)
    result = import_broker_report(AUGUST)

    assert result.unparsed == ()
    assert result.reconcile.discrepancies == ()
    assert result.committed
    assert result.written == 15


def test_second_import_changes_nothing(database: Path, opening_balances: Path) -> None:
    """Двойной импорт одного файла не меняет журнал."""
    import_csv_file(opening_balances)
    import_broker_report(AUGUST)
    before = _count(Transaction)

    again = import_broker_report(AUGUST)

    assert again.is_duplicate  # хеш совпал, сырьё не сохраняется повторно
    assert again.diff.summary() == {"new": 0, "unchanged": 15, "reversals": 0}
    assert _count(Transaction) == before


def test_overlapping_periods(database: Path, opening_balances: Path) -> None:
    """Два отчёта с перекрывающимся периодом дают корректный результат.

    Индекс в `natural_key` считается по календарному дню, а не по отчёту, —
    иначе повторённые в обоих отчётах операции задвоятся (спека 3.2).
    """
    import_csv_file(opening_balances)
    import_broker_report(AUGUST)

    result = import_broker_report(OVERLAPPING)

    assert result.diff.summary() == {"new": 2, "unchanged": 8, "reversals": 0}
    assert result.reconcile.ok

    with get_session_factory()() as session:
        rows = list(session.scalars(select(Transaction)))
        assert cash_balances(rows)["RUB"] == Decimal("54939.81")
        quantities = {
            session.get(Instrument, instrument_id).ticker: position.quantity
            for instrument_id, position in positions_on(rows).items()
        }
    assert quantities == {"SBER": Decimal(100), "SU26238RMFS4": Decimal(30), "LKOH": Decimal(10)}


def test_partial_fill_is_two_rows_in_the_ledger(database: Path, opening_balances: Path) -> None:
    """Ключ без различителя молча потерял бы вторую часть заявки (спека 3.2)."""
    import_csv_file(opening_balances)
    import_broker_report(AUGUST)

    with get_session_factory()() as session:
        lkoh = session.scalar(select(Instrument).where(Instrument.ticker == "LKOH"))
        rows = list(
            session.scalars(
                select(Transaction).where(
                    Transaction.instrument_id == lkoh.id,
                    Transaction.event_type == "BUY",
                )
            )
        )

    assert len(rows) == 2
    assert len({row.natural_key for row in rows}) == 2
    assert sum(row.quantity for row in rows) == Decimal(10)


def test_dry_run_writes_nothing(database: Path, opening_balances: Path) -> None:
    import_csv_file(opening_balances)
    before = _count(Transaction)

    result = import_broker_report(AUGUST, dry_run=True)

    assert result.diff.summary()["new"] == 15
    assert result.written == 0
    assert not result.committed
    assert _count(Transaction) == before


def test_import_without_opening_balances_fails_reconciliation(database: Path) -> None:
    """Без начальных остатков сверка не сходится — и импорт не подтверждается."""
    result = import_broker_report(AUGUST)

    assert not result.reconcile.ok
    assert not result.committed
    assert _count(Transaction) == 0

    labels = {item.label for item in result.reconcile.discrepancies}
    assert "RUB" in labels and "SBER" in labels


def test_force_records_the_reason(database: Path) -> None:
    """Подтверждение при несошедшейся сверке требует флага и пишет причину."""
    result = import_broker_report(AUGUST, force=True)

    assert result.committed
    assert not result.reconcile.ok
    assert _count(Transaction) == 15

    with get_session_factory()() as session:
        report = session.scalar(select(RawReport))
    assert report is not None
    assert report.note is not None
    assert "несошедшейся сверке" in report.note
