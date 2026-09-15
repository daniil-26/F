"""Стадия 1 парсера: `bytes` → список таблиц как есть.

Про сделки, купоны и комиссии этот модуль не знает ничего. Он меняется, только
если брокер сменит движок отчётов (спека 4.1), поэтому golden-тест на нём —
проверка формы документа, а не бизнес-логики.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import html as lxml_html

from portfolio.adapters.formats import normalize_text

__all__ = [
    "HEADER_SEPARATOR",
    "EncodingChoice",
    "RawCell",
    "RawTable",
    "decode_report",
    "detect_encoding",
    "extract_tables",
    "garbage_ratio",
]

_META_CHARSET_RE = re.compile(rb"""charset=["']?\s*([A-Za-z0-9_\-]+)""", re.IGNORECASE)

# Кодировки в порядке убывания вероятности для российских отчётов.
_FALLBACK_ENCODINGS = ("utf-8", "cp1251", "koi8-r")

_RUSSIAN_LETTERS = frozenset(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
)
# Типографика, законная в отчёте: кавычки, тире, неразрывные пробелы, знаки валют.
_ALLOWED_NON_ASCII = frozenset("«»„“”‘’—–…\u00a0\u202f\u2009№°±§©®™€₽$¢£¥µ·•")

# Доля «посторонних» неASCII-символов, выше которой текст считается испорченным.
# У правильного русского текста она около нуля, у перепутанной кодировки — под половину.
_GARBAGE_THRESHOLD = 0.15

# Разделитель частей составного заголовка: «Дата оплаты · Плановая».
HEADER_SEPARATOR = " · "


@dataclass(frozen=True)
class EncodingChoice:
    """Выбранная кодировка и то, на чём основан выбор."""

    name: str
    declared: str | None
    garbage_ratio: float

    @property
    def declaration_lies(self) -> bool:
        """Документ объявил одну кодировку, а читается в другой."""
        if self.declared is None:
            return False
        return _canonical(self.declared) != _canonical(self.name)

    @property
    def suspicious(self) -> bool:
        """Даже лучший вариант выглядит испорченным — файл сломан до нас."""
        return self.garbage_ratio > _GARBAGE_THRESHOLD


@dataclass(frozen=True)
class RawCell:
    """Ячейка с её местом в сетке таблицы.

    `column` — номер колонки после разворачивания объединений, а не порядковый
    номер ячейки в строке. Различие принципиально: в отчёте-выгрузке Excel
    заголовок «Дата оплаты» занимает две колонки, и без учёта объединений
    «Место совершения сделки» из шапки оказывается над «Дата поставки
    фактическая» в данных.
    """

    text: str
    column: int
    colspan: int = 1
    rowspan: int = 1

    @property
    def columns(self) -> range:
        return range(self.column, self.column + self.colspan)


@dataclass(frozen=True)
class RawTable:
    """Таблица документа без интерпретации: заголовки и ячейки строками."""

    index: int
    caption: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    preceding_text: tuple[str, ...] = field(default=())
    # Те же заголовки и строки, но с номерами колонок сетки. Пустые кортежи
    # означают таблицу без объединений — тогда номер колонки равен позиции.
    # Шапка хранится строками: под объединённой «Дата оплаты» стоят «Плановая»
    # и «Фактическая», и только вместе они называют колонку однозначно.
    header_rows: tuple[tuple[RawCell, ...], ...] = field(default=())
    row_cells: tuple[tuple[RawCell, ...], ...] = field(default=())
    grid_width: int = 0

    @property
    def header_cells(self) -> tuple[RawCell, ...]:
        return self.header_rows[0] if self.header_rows else ()

    @property
    def width(self) -> int:
        if self.grid_width:
            return self.grid_width
        return max(
            (len(row) for row in self.rows),
            default=len(self.headers),
        )

    @property
    def is_empty(self) -> bool:
        return not self.rows and not self.headers

    @property
    def has_merged_cells(self) -> bool:
        return any(
            cell.colspan > 1 or cell.rowspan > 1
            for row in (*self.row_cells, *self.header_rows)
            for cell in row
        )

    def as_dicts(self) -> list[dict[str, str]]:
        """Строки как отображение «заголовок → ячейка».

        Колонка ячейки сопоставляется с колонкой заголовка по сетке, а не по
        порядковому номеру: иначе объединённые ячейки сдвигают данные
        относительно шапки. Колонки без заголовка получают позиционное имя
        `col_3` — терять их нельзя, именно в них у брокеров иногда приезжает
        признак сделки.
        """
        names = self._column_names()
        result: list[dict[str, str]] = []

        for index, row in enumerate(self.rows):
            cells = self.row_cells[index] if index < len(self.row_cells) else ()
            item: dict[str, str] = {}
            for position, text in enumerate(row):
                column = cells[position].column if position < len(cells) else position
                name = names.get(column) or f"col_{column}"
                item[name] = text
            result.append(item)
        return result

    def column_names(self) -> list[str]:
        """Имена колонок по порядку сетки — то, чем размечены строки `as_dicts`."""
        names = self._column_names()
        return [names[column] for column in sorted(names)]

    def _column_names(self) -> dict[int, str]:
        """Номер колонки сетки → имя.

        Имя собирается по всем строкам шапки сверху вниз: объединённая
        «Дата оплаты» плюс уточнение «Плановая» дают «Дата оплаты · Плановая».
        Без уточнения обе колонки назывались бы одинаково, и вторая затирала бы
        первую в `as_dicts`.
        """
        header_rows = self.header_rows or (
            tuple(
                RawCell(text=text, column=position)
                for position, text in enumerate(self.headers)
            ),
        )

        parts: dict[int, list[str]] = {}
        for row in header_rows:
            for cell in row:
                if not cell.text:
                    continue
                for column in cell.columns:
                    chain = parts.setdefault(column, [])
                    if cell.text not in chain:
                        chain.append(cell.text)

        names: dict[int, str] = {}
        seen: dict[str, int] = {}
        for column in range(max(self.width, max(parts, default=-1) + 1)):
            base = HEADER_SEPARATOR.join(parts.get(column, ())) or f"col_{column}"
            if base in seen:
                seen[base] += 1
                names[column] = f"{base}__{seen[base]}"
            else:
                seen[base] = 0
                names[column] = base
        return names


