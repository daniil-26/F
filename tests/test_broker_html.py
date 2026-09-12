"""Golden-тесты парсера на фикстурах. Ни БД, ни сети.

Фикстуры обезличены. Каждый найденный на архиве баг превращается в новую строку
фикстуры — это самый ценный тест в проекте (спека 5.5).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from conftest import FIXTURES
from golden import load, report_to_dict, tables_to_dict
from portfolio.adapters.broker.anchors import Section, find_sections, resolve_columns
from portfolio.adapters.broker.mapping_v1 import parse, parse_report
from portfolio.adapters.broker.tables import extract_tables

REPORTS = sorted(FIXTURES.glob("*.html"))


@pytest.mark.parametrize("report", REPORTS, ids=lambda path: path.stem)
def test_tables_shape_matches_golden(report: Path) -> None:
    """Стадия 1: количество таблиц, заголовки, размерности.

    Меняться дальше почти не будет: структурная стадия зависит только от движка
    отчётов брокера (спека 4.1).
    """
    actual = tables_to_dict(extract_tables(report.read_bytes()))
    assert actual == load(report.with_suffix(".tables.json"))


@pytest.mark.parametrize("report", REPORTS, ids=lambda path: path.stem)
def test_parsed_report_matches_golden(report: Path) -> None:
    actual = report_to_dict(parse(report.read_bytes()))
    assert actual == load(report.with_suffix(".expected.json"))


@pytest.mark.parametrize("report", REPORTS, ids=lambda path: path.stem)
def test_nothing_unrecognized(report: Path) -> None:
    """Критерий готовности этапа: список нераспознанного пуст на всех отчётах."""
    _, _, unparsed = parse_report(report.read_bytes())
    assert unparsed == []


@pytest.mark.parametrize("report", REPORTS, ids=lambda path: path.stem)
def test_report_balances_agree_with_operations(report: Path) -> None:
    """Сумма денежных эффектов сходится с движением остатка в самом отчёте.

    Проверка внутренней согласованности фикстуры: если она не выполняется,
    сверка на импорте провалится, и разбираться придётся уже в БД.
    """
    parsed = parse(report.read_bytes())
    tables = extract_tables(report.read_bytes())
    cash_tables = find_sections(tables)[Section.CASH_BALANCES]

    opening = _opening_cash(cash_tables[0])
    closing = next(balance.quantity for balance in parsed.balances if balance.kind == "cash")
    movement = sum((operation.amount for operation in parsed.operations), Decimal(0))

    assert opening + movement == closing


def test_encoding_is_taken_from_the_document() -> None:
    """Отчёт в cp1251 разбирается так же, как в utf-8: кириллица не ломается."""
    parsed = parse((FIXTURES / "report_2019-03.html").read_bytes())
    assert parsed.account_code == "12345-АБВ"
    assert [operation.ticker for operation in parsed.operations][:2] == ["SBER", "SBER"]


def test_partial_fill_becomes_two_operations() -> None:
    """Заявка, исполненная двумя частями по одной цене в один день (спека 3.2)."""
    parsed = parse((FIXTURES / "report_2025-08.html").read_bytes())
    lkoh = [
        operation
        for operation in parsed.operations
        if operation.ticker == "LKOH" and operation.kind == "BUY"
    ]
    assert len(lkoh) == 2
    assert {operation.broker_trade_no for operation in lkoh} == {"1004", "1005"}
    assert all(operation.quantity == Decimal(5) for operation in lkoh)


def test_fees_are_split_by_kind_not_summed() -> None:
    """Комиссии разбираются по типам, а не сводятся в одну сумму (спека 4.1)."""
    parsed = parse((FIXTURES / "report_2025-08.html").read_bytes())
    kinds = {
        operation.fee_kind for operation in parsed.operations if operation.kind == "FEE"
    }
    assert kinds == {"BROKER", "DEPOSITARY"}


def test_coupon_is_recorded_gross_with_separate_tax() -> None:
    """Купон в журнал до налога, удержание — отдельным событием (спека 3.2)."""
    parsed = parse((FIXTURES / "report_2025-08.html").read_bytes())
    coupon = next(op for op in parsed.operations if op.kind == "COUPON")
    tax = next(op for op in parsed.operations if op.kind == "TAX")

    assert coupon.amount == Decimal("282.50")
    assert tax.amount == Decimal("-32.50")
    assert coupon.amount + tax.amount == Decimal("250.00")  # столько пришло на счёт
    assert coupon.withheld_at_source is False  # gross восстановлен, а не угадан


def test_bond_trade_includes_accrued_interest_in_cash_effect() -> None:
    parsed = parse((FIXTURES / "report_2025-08.html").read_bytes())
    bond = next(op for op in parsed.operations if op.ticker == "SU26238RMFS4" and op.kind == "BUY")

    assert bond.accrued_int == Decimal("123.45")
    assert bond.amount == Decimal("-10529.45")  # A-07: сумма сделки плюс НКД


def test_total_rows_are_dropped() -> None:
    parsed = parse((FIXTURES / "report_2025-08.html").read_bytes())
    assert all(
        "итого" not in (operation.note or "").lower() for operation in parsed.operations
    )
    assert len([op for op in parsed.operations if op.kind in {"BUY", "SELL"}]) == 5


def _opening_cash(table) -> Decimal:  # type: ignore[no-untyped-def]
    from portfolio.adapters.broker.anchors import resolve_columns
    from portfolio.adapters.formats import parse_decimal

    columns = resolve_columns(table)
    row = table.as_dicts()[0]
    return parse_decimal(row[columns["opening_balance"]])


# --- объединённые ячейки ------------------------------------------------------

MERGED = """<html><body><table>
 <tr>
  <th rowspan="2">Номер сделки</th><th rowspan="2">Количество ЦБ, шт.</th>
  <th colspan="2">Дата оплаты</th><th colspan="2">Дата поставки</th>
  <th rowspan="2">Место совершения сделки</th>
 </tr>
 <tr><th>Плановая</th><th>Фактическая</th><th>Плановая</th><th>Фактическая</th></tr>
 <tr><td>B-123456-789012</td><td>50</td><td>06.08.2025</td><td>07.08.2025</td>
     <td>08.08.2025</td><td>09.08.2025</td><td>Московская биржа (СПОТ: МБ T+)</td></tr>
