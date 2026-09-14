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
    python tools/report_structure.py parse отчёт.html
    python tools/report_structure.py compare архив/*.html
    python tools/report_structure.py compare архив/*.html --json > inventory.json
    python tools/report_structure.py digest архив/ -r -o digest.json

В режиме `compare` строки помечаются:

    =   встречается во всех файлах
    +   встречается только в части файлов — с ними и будет больше всего работы

Режим `digest` — единственный, который **безопасно отдавать наружу**: он
выгружает форму архива без данных. Подробности в разделе «Выжимка» ниже.

Выжимка (`digest`)
------------------
`dump` и `compare` печатают отчёт как есть, вместе с названиями бумаг и
комментариями, то есть с вашими данными. Отдавать такой вывод нельзя.

`digest` собирает то же знание о форме, но без значений:

* имена файлов — с обнулёнными цифрами (в имени бывает номер счёта);
* период — только год и месяц;
* подписи секций, заголовки колонок, опознанные логические колонки;
* словарь **брокера**: типы операций, виды сделок, площадки, валюты. Это
  вокабуляр отчёта, а не ваши данные;
* формы чисел и дат (`0 000.00`, `00.00.0000`) — по ним видно, сколько знаков
  после точки бывает и какие разделители;
* шаблоны комментариев, прогнанные через то же обезличивание, что и фикстуры:
  «Погашение купона № БУМАГА-01» вместо настоящего названия;
* количества строк по секциям.

Чего в выжимке нет: названий бумаг, эмитентов, ISIN, номеров гос. регистрации,
сумм, точных дат, ФИО, номеров счёта и договора.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _report_grid import (
    ReportGrid,
    detect_encoding,
    has_total_marker,
    header_name,
    is_date,
    is_number,
    is_time,
    is_total_marker,
    iter_report_files,
    load_grid,
    looks_like_table_row,
    normalize_text,
)
from anonymize_report import AliasStore, Anonymizer, Options

from portfolio.adapters.broker.anchors import is_known_column as _is_known_column_v2
from portfolio.adapters.broker.anchors import normalize_signature
from portfolio.adapters.broker.dto import ParsedOperation, ParsedReport
from portfolio.adapters.broker.mapping import parse as parse_with_mapping
from portfolio.adapters.broker.sections import split_sections
from portfolio.adapters.broker.tables import extract_tables as _extract_tables_v2
from portfolio.adapters.formats import FormatError, is_blank, parse_decimal
from portfolio.domain.events import KeyInput, assign_natural_keys

# Подписи раздела 1 → категория журнала. Раздел раскладывает остаток теми же
# категориями, что и журнал, но независимо от нашего разбора: сальдо торговых и
# неторговых операций, комиссии по видам. Поэтому сравнение по строкам этой
# таблицы локализует расхождение до категории, а не до отчёта целиком.
#
# Две подписи могут вести в одну категорию (депозитарий брокера и иные
# депозитарии), поэтому значения отчёта по категории складываются.
_CATEGORY_LABELS: tuple[tuple[str, str], ...] = (
    ("сальдо торговых операций", "TRADE"),
    ("сальдо неторговых операций", "NONTRADE"),
    ("комиссия брокера", "FEE·BROKER"),
    ("комиссия торговой системы", "FEE·EXCHANGE"),
    ("гербовый сбор", "FEE·STAMP"),
    ("комиссия депозитария брокера", "FEE·DEPOSITARY"),
    ("комиссия иных депозитариев", "FEE·DEPOSITARY"),
)

# Строка «Уплаченная комиссия и сборы» — итог по видам, а не ещё один вид.
# Складывать её с составляющими нельзя: комиссии посчитались бы дважды.
_FEE_TOTAL_LABELS = ("уплаченная комиссия и сборы в том числе", "уплаченная комиссия и сборы")

# Порядок строк сверки: подпись, категория, уровень вложенности.
_CATEGORY_ROWS: tuple[tuple[str, str, int], ...] = (
    ("сальдо торговых операций", "TRADE", 0),
    ("сальдо неторговых операций", "NONTRADE", 0),
    ("уплаченная комиссия и сборы", "FEE", 0),
    ("комиссия брокера", "FEE·BROKER", 1),
    ("комиссия торговой системы", "FEE·EXCHANGE", 1),
    ("гербовый сбор", "FEE·STAMP", 1),
    ("депозитарные комиссии", "FEE·DEPOSITARY", 1),
)

# Строки раздела 1, которые не сравниваются: это не категории движения, а
# остатки и их разложение. Остатки печатаются отдельными строками сверки.
_NOT_A_CATEGORY = ("остаток", "свободные средства")

# Строка «Входящий остаток (всего):» несёт не сумму, а код валюты колонки
# (A-25), само число стоит ниже. Поэтому итог ищется по нескольким подписям в
# порядке предпочтения, а строки с «плановый» исключаются: плановый остаток
# включает неисполненные обязательства и с журналом не сойдётся.
_OPENING_LABELS = ("входящий остаток всего", "входящий остаток в том числе")
_CLOSING_LABELS = ("исходящий остаток всего", "исходящий остаток в том числе")
_PLANNED_MARKER = "плановый"

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
    # Словари значений внутри именно этой секции. Без разбивки по секциям
    # «Погашение облигации» в деньгах (8.1.1) и в бумагах (8.2) неразличимы,
    # а это два разных события журнала.
    values: dict[str, list[str]] = field(default_factory=dict)


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
                    "values": section.values,
                }
                for section in self.sections
            ],
            "values": self.values,
        }


def collect(path: Path) -> Inventory:
    grid = load_grid(path)
    sections = _sections(grid)
    values = _values(grid)
    _fill_section_values(grid, sections)

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


def _fill_section_values(grid: ReportGrid, sections: list[SectionInfo]) -> None:
    by_title = {section.title: section for section in sections}

    for cell in grid.unique_cells():
        if grid.is_header_row(cell.row) or grid.is_section_row(cell.row):
            continue
        if has_total_marker(grid.row_text(cell.row)):
            continue
        if not looks_like_table_row(grid, cell.row):
            continue
        header = grid.header_of(cell)
        if header not in VALUE_COLUMNS:
            continue
        section = by_title.get(grid.sections.get(cell.row) or "(без секции)")
        if section is None:
            continue
        text = normalize_text(cell.text)
        if not text or is_number(text) or is_date(text) or is_time(text):
            continue
        if is_total_marker(text):
            continue
        bucket = section.values.setdefault(header, [])
        if text not in bucket:
            bucket.append(text)

    for section in sections:
        for values in section.values.values():
            values.sort()


def _values(grid: ReportGrid) -> dict[str, list[str]]:
    """Значения справочных колонок: типы операций, виды сделок, площадки, валюты."""
    collected: dict[str, set[str]] = {name: set() for name in VALUE_COLUMNS}

    for cell in grid.unique_cells():
        if grid.is_header_row(cell.row) or grid.is_section_row(cell.row):
            continue
        if has_total_marker(grid.row_text(cell.row)):
            continue
        if not looks_like_table_row(grid, cell.row):
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
        for name, values in sorted(section.values.items()):
            print(f"      {name}: {', '.join(values)}")

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
        "тип операции по секциям",
        {
            item.path.name: [
                f"{section.title} :: {value}"
                for section in item.sections
                for column in ("operation_type", "trade_kind")
                for value in section.values.get(column, [])
            ]
            for item in inventories
        },
        names,
    )

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


# --- выжимка для передачи наружу -------------------------------------------

# Колонки, значения которых в выжимку идут как есть: это вокабуляр брокера, а не
# данные клиента.
SAFE_VALUE_COLUMNS = ("operation_type", "trade_kind", "venue", "currency",
                      "price_currency", "amount_currency", "fee_currency")

_DIGIT_RE = re.compile(r"\d")
# Три заглавных слова подряд — почти всегда ФИО. Вокабуляр брокера так не
# выглядит: «Доход по финансовым инструментам», «Московская биржа (СПОТ: МБ T+)».
_FULL_NAME_RE = re.compile(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\b")


# Псевдонимы сводятся к заполнителям: в выжимке нужна форма комментария, а не
# перечень бумаг. Иначе «Погашение купона № …» дробится на сотню строк по одной.
_PLACEHOLDERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[A-Z]{2}000Z\d{6}\b"), "<isin>"),
    (re.compile(r"\bБУМАГА-\d+"), "<бумага>"),
    (re.compile(r"\bЭМИТЕНТ-\d+"), "<эмитент>"),
    (re.compile(r"\bРЕГНОМЕР-\d+"), "<рег.номер>"),
    (re.compile(r"\bКОД-\d+"), "<код>"),
    (re.compile(r"\bДЕПОЗИТАРИЙ-\d+"), "<депозитарий>"),
    (re.compile(r"\b(?:КЛИЕНТ|БРОКЕР|ЛИЦЕНЗИЯ-\d+|СОТРУДНИК-\d+|ФИО-\d+|КОНТАКТ-\d+)"), "<лицо>"),
    (re.compile(r"\b(?:СЧЕТ|ДОГОВОР)-\d+"), "<реквизит>"),
)


def _template(text: str) -> str:
    for pattern, placeholder in _PLACEHOLDERS:
        text = pattern.sub(placeholder, text)
    return text


def _safe_values(values: list[str]) -> list[str]:
    """Вторая линия обороны выжимки.

    Первая — брать значения только из строк таблицы. Но выжимка уезжает наружу,
    и одной проверки для этого мало: она структурная и молчит, если структура
    окажется неожиданной.
    """
    return [value for value in values if not _FULL_NAME_RE.search(value)]


def build_digest(paths: list[Path]) -> dict[str, object]:
    """Форма архива без данных — то, что можно показать постороннему.

    Комментарии прогоняются через то же обезличивание, что и фикстуры: в них
    приезжают названия бумаг и иногда основания платежей с фамилиями.
    """
    files: list[dict[str, object]] = []
    number_shapes: dict[str, int] = {}
    date_shapes: dict[str, int] = {}
    unknown_headers: dict[str, int] = {}
    comments: dict[str, int] = {}

    for path in paths:
        grid = load_grid(path)
        inventory = collect(path)
        masker = Anonymizer(AliasStore.load(path.parent / "_digest_not_saved.json", seed=0),
                            Options(dates="mask"))

        for cell in grid.unique_cells():
            text = cell.text
            if not text:
                continue
            if is_number(text):
                shape = _DIGIT_RE.sub("0", text)
                number_shapes[shape] = number_shapes.get(shape, 0) + 1
            elif is_date(text) or is_time(text):
                shape = _DIGIT_RE.sub("0", text)
                date_shapes[shape] = date_shapes.get(shape, 0) + 1
            elif grid.header_of(cell) == "comment" and looks_like_table_row(grid, cell.row):
                template = _template(masker.mask_text(text))[:160]
                comments[template] = comments.get(template, 0) + 1

        for index in sorted(grid.headers):
            for cell in grid.rows[index]:
                if cell is None or cell.row != index or not cell.text:
                    continue
                if header_name(cell.text) is None:
                    unknown_headers[cell.text] = unknown_headers.get(cell.text, 0) + 1

        files.append(
            {
                "file": _DIGIT_RE.sub("0", path.name),
                "period": _coarse_period(inventory.period),
                "encoding": inventory.encoding,
                "declared_encoding": inventory.declared_encoding,
                "rows": inventory.rows,
                "grid_width": inventory.width,
                "sections": [
                    {
                        "title": section.title,
                        "rows": section.rows,
                        "headers": section.headers,
                        "logical": section.logical,
                        "values": {
                            name: _safe_values(values)
                            for name, values in section.values.items()
                            if name in SAFE_VALUE_COLUMNS
                        },
                    }
                    for section in inventory.sections
                ],
            }
        )

    return {
        "files": files,
        "number_shapes": dict(sorted(number_shapes.items(), key=lambda item: -item[1])),
        "date_shapes": dict(sorted(date_shapes.items(), key=lambda item: -item[1])),
        "unknown_headers": dict(sorted(unknown_headers.items(), key=lambda item: -item[1])),
        "comment_templates": dict(sorted(comments.items(), key=lambda item: -item[1])[:80]),
    }


def _coarse_period(period: str | None) -> str | None:
    """Период огрубляется до месяцев: точные даты — тоже след владельца."""
    if period is None:
        return None
    months = re.findall(r"\d{2}\.(\d{2})\.(\d{4})", period)
    if not months:
        return None
    return " … ".join(f"{year}-{month}" for month, year in months)


def print_digest_summary(digest: dict[str, object]) -> None:
    files = digest["files"]
    assert isinstance(files, list)
    print(f"отчётов: {len(files)}")
    for item in files:
        assert isinstance(item, dict)
        sections = item["sections"]
        assert isinstance(sections, list)
        print(f"  {item['file']}  {item['period']}  секций: {len(sections)}, строк: {item['rows']}")

    for key, title in (
        ("number_shapes", "формы чисел"),
        ("date_shapes", "формы дат"),
        ("unknown_headers", "неопознанные заголовки"),
    ):
        block = digest[key]
        assert isinstance(block, dict)
        print(f"\n{title}: {len(block)}")
        for value, count in list(block.items())[:12]:
            print(f"  ×{count:<6} {value}")

    templates = digest["comment_templates"]
    assert isinstance(templates, dict)
    print(f"\nшаблоны комментариев: {len(templates)}")
    for value, count in list(templates.items())[:12]:
        print(f"  ×{count:<6} {value}")


def _operation_label(operation: ParsedOperation) -> str:
    """Комиссии различаются видом: одной строкой `FEE` расхождение не разобрать."""
    if operation.kind == "FEE" and operation.fee_kind:
        return f"FEE·{operation.fee_kind}"
    return operation.kind


def print_parse(path: Path) -> None:
    """Что парсер вычитал из отчёта: события, итоги по типам, контрольные остатки.

    `dump` показывает форму документа, а это — результат разбора. При
    расхождении сверки нужно именно второе: расхождение объясняется не тем, как
    устроен отчёт, а тем, во что превратились его строки.
    """
    report: ParsedReport = parse_with_mapping(path.read_bytes())
    # Итоги считаются по тем же операциям, что двигают остаток: повтор сделки
    # из 5.1 в 5.10 (A-22) журнал схлопывает ключом, и сумма «как напечатано»
    # завышена ровно на него. Список ниже показывает все строки, помечая
    # схлопнутые, — потерянную строку иначе не отличить от схлопнутой.
    settled = _settled_operations(report)
    counted = {id(item) for item in settled}

    print(f"=== {path}")
    print(f"версия парсера: {report.mapping_version}")
    print(f"период: {report.period_start} — {report.period_end}")
    print(f"счёт: {report.account_code}")

    repeats = len(report.operations) - len(settled)
    print(f"\nоперации: {len(report.operations)}, из них схлопнуто повторов: {repeats}")
    header = f"  {'дата':<12}{'тип':<16}{'бумага':<24}{'количество':>14}{'сумма':>16}  влт"
    print(header)
    for operation in sorted(report.operations, key=lambda item: (item.trade_date, item.kind)):
        name = (operation.ticker or operation.isin or "—")[:23]
        quantity = "—" if operation.quantity is None else f"{operation.quantity:>14}"
        mark = "  " if id(operation) in counted else " ·"
        print(
            f"{mark}{operation.trade_date!s:<12}{_operation_label(operation):<16}{name:<24}"
            f"{quantity:>14}{operation.amount:>16}  {operation.currency}"
        )
    if repeats:
        print("  · — строка уже учтена: та же сделка пришла и в 5.1, и в 5.10 (A-22)")

    _print_totals(settled)
    _print_cash_summary(path, report)
    _print_hints(settled)

    print("\nконтрольные остатки из отчёта")
    for balance in report.balances:
        label = balance.ticker or balance.isin or balance.currency
        suffix = f" ({balance.isin})" if balance.isin and balance.ticker else ""
        print(f"  {balance.kind:<10}{label + suffix:<32}{balance.quantity:>16}")

    print(f"\nнераспознанные строки: {len(report.unparsed)}")
    for row in report.unparsed:
        print(f"  • [{row.table}] {row.reason}")
        print(f"    {row.row}")


def _print_totals(operations: list[ParsedOperation]) -> None:
    """Итоги по типам: первое, что нужно при расхождении денег."""
    money: dict[tuple[str, str], Decimal] = {}
    counts: dict[tuple[str, str], int] = {}
    for operation in operations:
        key = (_operation_label(operation), operation.currency)
        money[key] = money.get(key, Decimal(0)) + operation.amount
        counts[key] = counts.get(key, 0) + 1

    print(f"\nитоги по типам операций\n  {'тип':<16}{'событий':>9}{'сумма денег':>18}  влт")
    for (label, currency), total in sorted(money.items()):
        print(f"  {label:<16}{counts[(label, currency)]:>9}{total:>18}  {currency}")

    totals: dict[str, Decimal] = {}
    for (_, currency), total in money.items():
        totals[currency] = totals.get(currency, Decimal(0)) + total
    for currency, total in sorted(totals.items()):
        print(f"  {'движение денег за период':<25}{total:>18}  {currency}")


def _cash_section_rows(path: Path) -> list[tuple[int, str, list[Decimal]]]:
    """Раздел 1 как есть: отступ, подпись строки, числа в ней.

    Раздел — не таблица, а блок «метка → значение»: слева подпись, справа
    колонка на каждую валюту. Отступ (номер колонки подписи) несёт иерархию:
    «в том числе» и его составляющие.
    """
    tables = _extract_tables_v2(path.read_bytes())
    rows: list[tuple[int, str, list[Decimal]]] = []

    for section in split_sections(tables, known_header=_known_v2):
        if section.number not in ("1", "1.1"):
            continue
        for row in section.table.row_cells:
            cells = [cell for cell in row if cell.text and not is_blank(cell.text)]
            if not cells:
                continue
            label = cells[0].text.strip()
            numbers: list[Decimal] = []
            for cell in cells[1:]:
                try:
                    numbers.append(parse_decimal(cell.text))
                except FormatError:
                    continue
            if _looks_numeric(label):
                continue
            rows.append((cells[0].column, label, numbers))
    return rows


def _looks_numeric(text: str) -> bool:
    """Подпись строки не бывает числом: такая ячейка — значение без метки."""
    try:
        parse_decimal(text)
    except FormatError:
        return False
    return True


def _known_v2(text: str) -> bool:
    from portfolio.adapters.broker.anchors import COLUMNS_V2

    return _is_known_column_v2(text, COLUMNS_V2)


def _find_total(
    rows: list[tuple[int, str, list[Decimal]]], markers: tuple[str, ...]
) -> Decimal | None:
    for marker in markers:
        for _, label, numbers in rows:
            signature = normalize_signature(label)
            if signature == marker and numbers and _PLANNED_MARKER not in signature:
                return numbers[-1]
    return None


def _print_cash_summary(path: Path, report: ParsedReport) -> None:
    """Разложение остатка глазами брокера рядом с итогами журнала.

    Отвечает на вопрос «куда делись деньги» точнее, чем итог по типам: раздел 1
    печатает свободные средства и комиссии по видам, то есть те же категории,
    что и журнал, но независимо от нашего разбора.
    """
    rows = _cash_section_rows(path)
    if not rows:
        print("\nденежная сводка: раздела 1 в отчёте нет")
        return

    print("\nразложение остатка по отчёту (раздел 1)")
    for indent, label, numbers in rows:
        shift = 2 if indent else 0
        title = " " * shift + label
        values = "  ".join(f"{value:>16}" for value in numbers) if numbers else ""
        print(f"  {title:<46}{values}")

    opening = _find_total(rows, _OPENING_LABELS)
    closing = _find_total(rows, _CLOSING_LABELS)
    movement = sum((item.amount for item in _settled_operations(report)), Decimal(0))

    _print_category_comparison(rows, report, opening, closing, movement)

    print("\nсходимость денег")
    print(f"  {'входящий остаток (всего)':<44}{_cell(opening):>16}")
    print(f"  {'движение по журналу':<44}{movement:>16}")
    if opening is not None and closing is not None:
        print(f"  {'получается на конец':<44}{opening + movement:>16}")
        print(f"  {'исходящий остаток (всего) по отчёту':<44}{closing:>16}")
        print(f"  {'РАСХОЖДЕНИЕ':<44}{opening + movement - closing:>16}")
    else:
        print("  входящий или исходящий остаток не найден — сверить нечем")

    _print_subkopeck(report)


def _settled_operations(report: ParsedReport) -> list[ParsedOperation]:
    """Операции, попадающие в остаток на конец периода.

    Два отсева, и оба повторяют поведение журнала, иначе сводка не сойдётся
    даже на верном разборе:

    * **повторы**. Сделка конца месяца печатается и в 5.1, и в 5.10 следующего
      отчёта (A-22). В журнале второе вхождение схлопывается ключом
      идемпотентности, поэтому и здесь считается один раз. Ключ берётся из
      `domain/events.py`, а не переписывается: два разных правила ключа
      разошлись бы молча;
    * **дата**. Деньги считаются по дате оплаты, и сделка, расчёты по которой
      приходятся на следующий период, в исходящий остаток не входит.
    """
    keys = assign_natural_keys(
        [
            KeyInput(
                account_code=report.account_code or "",
                kind=item.kind,
                trade_date=item.trade_date,
                instrument_ref=item.isin or item.ticker,
                quantity=item.quantity,
                price=item.price,
                amount=item.amount,
                fee_kind=item.fee_kind,
                broker_trade_no=item.broker_trade_no,
            )
            for item in report.operations
        ]
    )

    seen: set[str] = set()
    result: list[ParsedOperation] = []
    for item, key in zip(report.operations, keys, strict=True):
        if key in seen:
            continue
        seen.add(key)
        effective = item.settlement_date or item.trade_date
        if report.period_end is not None and effective > report.period_end:
            continue
        result.append(item)
    return result


def _print_subkopeck(report: ParsedReport) -> None:
    """Суммы точнее копейки — вторая причина расхождений в сотых долях.

    Отчёт печатает числа с четырьмя и шестью знаками после точки (цена, курс
    валютной пары). Если такая точность попала в денежный эффект, наш итог
    отличается от брокерского на доли копейки по каждой операции, а брокер
    округляет поштучно. Проекции ничего не округляют намеренно (спека: деньги —
    только Decimal), поэтому находка означает ошибку разбора, а не потерю.
    """
    odd = [
        item
        for item in report.operations
        if item.amount != item.amount.quantize(Decimal("0.01"))
    ]
    if not odd:
        return

    print(f"\nсуммы точнее копейки: {len(odd)}")
    for item in odd[:10]:
        name = item.ticker or item.isin or "—"
        print(f"  {item.trade_date} {_operation_label(item):<16}{name:<24}{item.amount:>18}")
    print("  итог журнала отличается от брокерского на доли копейки по каждой такой строке")


def _ours_by_category(operations: list[ParsedOperation]) -> dict[str, Decimal]:
    """Движение журнала теми же категориями, какими его печатает раздел 1.

    Сделки берутся без комиссий: комиссия — отдельное событие (A-06), и войдя
    в «сальдо торговых операций», она посчиталась бы дважды.
    """
    totals: dict[str, Decimal] = {}

    def add(key: str, value: Decimal) -> None:
        totals[key] = totals.get(key, Decimal(0)) + value

    for item in operations:
        if item.kind == "FEE":
            add("FEE", item.amount)
            add(_operation_label(item), item.amount)
        elif item.kind in ("BUY", "SELL"):
            add("TRADE", item.amount)
        else:
            add("NONTRADE", item.amount)
    return totals


def _reported_by_category(
    rows: list[tuple[int, str, list[Decimal]]],
) -> tuple[dict[str, Decimal], list[tuple[str, Decimal]]]:
    """Числа раздела 1 по категориям плюс строки, которым пары не нашлось."""
    reported: dict[str, Decimal] = {}
    orphans: list[tuple[str, Decimal]] = []

    for _, label, numbers in rows:
        if not numbers:
            continue
        signature = normalize_signature(label)
        if signature in _FEE_TOTAL_LABELS:
            reported["FEE"] = reported.get("FEE", Decimal(0)) + numbers[-1]
            continue
        matched = next(
            (key for marker, key in _CATEGORY_LABELS if signature == marker), None
        )
        if matched is not None:
            reported[matched] = reported.get(matched, Decimal(0)) + numbers[-1]
            continue
        if not any(marker in signature for marker in _NOT_A_CATEGORY):
            orphans.append((label, numbers[-1]))
    return reported, orphans


def _print_category_comparison(
    rows: list[tuple[int, str, list[Decimal]]],
    report: ParsedReport,
    opening: Decimal | None,
    closing: Decimal | None,
    movement: Decimal,
) -> None:
    """Расчётное против отчётного по каждой категории — строка к строке."""
    ours = _ours_by_category(_settled_operations(report))
    reported, orphans = _reported_by_category(rows)

    print(
        f"\nсверка по категориям\n  {'параметр':<38}{'в отчёте':>15}"
        f"{'в журнале':>15}{'разница':>13}"
    )
    print(f"  {'входящий остаток (всего)':<38}{_cell(opening):>15}{'—':>15}{'—':>13}")

    for label, key, indent in _CATEGORY_ROWS:
        mine = ours.get(key)
        theirs = reported.get(key)
        if mine is None and theirs is None:
            continue
        title = " " * (2 * indent) + label
        print(f"  {title:<38}{_cell(theirs):>15}{_cell(mine):>15}{_gap(theirs, mine):>13}")

    computed = None if opening is None else opening + movement
    print(
        f"  {'исходящий остаток (всего)':<38}{_cell(closing):>15}"
        f"{_cell(computed):>15}{_gap(closing, computed):>13}"
    )

    missing = sorted(set(ours) - set(reported) - {"FEE"})
    if missing:
        print("\n  журнал насчитал, а раздел 1 отдельной строкой не показывает:")
        for key in missing:
            print(f"    {key:<36}{'':>15}{ours[key]:>15}")

    if orphans:
        print("\n  строки раздела 1 без пары в журнале (сравнить глазами):")
        for label, value in orphans:
            print(f"    {label:<36}{value:>15}")


def _cell(value: Decimal | None) -> str:
    return "—" if value is None else str(value)


def _gap(reported: Decimal | None, mine: Decimal | None) -> str:
    """Разница считается только когда есть обе стороны: ноль вместо прочерка
    здесь означал бы «сошлось», а сошлось нечему."""
    if reported is None or mine is None:
        return "—"
    return str(mine - reported)


def _print_hints(operations: list[ParsedOperation]) -> None:
    """Суммы, с которыми стоит сравнить расхождение сверки.

    Каждая из трёх отвечает за одно незакрытое допущение: совпадение
    расхождения с такой суммой — не совпадение, а ответ.
    """
    accrued = sum(
        (abs(item.accrued_int) for item in operations if item.accrued_int), Decimal(0)
    )
    tax = sum((item.amount for item in operations if item.kind == "TAX"), Decimal(0))
    fees = sum((item.amount for item in operations if item.kind == "FEE"), Decimal(0))

    print("\nс чем сравнить расхождение денег")
    print(f"  сумма НКД по сделкам      {accrued:>16}   A-07: входит ли НКД в «Сумму сделки»")
    print(f"  сумма налога              {tax:>16}   A-04: купон брутто или нетто")
    print(f"  сумма комиссий            {fees:>16}   A-06: все ли виды учтены")
    print("  разделы займа (5.4, 5.9) пропускаются целиком — A-24;")
    print("  сколько в них строк, покажет `dump` этого же отчёта")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    dump = subparsers.add_parser("dump", help="инвентарь одного или нескольких отчётов")
    dump.add_argument("files", nargs="+", type=Path)
    dump.add_argument("--json", action="store_true")

    compare = subparsers.add_parser("compare", help="что есть в одних отчётах и нет в других")
    compare.add_argument("files", nargs="+", type=Path)
    compare.add_argument("--json", action="store_true")

    parse_cmd = subparsers.add_parser(
        "parse", help="что парсер вычитал из отчёта: события, итоги, остатки"
    )
    parse_cmd.add_argument("files", nargs="+", type=Path)

    digest = subparsers.add_parser(
        "digest",
        help="форма архива без данных — единственный режим, который можно отдать наружу",
    )
    digest.add_argument("files", nargs="+", type=Path, metavar="ПУТЬ")
    digest.add_argument("-r", "--recursive", action="store_true")
    digest.add_argument("--pattern", metavar="МАСКА")
    digest.add_argument("-o", "--out", type=Path, help="куда записать JSON")

    args = parser.parse_args(argv)

    if args.command == "parse":
        for path in args.files:
            print_parse(path)
        return 0

    if args.command == "digest":
        files, notes = iter_report_files(
            args.files, recursive=args.recursive, pattern=args.pattern
        )
        for note in notes:
            print(note, file=sys.stderr)
        if not files:
            print("не найдено ни одного отчёта", file=sys.stderr)
            return 2

        payload = build_digest(files)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.out is not None:
            args.out.write_text(text + "\n", encoding="utf-8")
            print_digest_summary(payload)
            print(f"\nВыжимка записана: {args.out}")
            print("В ней нет названий бумаг, сумм, точных дат и реквизитов — её можно отдавать.")
        else:
            print(text)
        return 0

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
