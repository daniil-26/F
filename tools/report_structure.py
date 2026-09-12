#!/usr/bin/env python3
"""Инвентарь структуры отчётов брокера и сравнение нескольких отчётов.

Зачем
-----
Шаг 0 этапа 1 требует ответа на вопрос «какие типы операций встречаются **за всю
историю**, а не в последнем отчёте» (STAGE-1). Вручную это не собирается: в
архиве десятки файлов, формат за годы менялся, а парсер, написанный под шесть
типов операций из последнего месяца, встретит двадцать пять на середине
бэкфилла.

Скрипт собирает инвентарь одного отчёта и сводит инвентари нескольких: какие
секции, шапки, типы операций и площадки есть в каждом файле и чего в каком не
хватает. Различия — это и есть список того, что придётся поддержать в
`mapping_v*`.

Использование
-------------
    python tools/report_structure.py dump отчёт.html
    python tools/report_structure.py compare архив/*.html
    python tools/report_structure.py compare архив/*.html --json > inventory.json

В режиме `compare` строки помечаются:

    =   встречается во всех файлах
    +   встречается только в части файлов — с ними и будет больше всего работы
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _report_grid import (
    ReportGrid,
    detect_encoding,
    has_total_marker,
    is_date,
    is_number,
    is_time,
    is_total_marker,
    load_grid,
    normalize_text,
)

# Колонки, значения которых образуют словарь предметных типов: именно их
# полноту и проверяет шаг 0.
VALUE_COLUMNS = ("operation_type", "trade_kind", "venue", "currency")

PERIOD_MARKERS = ("за период", "отчетный период", "период с")


@dataclass
class SectionInfo:
    title: str
    rows: int = 0
    headers: list[list[str]] = field(default_factory=list)
    logical: list[str] = field(default_factory=list)


@dataclass
class Inventory:
    path: Path
    encoding: str
    declared_encoding: str | None
    garbage_ratio: float
    rows: int
    width: int
    period: str | None
    sections: list[SectionInfo]
    values: dict[str, list[str]]
    decimals: list[int]
    anonymized: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "file": self.path.name,
            "encoding": self.encoding,
            "declared_encoding": self.declared_encoding,
            "garbage_ratio": round(self.garbage_ratio, 3),
            "rows": self.rows,
            "width": self.width,
            "period": self.period,
            "anonymized": self.anonymized,
            "decimals": self.decimals,
            "sections": [
                {
                    "title": section.title,
                    "rows": section.rows,
                    "headers": section.headers,
                    "logical": section.logical,
                }
                for section in self.sections
            ],
            "values": self.values,
        }


def collect(path: Path) -> Inventory:
    grid = load_grid(path)
    sections = _sections(grid)
    values = _values(grid)

    choice = detect_encoding(path.read_bytes())
    return Inventory(
        path=path,
        encoding=choice.name,
        declared_encoding=choice.declared,
        garbage_ratio=choice.garbage_ratio,
        rows=len(grid.rows),
        width=grid.width,
        period=_period(grid),
        sections=sections,
        values=values,
        decimals=sorted(_decimals(grid)),
        anonymized=_looks_anonymized(grid),
    )


def _sections(grid: ReportGrid) -> list[SectionInfo]:
    order: list[str] = []
    found: dict[str, SectionInfo] = {}

    for index in range(len(grid.rows)):
        title = grid.sections.get(index) or "(без секции)"
        if title not in found:
            found[title] = SectionInfo(title=title)
            order.append(title)
        section = found[title]

        if grid.is_section_row(index):
            continue
        if grid.is_header_row(index):
            header = [value for value in grid.row_text(index) if value]
            if header not in section.headers:
                section.headers.append(header)
            logical = sorted(set(grid.headers[index].values()))
            section.logical = sorted(set(section.logical) | set(logical))
            continue
        if any(grid.row_text(index)):
            section.rows += 1

    return [found[title] for title in order]


def _values(grid: ReportGrid) -> dict[str, list[str]]:
    """Значения справочных колонок: типы операций, виды сделок, площадки, валюты."""
    collected: dict[str, set[str]] = {name: set() for name in VALUE_COLUMNS}

    for cell in grid.unique_cells():
        if grid.is_header_row(cell.row) or grid.is_section_row(cell.row):
            continue
        if has_total_marker(grid.row_text(cell.row)):
            continue
        header = grid.header_of(cell)
        if header not in collected:
            continue
        text = normalize_text(cell.text)
        if (
            text
            and not is_number(text)
            and not is_date(text)
            and not is_time(text)
            and not is_total_marker(text)
        ):
            collected[header].add(text)

    return {name: sorted(values) for name, values in collected.items() if values}


def _decimals(grid: ReportGrid) -> set[int]:
    """Сколько знаков после точки встречается в числах.

    Нужно парсеру: цена приходит с четырьмя знаками, суммы с двумя, количество
    без дробной части — и `Decimal` должен их различать, а не округлять.
    """
    result: set[int] = set()
    for cell in grid.unique_cells():
        text = normalize_text(cell.text)
        if not is_number(text):
            continue
        head, _, tail = text.replace(",", ".").partition(".")
        _ = head
        result.add(len(tail))
    return result


def _period(grid: ReportGrid) -> str | None:
    for index in range(min(len(grid.rows), 20)):
        for value in grid.row_text(index):
            lowered = value.casefold()
            if any(marker in lowered for marker in PERIOD_MARKERS):
                return value
    return None


def _looks_anonymized(grid: ReportGrid) -> bool:
    """Отчёт уже обезличен, если во всех числах нет ни одной значащей цифры.

    Предохранитель, а не гарантия: обезличенный отчёт не годится для проверки
    арифметики остатков, а необезличенный нельзя коммитить.
    """
    digits = ""
    count = 0
    for cell in grid.unique_cells():
        text = normalize_text(cell.text)
        if is_number(text):
            count += 1
            digits += re.sub(r"\D", "", text)
    return count >= 5 and set(digits) <= {"0"}


# --- вывод ------------------------------------------------------------------


def print_dump(inventory: Inventory) -> None:
    print(f"\n=== {inventory.path}")
    declared = inventory.declared_encoding or "не объявлена"
    mismatch = " ← не совпадает с содержимым" if _lies(inventory) else ""
    print(f"кодировка: {inventory.encoding} (объявлена: {declared}){mismatch}")
    if inventory.garbage_ratio > 0.15:
        print(
            f"ВНИМАНИЕ: посторонних символов {inventory.garbage_ratio:.0%} — "
            "текст похож на испорченную кодировку"
        )
    print(f"строк: {inventory.rows}, ширина сетки: {inventory.width}")
    print(f"период: {inventory.period or '— не найден'}")
    print(f"обезличен: {'да' if inventory.anonymized else 'похоже, нет'}")
    print(f"знаков после точки в числах: {inventory.decimals}")

    print("\nсекции:")
    for section in inventory.sections:
        print(f"  {section.title}  — строк данных: {section.rows}")
        for header in section.headers:
            print(f"      шапка: {' | '.join(header)}")
        if section.logical:
            print(f"      опознанные колонки: {', '.join(section.logical)}")

    for name, values in inventory.values.items():
        print(f"\n{name} ({len(values)}):")
        for value in values:
            print(f"  • {value}")


def _lies(inventory: Inventory) -> bool:
    import codecs

    if inventory.declared_encoding is None:
        return False
    try:
        return codecs.lookup(inventory.declared_encoding).name != codecs.lookup(
            inventory.encoding
        ).name
    except LookupError:
        return True


def print_compare(inventories: list[Inventory]) -> None:
    names = [item.path.name for item in inventories]
    print("файлы:")
    for index, item in enumerate(inventories, start=1):
        flag = "обезличен" if item.anonymized else "НЕ обезличен"
        print(
            f"  {index}. {item.path.name} — {item.rows} строк, {flag}, "
            f"период: {item.period or '—'}"
        )

    _compare_block(
        "секции",
        {item.path.name: [section.title for section in item.sections] for item in inventories},
        names,
    )

    for name in VALUE_COLUMNS:
        values = {
            item.path.name: item.values.get(name, []) for item in inventories
        }
        if any(values.values()):
            _compare_block(name, values, names)

    _compare_block(
        "шапки секций",
        {
            item.path.name: [
                f"{section.title} :: {' | '.join(header)}"
                for section in item.sections
                for header in section.headers
            ]
            for item in inventories
        },
        names,
    )


def _compare_block(title: str, per_file: dict[str, list[str]], names: list[str]) -> None:
    universe: list[str] = []
    for values in per_file.values():
        for value in values:
            if value not in universe:
                universe.append(value)
    if not universe:
        return

    print(f"\n--- {title}: всего {len(universe)}")
    for value in sorted(universe):
        present = [name for name in names if value in per_file.get(name, [])]
        mark = "=" if len(present) == len(names) else "+"
        where = "" if mark == "=" else f"   [{', '.join(present)}]"
        print(f"  {mark} {value}{where}")

    partial = [v for v in universe if any(v not in per_file.get(n, []) for n in names)]
    if partial:
        print(f"  различий: {len(partial)} из {len(universe)} — именно они и требуют работы")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    dump = subparsers.add_parser("dump", help="инвентарь одного или нескольких отчётов")
    dump.add_argument("files", nargs="+", type=Path)
    dump.add_argument("--json", action="store_true")

    compare = subparsers.add_parser("compare", help="что есть в одних отчётах и нет в других")
    compare.add_argument("files", nargs="+", type=Path)
    compare.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    inventories = [collect(path) for path in args.files]

    if args.json:
        print(json.dumps([item.as_dict() for item in inventories], ensure_ascii=False, indent=2))
        return 0

    if args.command == "dump":
        for inventory in inventories:
            print_dump(inventory)
        return 0

    if len(inventories) < 2:
        print("для сравнения нужно не меньше двух файлов", file=sys.stderr)
        return 2
    print_compare(inventories)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:  # вывод ушёл в `head` — это не ошибка
        raise SystemExit(0) from None
