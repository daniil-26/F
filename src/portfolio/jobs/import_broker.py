"""Импорт отчёта брокера. Единственное место с транзакцией.

Порядок (STAGE-1, T8):

    сохранить сырьё с хешем        domain/raw.py
    распарсить                     adapters/broker
    разрешить тикеры               domain/instruments.py
    построить natural_key          domain/events.py
    сдиффить с журналом            domain/ledger.py
    сверить остатки                domain/reconcile.py
    ── граница --dry-run ──
    записать одной транзакцией     domain/ledger.py
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio.adapters.broker.dto import ParsedBalance, ParsedOperation, UnparsedRow
from portfolio.adapters.broker.mapping_v1 import MAPPING_VERSION, parse
from portfolio.config import Settings, get_settings
from portfolio.db import session_scope
from portfolio.domain.events import KeyInput, assign_natural_keys, to_event_type
from portfolio.domain.instruments import InstrumentResolver
from portfolio.domain.ledger import LedgerDiff, LedgerEntry, apply_diff, build_diff, load_entries
from portfolio.domain.raw import store_report
from portfolio.domain.reconcile import ExpectedBalance, ReconcileResult, reconcile
from portfolio.models import Account, AccountKind, FeeKind, Instrument

__all__ = ["ImportResult", "import_broker_report"]


@dataclass(frozen=True)
class ImportResult:
    """Что сделал (или сделал бы) импорт. Печатается CLI как таблица."""

    source: str
    account_code: str
    period_start: date | None
    period_end: date | None
    mapping_version: str
    sha256: str | None
    is_duplicate: bool
    diff: LedgerDiff
    reconcile: ReconcileResult
    unparsed: tuple[UnparsedRow, ...]
    written: int
    dry_run: bool
    committed: bool
    account_created: bool = False

    @property
    def ok(self) -> bool:
        return self.reconcile.ok and not self.unparsed


def import_broker_report(
    path: Path,
    *,
    account_code: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    settings: Settings | None = None,
) -> ImportResult:
    """Импортирует один отчёт. Всё внутри одной транзакции.

    При несошедшейся сверке подтверждение возможно, но требует явного флага
    `force`, а причина пишется в примечание к импорту (спека 4.1).
    """
    config = settings or get_settings()
    content = path.read_bytes()
    report = parse(content)

    with session_scope() as session:
        account, account_created = _resolve_account(
            session, account_code or report.account_code
        )

        stored = None
        if not dry_run:
            stored = store_report(
                session,
                content,
                original_filename=path.name,
                raw_dir=config.raw_dir,
                source="broker",
                parser_version=report.mapping_version,
                period_start=report.period_start,
                period_end=report.period_end,
            )

        resolver = InstrumentResolver(session)
        entries = _to_entries(
            report.operations,
            account=account,
            resolver=resolver,
            source_report_id=stored.report.id if stored is not None else None,
        )

        diff = build_diff(session, entries)
        written = apply_diff(session, diff)

        expected = _to_expected(report.balances, resolver, report.period_end)
        result = reconcile(
            load_entries(session, account.id),
            expected,
            as_of=report.period_end,
            cash_tolerance=config.reconcile_cash_tolerance,
            quantity_tolerance=config.reconcile_quantity_tolerance,
        )

        # ── граница --dry-run ──
        # Реквизиты читаются до отката: после него объекты сессии просрочены.
        sha256 = stored.report.sha256 if stored is not None else None
        is_duplicate = stored.is_duplicate if stored is not None else False
        code = account.code

        committed = not dry_run and (result.ok or force)
        if not committed:
            session.rollback()
        elif force and not result.ok and stored is not None:
            stored.report.note = (
                "импорт подтверждён при несошедшейся сверке: "
                + "; ".join(
                    f"{item.label}: отчёт {item.expected}, журнал {item.actual}"
                    for item in result.discrepancies
                )
            )

        return ImportResult(
            source=str(path),
            account_code=code,
            period_start=report.period_start,
            period_end=report.period_end,
            mapping_version=report.mapping_version or MAPPING_VERSION,
            sha256=sha256,
            is_duplicate=is_duplicate,
            diff=diff,
            reconcile=result,
            unparsed=report.unparsed,
            written=len(written) if committed else 0,
            dry_run=dry_run,
            committed=committed,
            account_created=account_created and committed,
        )


def _resolve_account(session: Session, code: str | None) -> tuple[Account, bool]:
    """Счёт отчёта. Заводится автоматически, если такого кода ещё нет (A-09)."""
    if code:
        found = session.scalar(select(Account).where(Account.code == code))
        if found is not None:
            return found, False

    accounts = list(session.scalars(select(Account)))
    if code is None:
        if len(accounts) == 1:
            return accounts[0], False
        raise ValueError(
            "в отчёте не найден код счёта, и в базе не один счёт — "
            "укажите счёт явно: --account"
        )

    account = Account(
        code=code,
        name=f"Брокерский счёт {code}",
        kind=AccountKind.BROKER,
        currency="RUB",
    )
    session.add(account)
    session.flush()
    return account, True


def _to_entries(
    operations: tuple[ParsedOperation, ...],
    *,
    account: Account,
    resolver: InstrumentResolver,
    source_report_id: int | None,
) -> list[LedgerEntry]:
    instruments: list[Instrument | None] = [
        resolver.resolve(
            ticker=operation.ticker,
            isin=operation.isin,
            name=operation.instrument_name,
            currency=operation.currency,
        )
        for operation in operations
    ]

    keys = assign_natural_keys(
        [
            KeyInput(
                account_code=account.code,
                kind=operation.kind,
                trade_date=operation.trade_date,
                instrument_ref=_instrument_ref(operation, instrument),
                quantity=operation.quantity,
                price=operation.price,
                amount=operation.amount,
                fee_kind=operation.fee_kind,
                broker_trade_no=operation.broker_trade_no,
            )
            for operation, instrument in zip(operations, instruments, strict=True)
        ]
    )

    return [
        LedgerEntry(
            natural_key=key,
            event_type=to_event_type(operation.kind),
            trade_date=operation.trade_date,
            settlement_date=operation.settlement_date,
            account_id=account.id,
            instrument_id=instrument.id if instrument is not None else None,
            quantity=operation.quantity,
            price=operation.price,
            accrued_int=operation.accrued_int,
            fee=operation.fee,
            fee_kind=FeeKind(operation.fee_kind) if operation.fee_kind else None,
            amount=operation.amount,
            currency=operation.currency,
            withheld_at_source=operation.withheld_at_source,
            broker_trade_no=operation.broker_trade_no,
            source_report_id=source_report_id,
            note=operation.note,
        )
        for operation, instrument, key in zip(operations, instruments, keys, strict=True)
    ]


def _instrument_ref(operation: ParsedOperation, instrument: Instrument | None) -> str | None:
    """Стабильная ссылка на инструмент для ключа: ISIN, иначе тикер."""
    if instrument is not None:
        return instrument.isin or instrument.ticker
    return operation.isin or operation.ticker


def _to_expected(
    balances: tuple[ParsedBalance, ...],
    resolver: InstrumentResolver,
    as_of: date | None,
) -> list[ExpectedBalance]:
    result: list[ExpectedBalance] = []
    for balance in balances:
        if balance.kind == "cash":
            result.append(
                ExpectedBalance(
                    kind="cash",
                    quantity=balance.quantity,
                    currency=balance.currency,
                    label=balance.currency,
                    as_of=balance.as_of or as_of,
                )
            )
            continue

        instrument = resolver.resolve(ticker=balance.ticker, isin=balance.isin)
        result.append(
            ExpectedBalance(
                kind="security",
                quantity=balance.quantity,
                currency=balance.currency,
                instrument_id=instrument.id if instrument is not None else None,
                label=balance.ticker or balance.isin or "?",
                as_of=balance.as_of or as_of,
            )
        )
    return result
