"""Сохранение сырья: файл на диск, хеш, защита от повторной загрузки.

Сырьё сохраняется всегда (спека 2, п.3): без него историю нельзя переиграть
новым парсером, а именно этим заканчивается каждая правка `mapping_v*`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio.models import RawReport

__all__ = ["StoredReport", "content_hash", "store_report"]


@dataclass(frozen=True)
class StoredReport:
    report: RawReport
    is_duplicate: bool


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def store_report(
    session: Session,
    content: bytes,
    *,
    original_filename: str,
    raw_dir: Path,
    source: str = "broker",
    parser_version: str | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    note: str | None = None,
) -> StoredReport:
    """Кладёт файл в `data/raw/` под именем-хешем и заводит запись `raw_reports`.

    Повторная загрузка того же файла обнаруживается по хешу до разбора. Запись
    при этом возвращается существующая: импорт остаётся идемпотентным, а не
    падает — перекрытие периодов между отчётами штатно (спека 4.1).
    """
    digest = content_hash(content)
    existing = session.scalar(select(RawReport).where(RawReport.sha256 == digest))
    if existing is not None:
        return StoredReport(report=existing, is_duplicate=True)

    suffix = Path(original_filename).suffix or ".bin"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stored_path = raw_dir / f"{digest}{suffix}"
    if not stored_path.exists():
        stored_path.write_bytes(content)

    report = RawReport(
        sha256=digest,
        source=source,
        original_filename=original_filename,
        stored_path=str(stored_path),
        parser_version=parser_version,
        period_start=period_start,
        period_end=period_end,
        note=note,
    )
    session.add(report)
    session.flush()
    return StoredReport(report=report, is_duplicate=False)
