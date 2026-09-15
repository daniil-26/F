"""Формат v2: выгрузка Excel одной таблицей (STAGE-1, разведка архива).

Отличия от v1, ради которых написан отдельный разбор: весь отчёт — одна
таблица, разделы пронумерованы строками внутри неё, инструмент сделки стоит
подзаголовком над группой, а шапки двухъярусные. Фикстура
`report_v2_2024-03.html` повторяет раскладку реального архива.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from conftest import FIXTURES
from portfolio.adapters.broker.anchors import COLUMNS_V2, is_known_column
from portfolio.adapters.broker.dto import ParsedOperation, ParsedReport
from portfolio.adapters.broker.mapping import parse
from portfolio.adapters.broker.mapping_v2 import MAPPING_VERSION, SKIPPED_SECTIONS
from portfolio.adapters.broker.sections import split_sections
from portfolio.adapters.broker.tables import extract_tables
from portfolio.cli import app
from portfolio.domain.instruments import InstrumentResolver
from portfolio.jobs.import_broker import import_broker_report
from portfolio.models import EventType, Instrument, InstrumentKind

runner = CliRunner()

REPORT = FIXTURES / "report_v2_2024-03.html"
OPENING_BALANCES = (
    "id,account,date,ticker,isin,quantity,cost_basis,basis_quality,currency,note\n"
    "ob-01,СЧЕТ-77,2024-02-29,,,100000.00,,known,RUB,остаток денег\n"
    "ob-02,СЧЕТ-77,2024-02-29,ОФЗ 26238,RU000A1038V6,10,800.00,estimated,RUB,\n"
    "ob-03,СЧЕТ-77,2024-02-29,Сбер ао,RU0009029540,100,250.00,estimated,RUB,\n"
)


@pytest.fixture(scope="module")
def report() -> ParsedReport:
    return parse(REPORT.read_bytes())


def _of_kind(report: ParsedReport, kind: str) -> list[ParsedOperation]:
    return [item for item in report.operations if item.kind == kind]


# -- выбор версии и нарезка на разделы ---------------------------------------


def test_dispatcher_picks_v2_by_document_shape(report: ParsedReport) -> None:
    """Версия выбирается по форме документа, а не по дате или имени файла."""
    assert report.mapping_version == MAPPING_VERSION

    v1 = parse((FIXTURES / "report_2025-08.html").read_bytes())
    assert v1.mapping_version == "v1"


def test_flat_table_is_split_into_numbered_sections() -> None:
    sections = split_sections(
        extract_tables(REPORT.read_bytes()),
        known_header=lambda text: is_known_column(text, COLUMNS_V2),
    )

    numbers = [section.number for section in sections]
    assert numbers.count("") == 1, "преамбула с реквизитами — одна"
    for expected in ("1", "2", "4", "5.1", "5.4", "5.10", "8.1.1", "8.2"):
        assert expected in numbers


def test_section_title_survives_extra_cells_in_its_row() -> None:
    """«4. Оценка активов» делит строку с подписями колонок.

    Если такую подпись не опознать, весь раздел уезжает в предыдущий и его
    числа становятся остатками ценных бумаг.
    """
    sections = split_sections(
        extract_tables(REPORT.read_bytes()),
        known_header=lambda text: is_known_column(text, COLUMNS_V2),
    )
    assert any(section.number == "4" for section in sections)


def test_evaluation_section_does_not_produce_balances(report: ParsedReport) -> None:
    assert [balance.ticker for balance in report.balances if balance.kind == "security"] == [
        "ОФЗ 26238",
        "Сбер ао",
    ]


# -- сделки ------------------------------------------------------------------


def test_instrument_comes_from_the_group_subheader(report: ParsedReport) -> None:
    """A-23: в строке сделки бумаги нет, она в подзаголовке группы.

    ISIN в подзаголовке тоже нет — он берётся из раздела 2 по наименованию и
    номеру гос. регистрации.
    """
    buy = _of_kind(report, "BUY")[0]

    assert buy.ticker == "Сбер ао"
    assert buy.isin == "RU0009029540"


def test_settlement_date_is_the_actual_one_not_the_planned(report: ParsedReport) -> None:
    """Двухъярусная шапка: «Дата оплаты» → «Плановая | Фактическая» (A-03)."""
    buy = _of_kind(report, "BUY")[0]

    assert buy.trade_date.isoformat() == "2024-03-05"
    assert buy.settlement_date is not None
    assert buy.settlement_date.isoformat() == "2024-03-06"


def test_accrued_interest_is_added_to_the_trade_amount(report: ParsedReport) -> None:
    """A-07: НКД стоит отдельной колонкой, значит в сумму сделки не входит."""
    sell = _of_kind(report, "SELL")[0]

    assert sell.accrued_int == Decimal("25.00")
    assert sell.amount == Decimal("4025.00")
    assert sell.quantity == Decimal(-5)


def test_fees_are_split_by_kind(report: ParsedReport) -> None:
    fees = {(item.fee_kind, item.amount) for item in _of_kind(report, "FEE")}

    assert ("BROKER", Decimal("-6.00")) in fees
    assert ("EXCHANGE", Decimal("-0.60")) in fees


def test_currency_of_money_is_the_settlement_currency(report: ParsedReport) -> None:
    """Еврооблигация котируется в долларах, а рассчитывается в рублях.

    Валюта денежного эффекта — «Валюта суммы сделки», не «Валюта цены».
    """
    assert {item.currency for item in report.operations} == {"RUB"}


def test_totals_and_venue_turnover_are_not_trades(report: ParsedReport) -> None:
    """«Итого по выпуску», «изменение» и обороты по площадкам — не сделки."""
    assert len(_of_kind(report, "BUY")) == 2  # 5.1 и её же повтор в 5.10
    assert len(_of_kind(report, "SELL")) == 1
    assert report.unparsed == ()


def test_loan_issue_moves_neither_position_nor_money(report: ParsedReport) -> None:
    """Выдача займа: бумаги остаются в собственности, денег она не приносит.

    Вознаграждение приходит только с возвратом, поэтому строка выдачи не
    порождает события — но и в нераспознанное не уходит.
    """
    assert _of_kind(report, "LENDING_INCOME") == []
    assert all(item.quantity is None for item in _of_kind(report, "FEE"))
    assert report.unparsed == ()


def test_unsettled_loan_section_stays_skipped() -> None:
    """A-24: раздел 5.9 — незавершённые сделки, расчётов по ним ещё не было.

    Деньги по ним придут в 5.4 следующего отчёта. Учесть их здесь значило бы
    задвоить вознаграждение, поэтому пропуск объявлен с причиной.
    """
    assert "5.9" in SKIPPED_SECTIONS
    assert "5.4" not in SKIPPED_SECTIONS


# -- неторговые операции -----------------------------------------------------


def test_coupon_instrument_is_restored_from_the_comment(report: ParsedReport) -> None:
    """В разделе 8.1.1 колонки бумаги нет — она названа в комментарии."""
    coupon = _of_kind(report, "COUPON")[0]

    assert coupon.amount == Decimal("35.00")
    assert coupon.isin == "RU000A1038V6"


def test_dividend_is_gross_and_tax_is_its_own_event(report: ParsedReport) -> None:
    """A-04: налог приходит отдельной строкой, значит дивиденд показан до него."""
    dividend = _of_kind(report, "DIVIDEND")[0]
    tax = _of_kind(report, "TAX")[0]

    assert dividend.amount == Decimal("1000.00")
    assert dividend.withheld_at_source is False
    assert tax.amount == Decimal("-130.00")


def test_zero_bond_redemption_marker_is_skipped(report: ParsedReport) -> None:
    """A-20: «Погашение облигации» идёт с нулевой суммой, деньги — «Погашение номинала»."""
    maturity = _of_kind(report, "MATURITY")
    amounts = sorted(item.amount for item in maturity)

    assert amounts == [Decimal(0), Decimal("5000.00")]
    assert all(item.amount != 0 or item.quantity is not None for item in maturity)


def test_bond_redemption_writes_off_the_position(report: ParsedReport) -> None:
    """Раздел 8.2 списывает бумаги, раздел 8.1.1 приносит за них деньги."""
    written_off = [item for item in _of_kind(report, "MATURITY") if item.quantity is not None]

    assert len(written_off) == 1
    assert written_off[0].quantity == Decimal(-5)
    assert written_off[0].amount == Decimal(0)


# -- остаток денег -----------------------------------------------------------


def test_cash_balance_is_the_actual_total_not_the_planned(report: ParsedReport) -> None:
    """A-25: плановый остаток включает неисполненные обязательства."""
    cash = [balance for balance in report.balances if balance.kind == "cash"]

    assert len(cash) == 1
    assert cash[0].quantity == Decimal("103919.00")
    assert cash[0].currency == "RUB"


def test_report_currency_rur_is_read_as_rub(report: ParsedReport) -> None:
    """«RUR» — обозначение брокера, в журнале валюта одна и та же."""
    assert all(balance.currency == "RUB" for balance in report.balances)


def test_cash_movements_agree_with_the_reported_balance(report: ParsedReport) -> None:
    """Внутренняя согласованность фикстуры: 100 000 + движения = 103 919.

    Повтор сделки из раздела 5.10 в сумму не входит: это то же событие (A-22),
    и в журнале оно одно.
    """
    seen: set[str | None] = set()
    movement = Decimal(0)
    for item in report.operations:
        key = f"{item.broker_trade_no}:{item.kind}:{item.fee_kind}"
        if item.broker_trade_no and key in seen:
            continue
        seen.add(key)
        movement += item.amount

    closing = next(item.quantity for item in report.balances if item.kind == "cash")
    assert Decimal("100000.00") + movement == closing


# -- импорт целиком ----------------------------------------------------------


def test_import_reconciles_end_to_end(database: Path, tmp_path: Path) -> None:
    """Главный критерий: отчёт нового формата импортируется и сходится."""
    opening = tmp_path / "opening_balances.csv"
    opening.write_text(OPENING_BALANCES, encoding="utf-8")

    assert runner.invoke(app, ["import-csv", str(opening)]).exit_code == 0
    result = runner.invoke(app, ["import-broker", str(REPORT)])

    assert result.exit_code == 0, result.stdout
    assert runner.invoke(app, ["check"]).exit_code == 0


def test_trade_repeated_in_section_5_10_is_written_once(database: Path, tmp_path: Path) -> None:
    """A-22: сделка стоит и в 5.1, и в 5.10 — ключ идемпотентности их схлопывает.

    Повтор виден в дифф как «уже в журнале»: покупка и две её комиссии.
    """
    opening = tmp_path / "opening_balances.csv"
    opening.write_text(OPENING_BALANCES, encoding="utf-8")
    runner.invoke(app, ["import-csv", str(opening)])

    result = import_broker_report(REPORT)

    assert result.committed
    assert result.diff.summary() == {"new": 11, "unchanged": 3, "reversals": 0}


# -- подзаголовок группы сделок ----------------------------------------------

_SUBHEADER_REPORT = """<html><body><table>
<tr><td>Номер счета клиента</td><td>СЧЕТ-77</td></tr>
<tr><td>за период с 01.03.2024 по 31.03.2024</td></tr>
<tr><td>2. Состояние портфеля ценных бумаг</td></tr>
<tr><td>Наименование ЦБ</td><td>Эмитент</td><td>Номер гос. регистрации</td><td>ISIN</td>
    <td>Количество ЦБ на начало периода, шт.</td><td>Количество ЦБ на конец периода, шт.</td></tr>
