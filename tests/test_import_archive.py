"""Импорт архива: каталог целиком, в хронологическом порядке (STAGE-1, T11).

Архив за несколько лет — это сотни файлов с именами вида `report (12).html`.
Порядок импорта здесь не косметика: позиции и остатки накопительны, и отчёт за
март, применённый после майского, даст верный итог и неверную историю.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FIXTURES
from portfolio.adapters.files import discover_reports
from portfolio.cli import EXIT_DISCREPANCY, EXIT_INPUT_ERROR, app
from portfolio.jobs.import_broker import import_broker_archive

runner = CliRunner()

AUGUST = FIXTURES / "report_2025-08.html"
SEPTEMBER = FIXTURES / "report_2025-08-16_09-15.html"
OPENING_BALANCES = (
    "id,account,date,ticker,isin,quantity,cost_basis,basis_quality,currency,note\n"
    "ob-001,12345-АБВ,2025-07-31,,,50000.00,,known,RUB,остаток денег\n"
    "ob-002,12345-АБВ,2025-07-31,SBER,RU0009029540,100,255.40,estimated,RUB,средняя\n"
    "ob-003,12345-АБВ,2025-07-31,SU26238RMFS4,RU000A1038V6,10,,unknown,RUB,нет цены\n"
)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """Каталог, в котором алфавитный порядок имён обратен хронологическому.

    Иначе тест на порядок проходил бы и на случайной сортировке.
    """
    directory = tmp_path / "архив"
    directory.mkdir()
    shutil.copy(SEPTEMBER, directory / "a_второй.html")
    shutil.copy(AUGUST, directory / "z_первый.html")
    return directory


@pytest.fixture
def opened(tmp_path: Path, database: Path) -> None:
    """Входящие остатки: без них не сходится уже первый отчёт."""
    path = tmp_path / "opening_balances.csv"
    path.write_text(OPENING_BALANCES, encoding="utf-8")
    assert runner.invoke(app, ["import-csv", str(path)]).exit_code == 0


def test_directory_is_imported_in_chronological_order(archive: Path, opened: None) -> None:
    result = import_broker_archive([archive])

    assert [item.period_start.isoformat() for item in result.results] == [
        "2025-08-01",
        "2025-08-16",
    ]
    assert result.ok
    assert result.committed == 2


def test_order_comes_from_the_report_not_the_file_name(archive: Path, opened: None) -> None:
    """Имена в архиве бывают вида `report (12).html` — по ним порядка не собрать."""
    for stale in ("a_второй.html", "z_первый.html"):
        (archive / stale).unlink()
    shutil.copy(SEPTEMBER, archive / "report (3).html")
    shutil.copy(AUGUST, archive / "report (7).html")

    result = import_broker_archive([archive])

    assert [Path(item.source).name for item in result.results] == [
        "report (7).html",
        "report (3).html",
    ]


def test_stops_on_first_report_that_does_not_reconcile(archive: Path, database: Path) -> None:
    """Без входящих остатков не сходится уже первый отчёт — второй не трогаем.

    Импорт следующего месяца поверх несошедшегося предыдущего накапливает
    расхождение и запутывает разбор.
    """
    result = import_broker_archive([archive])

    assert len(result.results) == 1
    assert not result.ok
    assert result.committed == 0
    assert result.stopped_early


def test_keep_going_imports_the_rest(archive: Path, database: Path) -> None:
    result = import_broker_archive([archive], stop_on_error=False)

    assert len(result.results) == 2
    assert not result.ok
    assert not result.stopped_early


def test_unreadable_report_is_reported_not_raised(tmp_path: Path, database: Path) -> None:
    directory = tmp_path / "битые"
    directory.mkdir()
    (directory / "мусор.html").write_text("<html><body>не отчёт</body></html>", encoding="utf-8")

    result = import_broker_archive([directory])

    assert result.results == ()
    assert len(result.failures) == 1
    assert result.failures[0][0].name == "мусор.html"
    assert not result.ok
    # Пропускать было нечего: «остальные не импортированы» здесь не про что.
    assert not result.stopped_early


def test_nested_directories_need_recursive(tmp_path: Path, database: Path) -> None:
    root = tmp_path / "архив"
    (root / "2025").mkdir(parents=True)
    shutil.copy(AUGUST, root / "2025" / "август.html")

    assert discover_reports([root]).files == ()
    assert len(discover_reports([root], recursive=True).files) == 1


def test_pattern_narrows_the_selection(archive: Path) -> None:
    found = discover_reports([archive], pattern="z_*.html")

    assert [path.name for path in found.files] == ["z_первый.html"]


def test_non_reports_are_skipped(archive: Path) -> None:
    (archive / "заметки.txt").write_text("не отчёт", encoding="utf-8")

    found = discover_reports([archive])

    assert all(path.suffix == ".html" for path in found.files)


def test_missing_path_is_noted_not_silently_dropped(tmp_path: Path) -> None:
    found = discover_reports([tmp_path / "нет-такого.html"])

    assert found.files == ()
    assert found.notes and "нет" in found.notes[0]


def test_cli_imports_a_directory(archive: Path, opened: None) -> None:
    result = runner.invoke(app, ["import-broker", str(archive)])

    assert result.exit_code == 0
    assert "Импорт архива" in result.stdout


def test_cli_says_the_rest_of_the_archive_was_left_alone(
    archive: Path, database: Path
) -> None:
    """Оборванный прогон печатается как архив, а не как одиночный отчёт.

    Иначе из вывода не видно, что остальные файлы каталога остались нетронутыми.
    """
    result = runner.invoke(app, ["import-broker", str(archive)])

    assert result.exit_code == EXIT_DISCREPANCY
    assert "Импорт архива" in result.stdout
    assert "--keep-going" in result.stdout


def test_cli_reports_empty_directory(tmp_path: Path, database: Path) -> None:
    empty = tmp_path / "пусто"
    empty.mkdir()

    result = runner.invoke(app, ["import-broker", str(empty)])

    assert result.exit_code == EXIT_INPUT_ERROR
    assert "Не найдено ни одного отчёта" in result.stdout


def test_cli_single_file_output_is_unchanged(database: Path) -> None:
    """Один файл — прежний подробный вывод, а не таблица архива."""
    result = runner.invoke(app, ["import-broker", str(AUGUST)])

    assert result.exit_code == EXIT_DISCREPANCY
    assert "Дифф с журналом" in result.stdout
    assert "Импорт архива" not in result.stdout


def test_cli_accepts_several_files(archive: Path, opened: None) -> None:
    result = runner.invoke(
        app,
        ["import-broker", str(archive / "z_первый.html"), str(archive / "a_второй.html")],
    )

    assert result.exit_code == 0
    assert "Отчётов: 2" in result.stdout
