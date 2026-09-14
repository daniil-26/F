"""Проверка инвентаря структуры отчётов (`tools/report_structure.py`).

Инвентарь — инструмент шага 0: по нему собирается список типов операций «за всю
историю», а не за последний месяц. Если он молча теряет секцию или считает
строку «Итого» видом сделки, шаг 0 даст неверную картину, и `mapping_v*` будет
написан под неё.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from _report_grid import header_name, load_grid  # noqa: E402
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


# --- находки на архиве из 14 отчётов ----------------------------------------


def test_three_level_section_numbering() -> None:
    """В отчётах с 2019 года появился третий уровень: «8.1.1 Зачислено/списано
    ДС по неторговым операциям». Без него подразделы 8.1.1 и 8.2 сливаются в
    один список, и прореживание считает их одной таблицей.
    """
    report = """<html><body><table>
     <tr><td colspan="3">8. Неторговые операции</td></tr>
     <tr><td colspan="3">8.1.1 Зачислено/списано ДС по неторговым операциям</td></tr>
     <tr><td>Дата</td><td>Тип операции</td><td>Сумма</td><td>Валюта</td></tr>
     <tr><td>05.08.2025</td><td>Погашение купона</td><td>1 234.56</td><td>RUR</td></tr>
     <tr><td colspan="3">8.2 Неторговые операции с ЦБ</td></tr>
     <tr><td>Дата</td><td>Тип операции</td><td>Наименование ЦБ</td><td>ISIN</td>
         <td>Количество ЦБ</td></tr>
     <tr><td>07.08.2025</td><td>Конвертация ЦБ</td><td>Бумага</td><td>RU000A105X64</td>
         <td>10</td></tr>
    </table></body></html>"""
    path = Path(__file__).parent / "fixtures" / "tools" / "_three_level.html"
    path.write_bytes(report.encode("utf-8"))
    try:
        inventory = collect(path)
        titles = [section.title for section in inventory.sections]
    finally:
        path.unlink()

    assert "8.1.1 Зачислено/списано ДС по неторговым операциям" in titles
    assert "8.2 Неторговые операции с ЦБ" in titles


def test_price_and_amount_currency_are_separate_columns() -> None:
    """Еврооблигация котируется в USD, а рассчитывается в рублях.

    Одна колонка «валюта» на обе означала бы, что сделка в долларах спишет
    доллары, — а списывает она рубли.
    """
    assert header_name("Валюта цены") == "price_currency"
    assert header_name("Валюта суммы сделки") == "amount_currency"
    assert header_name("Валюта брокерской комиссии") == "fee_currency"
    assert header_name("Валюта") == "currency"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("НКД", "accrued_int"),
        ("Брокерская комиссия", "fee_broker"),
        ("Комиссия ТС", "fee_exchange"),
        ("Гербовый сбор", "fee_stamp"),
        ("Зачислено ЦБ, шт.", "quantity_in"),
        ("Списано ЦБ, шт.", "quantity_out"),
        ("Доля ЦБ в портфеле, %", "share"),
        ("% по сделке", "rate"),
        ("Тип незавершенной сделки", "trade_kind"),
        ("Тип сделки РЕПО", "trade_kind"),
    ],
)
def test_columns_found_in_the_archive_are_recognized(header: str, expected: str) -> None:
    """Колонки, которых не было в первом разобранном отчёте: комиссии трёх
    видов, НКД, движения количества, ставка по сделке."""
    assert header_name(header) == expected


def test_operation_vocabulary_is_split_by_section() -> None:
    """«Погашение облигации» встречается и в денежной секции, и в бумажной.

    Это два разных события журнала: выплата и списание бумаги. Общий список без
    разбивки по секциям их путает.
    """
    report = """<html><body><table>
     <tr><td colspan="3">8.1.1 Зачислено/списано ДС по неторговым операциям</td></tr>
     <tr><td>Дата</td><td>Тип операции</td><td>Сумма</td><td>Валюта</td></tr>
     <tr><td>05.08.2025</td><td>Погашение облигации</td><td>1 000.00</td><td>RUR</td></tr>
     <tr><td colspan="3">8.2 Неторговые операции с ЦБ</td></tr>
     <tr><td>Дата</td><td>Тип операции</td><td>Наименование ЦБ</td><td>ISIN</td>
         <td>Количество ЦБ</td></tr>
     <tr><td>05.08.2025</td><td>Погашение облигации</td><td>Бумага</td>
         <td>RU000A105X64</td><td>10</td></tr>
    </table></body></html>"""
    path = Path(__file__).parent / "fixtures" / "tools" / "_two_sides.html"
    path.write_bytes(report.encode("utf-8"))
    try:
        inventory = collect(path)
        by_title = {section.title: section for section in inventory.sections}
    finally:
        path.unlink()

    money = by_title["8.1.1 Зачислено/списано ДС по неторговым операциям"]
    securities = by_title["8.2 Неторговые операции с ЦБ"]

    assert money.values["operation_type"] == ["Погашение облигации"]
    assert securities.values["operation_type"] == ["Погашение облигации"]


def test_parse_shows_what_the_parser_made_of_the_report(capsys: pytest.CaptureFixture[str]) -> None:
    """`dump` показывает форму документа, `parse` — результат разбора.

    При расхождении сверки нужно второе: расхождение объясняется не тем, как
    устроен отчёт, а тем, во что превратились его строки.
    """
    v2 = Path(__file__).parent / "fixtures" / "report_v2_2024-03.html"

    assert main(["parse", str(v2)]) == 0
    out = capsys.readouterr().out

    assert "версия парсера: v2" in out
    # Итоги по типам — то, с чем сравнивается расхождение денег.
    assert "итоги по типам операций" in out
    assert "движение денег за период" in out
    # Комиссии различаются видом: одной строкой `FEE` расхождение не разобрать.
    assert "FEE·BROKER" in out
    assert "FEE·EXCHANGE" in out
    # Три подсказки отвечают за три незакрытых допущения.
    assert "A-07" in out and "A-04" in out and "A-06" in out
    assert "нераспознанные строки: 0" in out


def test_parse_reconciles_cash_against_section_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Сводка сводит движение журнала с разложением остатка по разделу 1.

    Это и есть ответ на вопрос «куда делись деньги»: раздел 1 печатает
    свободные средства и комиссии по видам независимо от нашего разбора,
    поэтому расхождение локализуется до категории, а не до отчёта целиком.
    """
    v2 = Path(__file__).parent / "fixtures" / "report_v2_2024-03.html"

    assert main(["parse", str(v2)]) == 0
    out = capsys.readouterr().out

    assert "разложение остатка по отчёту (раздел 1)" in out
    # Итог сходится: 100 000.00 + 3 919.00 = 103 919.00.
    assert "РАСХОЖДЕНИЕ" in out
    line = next(item for item in out.splitlines() if "РАСХОЖДЕНИЕ" in item)
    assert line.split()[-1] == "0.00"
    # Комиссии сверяются по видам против строк раздела 1.
    assert "комиссия Брокера" in out and "комиссия торговой системы" in out


def test_cash_summary_counts_a_repeated_trade_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Сделка печатается и в 5.1, и в 5.10 (A-22).

    Журнал схлопывает повтор ключом идемпотентности, и сводка обязана делать
    то же: сложенная дважды покупка дала бы расхождение на верном разборе.
    """
    v2 = Path(__file__).parent / "fixtures" / "report_v2_2024-03.html"

    main(["parse", str(v2)])
    out = capsys.readouterr().out

    movement = next(item for item in out.splitlines() if "движение по журналу" in item)
    assert movement.split()[-1] == "3919.00"