</table></body></html>"""


def _merged_table():  # type: ignore[no-untyped-def]
    return extract_tables(MERGED.encode("utf-8"))[0]


def test_merged_cells_are_expanded_into_a_grid() -> None:
    """`colspan` и `rowspan` сдвигают соседей: без разворачивания «Место
    совершения сделки» из шапки оказывается над «Дата поставки фактическая».
    """
    table = _merged_table()

    assert table.has_merged_cells
    assert table.width == 7
    assert table.as_dicts()[0]["Место совершения сделки"] == "Московская биржа (СПОТ: МБ T+)"


def test_multi_row_header_is_merged_into_column_names() -> None:
    """Шапка двухстрочная: под объединённой «Дата оплаты» стоят «Плановая» и
    «Фактическая», и только вместе они называют колонку однозначно."""
    row = _merged_table().as_dicts()[0]

    assert row["Дата оплаты · Плановая"] == "06.08.2025"
    assert row["Дата оплаты · Фактическая"] == "07.08.2025"
    assert row["Дата поставки · Плановая"] == "08.08.2025"
    assert row["Дата поставки · Фактическая"] == "09.08.2025"


def test_second_header_row_is_not_data() -> None:
    """Строка «Плановая | Фактическая» — часть шапки, а не сделка."""
    table = _merged_table()

    assert len(table.rows) == 1
    assert table.rows[0][0] == "B-123456-789012"


def test_settlement_date_anchors_know_the_archive_wording() -> None:
    """В архиве дата расчётов называется «Дата оплаты» (A-03)."""
    columns = resolve_columns(_merged_table())

    assert columns["settlement_date"] == "Дата оплаты · Фактическая"
    assert columns["broker_trade_no"] == "Номер сделки"
    assert columns["quantity"] == "Количество ЦБ, шт."


def test_tables_without_merges_are_unchanged() -> None:
    """Обычная таблица разбирается как прежде: номер колонки равен позиции."""
    table = extract_tables(
        b"<html><body><table><tr><th>A</th><th>B</th></tr>"
        b"<tr><td>1</td><td>2</td></tr></table></body></html>"
    )[0]

    assert not table.has_merged_cells
    assert table.as_dicts() == [{"A": "1", "B": "2"}]
