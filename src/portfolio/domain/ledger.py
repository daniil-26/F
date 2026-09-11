"""Запись событий, идемпотентность по `natural_key`, дифф и сторно.

Журнал append-only (спека 2, п.1): запись не редактируется и не удаляется.
Исправление — сторнирующая запись плюс новая.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio.models import BasisQuality, EventType, FeeKind, Transaction

__all__ = ["LedgerDiff", "LedgerEntry", "apply_diff", "build_diff", "load_entries"]


@dataclass(frozen=True)
class LedgerEntry:
    """Событие, готовое к записи: идентификаторы разрешены, ключ построен."""

    natural_key: str
    event_type: EventType
    trade_date: date
    settlement_date: date | None
    account_id: int
    amount: Decimal
    currency: str
    instrument_id: int | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    accrued_int: Decimal | None = None
    fee: Decimal | None = None
    fee_kind: FeeKind | None = None
    withheld_at_source: bool | None = None
    broker_trade_no: str | None = None
    source_report_id: int | None = None
    source_row_id: str | None = None
    cost_basis: Decimal | None = None
    basis_quality: BasisQuality | None = None
    note: str | None = None

    def to_model(self, *, reverses_id: int | None = None) -> Transaction:
        return Transaction(
            natural_key=self.natural_key,
            event_type=self.event_type,
            trade_date=self.trade_date,
            settlement_date=self.settlement_date,
            account_id=self.account_id,
            instrument_id=self.instrument_id,
            quantity=self.quantity,
            price=self.price,
            accrued_int=self.accrued_int,
            fee=self.fee,
            fee_kind=self.fee_kind,
            amount=self.amount,
            currency=self.currency,
            withheld_at_source=self.withheld_at_source,
            broker_trade_no=self.broker_trade_no,
            source_report_id=self.source_report_id,
            source_row_id=self.source_row_id,
            cost_basis=self.cost_basis,
            basis_quality=self.basis_quality,
            note=self.note,
            reverses_id=reverses_id,
        )


@dataclass(frozen=True)
class LedgerDiff:
    """Что импорт сделает с журналом. Печатается как есть при `--dry-run`."""

    new: tuple[LedgerEntry, ...] = ()
    unchanged: tuple[LedgerEntry, ...] = ()
    # (запись журнала, которую сторнируем; новая версия события или None)
    reversals: tuple[tuple[Transaction, LedgerEntry | None], ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def is_empty(self) -> bool:
        return not self.new and not self.reversals

    def summary(self) -> dict[str, int]:
        return {
            "new": len(self.new),
            "unchanged": len(self.unchanged),
            "reversals": len(self.reversals),
        }


def load_entries(
    session: Session,
    account_id: int,
    *,
    upto: date | None = None,
) -> list[Transaction]:
    """Журнал счёта в устойчивом порядке. Проекции строятся только отсюда."""
    statement = select(Transaction).where(Transaction.account_id == account_id)
    if upto is not None:
        statement = statement.where(Transaction.trade_date <= upto)
    return list(session.scalars(statement.order_by(Transaction.trade_date, Transaction.id)))


def build_diff(
    session: Session,
    entries: Sequence[LedgerEntry],
    *,
    source_row_scope: str | None = None,
) -> LedgerDiff:
    """Дифф входного набора с журналом.

    Для отчётов брокера дифф идёт по `natural_key`: событие с уже существующим
    ключом не пишется повторно, и перекрытие периодов между отчётами проходит
    штатно.

    Для декларативных источников (CSV) этого мало. Ключ строки CSV не включает
    сумму — правка ставки дала бы тот же ключ и молча не записалась. Поэтому
    строки файла сверяются по `id` и по содержимому: изменившаяся строка даёт
    сторно плюс новую запись, исчезнувшая — только сторно (спека 6).
    """
    if source_row_scope is not None:
        return _declarative_diff(session, entries, source_row_scope)

    existing = _existing_by_key(session, [entry.natural_key for entry in entries])
    new: list[LedgerEntry] = []
    unchanged: list[LedgerEntry] = []
    for entry in entries:
        if entry.natural_key in existing:
            unchanged.append(entry)
        else:
            new.append(entry)

    return LedgerDiff(new=tuple(new), unchanged=tuple(unchanged))


def _declarative_diff(
    session: Session,
    entries: Sequence[LedgerEntry],
    scope: str,
) -> LedgerDiff:
    prefix = f"{scope}:"
    recorded = list(
        session.scalars(
            select(Transaction).where(Transaction.source_row_id.like(f"{prefix}%"))
        )
    )

    reversed_ids = {row.reverses_id for row in recorded if row.reverses_id is not None}
    live: dict[str, Transaction] = {}
    versions: dict[str, int] = {}
    for row in recorded:
        if row.source_row_id is None:
            continue
        if row.reverses_id is not None:
            continue
        versions[row.source_row_id] = versions.get(row.source_row_id, 0) + 1
        if row.id not in reversed_ids:
            live[row.source_row_id] = row

    new: list[LedgerEntry] = []
    unchanged: list[LedgerEntry] = []
    reversals: list[tuple[Transaction, LedgerEntry | None]] = []
    notes: list[str] = []
    seen: set[str] = set()

    for entry in entries:
        row_id = entry.source_row_id
        if row_id is None:
            raise ValueError("декларативный импорт требует source_row_id у каждой записи")
        seen.add(row_id)

        current = live.get(row_id)
        if current is None:
            new.append(entry)
            continue
        if _same_content(current, entry):
            unchanged.append(entry)
            continue

        revision = versions.get(row_id, 1)
        reversals.append((current, replace(entry, natural_key=f"{entry.natural_key}:r{revision}")))
        notes.append(f"строка {row_id} изменилась: сторно записи #{current.id} и новая запись")

    for row_id, row in live.items():
        if row_id not in seen:
            reversals.append((row, None))
            notes.append(f"строка {row_id} исчезла из файла: сторно записи #{row.id}")

    return LedgerDiff(
        new=tuple(new),
        unchanged=tuple(unchanged),
        reversals=tuple(reversals),
        notes=tuple(notes),
    )


def _same_content(row: Transaction, entry: LedgerEntry) -> bool:
    """Содержательное равенство строки журнала и строки файла.

    Сравниваются только предметные поля: `recorded_at` и ссылка на сырьё
    меняются при каждом импорте и изменением строки не являются.
    """
    return (
        row.event_type == entry.event_type
        and row.trade_date == entry.trade_date
        and row.settlement_date == entry.settlement_date
        and row.instrument_id == entry.instrument_id
        and _same_number(row.quantity, entry.quantity)
        and _same_number(row.price, entry.price)
        and _same_number(row.amount, entry.amount)
        and row.currency == entry.currency
        and _same_number(row.cost_basis, entry.cost_basis)
        and row.basis_quality == entry.basis_quality
    )


def _same_number(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return left == right


def apply_diff(session: Session, diff: LedgerDiff) -> list[Transaction]:
    """Записывает дифф. Вызывается внутри транзакции, открытой в `jobs/`."""
    written: list[Transaction] = []

    for original, replacement in diff.reversals:
        reversal = _reversal_of(original)
        session.add(reversal)
        session.flush()
        written.append(reversal)
        if replacement is not None:
            model = replacement.to_model()
            session.add(model)
            written.append(model)

    for entry in diff.new:
        model = entry.to_model()
        session.add(model)
        written.append(model)

    session.flush()
    return written


def _reversal_of(original: Transaction) -> Transaction:
    """Сторно: те же реквизиты с противоположными знаками.

    Сумма сторно и оригинала равна нулю — жёсткий инвариант (спека 5.2).
    """
    return Transaction(
        natural_key=f"reversal:{original.natural_key}",
        event_type=original.event_type,
        trade_date=original.trade_date,
        settlement_date=original.settlement_date,
        account_id=original.account_id,
        instrument_id=original.instrument_id,
        quantity=None if original.quantity is None else -original.quantity,
        price=original.price,
        accrued_int=None if original.accrued_int is None else -original.accrued_int,
        fee=None if original.fee is None else -original.fee,
        fee_kind=original.fee_kind,
        amount=-original.amount,
        currency=original.currency,
        withheld_at_source=original.withheld_at_source,
        broker_trade_no=original.broker_trade_no,
        source_report_id=original.source_report_id,
        source_row_id=original.source_row_id,
        cost_basis=original.cost_basis,
        basis_quality=original.basis_quality,
        note=f"сторно записи #{original.id}",
        reverses_id=original.id,
    )


def _existing_by_key(session: Session, keys: Sequence[str]) -> dict[str, Transaction]:
    if not keys:
        return {}
    found = session.scalars(select(Transaction).where(Transaction.natural_key.in_(keys)))
    return {row.natural_key: row for row in found}
