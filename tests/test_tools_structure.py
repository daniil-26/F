"""Проверка инвентаря структуры отчётов (`tools/report_structure.py`).

Инвентарь — инструмент шага 0: по нему собирается список типов операций «за всю
историю», а не за последний месяц. Если он молча теряет секцию или считает
строку «Итого» видом сделки, шаг 0 даст неверную картину, и `mapping_v*` будет
написан под неё.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from _report_grid import load_grid  # noqa: E402
from report_structure import collect, main  # noqa: E402

RAW = Path(__file__).parent / "fixtures" / "tools" / "broker_raw_sample.html"
SYNTHETIC = Path(__file__).parent / "fixtures" / "report_2025-08.html"


def test_sections_are_found() -> None:
    inventory = collect(RAW)
    titles = [section.title for section in inventory.sections]

    assert "1. Состояние денежных средств на счете" in titles
    assert "2. Состояние портфеля ценных бумаг" in titles
    assert "5.1 Биржевые сделки с ценными бумагами" in titles
    assert "8.1 Неторговые операции с ДС" in titles


def test_operation_vocabulary_is_collected() -> None:
    """Тот самый список, который нужен шагу 0."""
    inventory = collect(RAW)

    assert set(inventory.values["operation_type"]) == {
        "Вывод ДС",
        "Доход по финансовым инструментам",
        "Налог",
        "Погашение купона",
    }
    assert set(inventory.values["trade_kind"]) == {"Покупка", "Продажа"}


def test_total_rows_do_not_become_vocabulary() -> None:
    inventory = collect(RAW)
    for values in inventory.values.values():
        assert not any(value.lower().startswith(("итого", "оборот")) for value in values)


def test_decimal_places_are_reported() -> None:
    """Цена приходит с четырьмя знаками, суммы с двумя: парсер обязан различать."""
    assert collect(RAW).decimals == [0, 2, 4]


def test_period_is_found() -> None:
    assert "01.08.2025" in (collect(RAW).period or "")


def test_anonymized_reports_are_flagged() -> None:
    """Предохранитель: обезличенный отчёт не годится для проверки арифметики,
    необезличенный — для коммита."""
    assert collect(RAW).anonymized is False


def test_merged_headers_align_with_data() -> None:
    """Две строки шапки и объединённые ячейки сдвигают данные относительно
    заголовков: «Место совершения сделки» стоит над «Дата поставки фактическая»,
    если не разворачивать colspan."""
    grid = load_grid(RAW)
    venue_cells = [
        cell
        for cell in grid.unique_cells()
        if grid.header_of(cell) == "venue" and "биржа" in cell.text.lower()
    ]
    assert len(venue_cells) == 2


def test_works_on_the_other_broker_layout() -> None:
    """Синтетическая фикстура устроена иначе — секции отдельными таблицами.
    Инвентарь не должен падать на другой раскладке."""
    inventory = collect(SYNTHETIC)
    assert inventory.rows > 0
    assert inventory.values.get("currency") == ["RUB"]


def test_cli_compare_needs_two_files(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["compare", str(RAW)]) == 2
    assert main(["compare", str(RAW), str(SYNTHETIC)]) == 0
    out = capsys.readouterr().out
    assert "различий" in out
