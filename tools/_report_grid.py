"""Разбор отчёта брокера в сетку ячеек с учётом `colspan` и `rowspan`.

Общая часть вспомогательных скриптов. Отчёт этого брокера — выгрузка из Excel:
**весь документ — одна таблица**, секции и их шапки лежат строками внутри неё, а
объединённые ячейки сдвигают данные относительно заголовков. Без разворачивания
объединений «Место совершения сделки» из шапки оказывается над «Дата поставки
фактическая» в данных.

Модуль сознательно не зависит от `portfolio.adapters` (кроме декодирования):
вспомогательные скрипты не должны ломаться при калибровке парсера, и наоборот.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:  # запуск без `pip install -e .`
    sys.path.insert(0, str(_SRC))

from lxml import html as lxml_html  # noqa: E402

from portfolio.adapters.broker.tables import (  # noqa: E402
    decode_report,
    detect_encoding,
)
from portfolio.adapters.formats import normalize_text  # noqa: E402

__all__ = [
    "HEADER_ALIASES",
    "TABLE_ROW_MIN_CELLS",
    "TOTAL_MARKERS",
    "GridCell",
    "ReportGrid",
    "build_grid",
    "decode_report",
    "detect_encoding",
    "has_total_marker",
    "header_name",
    "is_total_marker",
    "iter_report_files",
    "load_grid",
    "looks_like_table_row",
    "normalize_text",
    "parse_document",
]


# Брокеры отдают HTML-выгрузку и под расширением .xls — это не бинарный Excel,
# а та же таблица, поэтому расширение само по себе ничего не решает.
REPORT_SUFFIXES = (".html", ".htm", ".xls")


def iter_report_files(
    paths: list[Path],
    *,
    recursive: bool = False,
    pattern: str | None = None,
    skip_suffixes: tuple[str, ...] = (),
) -> tuple[list[Path], list[str]]:
    """Разворачивает список путей: файл берётся как есть, каталог — по содержимому.

    Возвращает найденные файлы и заметки о пропущенном: «файла нет», «отчётов не
    найдено». Заметки печатает вызывающая сторона — молча проигнорированный
    аргумент хуже, чем лишняя строка в выводе.
    """
    found: list[Path] = []
    notes: list[str] = []

    for entry in paths:
        if entry.is_dir():
            inside = _reports_inside(entry, recursive=recursive, pattern=pattern,
                                     skip_suffixes=skip_suffixes)
            if not inside:
                notes.append(f"{entry}: отчётов не найдено")
            found.extend(inside)
            continue
        if not entry.exists():
            notes.append(f"{entry}: файла нет")
            continue
        found.append(entry)

    return found, notes


def _reports_inside(
    directory: Path,
    *,
    recursive: bool,
    pattern: str | None,
    skip_suffixes: tuple[str, ...],
) -> list[Path]:
    mask = pattern or "*"
    entries = directory.rglob(mask) if recursive else directory.glob(mask)

    result: list[Path] = []
    for path in sorted(entries):
        if not path.is_file():
            continue
        if any(path.name.endswith(suffix) for suffix in skip_suffixes):
            continue
        if pattern is None and path.suffix.lower() not in REPORT_SUFFIXES:
            continue
        result.append(path)
    return result


@dataclass
class GridCell:
    """Одна ячейка. `element` — узел lxml, его и правят скрипты обезличивания."""

    element: Any
    row: int
    col: int
    colspan: int
    rowspan: int

    @property
    def text(self) -> str:
        return normalize_text(self.element.text_content())


# Логическое имя колонки → подстроки заголовка. Словарь маленький и покрывает
# только то, что нужно вспомогательным скриптам: чувствительные колонки и те,
# по которым считается инвентарь отчёта.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "instrument_name": ("наименование цб", "краткое наименование", "бумага", "инструмент"),
    "issuer": ("эмитент",),
    "regnum": ("номер гос. регистрации", "гос. регистрации", "рег. номер", "номер регистрации"),
    "isin": ("isin",),
    "storage": ("место хранения", "депозитарий хранения"),
    "trade_no": ("номер сделки", "номер заявки", "номер поручения"),
    "operation_type": ("тип операции", "вид операции"),
    # «Тип сделки РЕПО», «Тип сделки займа», «Тип незавершенной сделки»: по ним
    # различаются части РЕПО и займа, поэтому это тоже вид сделки.
    "trade_kind": ("вид сделки", "тип сделки", "тип незавершенной сделки"),
    "comment": ("комментарий", "примечание", "основание"),
    "venue": ("место совершения", "торговая площадка", "место заключения"),
    # Валюта цены и валюта суммы различаются: еврооблигация котируется в USD, а
    # рассчитывается в рублях. Смешивать их в одну колонку нельзя.
    "price_currency": ("валюта цены",),
    "amount_currency": ("валюта суммы",),
    "fee_currency": ("валюта брокерской комиссии", "валюта комиссии"),
    "currency": ("валюта",),
    "accrued_int": ("нкд", "накопленный купонный доход"),
    "fee_broker": ("брокерская комиссия", "комиссия брокера", "вознаграждение брокера"),
    "fee_exchange": ("комиссия тс", "комиссия торговой системы", "клиринговая комиссия"),
    "fee_stamp": ("гербовый сбор",),
    "fee_depositary": ("комиссия депозитария", "депозитарная комиссия"),
    "quantity_in": ("зачислено цб", "цб к зачислению"),
    "quantity_out": ("списано цб", "цб к выводу"),
    "share": ("доля цб в портфеле", "доля в портфеле"),
    "rate": ("% по сделке", "ставка"),
    "date": ("дата",),
    "time": ("время",),
    "quantity": ("количество",),
    "price": ("цена",),
    "amount": ("сумма", "стоимость", "остаток", "сальдо", "оценка", "оборот"),
}

# Маркеры служебных строк. Лежат в тех же колонках, что и данные, поэтому нужны
# обоим скриптам: обезличиванию — чтобы не переименовать «Итого:» в бумагу,
# инвентарю — чтобы не принять «Оборот за период» за вид сделки.
TOTAL_MARKERS: tuple[str, ...] = (
    "итого", "всего", "общий итог", "подытог", "оборот", "изменение",
    "в том числе", "справочно",
)

_NUMBER_RE = re.compile(r"^-?\d{1,3}(?:[  ]\d{3})*(?:[.,]\d+)?$|^-?\d+(?:[.,]\d+)?$")
_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


@dataclass
class ReportGrid:
    """Сетка отчёта плюс привязка строк к секциям и заголовкам."""

    document: Any
    rows: list[list[GridCell | None]]
    # индекс строки-шапки → {номер колонки: логическое имя}
    headers: dict[int, dict[int, str]]
    # индекс строки → заголовок секции, в которой она лежит
    sections: dict[int, str]
    # индексы строк, которые сами являются подписями секций
    section_rows: frozenset[int]

    @property
    def width(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    def unique_cells(self) -> list[GridCell]:
        """Каждая ячейка по одному разу: объединённая занимает несколько мест."""
        seen: set[int] = set()
        result: list[GridCell] = []
        for row in self.rows:
            for cell in row:
                if cell is None or id(cell.element) in seen:
                    continue
                seen.add(id(cell.element))
                result.append(cell)
        return result

    def header_of(self, cell: GridCell) -> str | None:
        """Логическое имя колонки ячейки по ближайшей шапке выше.

        Шапка не переходит через подпись секции: иначе колонки «Состояния
        портфеля» применились бы к строкам «Сделок», и «Дата сделки» оказалась
        бы «наименованием ЦБ».
        """
        header_row: int | None = None
        for row_index in sorted(self.headers):
            if row_index < cell.row:
                header_row = row_index
            else:
                break
        if header_row is None:
            return None
        if self.section_between(header_row, cell.row):
            return None
        return self.headers[header_row].get(cell.col)

    def section_between(self, start: int, end: int) -> bool:
        """Есть ли подпись секции строго между двумя строками."""
        return any(index in self.section_rows for index in range(start + 1, end + 1))

    def is_header_row(self, index: int) -> bool:
        return index in self.headers

    def is_section_row(self, index: int) -> bool:
        return index in self.section_rows

    def row_text(self, row_index: int) -> list[str]:
        seen: set[int] = set()
        values: list[str] = []
        for cell in self.rows[row_index]:
            if cell is None or id(cell.element) in seen:
                continue
            seen.add(id(cell.element))
            values.append(cell.text)
        return values


def parse_document(content: bytes) -> Any:
    return lxml_html.fromstring(decode_report(content))


def build_grid(document: Any) -> ReportGrid:
    """Разворачивает все таблицы документа в одну сетку строк.

    Таблиц в этих отчётах одна, но если брокер сменит выгрузку на несколько,
    строки просто продолжатся ниже — скрипты от этого не ломаются.
    """
    rows: list[list[GridCell | None]] = []
    for table in document.iter("table"):
        rows.extend(_table_rows(table, offset=len(rows)))

    headers = {
        index: columns
        for index, columns in ((i, _header_columns(row)) for i, row in enumerate(rows))
        if columns
    }
    sections, section_rows = _sections(rows)
    return ReportGrid(
        document=document,
        rows=rows,
        headers=headers,
        sections=sections,
        section_rows=section_rows,
    )


def load_grid(path: Path) -> ReportGrid:
    return build_grid(parse_document(path.read_bytes()))


def header_name(text: str) -> str | None:
    """Заголовок колонки → логическое имя. Выигрывает самое длинное совпадение."""
    signature = normalize_text(text).casefold().replace("ё", "е")
    if not signature:
        return None

    best: tuple[int, str] | None = None
    for logical, variants in HEADER_ALIASES.items():
        for variant in variants:
            if variant in signature and (best is None or len(variant) > best[0]):
                best = (len(variant), logical)
    return best[1] if best else None


# Строка таблицы имеет хотя бы столько заполненных ячеек. Отсекает то, что стоит
# в тех же колонках, но таблицей не является: подписи, подтверждение клиента,
# дату формирования отчёта. Без этого «Руководитель компании | Петров П. П.»
# попадает в словарь значений колонки, на которую пришёлся по сетке.
TABLE_ROW_MIN_CELLS = 3


def looks_like_table_row(grid: ReportGrid, index: int) -> bool:
    values = [value for value in grid.row_text(index) if value]
    return len(values) >= TABLE_ROW_MIN_CELLS


def is_total_marker(text: str) -> bool:
    signature = normalize_text(text).casefold().replace("ё", "е").rstrip(": ")
    # Граница слова, а не пробел: «оборот, RUR» — такая же служебная строка,
    # как «оборот за период».
    return any(re.match(rf"{re.escape(marker)}\b", signature) for marker in TOTAL_MARKERS)


def has_total_marker(values: list[str]) -> bool:
    return any(is_total_marker(value) for value in values)


def is_number(text: str) -> bool:
    return bool(_NUMBER_RE.match(text.strip()))


def is_date(text: str) -> bool:
    return bool(_DATE_RE.match(text.strip()))


def is_time(text: str) -> bool:
    return bool(_TIME_RE.match(text.strip()))


def _table_rows(table: Any, offset: int) -> list[list[GridCell | None]]:
    occupied: dict[tuple[int, int], GridCell] = {}
    tr_list = [tr for tr in table.iter("tr") if _nearest(tr, "table") is table]

    for row_index, tr in enumerate(tr_list):
        column = 0
        for td in tr.iter("td", "th"):
            if _nearest(td, "tr") is not tr:
                continue
            while (row_index, column) in occupied:
                column += 1
            colspan = _span(td, "colspan")
            rowspan = _span(td, "rowspan")
            cell = GridCell(
                element=td,
                row=row_index + offset,
                col=column,
                colspan=colspan,
                rowspan=rowspan,
            )
            for delta_row in range(rowspan):
                for delta_col in range(colspan):
                    occupied[(row_index + delta_row, column + delta_col)] = cell
            column += colspan

    if not occupied:
        return []
    height = max(row for row, _ in occupied) + 1
    width = max(col for _, col in occupied) + 1
    return [[occupied.get((row, col)) for col in range(width)] for row in range(height)]


def _span(element: Any, name: str) -> int:
    raw = (element.get(name) or "1").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _nearest(element: Any, tag: str) -> Any | None:
    for ancestor in element.iterancestors():
        if ancestor.tag == tag:
            return ancestor
    return None


def _header_columns(row: list[GridCell | None]) -> dict[int, str]:
    """Строка-шапка: не меньше трёх опознанных заголовков и ни одного числа.

    Порог в три заголовка отсекает подписи секций и строки «Итого», в которых
    тоже встречается слово «сумма».
    """
    columns: dict[int, str] = {}
    seen: set[int] = set()
    values = 0

    for cell in row:
        if cell is None or id(cell.element) in seen:
            continue
        seen.add(id(cell.element))
        text = cell.text
        if not text:
            continue
        values += 1
        if is_number(text) or is_date(text) or is_time(text):
            return {}
        logical = header_name(text)
        if logical is None:
            continue
        for column in range(cell.col, cell.col + cell.colspan):
            columns[column] = logical

    return columns if len(columns) >= 3 and values >= 3 else {}


# «5.1 Биржевые сделки…», «8.1.1 Зачислено/списано ДС…»: номер до трёх ступеней,
# затем текст, начинающийся с буквы. Требование буквы отсекает «1 234 567.89»,
# которое иначе читается как секция «1». Третья ступень встречается в отчётах с
# 2019 года и без неё подразделы 8.1.1 и 8.2 сливаются в один список.
_SECTION_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+([^\W\d_].*)$")


def _sections(
    rows: list[list[GridCell | None]],
) -> tuple[dict[int, str], frozenset[int]]:
    """Строка → секция. Подпись секции — «5.1 Биржевые сделки…» первой ячейкой."""
    result: dict[int, str] = {}
    section_rows: set[int] = set()
    current = ""

    for index, row in enumerate(rows):
        first = next((cell.text for cell in row if cell is not None and cell.text), "")
        match = _SECTION_RE.match(first)
        if match and len(first) < 120 and not is_number(first):
            current = first
            section_rows.add(index)
        result[index] = current
    return result, frozenset(section_rows)