<tr><td>Облигация 1Р-01</td><td>ЭМИТЕНТ-01</td><td>4B02-01-00001-A</td><td>RU000A1038V6</td>
    <td>0</td><td>9</td></tr>
<tr><td>5.1 Биржевые сделки с ценными бумагами</td></tr>
<tr><td>Номер сделки</td><td>Дата сделки</td><td>Вид сделки</td><td>Цена одной ЦБ</td>
    <td>Количество ЦБ, шт.</td><td>Сумма сделки</td><td>Валюта суммы сделки</td>
    <td>Дата оплаты</td></tr>
<tr><td>MC0123456789  Облигация 1Р-01  4B02-01-00001-A  RUR</td></tr>
<tr><td>B-000001-000001</td><td>05.03.2024</td><td>Покупка</td><td>1000.00</td>
    <td>9</td><td>9000.00</td><td>RUR</td><td>05.03.2024</td></tr>
</table></body></html>"""


def test_issue_code_in_subheader_is_not_mistaken_for_isin() -> None:
    """Первым в подзаголовке стоит внутренний код выпуска формы ISIN.

    «MC» плюс десять цифр проходит проверку формы ISIN, но ISIN-ом этой бумаги
    не является. Взятый за него, он заводит бумагу в справочнике второй раз:
    сделки уходят на неё, контрольный остаток — на запись из раздела 2, и
    сверка показывает ноль против количества по обеим строкам сразу.
    """
    report = parse(_SUBHEADER_REPORT.encode("utf-8"))

    (trade,) = _of_kind(report, "BUY")
    (balance,) = [item for item in report.balances if item.kind == "security"]

    assert trade.isin == "RU000A1038V6", "ISIN берётся из раздела 2, а не из подзаголовка"
    assert trade.ticker == balance.ticker
    assert trade.isin == balance.isin
    assert not report.unparsed


def test_unknown_isin_shaped_token_still_identifies_the_paper() -> None:
    """Бумаги нет в разделе 2 — тогда токен формы ISIN лучше, чем ничего:
    по нему строку хотя бы видно во «Входящих»."""
    report = parse(
        _SUBHEADER_REPORT.replace("Облигация 1Р-01  4B02-01-00001-A  RUR", "RUR").encode("utf-8")
    )

    (trade,) = _of_kind(report, "BUY")

    assert trade.isin == "MC0123456789"
    assert trade.ticker is None


_LOAN_REPORT = """<html><body><table>
<tr><td>Номер счета клиента</td><td>СЧЕТ-77</td></tr>
<tr><td>за период с 01.03.2024 по 31.03.2024</td></tr>
<tr><td>2. Состояние портфеля ценных бумаг</td></tr>
<tr><td>Наименование ЦБ</td><td>Эмитент</td><td>Номер гос. регистрации</td><td>ISIN</td>
    <td>Количество ЦБ на начало периода, шт.</td><td>Количество ЦБ на конец периода, шт.</td></tr>
