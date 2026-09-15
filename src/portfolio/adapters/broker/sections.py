"""Стадия 1.5: плоская выгрузка Excel → секции с собственными шапками.

Отчёт этого брокера — **одна таблица на весь документ**. Подписи секций
(«5.1 Биржевые сделки с ценными бумагами»), шапки колонок, подзаголовки групп по
бумагам и строки «Итого» лежат строками внутри неё. `extract_tables` такую
выгрузку отдаёт одной `RawTable` на 700 строк, и разбирать её как таблицу
бессмысленно: у соседних строк разные колонки.

Этот модуль режет плоскую сетку на секции, каждая со своей шапкой, и отдаёт их
теми же `RawTable`, что и обычный отчёт. Благодаря этому `resolve_columns` и
`as_dicts` работают дальше без изменений.

Про сделки и купоны модуль не знает ничего — только про форму документа.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from portfolio.adapters.broker.tables import RawCell, RawTable
from portfolio.adapters.formats import FormatError, is_blank, parse_decimal

__all__ = [
    "SECTION_NUMBER_RE",
    "ReportSection",
    "split_sections",
]

# «5.1 Биржевые сделки», «8.1.1 Зачислено/списано ДС», «2. Состояние портфеля».
# До трёх уровней: глубже брокер не нумерует. Хвост обязан содержать букву:
# без этого «1 234.56» — сумма в единственной заполненной ячейке — читается как
# подпись секции №1, и весь остаток отчёта уезжает в несуществующую секцию.
SECTION_NUMBER_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+(\S.*)$")
_LETTER_RE = re.compile(r"[^\W\d_]")

# Шапкой считается строка, в которой опознано хотя бы столько заголовков. Два —
# потому что у секции 8.2 их пять, а у самой узкой таблицы отчёта — четыре;
# порог в три отсекал бы шапки, где половина колонок брокеру в новинку.
MIN_HEADER_MATCHES = 2

# Продолжение многострочной шапки («Плановая | Фактическая» под «Дата оплаты»)
# имеет несколько заполненных ячеек. Подзаголовок группы по бумаге — ровно одну,
# и это единственное, что их надёжно различает.
MIN_CONTINUATION_CELLS = 2


@dataclass(frozen=True)
class ReportSection:
    """Одна таблица одной секции: подпись, шапка, строки.

    `groups` идёт параллельно строкам таблицы: в разделах сделок инструмент
    стоит не колонкой, а строкой-подзаголовком над своей группой сделок, и без
    этой привязки сделка теряет бумагу.
    """

    number: str
    title: str
    table: RawTable
    groups: tuple[str | None, ...] = field(default=())

    @property
    def is_empty(self) -> bool:
        return not self.table.rows

    def group_of(self, index: int) -> str | None:
        return self.groups[index] if index < len(self.groups) else None


def split_sections(
    tables: list[RawTable],
    *,
    known_header: Callable[[str], bool],
) -> list[ReportSection]:
    """Режет отчёт на секции по подписям и шапкам.

    Новая секция начинается на подписи вида «5.1 …». Внутри секции каждая новая
    шапка начинает **новую** таблицу с тем же номером: в «1. Состояние денежных
    средств» таких таблиц несколько подряд, и склеивать их нельзя — колонки
    разные.
    """
    sections: list[ReportSection] = []
    builder = _Builder(known_header=known_header)

    for table in tables:
        for row in (*table.header_rows, *table.row_cells):
            builder.feed(row)
        builder.flush()

    sections.extend(builder.result)
    return sections


class _Builder:
    """Накопитель строк. Вынесен в класс, потому что состояние — половина логики."""

    def __init__(self, *, known_header: Callable[[str], bool]) -> None:
        self._known_header = known_header
        self.result: list[ReportSection] = []

        self._number = ""
        self._title = ""
        self._header: list[tuple[RawCell, ...]] = []
        self._rows: list[tuple[RawCell, ...]] = []
        self._groups: list[str | None] = []
        self._group: str | None = None
        self._previous_was_header = False

    def feed(self, row: tuple[RawCell, ...]) -> None:
        filled = [cell for cell in row if cell.text and not is_blank(cell.text)]
        if not filled:
            self._previous_was_header = False
            return

        title = _section_title(filled)
        if title is not None:
            self.flush()
            self._number, self._title = title
            self._group = None
            self._previous_was_header = False
            return

        if self._is_header(filled):
            # Шапка после данных — это новая таблица той же секции.
            if self._rows:
                self.flush(keep_section=True)
            self._header.append(row)
            self._previous_was_header = True
            return

        if self._previous_was_header and self._is_continuation(filled):
            self._header.append(row)
            return
        self._previous_was_header = False

        if len(filled) == 1 and not _has_number(filled):
            # Подзаголовок группы: бумага, под которой идут её сделки.
            self._group = filled[0].text
            return

        self._rows.append(row)
        self._groups.append(self._group)

    def flush(self, *, keep_section: bool = False) -> None:
        if self._rows or self._header:
            self.result.append(
                ReportSection(
                    number=self._number,
                    title=self._title,
                    table=_as_table(len(self.result), self._title, self._header, self._rows),
                    groups=tuple(self._groups),
                )
            )
        self._header = []
        self._rows = []
        self._groups = []
        self._previous_was_header = False
        if not keep_section:
            self._number = ""
            self._title = ""
            self._group = None

    def _is_header(self, filled: list[RawCell]) -> bool:
        if _has_number(filled):
            return False
        matches = sum(1 for cell in filled if self._known_header(cell.text))
        return matches >= MIN_HEADER_MATCHES and matches * 2 >= len(filled)

    def _is_continuation(self, filled: list[RawCell]) -> bool:
        """Второй ярус шапки: несколько текстовых ячеек сразу под первым."""
        return len(filled) >= MIN_CONTINUATION_CELLS and not _has_number(filled)


def _section_title(filled: list[RawCell]) -> tuple[str, str] | None:
    """Подпись секции: первая ячейка строки начинается с номера раздела.

    Обычно она в строке одна, но не всегда: у «4. Оценка активов» в той же
    строке стоят подписи колонок «по цене закрытия». Требование «ровно одна
    ячейка» теряло бы такую секцию целиком, и её строки уезжали бы в предыдущую.
    Защита от ложных срабатываний — отсутствие чисел в строке: строка данных без
    единого числа не бывает.
    """
    if filled[0].column != 0 or _has_number(filled):
        return None
    text = filled[0].text
    if _is_number(text):
        return None
    match = SECTION_NUMBER_RE.match(text)
    if match is None or not _LETTER_RE.search(match.group(2)):
        return None
    return match.group(1), text


def _has_number(filled: list[RawCell]) -> bool:
    return any(_is_number(cell.text) for cell in filled)


def _is_number(text: str) -> bool:
    try:
        parse_decimal(text)
    except FormatError:
        return False
    return True


def _as_table(
    index: int,
    caption: str,
    header: list[tuple[RawCell, ...]],
    rows: list[tuple[RawCell, ...]],
) -> RawTable:
    header_rows = tuple(header)
    row_cells = tuple(rows)
    return RawTable(
        index=index,
        caption=caption,
        headers=tuple(cell.text for cell in header_rows[0]) if header_rows else (),
        rows=tuple(tuple(cell.text for cell in row) for row in row_cells),
        header_rows=header_rows,
        row_cells=row_cells,
        grid_width=max(
            (cell.column + cell.colspan for row in (*header_rows, *row_cells) for cell in row),
            default=0,
        ),
    )
