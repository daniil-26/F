"""Снапшоты ответов ISS на диске.

`snapshots/{дата}/{эндпоинт}/{secid}.json` (`docs/PARALLEL-TRACK.md`, A1).
Структура плоская намеренно: снапшот должен читаться глазами и `jq`, а не
только кодом, который его записал.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

JsonDict = dict[str, Any]

BOARDS = "boards"
BOARD_SECURITIES = "board-securities"
SECURITIES = "securities"
BONDIZATION = "bondization"
HISTORY = "history"


class SnapshotMissing(FileNotFoundError):
    """Снапшота нет на диске. Разбор из сети не подхватывается: сначала выгрузка."""


@dataclass(frozen=True)
class SnapshotStore:
    """Каталог снапшотов одной даты."""

    root: Path
    day: str

    @classmethod
    def for_day(cls, root: Path, day: date | str | None = None) -> SnapshotStore:
        if day is None:
            return cls.latest(root)
        return cls(root=root, day=day if isinstance(day, str) else day.isoformat())

    @classmethod
    def latest(cls, root: Path) -> SnapshotStore:
        """Самый свежий снапшот. Пустой каталог — ошибка, а не пустой результат."""
        days = sorted(path.name for path in root.glob("*") if path.is_dir())
        if not days:
            raise SnapshotMissing(f"в {root} нет ни одного снапшота")
        return cls(root=root, day=days[-1])

    @property
    def directory(self) -> Path:
        return self.root / self.day

    def path(self, endpoint: str, name: str) -> Path:
        return self.directory / endpoint / f"{name}.json"

    def save(self, endpoint: str, name: str, payload: JsonDict) -> Path:
        path = self.path(endpoint, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    def load(self, endpoint: str, name: str) -> JsonDict:
        path = self.path(endpoint, name)
        if not path.exists():
            raise SnapshotMissing(str(path))
        payload: JsonDict = json.loads(path.read_text(encoding="utf-8"))
        return payload

    def has(self, endpoint: str, name: str) -> bool:
        return self.path(endpoint, name).exists()

    def names(self, endpoint: str) -> list[str]:
        directory = self.directory / endpoint
        if not directory.exists():
            return []
        return sorted(path.stem for path in directory.glob("*.json"))
