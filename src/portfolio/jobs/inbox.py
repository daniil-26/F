"""`portfolio inbox`: очередь того, что требует внимания.

Этап 1 — нераспознанные строки отчётов и расхождения последней сверки
(спека 4.8). Рейтинги, оферты и вклады добавятся позже.
"""

from __future__ import annotations

from portfolio.config import Settings
from portfolio.domain.inbox import (
    InboxItem,
    from_discrepancies,
    from_tolerated,
    from_unparsed,
)
from portfolio.jobs.check import run_check

__all__ = ["collect_inbox"]


def collect_inbox(settings: Settings | None = None) -> list[InboxItem]:
    check = run_check(settings)
    items: list[InboxItem] = []

    for report in check.reports:
        if report.missing_file:
            items.append(
                InboxItem(
                    kind="missing_raw",
                    title=f"Пропал файл сырья: {report.filename}",
                    detail=(
                        "запись в raw_reports есть, файла в data/raw нет — "
                        "восстановить из резервной копии (спека 5.8)"
                    ),
                    source=report.filename,
                )
            )
        items.extend(from_unparsed(report.unparsed, source=report.filename))
        items.extend(
            from_discrepancies(
                report.result.discrepancies,
                source=report.filename,
                as_of=report.period_end,
            )
        )
        items.extend(
            from_tolerated(
                report.result.tolerated,
                source=report.filename,
                as_of=report.period_end,
            )
        )

    items.extend(
        InboxItem(
            kind="invariant",
            title=f"Нарушен инвариант: {violation.check}",
            detail=violation.message,
        )
        for violation in check.violations
    )
    return items