<tr><td>Сбер ао</td><td>ЭМИТЕНТ-01</td><td>10301481B</td><td>RU0009029540</td>
    <td>10</td><td>10</td></tr>
<tr><td>5.4 Сделки займа ценных бумаг</td></tr>
<tr><td>Номер сделки</td><td>Дата сделки</td><td>Вид сделки</td><td>Количество ЦБ, шт.</td>
    <td>Сумма сделки</td><td>% по сделке</td><td>Валюта суммы сделки</td>
    <td>Брокерская комиссия</td><td>Валюта брокерской комиссии</td>
    <td>Тип сделки займа</td><td>Дата оплаты</td></tr>
<tr><td>ПАО "Сбербанк России" Сбер ао 10301481B RUR</td></tr>
<tr><td>B-000101-000003</td><td>18.03.2024</td><td>Выдача займа ценных бумаг</td><td>10</td>
    <td>3 000.00</td><td></td><td>RUR</td><td>0.00</td><td>RUR</td>
    <td>1-я часть</td><td>19.03.2024</td></tr>
<tr><td>B-000101-000004</td><td>19.03.2024</td><td>Возврат займа ценных бумаг</td><td>10</td>
    <td>3 000.00</td><td>0.25</td><td>RUR</td><td>0.07</td><td>RUR</td>
    <td>2-я часть</td><td>20.03.2024</td></tr>
