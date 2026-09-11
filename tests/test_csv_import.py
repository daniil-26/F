"""Декларативные CSV: валидация, дифф по `id`, сторно при правке."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from portfolio.adapters.csv_input import CsvValidationError, read_opening_balances
from portfolio.db import get_session_factory
from portfolio.jobs.import_csv import import_csv_file
from portfolio.models import EventType, Transaction

HEADER = "id,account,date,ticker,isin,quantity,cost_basis,basis_quality,currency,note\n"
ROW_CASH = "ob-001,12345-АБВ,2025-07-31,,,50000.00,,known,RUB,остаток денег\n"
ROW_SBER = "ob-002,12345-АБВ,2025-07-31,SBER,RU0009029540,100,255.40,estimated,RUB,средняя\n"


def _write(path: Path, *rows: str) -> Path:
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return path


def _ledger() -> list[Transaction]:
    with get_session_factory()() as session:
        return list(session.scalars(select(Transaction).order_by(Transaction.id)))


def test_validation_reports_line_and_field(tmp_path: Path) -> None:
    """«строка 14, поле rate: ожидалось число» — формат сообщений из спеки 6."""
    path = _write(
        tmp_path / "opening_balances.csv",
        "ob-001,12345-АБВ,2025-07-31,,,не число,,known,RUB,\n",
    )

    with pytest.raises(CsvValidationError) as error:
        read_opening_balances(path)

    assert "строка 2, поле quantity" in error.value.problems[0]


def test_duplicate_id_is_rejected(tmp_path: Path) -> None:
    """Дифф идёт по `id`: повтор ключа делает его бессмысленным."""
    path = _write(tmp_path / "opening_balances.csv", ROW_CASH, ROW_CASH)

    with pytest.raises(CsvValidationError) as error:
        read_opening_balances(path)

    assert "уже встречался" in error.value.problems[0]


def test_unknown_basis_quality_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "opening_balances.csv",
        "ob-001,12345-АБВ,2025-07-31,,,100,,примерно,RUB,\n",
    )

    with pytest.raises(CsvValidationError) as error:
        read_opening_balances(path)

    assert "basis_quality" in error.value.problems[0]


def test_first_column_must_be_id(tmp_path: Path) -> None:
    path = tmp_path / "opening_balances.csv"
    path.write_text("account,date,quantity,cost_basis,basis_quality\n", encoding="utf-8")

    with pytest.raises(CsvValidationError) as error:
        read_opening_balances(path)

    assert "первая колонка" in error.value.problems[0]


def test_import_writes_opening_balances(database: Path, tmp_path: Path) -> None:
    path = _write(tmp_path / "opening_balances.csv", ROW_CASH, ROW_SBER)

    result = import_csv_file(path)

    assert result.diff.summary() == {"new": 2, "unchanged": 0, "reversals": 0}
    rows = _ledger()
    assert [row.event_type for row in rows] == [EventType.OPENING_BALANCE] * 2
    assert rows[0].amount == Decimal("50000.00")     # деньги в amount
    assert rows[1].quantity == Decimal(100)          # бумаги в quantity
    assert rows[1].amount == Decimal(0)              # бумажный остаток денег не двигает
    assert rows[1].cost_basis == Decimal("255.40")


def test_reimport_without_changes_writes_nothing(database: Path, tmp_path: Path) -> None:
    path = _write(tmp_path / "opening_balances.csv", ROW_CASH, ROW_SBER)
    import_csv_file(path)

    result = import_csv_file(path)

    assert result.diff.summary() == {"new": 0, "unchanged": 2, "reversals": 0}
    assert len(_ledger()) == 2


def test_edited_row_gives_reversal_plus_new_record(database: Path, tmp_path: Path) -> None:
    """Правка строки — сторно плюс новая запись, а не удаление (спека 6)."""
    path = _write(tmp_path / "opening_balances.csv", ROW_CASH, ROW_SBER)
    import_csv_file(path)

    _write(
        path,
        ROW_CASH,
        "ob-002,12345-АБВ,2025-07-31,SBER,RU0009029540,120,255.40,estimated,RUB,средняя\n",
    )
    result = import_csv_file(path)

    assert result.diff.summary() == {"new": 0, "unchanged": 1, "reversals": 1}

    rows = _ledger()
    assert len(rows) == 4
    reversal = rows[2]
    replacement = rows[3]
    assert reversal.reverses_id == rows[1].id
    assert reversal.quantity == Decimal(-100)      # сторно гасит оригинал
    assert replacement.quantity == Decimal(120)
    assert reversal.natural_key != replacement.natural_key != rows[1].natural_key
    assert rows[1].quantity == Decimal(100)        # оригинал не тронут: журнал append-only


def test_removed_row_is_reversed_not_deleted(database: Path, tmp_path: Path) -> None:
    path = _write(tmp_path / "opening_balances.csv", ROW_CASH, ROW_SBER)
    import_csv_file(path)

    _write(path, ROW_CASH)
    result = import_csv_file(path)

    assert result.diff.summary() == {"new": 0, "unchanged": 1, "reversals": 1}
    rows = _ledger()
    assert len(rows) == 3
    assert rows[2].reverses_id == rows[1].id
    assert rows[1].quantity + rows[2].quantity == Decimal(0)


def test_cash_flows_sign_comes_from_event_type(database: Path, tmp_path: Path) -> None:
    path = tmp_path / "cash_flows.csv"
    path.write_text(
        "id,account,date,kind,amount,currency,note\n"
        "cf-001,12345-АБВ,2025-08-01,CASH_IN,100000,RUB,довнесение\n"
        "cf-002,12345-АБВ,2025-08-29,CASH_OUT,20000,RUB,изъятие\n",
        encoding="utf-8",
    )

    import_csv_file(path)

    rows = _ledger()
    assert [row.amount for row in rows] == [Decimal("100000"), Decimal("-20000")]


def test_dry_run_writes_nothing(database: Path, tmp_path: Path) -> None:
    path = _write(tmp_path / "opening_balances.csv", ROW_CASH, ROW_SBER)

    result = import_csv_file(path, dry_run=True)

    assert result.diff.summary()["new"] == 2
    assert result.written == 0
    assert _ledger() == []
