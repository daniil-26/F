"""Поиск файлов отчётов на диске.

Знание о том, как разложен архив, — такой же внешний мир, как HTML и CSV, и
живёт в адаптерах. Импорт архива берёт файлы отсюда же, что и вспомогательные
скрипты: одно правило отбора на всех, иначе `import-broker` возьмёт файлы,
которых не видел инвентарь, и наоборот.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["REPORT_SUFFIXES", "DiscoveredFiles", "discover_reports"]

# Брокеры отдают HTML-выгрузку и под расширением .xls — это не бинарный Excel,
# а та же таблица, поэтому расширение само по себе ничего не решает.
REPORT_SUFFIXES = (".html", ".htm", ".xls")


@dataclass(frozen=True)
class DiscoveredFiles:
    """Найденные файлы и заметки о пропущенном.

    Заметки возвращаются, а не печатаются: молча проигнорированный аргумент
    хуже лишней строки в выводе, но решать, куда её писать, — дело вызывающего.
    """

    files: tuple[Path, ...]
    notes: tuple[str, ...]


def discover_reports(
    paths: Sequence[Path],
    *,
    recursive: bool = False,
    pattern: str | None = None,
    skip_suffixes: tuple[str, ...] = (),
) -> DiscoveredFiles:
    """Разворачивает пути: файл берётся как есть, каталог — по содержимому."""
    found: list[Path] = []
    notes: list[str] = []

    for entry in paths:
        if entry.is_dir():
            inside = _inside(
                entry, recursive=recursive, pattern=pattern, skip_suffixes=skip_suffixes
            )
            if not inside:
                notes.append(f"{entry}: отчётов не найдено")
            found.extend(inside)
            continue
        if not entry.exists():
            notes.append(f"{entry}: файла нет")
            continue
        found.append(entry)

    return DiscoveredFiles(files=tuple(found), notes=tuple(notes))


def _inside(
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