</table></body></html>"""


def test_loan_return_brings_income_and_fee() -> None:
    """A-21, закрыто прогоном по архиву: «% по сделке» — сумма вознаграждения.

    Что это рубли, а не ставка, показала сверка: разность вознаграждения и
    брокерской комиссии по разделу совпала с расхождением до копейки.
    """
    report = parse(_LOAN_REPORT.encode("utf-8"))

    (income,) = _of_kind(report, "LENDING_INCOME")
    assert income.amount == Decimal("0.25")
    assert income.trade_date.isoformat() == "2024-03-19"
    assert income.ticker == "Сбер ао", "бумага берётся из подзаголовка группы (A-23)"
    # Позицию заём не меняет: бумаги остаются в собственности.
    assert income.quantity is None

    (fee,) = _of_kind(report, "FEE")
    assert fee.amount == Decimal("-0.07")
    assert fee.fee_kind == "BROKER"

    assert report.unparsed == ()


def test_lending_income_is_not_an_external_flow() -> None:
    """Деньги не приходят извне — их зарабатывает сам портфель.

    Записанное как `CASH_IN`, вознаграждение завысило бы внешний приток и
    занизило доходность, а инвариант «external_flow равен сумме внешних
    событий» остался бы выполненным и считал бы неверно.
    """
    from portfolio.domain.events import is_external

    assert not is_external(EventType.LENDING_INCOME)


def test_zero_reward_does_not_become_an_event() -> None:
    """Нулевое вознаграждение — не событие: ноль в append-only журнале потом
    неотличим от ошибки импорта (то же правило, что и в A-20)."""
    report = parse(_LOAN_REPORT.replace("<td>0.25</td>", "<td>0.00</td>").encode("utf-8"))

    assert _of_kind(report, "LENDING_INCOME") == []


_PENDING_DELIVERY_REPORT = """<html><body><table>
<tr><td>Номер счета клиента</td><td>СЧЕТ-77</td></tr>
<tr><td>за период с 01.03.2024 по 31.03.2024</td></tr>
<tr><td>2. Состояние портфеля ценных бумаг</td></tr>
<tr><td>Наименование ЦБ</td><td>ISIN</td><td>Количество ЦБ на начало периода, шт.</td>
    <td>Количество ЦБ на конец периода, шт.</td><td>ЦБ к зачислению, шт.</td>
    <td>ЦБ к выводу, шт.</td><td>Плановое количество ЦБ, шт.</td></tr>