def garbage_ratio(text: str) -> float:
    """Доля неASCII-символов, не являющихся русскими буквами или типографикой.

    Мера испорченности текста. У верно прочитанного отчёта она около нуля; у
    UTF-8, прочитанного как CP1251, — около половины: «Состояние» превращается в
    «РЎРѕСЃС‚РѕСЏРЅРёРµ», где половина знаков — белорусские и сербские буквы,
    которых в русском отчёте быть не может.
    """
    foreign = [char for char in text if not char.isascii()]
    if not foreign:
        return 0.0
    bad = sum(
        1
        for char in foreign
        if char not in _RUSSIAN_LETTERS and char not in _ALLOWED_NON_ASCII
    )
    return bad / len(foreign)


def detect_encoding(content: bytes) -> EncodingChoice:
    """Выбирает кодировку по качеству результата, а не по объявлению.

    Объявлению доверять нельзя: файл, сохранённый в UTF-8 со старой метой
    `charset=windows-1251`, читается как CP1251 без единой ошибки — CP1251
    определён почти на всех байтах. Русский текст при этом превращается в
    «РЎРѕСЃС‚РѕСЏРЅРёРµ», и дальше по цепочке ломается всё: якоря не находятся,
    секции не опознаются, обезличенная фикстура уносит мусор в git.

    Поэтому все правдоподобные кодировки пробуются, а выбирается та, чей
    результат меньше похож на мусор. Объявленная идёт первой и выигрывает при
    равном качестве.
    """
    declared_match = _META_CHARSET_RE.search(content[:4096])
    declared = (
        declared_match.group(1).decode("ascii", "ignore").lower()
        if declared_match
        else None
    )

    # Валидная многобайтная UTF-8 — это UTF-8, что бы ни объявляла мета.
    # Русский текст в CP1251 почти никогда не оказывается валидным UTF-8, а вот
    # обратное — файл в UTF-8 со старой метой — встречается постоянно.
    utf8 = "utf-8-sig" if content.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        text = content.decode(utf8)
    except (UnicodeDecodeError, ValueError):
        pass
    else:
        if not text.isascii():
            return EncodingChoice(
                name=utf8, declared=declared, garbage_ratio=garbage_ratio(text)
            )

    candidates: list[str] = []
    if declared:
        candidates.append(declared)
    candidates.extend(_FALLBACK_ENCODINGS)

    best: EncodingChoice | None = None
    for encoding in candidates:
        try:
            text = content.decode(encoding)
        except (LookupError, UnicodeDecodeError, ValueError):
            continue
        ratio = garbage_ratio(text)
        if best is None or ratio < best.garbage_ratio:
            best = EncodingChoice(name=encoding, declared=declared, garbage_ratio=ratio)
        if best.garbage_ratio == 0.0:
            break

    if best is None:
        return EncodingChoice(name="utf-8", declared=declared, garbage_ratio=1.0)
    return best


def decode_report(content: bytes) -> str:
    """Декодирует отчёт. Кодировка определяется по качеству результата."""
    choice = detect_encoding(content)
    return content.decode(choice.name, errors="replace")


def _canonical(encoding: str) -> str:
    import codecs

    try:
        return codecs.lookup(encoding).name
    except LookupError:
        return encoding.strip().lower()


def extract_tables(content: bytes) -> list[RawTable]:
    """Достаёт все таблицы документа в порядке их появления."""
    text = decode_report(content)
    if not text.strip():
        return []

    document = lxml_html.fromstring(text)
    tables: list[RawTable] = []

    for index, element in enumerate(document.iter("table")):
        header_rows, row_cells = _rows_of(element)
        tables.append(
            RawTable(
                index=index,
                caption=_caption_of(element),
                headers=tuple(cell.text for cell in header_rows[0]) if header_rows else (),
                rows=tuple(tuple(cell.text for cell in row) for row in row_cells),
                preceding_text=_preceding_text(element),
                header_rows=header_rows,
                row_cells=row_cells,
                grid_width=_grid_width(header_rows, row_cells),
            )
        )
    return tables


