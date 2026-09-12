#!/usr/bin/env python3
"""Обезличивание отчётов брокера для использования в `tests/fixtures/`.

Зачем
-----
Golden-тесты парсера требуют настоящих отчётов (спека 5.5), а настоящий отчёт
— это персональные данные: ФИО, номер договора, суммы, состав портфеля. Скрипт
убирает личное, сохраняя всё, от чего зависит разбор: структуру таблицы,
подписи секций, заголовки колонок, названия типов операций, форму чисел и дат,
количество строк.

Что делается с чем
------------------
| Что                                             | По умолчанию                  |
|-------------------------------------------------|-------------------------------|
| ФИО, договор, номер счёта, брокер, сотрудники   | псевдоним по категории        |
| суммы, цены, количества                         | `0 000.00` с сохранением формы |
| даты и время                                    | сдвиг на скрытое число дней   |
| номера сделок                                   | цифры → нули                  |
| бумаги: тикер, эмитент, ISIN, гос. регистрация  | стабильный псевдоним          |
| место хранения (депозитарий)                    | псевдоним                     |
| `x:num`, `x:str` (числа Excel в атрибутах)      | очищаются                     |
| `<img src>` (логотипы, подписи, штампы)         | удаляются                     |
| метаданные документа (автор, компания)          | удаляются                     |
| подписи секций, заголовки, типы операций, валюты| не меняются                   |

Почему даты сдвигаются, а не обнуляются
---------------------------------------
Обнулённые даты превращают отчёт в структурный макет: сверка остатков на дату
по такому файлу не проверяется, а именно она и есть смысл golden-теста. Сдвиг на
скрытое число дней, кратное семи, сохраняет интервалы, порядок, дни недели и
связь T+1 — и не говорит, когда это было. Сдвиг одинаков для всех отчётов одного
прогона: он хранится в файле соответствий.

Почему суммы всё-таки обнуляются
--------------------------------
Сохранить суммы — значит выложить свои деньги в git. Обнуление делает фикстуру
непригодной для проверки арифметики остатков, поэтому деление такое: обезличенные
фикстуры проверяют **разбор** (секции, колонки, типы операций, отсутствие
нераспознанных строк), а арифметика проверяется на **необезличенном** архиве
локально, в каталоге, который не попадает в git. `--amounts keep` оставляет суммы
— только для таких локальных фикстур.

Файл соответствий
-----------------
`--mapping` (по умолчанию `.anonymize-mapping.json` рядом с результатом) хранит
«оригинал → псевдоним» и сдвиг дат. Он нужен, чтобы несколько отчётов архива
обезличились согласованно: одна бумага получает один псевдоним во всех месяцах,
иначе сквозной импорт архива распадётся. **Этот файл — ключ к
деобезличиванию. Он не должен попадать в git.**

Использование
-------------
    python tools/anonymize_report.py отчёт.html -o tests/fixtures/report_01.html
    python tools/anonymize_report.py архив/ --out-dir tests/fixtures/
    python tools/anonymize_report.py архив/ -r --out-dir tests/fixtures/
    python tools/anonymize_report.py отчёт.html --dates mask --keep-instruments
    python tools/anonymize_report.py отчёт.html --dry-run   # только отчёт, без записи

После прогона скрипт печатает остаточные подозрения — строки, которые выглядят
как названия или идентификаторы и не были распознаны. Их нужно просмотреть
глазами: автоматическое обезличивание свободного текста принципиально неполно.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _report_grid import (
    GridCell,
    ReportGrid,
    build_grid,
    has_total_marker,
    is_date,
    is_number,
    is_time,
    normalize_text,
    parse_document,
)

# --- что считается личным ---------------------------------------------------

# Подпись в строке → категория значения, которое стоит правее. Список закрытый:
# «Валюта отчета» и «Состояние счета» сюда не входят и остаются как есть.
IDENTITY_LABELS: tuple[tuple[str, str], ...] = (
    ("наименование клиента", "client"),
    ("фио клиента", "client"),
    ("клиент", "client"),
    ("владелец счета", "client"),
    ("договор на брокерское обслуживание", "contract"),
    ("генеральное соглашение", "contract"),
    ("номер договора", "contract"),
    ("номер счета клиента", "account"),
    ("номер счета", "account"),
    ("код клиента", "account"),
    ("субсчет", "account"),
    ("руководитель компании", "staff"),
    ("сотрудник, отвечающий за внутренний учет", "staff"),
    ("главный бухгалтер", "staff"),
    ("генеральный директор", "staff"),
    ("контактное лицо", "staff"),
    ("исполнитель", "staff"),
    ("телефон", "contact"),
    ("e-mail", "contact"),
    ("email", "contact"),
    ("адрес", "contact"),
    ("инн", "contact"),
    ("кпп", "contact"),
    ("паспорт", "contact"),
)

# Личное внутри одной ячейки: «Брокер: ООО …».
INLINE_IDENTITY: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(брокер\s*:\s*)([^,;]+)"), "broker"),
    (re.compile(r"(?i)(наименование\s+брокера\s*:\s*)([^,;]+)"), "broker"),
    (re.compile(r"(?i)(лицензи\w*\s*(?:№|n|no)?\s*:?\s*)([\d][\d\-/]{4,})"), "license"),
)

# Колонки, содержащие идентификаторы бумаг.
INSTRUMENT_COLUMNS = {
    "instrument_name": "instrument",
    "issuer": "issuer",
    "regnum": "regnum",
    "isin": "isin",
}

PSEUDONYM_FORMATS = {
    "client": "КЛИЕНТ",
    "broker": "БРОКЕР",
    "contract": "ДОГОВОР-{n:02d}",
    "account": "СЧЕТ-{n:02d}",
    "staff": "СОТРУДНИК-{n:02d}",
    "contact": "КОНТАКТ-{n:02d}",
    "storage": "ДЕПОЗИТАРИЙ-{n:02d}",
    # Значение в колонке ISIN, не являющееся ISIN: у части выпусков там стоит
    # внутренний код. Отдельная категория, чтобы не спутать с БУМАГА-nn.
    "isin": "КОД-{n:02d}",
    "person": "ФИО-{n:02d}",
    "license": "ЛИЦЕНЗИЯ-{n:02d}",
    "instrument": "БУМАГА-{n:02d}",
    "issuer": "ЭМИТЕНТ-{n:02d}",
    "regnum": "РЕГНОМЕР-{n:02d}",
}

# --- формы значений ---------------------------------------------------------

ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")
# `1-01-40155-F`, `4B02-14-00124-A-002P`, `1-02-00143-A`
REGNUM_RE = re.compile(
    r"\b\d[A-Z0-9]{0,4}-\d{2}-\d{4,6}-[A-Z](?:-[A-Z0-9]{1,8})?\b"  # 1-01-40155-F
    r"|\b\d{6,10}[A-Z]\b"  # 10301481B — акции старых выпусков
)
DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
# Денежное число: с десятичной частью или с разделителем разрядов. Не цепляет
# `13%` и номера выпусков. Хвостовое `(?!\.\d)` не даёт зацепиться за часть даты:
# в `00.00.0000` иначе нашлось бы «число» `00.00`, и дата уехала бы в `0.00.0000`.
MONEY_RE = re.compile(
    r"(?<![\d.,:\-/])-?\d{1,3}(?:[ \u00a0]\d{3})+(?:\.\d+)?(?![\d%])(?!\.\d)"
    r"|(?<![\d.,:\-/])-?\d+\.\d{2,}(?![\d%])(?!\.\d)"
)
TRADE_NO_RE = re.compile(r"^[A-ZА-Я]{0,3}[-/]?\d[\d\-/]{3,}$", re.IGNORECASE)
# «Иванов И. И.» в свободном тексте: брокер пишет так в основаниях платежей.
# Заглавное слово плюс два инициала — почти всегда человек.
PERSON_RE = re.compile(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.")
# Префиксы уже подставленных псевдонимов: по ним видно, что значение обезличено
# и второй раз его переименовывать нельзя — иначе БУМАГА-02 превратится в БУМАГА-03.
PSEUDONYM_PREFIXES = (
    "БУМАГА", "ЭМИТЕНТ", "РЕГНОМЕР", "ДЕПОЗИТАРИЙ", "КЛИЕНТ", "БРОКЕР",
    "СЧЕТ", "ДОГОВОР", "СОТРУДНИК", "КОНТАКТ", "ФИО-", "ЛИЦЕНЗИЯ", "КОД-",
)
# Псевдо-ISIN опознаётся по форме, а не по префиксу: код страны в нём сохраняется
# от оригинала (US, MC, XS), и проверка на литерал «RU000Z» объявляла бы
# собственные псевдонимы иностранных бумаг остаточным подозрением.
PSEUDO_ISIN_RE = re.compile(r"^[A-Z]{2}000Z\d{6}$")
# Только купонный контекст: «№» встречается ещё и в номере лицензии брокера.
COUPON_TAIL_RE = re.compile(r"(?i)((?:погашение|выплата)\s+купона\s*№\s*)([^,;]+)$")
# Текст в кавычках в отчёте брокера — почти всегда название организации:
# «ПАО "Северсталь"», «Публичное акционерное общество "Северсталь"». Правило
# общее намеренно: узкое (только после ПАО/ООО) пропускало развёрнутую форму.
QUOTED_NAME_RE = re.compile(r'"([^"]{2,80})"|«([^»]{2,80})»')

# Секции, которые прореживаются, и способ выбора строк. Номер сравнивается с
# началом подписи, поэтому «5» покрывает 5.1, 5.4 и 5.10.
#   rows   — прореживаются строки таблицы;
#   groups — сначала бумаги (строка-подзаголовок выпуска со всем её блоком),
#            потом строки внутри каждой оставленной бумаги.
SAMPLE_SECTION_KINDS: dict[str, str] = {"2": "rows", "5": "groups", "8": "rows"}
DEFAULT_SAMPLE_SECTIONS: tuple[str, ...] = ("2", "5", "8")

# Правило из задачи: прореживать только там, где строк больше двух. На одной и
# двух строках прореживание уничтожило бы саму секцию.
SAMPLE_MIN_ITEMS = 2

# Строка таблицы имеет хотя бы столько заполненных ячеек. Отсекает хвост
# документа — подписи, подтверждение клиента, дату формирования отчёта: секция
# кончается таблицей, а не последней подписью, но в разметке это не размечено.
SAMPLE_MIN_CELLS = 3

MASKED_DATE = "00.00.0000"
MASKED_TIME = "00:00:00"

# Слова, которые не являются названиями: по ним не срабатывает остаточный поиск.
KNOWN_TERMS = frozenset(
    {
        "покупка", "продажа", "активный", "плановая", "фактическая", "итого",
        "общий итог", "оборот", "изменение", "ввод дс", "вывод дс", "налог",
        "погашение купона", "доход по финансовым инструментам", "rur", "rub",
        "usd", "eur", "cny", "московская биржа", "спб биржа", "otc",
        "выдача займа ценных бумаг", "возврат займа ценных бумаг",
        "1-я часть", "2-я часть", "от брокера",
    }
)


# --- файл соответствий ------------------------------------------------------


@dataclass
class AliasStore:
    """Стабильные псевдонимы и сдвиг дат. Ключ к деобезличиванию — не в git."""

    path: Path
    aliases: dict[str, dict[str, str]] = field(default_factory=dict)
    date_offset_days: int = 0

    @classmethod
    def load(cls, path: Path, seed: int | None = None) -> AliasStore:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                path=path,
                aliases=payload.get("aliases", {}),
                date_offset_days=int(payload.get("date_offset_days", 0)),
            )
        rng = random.Random(seed)
        # Кратно семи: дни недели сохраняются, значит торговые дни остаются
        # торговыми, а выходные — выходными.
        offset = 7 * rng.randint(-520, -52)
        return cls(path=path, aliases={}, date_offset_days=offset)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "_внимание": (
                        "файл восстанавливает исходные данные из обезличенных. "
                        "Не коммитить, хранить отдельно от фикстур."
                    ),
                    "date_offset_days": self.date_offset_days,
                    "aliases": self.aliases,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def alias(self, category: str, value: str) -> str:
        """Стабильный псевдоним. Один оригинал — один псевдоним навсегда.

        Уже подставленный псевдоним возвращается как есть: повторный прогон по
        обезличенному файлу не должен переименовывать СЧЕТ-01 в СЧЕТ-02, иначе
        скрипт нельзя запускать дважды, а это ровно то, что делают руками.
        """
        if is_pseudonym(value):
            return normalize_text(value)

        bucket = self.aliases.setdefault(category, {})
        key = normalize_text(value)
        if key in bucket:
            return bucket[key]

        template = PSEUDONYM_FORMATS.get(category, category.upper() + "-{n:02d}")
        number = len(bucket) + 1
        alias = template.format(n=number) if "{n" in template else template
        bucket[key] = alias
        return alias

    def isin_alias(self, value: str) -> str:
        """Псевдо-ISIN той же формы: код страны, 9 знаков, контрольная цифра.

        Форму приходится сохранять: `mapping_v1` проверяет ISIN регуляркой, и
        псевдоним вида «БУМАГА-01» просто не был бы распознан как ISIN.
        """
        if is_pseudonym(value):
            return normalize_text(value)

        bucket = self.aliases.setdefault("isin", {})
        key = normalize_text(value).upper()
        if key in bucket:
            return bucket[key]

        country = key[:2] if re.fullmatch(r"[A-Z]{2}", key[:2]) else "RU"
        number = len(bucket) + 1
        alias = f"{country}000Z{number:05d}{number % 10}"
        bucket[key] = alias
        return alias


# --- обезличивание ----------------------------------------------------------


@dataclass
class Options:
    dates: str = "shift"          # shift | mask | keep
    amounts: str = "mask"         # mask | keep
    keep_instruments: bool = False
    flat_identity: bool = False   # всё личное одной строкой XXX, как в примере
    # Доля строк операций, которую оставить (0 < sample <= 1). None — оставить все.
    sample: float | None = None
    sample_sections: tuple[str, ...] = DEFAULT_SAMPLE_SECTIONS


@dataclass
class Stats:
    changed: Counter[str] = field(default_factory=Counter)
    residual: Counter[str] = field(default_factory=Counter)
    # Строки, выброшенные при прореживании, по секциям.
    dropped: Counter[str] = field(default_factory=Counter)
    # Бумаги, выброшенные целиком: считаются отдельно от строк, иначе итог
    # «сколько строк удалено» перестаёт сходиться с документом.
    dropped_groups: Counter[str] = field(default_factory=Counter)

    def note(self, category: str) -> None:
        self.changed[category] += 1

    @property
    def dropped_rows(self) -> int:
        return sum(self.dropped.values())


class Anonymizer:
    def __init__(self, store: AliasStore, options: Options) -> None:
        self.store = store
        self.options = options
        self.stats = Stats()
        # Зерно выбора строк: от содержимого файла, чтобы повторный прогон дал
        # ту же выборку, а разные отчёты — разную.
        self._digest = ""

    # -- точка входа --------------------------------------------------------

    def run(self, content: bytes) -> bytes:
        document = parse_document(content)
        self._clean_head(document)
        self._clean_images(document)

        # Прореживание идёт до обезличивания: выброшенные строки не попадают в
        # файл соответствий, и в нём остаются только те бумаги, что в фикстуре.
        self._digest = hashlib.sha256(content).hexdigest()
        if self.options.sample is not None:
            self._sample_document(document)

        grid = build_grid(document)
        for cell in grid.unique_cells():
            self._process_cell(cell, grid)

        self._collect_residual(grid)
        return self._serialize(document)

    # -- прореживание -------------------------------------------------------

    def _sample_document(self, document: Any) -> None:
        """Оставляет заданную долю строк операций.

        Фикстура из архива за годы — это тысячи строк, из которых парсер
        проверяется первыми же десятками. Прореживание уменьшает её, сохраняя
        разнообразие: строки выбираются псевдослучайно с зерном от содержимого
        файла, поэтому повторный прогон даёт тот же результат.

        **Арифметика после этого не сходится**: итоги и остатки считались по
        всем строкам. Прореженная фикстура проверяет разбор, а не сверку.
        """
        grid = build_grid(document)
        doomed: list[int] = []

        for title, rows in self._sections_in_order(grid).items():
            kind = self._sample_kind(title)
            if kind is None:
                continue
            if kind == "groups":
                doomed.extend(self._sample_trade_section(grid, title, rows))
            else:
                doomed.extend(self._sample_plain_section(grid, title, rows))

        for index in sorted(set(doomed), reverse=True):
            _remove_row(grid, index)

    def _sections_in_order(self, grid: ReportGrid) -> dict[str, list[int]]:
        result: dict[str, list[int]] = {}
        for index in range(len(grid.rows)):
            result.setdefault(grid.sections.get(index) or "", []).append(index)
        return result

    def _sample_kind(self, title: str) -> str | None:
        match = re.match(r"^(\d{1,2})(?:\.\d{1,2})?[.\s]", title)
        if match is None:
            return None
        number = match.group(1)
        if number not in self.options.sample_sections:
            return None
        return SAMPLE_SECTION_KINDS.get(number, "rows")

    def _sample_plain_section(
        self, grid: ReportGrid, title: str, rows: list[int]
    ) -> list[int]:
        """Секции 2 и 8: прореживаются строки данных таблицы.

        Шапки, подписи секций и строки «Итого» не трогаются: без них фикстура
        перестанет проверять то, ради чего она есть.
        """
        return self._drop_list(self._table_rows(grid, rows), title)

    def _table_rows(self, grid: ReportGrid, rows: list[int]) -> list[int]:
        """Строки таблицы секции: после её шапки и достаточно широкие.

        Нумерация секций в отчёте разрежена, а конец секции в разметке ничем не
        отмечен: строки после последней таблицы (подписи, подтверждение клиента,
        дата формирования) формально принадлежат последней секции. Прореживать
        их нельзя.
        """
        start = self._header_row(grid, rows)
        if start is None:
            return []
        return [
            index
            for index in rows
            if index > start and self._is_data_row(grid, index)
        ]

    @staticmethod
    def _header_row(grid: ReportGrid, rows: list[int]) -> int | None:
        headers = [index for index in rows if grid.is_header_row(index)]
        return min(headers) if headers else None

    def _sample_trade_section(
        self, grid: ReportGrid, title: str, rows: list[int]
    ) -> list[int]:
        """Секции 5.*: сначала бумаги, потом строки внутри каждой оставшейся.

        Бумага в этом отчёте — строка-подзаголовок с описанием выпуска, за ней
        её сделки и её же «Итого по выпуску». Выброшенная бумага уходит целиком:
        оставить её итог без сделок значит получить фикстуру, которой в природе
        не бывает.
        """
        start = self._header_row(grid, rows)
        if start is None:
            return []
        groups = self._trade_groups(grid, [index for index in rows if index > start])
        if not groups:
            return self._sample_plain_section(grid, title, rows)

        doomed: list[int] = []
        kept = self._keep_indexes(len(groups), f"{title}|бумаги")

        for position, group in enumerate(groups):
            if position not in kept:
                # Бумага уходит целиком: подзаголовок выпуска, её сделки и её
                # «Итого по выпуску». Итог без сделок — фикстура, которой не бывает.
                doomed.extend(group["all"])
                self.stats.dropped[title] += len(group["all"])
                self.stats.dropped_groups[title] += 1
                continue
            doomed.extend(self._drop_list(group["data"], f"{title}|{group['name']}"))
        return doomed

    def _trade_groups(self, grid: ReportGrid, rows: list[int]) -> list[dict[str, Any]]:
        """Разбивает секцию сделок на блоки «выпуск → его строки → его итоги»."""
        groups: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None

        for index in rows:
            if grid.is_section_row(index) or grid.is_header_row(index):
                current = None
                continue

            values = [value for value in grid.row_text(index) if value]
            if not values:
                continue

            if _is_group_header(grid, index, values):
                current = {
                    "name": values[0],
                    "data": [],
                    "all": [index],
                }
                groups.append(current)
                continue

            if current is None:
                continue

            if has_total_marker(values):
                if _is_section_total(values):
                    current = None
                    continue
                current["all"].append(index)
                continue

            if self._is_data_row(grid, index):
                current["data"].append(index)
                current["all"].append(index)
        return groups

    def _drop_list(self, candidates: list[int], key: str) -> list[int]:
        kept = self._keep_indexes(len(candidates), key)
        doomed = [index for position, index in enumerate(candidates) if position not in kept]
        if doomed:
            self.stats.dropped[key.split("|")[0]] += len(doomed)
        return doomed

    def _keep_indexes(self, count: int, key: str) -> set[int]:
        """Какие позиции оставить: доля от общего числа, но не меньше одной.

        При двух и менее элементах не трогаем ничего (правило из задачи), иначе
        округляем к ближайшему: 0.7 от пяти — четыре, от трёх — две.
        """
        fraction = self.options.sample
        if fraction is None or count <= SAMPLE_MIN_ITEMS:
            return set(range(count))

        keep = max(1, math.floor(count * fraction + 0.5))
        if keep >= count:
            return set(range(count))

        rng = random.Random(f"{self._digest}|{key}")
        return set(rng.sample(range(count), keep))

    def _is_data_row(self, grid: ReportGrid, index: int) -> bool:
        if grid.is_section_row(index) or grid.is_header_row(index):
            return False
        values = [value for value in grid.row_text(index) if value]
        if len(values) < SAMPLE_MIN_CELLS or has_total_marker(values):
            return False
        return _is_droppable(grid, index)

    # -- шапка документа и картинки ----------------------------------------

    def _clean_head(self, document: Any) -> None:
        """Метаданные Excel про автора и компанию, XML-острова Office."""
        drop_meta = {
            "author", "lastauthor", "company", "originator", "subject",
            "keywords", "description", "owner", "creator",
        }
        for meta in document.iter("meta"):
            if (meta.get("name") or "").lower() in drop_meta:
                meta.getparent().remove(meta)
                self.stats.note("meta")

        for island in list(document.iter("xml")):
            island.getparent().remove(island)
            self.stats.note("xml-остров")

        for title in document.iter("title"):
            if normalize_text(title.text_content()):
                title.text = "Отчет брокера"
                self.stats.note("title")

    def _clean_images(self, document: Any) -> None:
        """Логотипы, факсимиле подписей и штампы — обычно прямо в `src`."""
        for image in document.iter("img"):
            for attribute in ("src", "alt", "title", "srcset", "longdesc"):
                if image.get(attribute) is not None:
                    del image.attrib[attribute]
                    self.stats.note("img")

    # -- ячейки -------------------------------------------------------------

    def _process_cell(self, cell: GridCell, grid: ReportGrid) -> None:
        self._clean_excel_attributes(cell.element)

        text = cell.text
        if not text:
            return

        # Подпись секции остаётся дословно: её номер — не сумма. «5.10
        # Исполнение обязательств по сделке» иначе становится «0 000.00
        # Исполнение обязательств по сделке», потому что «5.10» подходит под
        # форму денежного числа.
        if grid.is_section_row(cell.row) and text == grid.sections.get(cell.row):
            return

        category = self._identity_category(cell, grid)
        if category is not None:
            self._replace_whole(cell, self._identity_alias(category, text))
            self.stats.note(category)
            return

        # Подписи секций, строки-шапки и итоги стоят в тех же колонках, что и
        # данные. Без этой проверки «Дата сделки» из шапки получила бы псевдоним
        # бумаги, а «Итого:» — псевдоним эмитента.
        structural = (
            grid.is_header_row(cell.row)
            or grid.is_section_row(cell.row)
            or has_total_marker(grid.row_text(cell.row))
        )
        if structural:
            self._mask_structural(cell)
            return

        # Колоночные правила — только для настоящих строк таблицы. Строка из
        # двух ячеек под шапкой из шестнадцати колонок таблицей не является:
        # «На начало отчетного периода» иначе получает псевдоним из колонки ISIN.
        header = grid.header_of(cell) if _looks_like_table_row(grid, cell.row) else None
        if header == "storage":
            self._replace_whole(cell, self._identity_alias("storage", text))
            self.stats.note("storage")
            return

        if not self.options.keep_instruments and header in INSTRUMENT_COLUMNS:
            kind = INSTRUMENT_COLUMNS[header]
            if kind == "isin" and ISIN_RE.fullmatch(text.strip()):
                alias = self.store.isin_alias(text)
            else:
                alias = self.store.alias(kind, text)
                if kind == "issuer":
                    self._learn_quoted_parts(text, alias)
            self._replace_whole(cell, alias)
            self.stats.note(kind)
            return

        if is_number(text):
            if self.options.amounts == "mask":
                self._replace_whole(cell, mask_number(text))
                self.stats.note("число")
            return

        if is_date(text):
            self._replace_whole(cell, self._date(text))
            self.stats.note("дата")
            return

        if is_time(text):
            self._replace_whole(cell, MASKED_TIME)
            self.stats.note("время")
            return

        if header == "trade_no" and TRADE_NO_RE.match(text):
            self._replace_whole(cell, mask_digits(text))
            self.stats.note("номер сделки")
            return

        # Остальное — свободный текст: комментарии, подписи групп, шапка отчёта.
        self._mask_text_nodes(cell.element)

    def _mask_structural(self, cell: GridCell) -> None:
        """В служебной строке правятся только числа, даты и время."""
        text = cell.text
        if is_number(text):
            if self.options.amounts == "mask":
                self._replace_whole(cell, mask_number(text))
                self.stats.note("число")
        elif is_date(text):
            self._replace_whole(cell, self._date(text))
            self.stats.note("дата")
        elif is_time(text):
            self._replace_whole(cell, MASKED_TIME)
            self.stats.note("время")
        else:
            self._mask_text_nodes(cell.element)

    def _clean_excel_attributes(self, element: Any) -> None:
        """`x:num` несёт точное число отдельно от видимого текста.

        Обнулить текст и забыть про атрибут — самая дорогая ошибка этого
        скрипта: суммы остались бы в файле, просто перестали быть видимыми.
        """
        for attribute in list(element.attrib):
            name = attribute.split("}")[-1].split(":")[-1]
            if name in {"num", "str", "fmla"} and element.get(attribute):
                element.set(attribute, "")
                self.stats.note("x:num")

    def _identity_category(self, cell: GridCell, grid: ReportGrid) -> str | None:
        """Категория, если левее в строке стоит подпись из `IDENTITY_LABELS`."""
        best: tuple[int, str] | None = None
        for other in grid.rows[cell.row]:
            if other is None or other.col >= cell.col:
                continue
            label = normalize_text(other.text).casefold().replace("ё", "е").rstrip(": ")
            if not label:
                continue
            for alias, category in IDENTITY_LABELS:
                if (label == alias or label.startswith(alias)) and (
                    best is None or len(alias) > best[0]
                ):
                    best = (len(alias), category)
        return best[1] if best else None

    def _identity_alias(self, category: str, value: str) -> str:
        if self.options.flat_identity:
            return "XXX"
        # Номер договора обычно приходит вместе с датой: «12345 от 01.02.2019».
        match = DATE_RE.search(value)
        stripped = re.sub(r"(?i)\s+от\s*$", "", DATE_RE.sub("", value).strip())
        alias = self.store.alias(category, stripped)
        return f"{alias} от {self._date(match.group(0))}" if match else alias

    # -- правка текста ------------------------------------------------------

    def _replace_whole(self, cell: GridCell, replacement: str) -> None:
        """Заменить содержимое ячейки целиком, сохранив её разметку настолько,
        насколько это возможно: внутренние `span` с отступами остаются."""
        element = cell.element
        children = [child for child in element if child.tag not in {"br", "img"}]
        if not children:
            element.text = replacement
            return

        # В ячейке есть разметка — безопаснее править текстовые узлы.
        nodes = _text_nodes(element)
        if len(nodes) == 1:
            _set_text_node(element, nodes[0], replacement)
            return
        self._mask_text_nodes(element)

    def _mask_text_nodes(self, element: Any) -> None:
        for node in _text_nodes(element):
            masked = self.mask_text(node[2])
            if masked != node[2]:
                _set_text_node(element, node, masked)

    def mask_text(self, text: str) -> str:
        """Обезличивание внутри свободного текста.

        Порядок важен: сначала то, что распознаётся однозначно по форме
        (ISIN, гос. регистрация, даты), потом денежные суммы, потом названия.
        Иначе `1-02-00143-A` распалось бы на «числа».
        """
        if not text.strip():
            return text

        result = text
        for pattern, category in INLINE_IDENTITY:
            result = pattern.sub(
                lambda match, category=category: match.group(1)
                + self._identity_alias(category, match.group(2)),
                result,
            )

        # Личное вычищается всегда, даже когда названия бумаг решено оставить.
        result = PERSON_RE.sub(lambda m: self.store.alias("person", m.group(0)), result)
        result = self._replace_learned(result, ("client", "staff", "broker"), stem=True)
        # «Иванова И. И.» → «КЛИЕНТ И. И.» после замены фамилии: инициалы без
        # фамилии смысла не несут и в фикстуре выглядят недоделкой.
        result = re.sub(
            r"(КЛИЕНТ|БРОКЕР|ФИО-\d+|СОТРУДНИК-\d+)\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.", r"\1", result
        )

        if not self.options.keep_instruments:
            result = ISIN_RE.sub(lambda m: self.store.isin_alias(m.group(0)), result)
            result = REGNUM_RE.sub(lambda m: self.store.alias("regnum", m.group(0)), result)
            result = QUOTED_NAME_RE.sub(self._alias_quoted, result)
            result = self._replace_learned(result, ("instrument", "issuer", "regnum"))
            result = COUPON_TAIL_RE.sub(self._alias_coupon_tail, result)

        result = DATE_RE.sub(lambda m: self._date(m.group(0)), result)
        result = TIME_RE.sub(MASKED_TIME, result)
        if self.options.amounts == "mask":
            result = MONEY_RE.sub(lambda m: mask_number(m.group(0)), result)
        return result

    def _alias_quoted(self, match: re.Match[str]) -> str:
        """Содержимое кавычек → псевдоним эмитента, кавычки на месте."""
        value = match.group(1) or match.group(2)
        quotes = ('"', '"') if match.group(1) is not None else ("«", "»")
        if is_pseudonym(value):
            return match.group(0)
        return f"{quotes[0]}{self.store.alias('issuer', value)}{quotes[1]}"

    def _alias_coupon_tail(self, match: re.Match[str]) -> str:
        """«Погашение купона № <бумага>». Уже подставленный псевдоним не трогаем."""
        value = normalize_text(match.group(2))
        if is_pseudonym(value):
            return match.group(0)
        return match.group(1) + self.store.alias("instrument", value)

    def _learn_quoted_parts(self, text: str, alias: str) -> None:
        """«ПАО "Северсталь"» в портфеле и «"Северсталь"» в комментарии — одна
        организация. Без этого она получила бы два разных псевдонима, и связь
        между секциями в фикстуре распалась бы."""
        bucket = self.store.aliases.setdefault("issuer", {})
        for match in QUOTED_NAME_RE.finditer(text):
            value = normalize_text(match.group(1) or match.group(2))
            if len(value) >= 3:
                bucket.setdefault(value, alias)

    def _replace_learned(
        self, text: str, categories: tuple[str, ...], *, stem: bool = False
    ) -> str:
        """Подставить уже выученные псевдонимы.

        Бумага, названная в портфеле, встречается ещё и в комментарии к купону;
        фамилия клиента — в основании платежа. `stem=True` ловит падежи:
        «Иванова» от выученного «Иванов», иначе в тексте осталась бы фамилия.
        """
        result = text
        for category in categories:
            bucket = self.store.aliases.get(category, {})
            for original in sorted(bucket, key=len, reverse=True):
                if len(original) < 4:
                    continue
                if original in result:
                    result = result.replace(original, bucket[original])
                if not stem:
                    continue
                for word in original.split():
                    if len(word) >= 5 and word[0].isupper():
                        result = re.sub(
                            rf"\b{re.escape(word)}[а-яё]{{0,3}}\b", bucket[original], result
                        )
        return result

    def _date(self, text: str) -> str:
        if self.options.dates == "keep":
            return text
        if self.options.dates == "mask":
            return MASKED_DATE

        match = DATE_RE.fullmatch(text.strip())
        if match is None:
            return MASKED_DATE
        day, month, year = (int(part) for part in match.groups())
        try:
            shifted = date(year, month, day) + timedelta(days=self.store.date_offset_days)
        except ValueError:
            # Уже обезличенная дата вида 00.00.0000 — оставляем как есть.
            return text
        return f"{shifted.day:02d}.{shifted.month:02d}.{shifted.year:04d}"

    # -- остаточные подозрения ---------------------------------------------

    def _collect_residual(self, grid: ReportGrid) -> None:
        """То, что выглядит названием и осталось в документе.

        Автоматическое обезличивание свободного текста принципиально неполно:
        брокер может написать «зачисление по поручению Иванова И. И.» в
        комментарии, и ни одно правило этого не предскажет. Поэтому результат
        обязателен к просмотру глазами, а скрипт помогает, показывая куда смотреть.
        """
        for cell in grid.unique_cells():
            text = cell.text
            if not text or normalize_text(text).casefold() in KNOWN_TERMS:
                continue
            for match in QUOTED_NAME_RE.finditer(text):
                value = match.group(1) or match.group(2)
                if not is_pseudonym(value):
                    self.stats.residual[f'кавычки: "{value}"'] += 1
            for pattern, label in ((ISIN_RE, "ISIN"), (REGNUM_RE, "гос. регистрация")):
                for match in pattern.finditer(text):
                    if not is_pseudonym(match.group(0)):
                        self.stats.residual[f"{label}: {match.group(0)}"] += 1
            if PERSON_RE.search(text) or re.search(
                r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\b", text
            ):
                self.stats.residual[f"похоже на ФИО: {text[:60]}"] += 1
            if re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", text):
                self.stats.residual[f"похоже на e-mail: {text[:60]}"] += 1

    # -- вывод --------------------------------------------------------------

    def _serialize(self, document: Any) -> bytes:
        from lxml import html as lxml_html

        return lxml_html.tostring(
            document, encoding="utf-8", doctype="<!DOCTYPE html>", pretty_print=False
        )


def _looks_like_table_row(grid: ReportGrid, index: int) -> bool:
    values = [value for value in grid.row_text(index) if value]
    return len(values) >= SAMPLE_MIN_CELLS


def _is_group_header(grid: ReportGrid, index: int, values: list[str]) -> bool:
    """Строка-подзаголовок выпуска: одна заполненная ячейка с описанием бумаги.

    В отчёте это `ПАО "Эмитент" RU000A103QK3 4B02-01-00566-R-001P RUR` — именно
    отсюда берётся инструмент сделок, стоящих ниже.
    """
    if len(values) != 1 or has_total_marker(values):
        return False
    text = values[0]
    if len(text) < 4 or is_number(text) or is_date(text) or is_time(text):
        return False
    return _is_droppable(grid, index)


def _is_section_total(values: list[str]) -> bool:
    """«Общий итог» и «Оборот за отчетный период» относятся к секции, а не к
    выпуску: на них блок выпуска заканчивается."""
    signature = normalize_text(values[0]).casefold().replace("ё", "е")
    return signature.startswith(("общий итог", "оборот за"))


def _is_droppable(grid: ReportGrid, index: int) -> bool:
    """Строку можно выбросить, только если её ячейки принадлежат ей одной.

    Объединение по вертикали (`rowspan`) связывает строки между собой, и
    удаление одной из них сдвинуло бы соседние.
    """
    cells = [cell for cell in grid.rows[index] if cell is not None]
    return bool(cells) and all(
        cell.row == index and cell.rowspan == 1 for cell in cells
    )


def _remove_row(grid: ReportGrid, index: int) -> bool:
    for cell in grid.rows[index]:
        if cell is None or cell.row != index:
            continue
        row_element = cell.element.getparent()
        parent = row_element.getparent() if row_element is not None else None
        if parent is not None:
            parent.remove(row_element)
            return True
        return False
    return False


def is_pseudonym(value: str) -> bool:
    """Значение уже обезличено этим скриптом.

    Настоящий ISIN вида `XX000Z123456` теоретически возможен, но в природе не
    встречается; цена ошибки в эту сторону — лишний псевдоним вместо утечки.
    """
    text = normalize_text(value)
    return text.startswith(PSEUDONYM_PREFIXES) or bool(PSEUDO_ISIN_RE.match(text))


def mask_number(text: str) -> str:
    """Число → форма без величины: `12 345.67` → `0 000.00`, `15` → `00`.

    Сохраняется то, что важно парсеру: знак, разделитель разрядов, число знаков
    после запятой. Порядок величины — не сохраняется: он и есть то, что выдаёт
    размер портфеля.
    """
    value = normalize_text(text)
    sign = "-" if value.startswith("-") else ""
    body = value.lstrip("+-")

    # Разделитель разрядов берётся тот же, что в источнике.
    separator = "\u00a0" if "\u00a0" in body else " "
    decimal_separator = "," if ("," in body and "." not in body) else "."
    fraction = re.split(r"[.,]", body)[1] if re.search(r"[.,]\d", body) else ""

    if not fraction:
        return f"{sign}00"
    # Группа разрядов ставится всегда, даже если её не было: иначе по наличию
    # разделителя видно, была сумма больше тысячи или меньше.
    return f"{sign}0{separator}000{decimal_separator}{'0' * len(fraction)}"


def mask_digits(text: str) -> str:
    """Цифры → нули с сохранением остального: `B-123456-789012` → `B-000000-000000`."""
    return re.sub(r"\d", "0", text)


def _text_nodes(element: Any) -> list[tuple[Any, str, str]]:
    """Все текстовые узлы внутри ячейки как (узел-владелец, поле, значение)."""
    nodes: list[tuple[Any, str, str]] = []
    if element.text and element.text.strip():
        nodes.append((element, "text", element.text))
    for child in element.iter():
        if child is element:
            continue
        if child.text and child.text.strip():
            nodes.append((child, "text", child.text))
        if child.tail and child.tail.strip():
            nodes.append((child, "tail", child.tail))
    return nodes


def _set_text_node(element: Any, node: tuple[Any, str, str], value: str) -> None:
    owner, field_name, _ = node
    setattr(owner, field_name, value)
    _ = element


# --- CLI --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Обезличить отчёт брокера для tests/fixtures/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Результат обязателен к просмотру глазами: свободный текст "
            "обезличивается эвристиками, и скрипт сам сообщает, где мог не сработать."
        ),
    )
    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        metavar="ПУТЬ",
        help="файлы отчётов или каталоги с ними",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="заходить во вложенные каталоги; структура сохраняется в результате",
    )
    parser.add_argument(
        "--pattern",
        metavar="МАСКА",
        help=(
            "маска имён внутри каталога, например 'report_2024*.html'. "
            f"По умолчанию берутся файлы с расширениями {', '.join(REPORT_SUFFIXES)}"
        ),
    )
    parser.add_argument("-o", "--out", type=Path, help="файл результата (для одного входа)")
    parser.add_argument("--out-dir", type=Path, help="каталог результата")
    parser.add_argument(
        "--mapping",
        type=Path,
        help="файл соответствий (по умолчанию .anonymize-mapping.json рядом с результатом)",
    )
    parser.add_argument(
        "--dates",
        choices=("shift", "mask", "keep"),
        default="shift",
        help=(
            "shift — сдвиг на скрытое число дней (по умолчанию); "
            "mask — 00.00.0000; keep — оставить"
        ),
    )
    parser.add_argument(
        "--amounts",
        choices=("mask", "keep"),
        default="mask",
        help="mask — обнулить суммы (по умолчанию); keep — оставить (только для локальных фикстур)",
    )
    parser.add_argument(
        "--keep-instruments",
        action="store_true",
        help="оставить названия бумаг, эмитентов и ISIN как есть (раскрывает состав портфеля)",
    )
    parser.add_argument(
        "--sample",
        type=float,
        metavar="ДОЛЯ",
        help=(
            "оставить только эту долю строк операций, например 0.7 "
            "(секции 2, 5 и 8; там, где строк больше двух). "
            "Итоги после прореживания не сходятся — фикстура проверяет разбор"
        ),
    )
    parser.add_argument(
        "--sample-sections",
        default=",".join(DEFAULT_SAMPLE_SECTIONS),
        metavar="НОМЕРА",
        help="какие секции прореживать, через запятую (по умолчанию 2,5,8)",
    )
    parser.add_argument(
        "--flat-identity",
        action="store_true",
        help="всё личное заменять на XXX вместо псевдонимов по категориям",
    )
    parser.add_argument("--seed", type=int, help="фиксированный сдвиг дат (для тестов)")
    parser.add_argument("--dry-run", action="store_true", help="ничего не записывать")
    parser.add_argument("--quiet", action="store_true", help="только ошибки")
    return parser


# Брокеры отдают HTML-выгрузку и под расширением .xls — это не бинарный Excel,
# а та же таблица, поэтому расширение само по себе ничего не решает.
REPORT_SUFFIXES = (".html", ".htm", ".xls")
ANON_SUFFIX = ".anon.html"


@dataclass(frozen=True)
class Job:
    """Одна пара «исходный отчёт → куда записать результат»."""

    source: Path
    target: Path


def collect_jobs(args: argparse.Namespace) -> tuple[list[Job], list[str]]:
    """Разворачивает аргументы в список заданий.

    Аргументом может быть файл или каталог. Для каталога берутся отчёты внутри
    (с `--recursive` — и во вложенных, с сохранением структуры в результате):
    архив за годы обычно разложен по подкаталогам, и складывать его в один
    плоский каталог — терять эту разметку и напарываться на совпадающие имена.
    """
    jobs: list[Job] = []
    notes: list[str] = []
    seen_targets: dict[Path, Path] = {}

    for entry in args.files:
        if entry.is_dir():
            found = _reports_in(entry, args, notes)
            if not found:
                notes.append(f"{entry}: отчётов не найдено")
            for source in found:
                jobs.append(Job(source=source, target=_target_for(source, entry, args)))
            continue

        if not entry.exists():
            notes.append(f"{entry}: файла нет")
            continue

        jobs.append(Job(source=entry, target=_target_for(entry, entry.parent, args)))

    if args.out is not None:
        # С --out все задания метят в один файл. Схлопывать их в одно нельзя:
        # тогда каталог из десяти отчётов молча превратился бы в один файл.
        return jobs, notes

    unique: list[Job] = []
    for job in jobs:
        clash = seen_targets.get(job.target)
        if clash is not None:
            notes.append(
                f"{job.source}: результат совпал бы с {clash} — пропущен, "
                "разведите каталоги или используйте --recursive"
            )
            continue
        seen_targets[job.target] = job.source
        unique.append(job)
    return unique, notes


def _reports_in(directory: Path, args: argparse.Namespace, notes: list[str]) -> list[Path]:
    pattern = args.pattern or "*"
    entries = directory.rglob(pattern) if args.recursive else directory.glob(pattern)

    found: list[Path] = []
    for path in sorted(entries):
        if not path.is_file():
            continue
        if path.name.endswith(ANON_SUFFIX):
            # Результат предыдущего прогона: обезличивать его повторно
            # бессмысленно, а в каталоге он лежит рядом с исходниками.
            continue
        if args.pattern is None and path.suffix.lower() not in REPORT_SUFFIXES:
            continue
        found.append(path)

    _ = notes
    return found


def _target_for(source: Path, root: Path, args: argparse.Namespace) -> Path:
    if args.out is not None:
        return args.out

    name = source.name
    for suffix in REPORT_SUFFIXES:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    name += ANON_SUFFIX

    if args.out_dir is None:
        return source.with_name(name)

    try:
        relative = source.parent.relative_to(root)
    except ValueError:
        relative = Path()
    return args.out_dir / relative / name


def _mapping_dir(args: argparse.Namespace) -> Path:
    if args.out_dir is not None:
        return args.out_dir
    if args.out is not None:
        return args.out.parent
    first = args.files[0]
    return first if first.is_dir() else first.parent


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    jobs, notes = collect_jobs(args)
    for note in notes:
        print(note, file=sys.stderr)
    if not jobs:
        print("не найдено ни одного отчёта для обработки", file=sys.stderr)
        return 2
    if args.out is not None and len(jobs) > 1:
        print(
            "--out годится для одного файла; для нескольких используйте --out-dir",
            file=sys.stderr,
        )
        return 2

    mapping_path = args.mapping or _mapping_dir(args) / ".anonymize-mapping.json"
    store = AliasStore.load(mapping_path, seed=args.seed)

    if args.sample is not None and not 0 < args.sample <= 1:
        print("--sample принимает долю в диапазоне (0, 1], например 0.7", file=sys.stderr)
        return 2
    if args.sample is not None and args.amounts == "keep":
        print(
            "ВНИМАНИЕ: --sample вместе с --amounts keep даёт настоящие суммы, "
            "которые не сходятся с итогами. Такая фикстура вводит в заблуждение.",
            file=sys.stderr,
        )

    options = Options(
        dates=args.dates,
        amounts=args.amounts,
        keep_instruments=args.keep_instruments,
        flat_identity=args.flat_identity,
        sample=args.sample,
        sample_sections=tuple(
            part.strip() for part in args.sample_sections.split(",") if part.strip()
        ),
    )

    failures = 0
    processed = 0
    skipped = 0

    for job in jobs:
        content = job.source.read_bytes()
        if not _looks_like_report(content):
            print(f"{job.source}: не похоже на отчёт (нет таблиц) — пропущен", file=sys.stderr)
            skipped += 1
            continue

        anonymizer = Anonymizer(store, options)
        try:
            result = anonymizer.run(content)
        except Exception as error:
            print(f"{job.source}: не обработан — {error}", file=sys.stderr)
            failures += 1
            continue

        problem = verify_structure(
            content, result, dropped_rows=anonymizer.stats.dropped_rows
        )
        if problem is not None:
            print(f"{job.source}: структура изменилась — {problem}", file=sys.stderr)
            failures += 1
            continue

        if not args.dry_run:
            job.target.parent.mkdir(parents=True, exist_ok=True)
            job.target.write_bytes(result)

        processed += 1
        if not args.quiet:
            _print_report(job.source, job.target, anonymizer.stats, dry_run=args.dry_run)

    if not args.dry_run:
        store.save()

    if not args.quiet:
        if len(jobs) > 1 or skipped or failures:
            print(
                f"\nИтого: обработано {processed}, пропущено {skipped}, "
                f"с ошибкой {failures} из {len(jobs)}"
            )
        if not args.dry_run:
            print(
                f"Файл соответствий: {mapping_path} — "
                "не коммитить, это ключ к исходным данным."
            )

    return 1 if failures else 0


def _looks_like_report(content: bytes) -> bool:
    """Дешёвая проверка до разбора: в отчёте есть таблицы.

    Каталог архива редко бывает стерильным — в нём лежат pdf, скриншоты и
    случайные выгрузки. Молча превратить такой файл в «обезличенный отчёт»
    хуже, чем пропустить его с явным сообщением.
    """
    return b"<table" in content.lower()[:2_000_000]


def verify_structure(before: bytes, after: bytes, *, dropped_rows: int = 0) -> str | None:
    """Обезличивание правит текст, но не форму. Проверяется той же стадией 1.

    Если таблица, строки или ширины разошлись — скрипт сломал фикстуру, и
    golden-тест на ней проверял бы не то, что в отчёте.

    При прореживании строк становится меньше ровно на число выброшенных;
    построчное сравнение при этом невозможно — индексы сдвигаются, — поэтому
    проверяется число таблиц и точное число оставшихся строк.
    """
    from portfolio.adapters.broker.tables import extract_tables

    source = extract_tables(before)
    target = extract_tables(after)

    if len(source) != len(target):
        return f"таблиц было {len(source)}, стало {len(target)}"

    rows_before = sum(len(table.rows) for table in source)
    rows_after = sum(len(table.rows) for table in target)
    if dropped_rows:
        if rows_after != rows_before - dropped_rows:
            return (
                f"строк было {rows_before}, выброшено {dropped_rows}, "
                f"ожидалось {rows_before - dropped_rows}, стало {rows_after}"
            )
        return None

    for left, right in zip(source, target, strict=True):
        if len(left.rows) != len(right.rows):
            return f"в таблице #{left.index} строк было {len(left.rows)}, стало {len(right.rows)}"
        for index, (row_before, row_after) in enumerate(zip(left.rows, right.rows, strict=True)):
            if len(row_before) != len(row_after):
                return (
                    f"в таблице #{left.index}, строке {index} ячеек было "
                    f"{len(row_before)}, стало {len(row_after)}"
                )
    return None


def _print_report(source: Path, target: Path, stats: Stats, *, dry_run: bool) -> None:
    action = "будет записан" if dry_run else "записан"
    print(f"\n{source} → {target} ({action})")
    if stats.changed:
        print("  заменено:")
        for category, count in stats.changed.most_common():
            print(f"    {category:<16} {count}")
    if stats.dropped:
        print(f"  выброшено при прореживании (строк: {stats.dropped_rows}):")
        for section, count in stats.dropped.most_common():
            groups = stats.dropped_groups.get(section, 0)
            suffix = f", из них бумаг целиком: {groups}" if groups else ""
            print(f"    {count:>4}  {section}{suffix}")
        print("    итоги и остатки после прореживания не сходятся — это ожидаемо")
    if stats.residual:
        print("  ОСТАТОЧНЫЕ ПОДОЗРЕНИЯ — просмотреть глазами:")
        for item, count in stats.residual.most_common(20):
            print(f"    ×{count}  {item}")
    else:
        print("  остаточных подозрений не найдено (но это не гарантия)")


if __name__ == "__main__":
    raise SystemExit(main())
