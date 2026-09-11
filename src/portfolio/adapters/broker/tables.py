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

__all__ = ["RawTable", "decode_report", "extract_tables"]

_META_CHARSET_RE = re.compile(rb"""charset=["']?\s*([A-Za-z0-9_\-]+)""", re.IGNORECASE)

# Кодировки в порядке убывания вероятности для российских отчётов.
_FALLBACK_ENCODINGS = ("utf-8", "cp1251", "koi8-r")


@dataclass(frozen=True)
class RawTable:
    """Таблица документа без интерпретации: заголовки и ячейки строками."""

    index: int
    caption: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    preceding_text: tuple[str, ...] = field(default=())

    @property
    def width(self) -> int:
        return max(
            (len(row) for row in self.rows),
            default=len(self.headers),
        )

    @property
    def is_empty(self) -> bool:
        return not self.rows and not self.headers

    def as_dicts(self) -> list[dict[str, str]]:
        """Строки как отображение «заголовок → ячейка».

        Колонки без заголовка получают позиционное имя `col_3`: терять их нельзя,
        именно в них у брокеров иногда приезжает признак сделки.
        """
        names = self._column_names()
        result: list[dict[str, str]] = []
        for row in self.rows:
            item: dict[str, str] = {}
            for position, cell in enumerate(row):
                name = names[position] if position < len(names) else f"col_{position}"
                item[name] = cell
            result.append(item)
        return result

    def _column_names(self) -> list[str]:
        names: list[str] = []
        seen: dict[str, int] = {}
        for position in range(self.width):
            raw = self.headers[position] if position < len(self.headers) else ""
            name = raw or f"col_{position}"
            if name in seen:
                seen[name] += 1
                name = f"{name}__{seen[name]}"
            else:
                seen[name] = 0
            names.append(name)
        return names


def decode_report(content: bytes) -> str:
    """Декодирует отчёт, уважая объявленную в документе кодировку.

    Отчёты приходят и в UTF-8, и в CP1251; ошибка здесь превращает кириллицу в
    мусор, и якоря перестают находиться.
    """
    declared = _META_CHARSET_RE.search(content[:4096])
    candidates: list[str] = []
    if declared:
        candidates.append(declared.group(1).decode("ascii", "ignore").lower())
    candidates.extend(_FALLBACK_ENCODINGS)

    for encoding in candidates:
        if not encoding:
            continue
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def extract_tables(content: bytes) -> list[RawTable]:
    """Достаёт все таблицы документа в порядке их появления."""
    text = decode_report(content)
    if not text.strip():
        return []

    document = lxml_html.fromstring(text)
    tables: list[RawTable] = []

    for index, element in enumerate(document.iter("table")):
        headers, rows = _rows_of(element)
        tables.append(
            RawTable(
                index=index,
                caption=_caption_of(element),
                headers=headers,
                rows=rows,
                preceding_text=_preceding_text(element),
            )
        )
    return tables


def _rows_of(table: lxml_html.HtmlElement) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Строки, принадлежащие именно этой таблице, а не вложенной в неё.

    Отчёты брокеров верстаются таблицами, вложенность обычна. Строка относится
    к ближайшему предку `table`.
    """
    collected: list[tuple[str, ...]] = []
    header: tuple[str, ...] = ()
    header_taken = False

    for row in table.iter("tr"):
        if _nearest_table(row) is not table:
            continue
        cells = [cell for cell in row.iter("td", "th") if _nearest_row(cell) is row]
        if not cells:
            continue
        values = tuple(_cell_text(cell) for cell in cells)

        is_header_row = not header_taken and all(cell.tag == "th" for cell in cells)
        if is_header_row:
            header = values
            header_taken = True
            continue
        collected.append(values)

    if not header_taken and collected:
        # Заголовки без <th> — обычное дело. Первая строка считается заголовком,
        # только если в ней нет чисел: иначе это данные.
        first = collected[0]
        if first and not any(_looks_numeric(cell) for cell in first):
            header = first
            collected = collected[1:]

    return header, tuple(collected)


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
