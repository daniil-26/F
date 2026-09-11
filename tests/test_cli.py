"""CLI: команды работают, коды возврата корректны (STAGE-1, T10)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FIXTURES
from portfolio.cli import EXIT_DISCREPANCY, EXIT_INPUT_ERROR, app

runner = CliRunner()

AUGUST = FIXTURES / "report_2025-08.html"
SEPTEMBER = FIXTURES / "report_2025-08-16_09-15.html"


@pytest.fixture
def opening_balances(tmp_path: Path) -> Path:
    path = tmp_path / "opening_balances.csv"
    path.write_text(
        "id,account,date,ticker,isin,quantity,cost_basis,basis_quality,currency,note\n"
        "ob-001,12345-АБВ,2025-07-31,,,50000.00,,known,RUB,остаток денег\n"
        "ob-002,12345-АБВ,2025-07-31,SBER,RU0009029540,100,255.40,estimated,RUB,средняя\n"
        "ob-003,12345-АБВ,2025-07-31,SU26238RMFS4,RU000A1038V6,10,,unknown,RUB,нет цены\n",
        encoding="utf-8",
    )
    return path


def test_check_returns_zero_after_every_month(database: Path, opening_balances: Path) -> None:
    """Требование «после каждого месяца» важнее остальных (STAGE-1).

    Сверка только на последнем отчёте маскирует взаимно компенсирующиеся ошибки.
    """
    assert runner.invoke(app, ["import-csv", str(opening_balances)]).exit_code == 0

    assert runner.invoke(app, ["import-broker", str(AUGUST)]).exit_code == 0
    assert runner.invoke(app, ["check"]).exit_code == 0

    assert runner.invoke(app, ["import-broker", str(SEPTEMBER)]).exit_code == 0
    result = runner.invoke(app, ["check"])

    assert result.exit_code == 0
    assert "Сверка сошлась" in result.stdout


def test_check_returns_nonzero_on_discrepancy(database: Path) -> None:
    """Ненулевой код возврата — это уведомление от cron (спека 6)."""
    assert runner.invoke(app, ["import-broker", str(AUGUST), "--force"]).exit_code != 0

    result = runner.invoke(app, ["check"])

    assert result.exit_code == EXIT_DISCREPANCY
    assert "Расхождения" in result.stdout


def test_inbox_lists_discrepancies(database: Path) -> None:
    runner.invoke(app, ["import-broker", str(AUGUST), "--force"])

    result = runner.invoke(app, ["inbox"])

    assert result.exit_code == EXIT_DISCREPANCY
    assert "Входящие" in result.stdout


def test_inbox_is_empty_when_everything_reconciles(
    database: Path, opening_balances: Path
) -> None:
    runner.invoke(app, ["import-csv", str(opening_balances)])
    runner.invoke(app, ["import-broker", str(AUGUST)])

    result = runner.invoke(app, ["inbox"])

    assert result.exit_code == 0
    assert "Входящие пусты" in result.stdout


def test_dry_run_prints_changes_and_writes_nothing(
    database: Path, opening_balances: Path
) -> None:
    runner.invoke(app, ["import-csv", str(opening_balances)])

    result = runner.invoke(app, ["import-broker", str(AUGUST), "--dry-run"])

    assert result.exit_code == 0
    assert "--dry-run" in result.stdout
    assert runner.invoke(app, ["check"]).exit_code == 0  # журнал не изменился


def test_broken_csv_returns_input_error(database: Path, tmp_path: Path) -> None:
    path = tmp_path / "opening_balances.csv"
    path.write_text(
        "id,account,date,ticker,isin,quantity,cost_basis,basis_quality,currency,note\n"
        "ob-001,12345-АБВ,2025-07-31,,,не число,,known,RUB,\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["import-csv", str(path)])

    assert result.exit_code == EXIT_INPUT_ERROR
    assert "поле quantity" in result.stdout
