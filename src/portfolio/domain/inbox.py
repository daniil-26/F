"""Очередь того, что требует внимания.

Этап 1 — минимальный состав (STAGE-1, T7): нераспознанные строки отчёта и
расхождения сверки. Рейтинги, оферты и вклады добавятся на этапах 2–3.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from portfolio.adapters.broker.dto import UnparsedRow
from portfolio.domain.reconcile import Discrepancy

__all__ = ["InboxItem", "from_discrepancies", "from_tolerated", "from_unparsed"]


@dataclass(frozen=True)
class InboxItem:
    kind: str
    title: str
    detail: str
    source: str | None = None
    as_of: date | None = None


def from_unparsed(rows: Iterable[UnparsedRow], source: str | None = None) -> list[InboxItem]:
    return [
        InboxItem(
            kind="unparsed_row",
            title=f"Нераспознанная строка: {row.table}",
            detail=f"{row.reason}. Строка: {row.row}",
            source=source,
        )
        for row in rows
    ]


def from_tolerated(
    discrepancies: Sequence[Discrepancy],
    source: str | None = None,
    as_of: date | None = None,
) -> list[InboxItem]:
    """Расхождения, принятые мягким допуском (A-27).

    Во «Входящие» они попадают отдельным видом: импорт они не останавливают, но
    остаток накопителен, и растущая из месяца в месяц разница — уже не
    неточность займа, а ошибка разбора.
    """
    return [
        InboxItem(
            kind="tolerated_discrepancy",
            title=f"Принято в пределах допуска: {item.label}",
            detail=(
                f"в отчёте {item.expected}, в журнале {item.actual}, "
                f"разница {item.difference}. Отчёт содержит заём бумаг (A-27); "
                f"растущая разница означает ошибку разбора"
            ),
            source=source,
            as_of=as_of,
        )
        for item in discrepancies
    ]


def from_discrepancies(
    discrepancies: Sequence[Discrepancy],
    source: str | None = None,
    as_of: date | None = None,
) -> list[InboxItem]:
    return [
        InboxItem(
            kind="reconcile_discrepancy",
            title=f"Расхождение сверки: {item.label}",
            detail=(
                f"в отчёте {item.expected}, в журнале {item.actual}, "
                f"разница {item.difference}"
            ),
            source=source,
            as_of=as_of,
        )
        for item in discrepancies
    ]
