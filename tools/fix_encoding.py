#!/usr/bin/env python3
"""Починка отчётов с испорченной кодировкой.

Что чинится
-----------
Классическая порча round-trip: текст был в UTF-8, кто-то прочитал его как
CP1251 (или latin-1, koi8-r) и сохранил обратно в UTF-8. «Состояние денежных
средств» превращается в «РЎРѕСЃС‚РѕСЏРЅРёРµ РґРµРЅРµР¶РЅС‹С…».

Такая порча обратима **точно**: обратное преобразование возвращает исходные
байты символ в символ. Скрипт разворачивает её и проверяет результат.

Чего скрипт не делает
---------------------
Не трогает файлы, которые читаются правильно: преобразование над нормальным
русским текстом не проходит (кодировщик отвергает кириллицу вне своей таблицы),
и это не эвристика, а свойство самих кодировок.

Не чинит порчу с потерей: если при первом неверном чтении часть байтов была
заменена на «?» или U+FFFD, восстанавливать нечего. Такой файл скрипт называет
и оставляет как есть — исходник нужно достать заново.

Использование
-------------
    python tools/fix_encoding.py архив/ -r --dry-run   # что будет сделано
    python tools/fix_encoding.py архив/ -r             # рядом: отчёт.fixed.html
    python tools/fix_encoding.py отчёт.html --in-place # поверх, с копией .bak

Проверка результата: `python tools/report_structure.py dump файл` печатает
кодировку и долю посторонних символов.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _report_grid import (
    decode_report,
    detect_encoding,
    iter_report_files,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio.adapters.broker.tables import garbage_ratio

# Кодировки, в которых чаще всего «читают» русский UTF-8 по ошибке.
MOJIBAKE_ENCODINGS = ("cp1251", "cp1252", "latin-1", "koi8-r")

# Порча может быть наложена дважды: файл пережил две неверные перекодировки.
MAX_ROUNDS = 3

# Ниже этого порога текст считается здоровым и не трогается.
GARBAGE_THRESHOLD = 0.15

FIXED_SUFFIX = ".fixed.html"

_META_CHARSET_RE = re.compile(r"""(charset=["']?\s*)([A-Za-z0-9_\-]+)""", re.IGNORECASE)


@dataclass(frozen=True)
class Repair:
    """Результат починки одного файла."""

    path: Path
    encoding: str
    ratio_before: float
    ratio_after: float
    rounds: tuple[str, ...]
    text: str | None

    @property
    def repaired(self) -> bool:
        return bool(self.rounds)

    @property
    def broken(self) -> bool:
        return self.ratio_after > GARBAGE_THRESHOLD

    def describe(self) -> str:
        if self.repaired and not self.broken:
            chain = " → ".join(self.rounds)
            return (
                f"починен ({chain}): посторонних символов было "
                f"{self.ratio_before:.0%}, стало {self.ratio_after:.0%}"
            )
        if self.repaired:
            return (
                f"починен частично: было {self.ratio_before:.0%}, "
                f"стало {self.ratio_after:.0%} — просмотреть глазами"
            )
        if self.broken:
            return (
                f"не чинится: посторонних символов {self.ratio_before:.0%}, "
                "обратное преобразование не проходит. Похоже, при первой "
                "перекодировке часть символов потеряна — нужен исходник"
            )
        return "не требует починки"


def repair_text(text: str) -> tuple[str, list[str]]:
    """Разворачивает порчу, пока результат улучшается.

    Каждый круг: текст кодируется обратно в подозреваемую однобайтную кодировку
    и читается как UTF-8. Круг принимается, только если он **строго** уменьшил
    долю посторонних символов, — поэтому здоровый текст остаётся нетронутым, а
    случайное «улучшение» невозможно.
    """
    current = text
    applied: list[str] = []

    for _ in range(MAX_ROUNDS):
        best: tuple[float, str, str] | None = None
        current_ratio = garbage_ratio(current)

        for encoding in MOJIBAKE_ENCODINGS:
            try:
                candidate = current.encode(encoding).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            ratio = garbage_ratio(candidate)
            if ratio < current_ratio - 0.01 and (best is None or ratio < best[0]):
                best = (ratio, candidate, encoding)

        if best is None:
            break
        applied.append(best[2])
        current = best[1]

    return current, applied


def repair_file(path: Path) -> Repair:
    content = path.read_bytes()
    choice = detect_encoding(content)
    text = decode_report(content)
    before = garbage_ratio(text)

    if before <= GARBAGE_THRESHOLD:
        return Repair(path, choice.name, before, before, (), None)

    fixed, rounds = repair_text(text)
    after = garbage_ratio(fixed)
    if not rounds:
        return Repair(path, choice.name, before, before, (), None)

    return Repair(path, choice.name, before, after, tuple(rounds), _with_utf8_meta(fixed))


def _with_utf8_meta(text: str) -> str:
    """Приводит объявление кодировки к UTF-8: файл сохраняется именно в ней.

    Иначе починенный файл получит ту же болезнь: содержимое одно, объявление
    другое, и следующий читатель снова прочитает его неверно.
    """
    return _META_CHARSET_RE.sub(lambda match: match.group(1) + "utf-8", text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Починить отчёты с испорченной кодировкой",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Чинится порча вида «UTF-8 прочитали как CP1251 и сохранили». "
            "Она обратима точно; файлы, читающиеся правильно, не трогаются."
        ),
    )
    parser.add_argument("files", nargs="+", type=Path, metavar="ПУТЬ",
                        help="файлы отчётов или каталоги с ними")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="заходить во вложенные каталоги")
    parser.add_argument("--pattern", metavar="МАСКА", help="маска имён внутри каталога")
    parser.add_argument("--in-place", action="store_true",
                        help="переписать исходный файл, сохранив копию рядом (.bak)")
    parser.add_argument("--out-dir", type=Path, help="каталог для починенных файлов")
    parser.add_argument("--dry-run", action="store_true", help="ничего не записывать")
    parser.add_argument("--quiet", action="store_true", help="только проблемы")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    files, notes = iter_report_files(
        args.files,
        recursive=args.recursive,
        pattern=args.pattern,
        skip_suffixes=(FIXED_SUFFIX, ".bak"),
    )
    for note in notes:
        print(note, file=sys.stderr)
    if not files:
        print("не найдено ни одного файла", file=sys.stderr)
        return 2

    repaired = 0
    healthy = 0
    hopeless = 0

    for path in files:
        report = repair_file(path)

        if report.repaired and report.text is not None and not args.dry_run:
            target = _target_for(path, args)
            if args.in_place:
                backup = path.with_suffix(path.suffix + ".bak")
                if not backup.exists():
                    backup.write_bytes(path.read_bytes())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(report.text, encoding="utf-8")

        if report.repaired:
            repaired += 1
            if not args.quiet:
                print(f"{path}: {report.describe()}")
        elif report.broken:
            hopeless += 1
            print(f"{path}: {report.describe()}", file=sys.stderr)
        else:
            healthy += 1
            if not args.quiet:
                print(f"{path}: {report.describe()}")

    if not args.quiet:
        action = "было бы починено" if args.dry_run else "починено"
        print(
            f"\nИтого: {action} {repaired}, здоровых {healthy}, "
            f"не чинится {hopeless} из {len(files)}"
        )
    return 1 if hopeless else 0


def _target_for(source: Path, args: argparse.Namespace) -> Path:
    if args.in_place:
        return source
    if args.out_dir is not None:
        return args.out_dir / (source.stem + FIXED_SUFFIX)
    return source.with_name(source.stem + FIXED_SUFFIX)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:  # вывод ушёл в `head` — это не ошибка
        raise SystemExit(0) from None