<tr><td>Сбер ао</td><td>RU0009029540</td><td>0.00</td>
    <td>0.00</td><td>5.00</td><td>0.00</td><td>5.00</td></tr>
<tr><td>ОФЗ 26238</td><td>RU000A1038V6</td><td>5.00</td>
    <td>5.00</td><td>0.00</td><td>0.00</td><td>5.00</td></tr>
</table></body></html>"""


def test_control_quantity_is_the_planned_one() -> None:
    """Сделка последнего дня периода с поставкой в следующем (A-26).

    В отчёте она стоит как «ЦБ к зачислению»: на счёт депо бумаги ещё не
    поставлены, поэтому «Количество ЦБ на конец периода» их не показывает. В
    журнале они уже есть — позиции считаются по дате сделки (A-03). Контрольным
    числом поэтому служит плановое количество.
    """
    report = parse(_PENDING_DELIVERY_REPORT.encode("utf-8"))
    by_name = {item.ticker: item.quantity for item in report.balances if item.kind == "security"}

    assert by_name["Сбер ао"] == Decimal("5.00"), "бумага в пути — не ноль"
    assert by_name["ОФЗ 26238"] == Decimal("5.00")
    assert report.unparsed == ()


def test_control_quantity_falls_back_to_the_closing_one() -> None:
    """Колонки планового количества может не быть — тогда фактическое."""
    report = parse(
        _PENDING_DELIVERY_REPORT.replace("<td>Плановое количество ЦБ, шт.</td>", "")
        .replace("<td>0.00</td><td>5.00</td><td>0.00</td><td>5.00</td>",
                 "<td>0.00</td><td>5.00</td><td>0.00</td>")
        .replace("<td>5.00</td><td>0.00</td><td>0.00</td><td>5.00</td>",
                 "<td>5.00</td><td>0.00</td><td>0.00</td>")
        .encode("utf-8")
    )
    by_name = {item.ticker: item.quantity for item in report.balances if item.kind == "security"}

    assert by_name["Сбер ао"] == Decimal("0.00")


_UNBALANCED_LOAN_REPORT = """<html><body><table>
<tr><td>Номер счета клиента</td><td>СЧЕТ-77</td></tr>
<tr><td>за период с 01.03.2024 по 31.03.2024</td></tr>
<tr><td>1. Состояние денежных средств на счете</td></tr>
<tr><td>Входящий остаток (всего):</td><td>RUR</td></tr>
<tr><td>Исходящий остаток (всего):</td><td>0.48</td></tr>
<tr><td>2. Состояние портфеля ценных бумаг</td></tr>
<tr><td>Наименование ЦБ</td><td>ISIN</td><td>Количество ЦБ на конец периода, шт.</td>
    <td>Плановое количество ЦБ, шт.</td></tr>
<tr><td>Сбер ао</td><td>RU0009029540</td><td>0</td><td>0</td></tr>
<tr><td>{section}</td></tr>
<tr><td>Номер сделки</td><td>Дата сделки</td><td>Вид сделки</td><td>Количество ЦБ, шт.</td>
    <td>Сумма сделки</td><td>% по сделке</td><td>Валюта суммы сделки</td>
    <td>Брокерская комиссия</td><td>Валюта брокерской комиссии</td>
    <td>Тип сделки займа</td><td>Дата оплаты</td></tr>