def _grid_width(
    header_rows: tuple[tuple[RawCell, ...], ...],
    row_cells: tuple[tuple[RawCell, ...], ...],
) -> int:
    return max(
        (cell.column + cell.colspan for row in (*row_cells, *header_rows) for cell in row),
        default=0,
    )


def _rows_of(
    table: lxml_html.HtmlElement,
) -> tuple[tuple[tuple[RawCell, ...], ...], tuple[tuple[RawCell, ...], ...]]:
    """Строки, принадлежащие именно этой таблице, а не вложенной в неё.

    Отчёты брокеров верстаются таблицами, вложенность обычна: строка относится к
    ближайшему предку `table`, ячейка — к ближайшей `tr`.

    Каждая ячейка получает номер колонки **сетки**: `colspan` сдвигает соседей
    вправо, `rowspan` занимает место в следующих строках. Без этого объединённая
    шапка выгрузки Excel не совпадает со своими же данными.
    """
    collected: list[tuple[RawCell, ...]] = []
    header_rows: list[tuple[RawCell, ...]] = []
    occupied: dict[tuple[int, int], bool] = {}

    rows = [row for row in table.iter("tr") if _nearest_table(row) is table]
    for index, row in enumerate(rows):
        cells = [cell for cell in row.iter("td", "th") if _nearest_row(cell) is row]
        if not cells:
            continue

        placed: list[RawCell] = []
        column = 0
        for cell in cells:
            while (index, column) in occupied:
                column += 1
            colspan = _span(cell, "colspan")
            rowspan = _span(cell, "rowspan")
            for delta_row in range(rowspan):
                for delta_column in range(colspan):
                    occupied[(index + delta_row, column + delta_column)] = True
            placed.append(
                RawCell(
                    text=_cell_text(cell),
                    column=column,
                    colspan=colspan,
                    rowspan=rowspan,
                )
            )
            column += colspan

        # Шапка бывает многострочной: под объединённой «Дата оплаты» стоят
        # «Плановая» и «Фактическая». Пока строки состоят из одних <th> и данных
        # ещё не было, они все относятся к шапке.
        is_header_row = not collected and all(cell.tag == "th" for cell in cells)
        if is_header_row:
            header_rows.append(tuple(placed))
            continue
        collected.append(tuple(placed))

    if not header_rows and collected:
        # Заголовки без <th> — обычное дело. Первая строка считается заголовком,
        # только если в ней нет чисел: иначе это данные.
        first = collected[0]
        if first and not any(_looks_numeric(cell.text) for cell in first):
            header_rows.append(first)
            collected = collected[1:]

    return tuple(header_rows), tuple(collected)


def _span(element: lxml_html.HtmlElement, name: str) -> int:
    raw = (element.get(name) or "1").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        # Брокер иногда печатает colspan="" — это одна колонка, а не ошибка.
        return 1


def _nearest_table(element: lxml_html.HtmlElement) -> lxml_html.HtmlElement | None:
    for ancestor in element.iterancestors():
        if ancestor.tag == "table":
            return ancestor
    return None


def _nearest_row(element: lxml_html.HtmlElement) -> lxml_html.HtmlElement | None:
    for ancestor in element.iterancestors():
        if ancestor.tag == "tr":
            return ancestor
    return None


def _cell_text(cell: lxml_html.HtmlElement) -> str:
    return normalize_text(cell.text_content())


def _looks_numeric(cell: str) -> bool:
    return bool(re.fullmatch(r"[-+(]?[\d\s .,]+\)?", cell)) and any(
        ch.isdigit() for ch in cell
    )


def _caption_of(table: lxml_html.HtmlElement) -> str:
    for caption in table.iter("caption"):
        if _nearest_table(caption) is table:
            return _cell_text(caption)
    return ""


def _preceding_text(table: lxml_html.HtmlElement, limit: int = 3) -> tuple[str, ...]:
    """Текст перед таблицей — по нему якорятся секции (спека 4.1).

    Берётся с двух сторон: предыдущие соседи самой таблицы и предыдущие соседи
    её предков. Второе нужно, когда таблица завёрнута в `div` или ячейку.
    """
    found: list[str] = []
    node: lxml_html.HtmlElement | None = table

    while node is not None and len(found) < limit:
        for sibling in node.itersiblings(preceding=True):
            if sibling.tag == "table":
                break
            text = _cell_text(sibling)
            if text:
                found.append(text)
                if len(found) >= limit:
                    break
        tail = normalize_text(node.getparent().text) if node.getparent() is not None else ""
        if tail and len(found) < limit:
            found.append(tail)
        node = node.getparent()

    return tuple(reversed(found))
