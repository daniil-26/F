"""Импорт декларативных CSV: начальные остатки и денежные потоки.

Файл описывает желаемое состояние. Импортёр диффит его с журналом по колонке
`id` и сам генерирует сторно: правка строки даёт сторно плюс новую запись, а не
удаление (спека 6). Механизм проверяется на самом простом файле — потом тот же
код обслуживает вклады и денежные потоки.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio.adapters.csv_input import (
    CashFlowRow,
    OpeningBalanceRow,
    read_cash_flows,
    read_opening_balances,
    sniff_kind,
)
from portfolio.config import Settings, get_settings
from portfolio.db import session_scope
from portfolio.domain.events import KeyInput, natural_key, to_event_type
from portfolio.domain.instruments import InstrumentResolver
from portfolio.domain.ledger import LedgerDiff, LedgerEntry, apply_diff, build_diff
from portfolio.models import Account, AccountKind, BasisQuality, EventType

__all__ = ["CsvImportResult", "import_csv_file"]


@dataclass(frozen=True)
class CsvImportResult:
    source: str
    kind: str
    rows: int
    diff: LedgerDiff
    written: int
    dry_run: bool
    committed: bool


def import_csv_file(
    path: Path,
    *,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> CsvImportResult:
    """Импортирует один декларативный файл целиком."""
    _ = settings or get_settings()
    kind = sniff_kind(path)
    scope = kind

    with session_scope() as session:
        resolver = InstrumentResolver(session)

        if kind == "opening_balances":
            opening_rows = read_opening_balances(path)
            entries = _opening_entries(session, resolver, opening_rows, scope)
            row_count = len(opening_rows)
        elif kind == "cash_flows":
            flow_rows = read_cash_flows(path)
            entries = _cash_flow_entries(session, flow_rows, scope)
            row_count = len(flow_rows)
        else:  # pragma: no cover - sniff_kind не возвращает других значений
            raise ValueError(f"неизвестный вид файла: {kind}")

        diff = build_diff(session, entries, source_row_scope=scope)
        written = apply_diff(session, diff)

        committed = not dry_run
        if not committed:
            session.rollback()

        return CsvImportResult(
            source=str(path),
            kind=kind,
            rows=row_count,
            diff=diff,
            written=len(written) if committed else 0,
            dry_run=dry_run,
            committed=committed,
        )


def _opening_entries(
    session: Session,
    resolver: InstrumentResolver,
    rows: list[OpeningBalanceRow],
    scope: str,
) -> list[LedgerEntry]:
    entries: list[LedgerEntry] = []

    for row in rows:
        account = _account(session, row.account)
        instrument = (
            None
            if row.is_cash
            else resolver.resolve(ticker=row.ticker, isin=row.isin, currency=row.currency)
        )
        source_row_id = f"{scope}:{row.id}"

        entries.append(
            LedgerEntry(
                natural_key=natural_key(
                    KeyInput(
                        account_code=account.code,
                        kind=EventType.OPENING_BALANCE.value,
                        trade_date=row.date,
                        instrument_ref=row.isin or row.ticker,
                        source_row_id=source_row_id,
                    )
                ),
                event_type=EventType.OPENING_BALANCE,
                trade_date=row.date,
                settlement_date=row.date,
                account_id=account.id,
                instrument_id=instrument.id if instrument is not None else None,
                # Денежный остаток входит в `amount`, бумажный — в `quantity`.
                # Начальный остаток бумаг денег не двигает.
                quantity=None if row.is_cash else row.quantity,
                amount=row.quantity if row.is_cash else Decimal(0),
                currency=row.currency,
                cost_basis=row.cost_basis,
                basis_quality=BasisQuality(row.basis_quality),
                source_row_id=source_row_id,
                note=row.note,
            )
        )
    return entries


def _cash_flow_entries(
    session: Session,
    rows: list[CashFlowRow],
    scope: str,
) -> list[LedgerEntry]:
    entries: list[LedgerEntry] = []

    for row in rows:
        account = _account(session, row.account)
        source_row_id = f"{scope}:{row.id}"
        event_type = to_event_type(row.kind)
        # Знак задаётся типом события, а не колонкой: изъятие уменьшает остаток
        # независимо от того, как записана сумма в файле.
        amount = -abs(row.amount) if event_type is EventType.CASH_OUT else row.amount

        entries.append(
            LedgerEntry(
                natural_key=natural_key(
                    KeyInput(
                        account_code=account.code,
                        kind=event_type.value,
                        trade_date=row.date,
                        source_row_id=source_row_id,
                    )
                ),
                event_type=event_type,
                trade_date=row.date,
                settlement_date=row.date,
                account_id=account.id,
                amount=amount,
                currency=row.currency,
                source_row_id=source_row_id,
                note=row.note,
            )
        )
    return entries


def _account(session: Session, code: str) -> Account:
    found = session.scalar(select(Account).where(Account.code == code))
    if found is not None:
        return found

    account = Account(
        code=code,
        name=f"Счёт {code}",
        kind=AccountKind.BROKER,
        currency="RUB",
    )
    session.add(account)
    session.flush()
    return account
