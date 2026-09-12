"""`portfolio check`: сверка остатков и жёсткие инварианты.

Сверка идёт **по каждому сохранённому отчёту**, а не только по последнему
(STAGE-1, «Готовность этапа»). Сверка только на последнем отчёте маскирует
взаимно компенсирующиеся ошибки: пропущенная покупка и пропущенная продажа одной
бумаги в разные месяцы дадут верный итог и неверную историю.

Контрольные числа не хранятся отдельной таблицей — они берутся из сохранённого
сырья: `data/raw/` плюс код полностью восстанавливают состояние (спека 5.8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio.adapters.broker.dto import ParsedBalance, UnparsedRow
from portfolio.adapters.broker.mapping import parse
from portfolio.config import Settings, get_settings
from portfolio.db import session_scope
from portfolio.domain.instruments import InstrumentResolver
from portfolio.domain.invariants import Violation, check_all
from portfolio.domain.ledger import load_entries
from portfolio.domain.reconcile import ExpectedBalance, ReconcileResult, reconcile
from portfolio.models import Account, RawReport, Transaction

__all__ = ["CheckResult", "ReportCheck", "run_check"]


@dataclass(frozen=True)
class ReportCheck:
    report_id: int
    filename: str
    period_end: date | None
    account_code: str
    result: ReconcileResult
    unparsed: tuple[UnparsedRow, ...]
    missing_file: bool = False


@dataclass(frozen=True)
class CheckResult:
    reports: tuple[ReportCheck, ...]
    violations: tuple[Violation, ...]
    transactions: int

    @property
    def ok(self) -> bool:
        return (
            not self.violations
            and all(item.result.ok and not item.unparsed for item in self.reports)
            and not any(item.missing_file for item in self.reports)
        )

    @property
    def discrepancy_count(self) -> int:
        return sum(len(item.result.discrepancies) for item in self.reports)


def run_check(settings: Settings | None = None) -> CheckResult:
    """Проверяет журнал целиком. Ничего не пишет."""
    _ = settings or get_settings()

    with session_scope() as session:
        transactions = list(session.scalars(select(Transaction)))
        violations = check_all(transactions)

        reports = tuple(
            _check_report(session, report)
            for report in session.scalars(
                select(RawReport)
                .where(RawReport.source == "broker")
                .order_by(RawReport.period_end, RawReport.id)
            )
        )

        return CheckResult(
            reports=reports,
            violations=tuple(violations),
            transactions=len(transactions),
        )


def _check_report(session: Session, report: RawReport) -> ReportCheck:
    path = Path(report.stored_path)
    account = _account_of(session, report)

    if not path.exists():
        return ReportCheck(
            report_id=report.id,
            filename=report.original_filename,
            period_end=report.period_end,
            account_code=account.code if account is not None else "?",
            result=ReconcileResult(as_of=report.period_end, discrepancies=(), checked=0),
            unparsed=(),
            missing_file=True,
        )

    parsed = parse(path.read_bytes())
    if account is None:
        account = session.scalar(select(Account).where(Account.code == parsed.account_code))
    if account is None:
        raise ValueError(f"отчёт {report.original_filename}: счёт не найден в базе")

    resolver = InstrumentResolver(session)
    expected = [_expected(balance, resolver, parsed.period_end) for balance in parsed.balances]

    result = reconcile(
        load_entries(session, account.id),
        expected,
        as_of=parsed.period_end,
        cash_tolerance=get_settings().reconcile_cash_tolerance,
        quantity_tolerance=get_settings().reconcile_quantity_tolerance,
    )

    return ReportCheck(
        report_id=report.id,
        filename=report.original_filename,
        period_end=parsed.period_end or report.period_end,
        account_code=account.code,
        result=result,
        unparsed=parsed.unparsed,
    )


def _account_of(session: Session, report: RawReport) -> Account | None:
    """Счёт отчёта — тот, на котором лежат записи этого импорта."""
    account_id = session.scalar(
        select(Transaction.account_id).where(Transaction.source_report_id == report.id).limit(1)
    )
    if account_id is None:
        return None
    return session.get(Account, account_id)


def _expected(
    balance: ParsedBalance,
    resolver: InstrumentResolver,
    as_of: date | None,
) -> ExpectedBalance:
    if balance.kind == "cash":
        return ExpectedBalance(
            kind="cash",
            quantity=balance.quantity,
            currency=balance.currency,
            label=balance.currency,
            as_of=balance.as_of or as_of,
        )

    # `find`, а не `resolve`: проверка не заводит справочник. Бумага, которой
    # нет в базе, — расхождение, а не повод создать запись.
    instrument = resolver.find(ticker=balance.ticker, isin=balance.isin)
    return ExpectedBalance(
        kind="security",
        quantity=balance.quantity,
        currency=balance.currency,
        instrument_id=instrument.id if instrument is not None else None,
        label=balance.ticker or balance.isin or "?",
        as_of=balance.as_of or as_of,
    )