<tr><td>ПАО "Сбербанк России" Сбер ао 10301481B RUR</td></tr>
<tr><td>B-000101-000004</td><td>19.03.2024</td><td>Возврат займа ценных бумаг</td><td>10</td>
    <td>3 000.00</td><td>0.25</td><td>RUR</td><td>0.07</td><td>RUR</td>
    <td>2-я часть</td><td>20.03.2024</td></tr>
</table></body></html>"""


def test_loan_section_is_flagged_on_the_report() -> None:
    """Признак нужен сверке: мягкий допуск применяется только к таким отчётам."""
    assert parse(_LOAN_REPORT.encode("utf-8")).has_loan_section
    # Раздела 5.4 в отчёте может не быть вовсе — тогда сверка строгая.
    assert not parse(_PENDING_DELIVERY_REPORT.encode("utf-8")).has_loan_section


def test_small_gap_is_accepted_only_when_the_report_has_loans(
    database: Path, tmp_path: Path
) -> None:
    """A-27: послабление привязано к разделу займа, а не к величине расхождения.

    Один и тот же файл с одним и тем же расхождением в 0.30 импортируется,
    когда строки стоят в разделе займа, и не импортируется, когда те же строки
    объявлены биржевыми сделками.
    """
    loan = tmp_path / "loan.html"
    loan.write_text(
        _UNBALANCED_LOAN_REPORT.format(section="5.4 Сделки займа ценных бумаг"),
        encoding="utf-8",
    )

    result = import_broker_report(loan)

    assert result.committed, "отчёт с займом импортируется"
    assert result.reconcile.ok
    (tolerated,) = result.reconcile.tolerated
    assert tolerated.difference == Decimal("-0.30")


def test_same_gap_without_loans_stops_the_import(database: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain.html"
    plain.write_text(
        _UNBALANCED_LOAN_REPORT.format(section="5.1 Биржевые сделки с ценными бумагами"),
        encoding="utf-8",
    )

    result = import_broker_report(plain)

    assert not result.committed
    assert result.reconcile.tolerated == ()
    assert result.reconcile.discrepancies


def test_same_name_with_a_new_isin_is_another_security(session: Session) -> None:
    """A-28: конвертация выпускает бумагу с тем же именем и новым ISIN.

    Считая их одной записью, списание старого выпуска и зачисление нового
    схлопываются, и количества расходятся ровно на размер позиции.
    """
    resolver = InstrumentResolver(session)

    old = resolver.resolve(ticker="Акция", isin="RU0000000001")
    new = resolver.resolve(ticker="Акция", isin="RU0000000002")

    assert old is not None and new is not None
    assert old.id != new.id


def test_unidentified_record_is_enriched_not_duplicated(session: Session) -> None:
    """Запись без ISIN ещё не опознана — ISIN отчёта её дозаполняет.

    Конфликта здесь нет: спорить может только другой ISIN, а не его отсутствие.
    """
    resolver = InstrumentResolver(session)

    blank = resolver.resolve(ticker="Акция")
    filled = resolver.resolve(ticker="Акция", isin="RU0000000001")

    assert blank is not None and filled is not None
    assert blank.id == filled.id
    assert filled.isin == "RU0000000001"


def test_lookup_by_name_alone_prefers_the_unidentified_record(session: Session) -> None:
    """Наименование больше не уникально, и выбор обязан быть объявленным.

    Предпочтение — записи без ISIN: она ещё не опознана, и ISIN отчёта её
    дозаполнит. Молча взять первую попавшуюся значило бы привязать операции к
    случайному выпуску.
    """
    identified = Instrument(ticker="АКЦИЯ", isin="RU0000000001", kind=InstrumentKind.SHARE)
    unidentified = Instrument(ticker="АКЦИЯ", kind=InstrumentKind.SHARE)
    session.add_all([identified, unidentified])
    session.flush()

    found = InstrumentResolver(session).find(ticker="Акция")

    assert found is not None
    assert found.id == unidentified.id


def test_lookup_by_name_never_returns_another_issue(session: Session) -> None:
    """Явный и другой ISIN не перекрывается совпадением наименования."""
    session.add(Instrument(ticker="АКЦИЯ", isin="RU0000000001", kind=InstrumentKind.SHARE))
    session.flush()

    assert InstrumentResolver(session).find(ticker="Акция", isin="RU0000000002") is None
