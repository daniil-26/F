"""Все таблицы SQLAlchemy одним файлом. Бизнес-методов на моделях нет (спека 9).

Этап 1 создаёт четыре таблицы: `accounts`, `instruments`, `transactions`,
`raw_reports`. Асимметрия намеренная (STAGE-1, T4):

* `transactions` — полная схема по спеке 3.2 сразу, включая поля, которые на
  этом этапе останутся пустыми (`cbr_rate`, `predecessor_id`). Менять схему
  журнала задним числом дорого: переимпорт всей истории.
* `instruments` — минимальный: тикер, ISIN, тип, валюта. Эмитенты, секторы и
  графики платежей появятся на этапе 2.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class _SqliteDecimal(TypeDecorator[Decimal]):
    """Точный Decimal в SQLite, который хранит NUMERIC как float.

    Боевая БД — PostgreSQL с нативным NUMERIC; вариант нужен только для тестов,
    которым не нужен поднятый Postgres. Агрегаты считаются в Python над
    выбранными строками, поэтому лексикографический порядок хранения не мешает.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: object, dialect: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("в денежное поле передан float (спека 2, п.6)")
        return format(Decimal(str(value)), "f")

    def process_result_value(self, value: object, dialect: object) -> Decimal | None:
        return None if value is None else Decimal(str(value))


def _numeric(precision: int, scale: int) -> Numeric:
    return Numeric(precision, scale, asdecimal=True).with_variant(
        _SqliteDecimal(), "sqlite"
    )


# Денежные суммы. NUMERIC, никогда float (спека 2, п.6).
AMOUNT = _numeric(24, 6)
QUANTITY = _numeric(28, 10)
PRICE = _numeric(28, 10)
RATE = _numeric(20, 10)


class Base(DeclarativeBase):
    pass


class EventType(enum.StrEnum):
    """Типы событий журнала (спека 3.2).

    Перечисление живёт здесь, потому что от него зависит CHECK-ограничение в БД.
    Признак «внешний / внутренний» и построение `natural_key` — в `domain/events.py`.
    """

    OPENING_BALANCE = "OPENING_BALANCE"
    CASH_IN = "CASH_IN"
    CASH_OUT = "CASH_OUT"
    TRANSFER = "TRANSFER"
    BUY = "BUY"
    SELL = "SELL"
    COUPON = "COUPON"
    DIVIDEND = "DIVIDEND"
    AMORTIZATION = "AMORTIZATION"
    MATURITY = "MATURITY"
    FX_CONVERT = "FX_CONVERT"
    FEE = "FEE"
    TAX = "TAX"
    CONVERSION = "CONVERSION"
    SPIN_OFF = "SPIN_OFF"
    SPLIT = "SPLIT"
    DEFAULT = "DEFAULT"
    PARTIAL_REDEMPTION = "PARTIAL_REDEMPTION"
    DEPOSIT_OPEN = "DEPOSIT_OPEN"
    DEPOSIT_INTEREST = "DEPOSIT_INTEREST"
    DEPOSIT_CLOSE = "DEPOSIT_CLOSE"


class AccountKind(enum.StrEnum):
    BROKER = "BROKER"
    IIS = "IIS"
    BANK = "BANK"


class InstrumentKind(enum.StrEnum):
    SHARE = "SHARE"
    BOND = "BOND"
    ETF = "ETF"
    CURRENCY = "CURRENCY"
    OTHER = "OTHER"


class BasisQuality(enum.StrEnum):
    KNOWN = "known"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class FeeKind(enum.StrEnum):
    """Комиссии разбираются по типам, не сводятся в одну сумму (спека 4.1)."""

    BROKER = "BROKER"
    DEPOSITARY = "DEPOSITARY"
    EXCHANGE = "EXCHANGE"
    WITHDRAWAL = "WITHDRAWAL"
    OTHER = "OTHER"


def _enum_column(enum_cls: type[enum.Enum], name: str) -> Enum:
    """VARCHAR + CHECK вместо native enum: миграции проще, значений мало."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda e: [member.value for member in e],
        validate_strings=True,
    )


class Account(Base):
    """Брокерский счёт, ИИС или банковский вклад.

    Тип счёта меняет налоговую логику (спека 3): для ИИС вывод средств означает
    закрытие, а перевод между ИИС и обычным счётом не внутренний для налогов,
    хотя внутренний для доходности.
    """

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[AccountKind] = mapped_column(_enum_column(AccountKind, "account_kind"))
    broker: Mapped[str | None] = mapped_column(String(128))
    bank: Mapped[str | None] = mapped_column(String(128))
    currency: Mapped[str] = mapped_column(String(3))
    tax_regime: Mapped[str | None] = mapped_column(String(32))
    opened_at: Mapped[date | None] = mapped_column(Date)
    closed_at: Mapped[date | None] = mapped_column(Date)


class Instrument(Base):
    """Минимальный справочник инструментов. Эмитенты и секторы — этап 2."""

    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str | None] = mapped_column(String(64))
    isin: Mapped[str | None] = mapped_column(String(12))
    name: Mapped[str | None] = mapped_column(String(255))
    kind: Mapped[InstrumentKind] = mapped_column(
        _enum_column(InstrumentKind, "instrument_kind"),
        default=InstrumentKind.OTHER,
    )
    currency: Mapped[str | None] = mapped_column(String(3))

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_instruments_ticker"),
        UniqueConstraint("isin", name="uq_instruments_isin"),
        CheckConstraint(
            "ticker IS NOT NULL OR isin IS NOT NULL",
            name="ck_instruments_identified",
        ),
    )


class RawReport(Base):
    """Сохранённое сырьё: файл на диске, хеш содержимого, версия парсера.

    Имя файла в `data/raw/` — хеш содержимого (спека 9), поэтому повторная
    загрузка того же файла обнаруживается до разбора.
    """

    __tablename__ = "raw_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    source: Mapped[str] = mapped_column(String(32))
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(512))
    parser_version: Mapped[str | None] = mapped_column(String(32))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Transaction(Base):
    """Журнал операций. Append-only: исправление — сторно плюс новая запись.

    Знаки: `quantity` — изменение позиции (покупка +, продажа −), `amount` —
    изменение денежного остатка (приход +, расход −). Обе проекции получаются
    суммированием без ветвлений по типу события (допущение A-05).
    """

    __tablename__ = "transactions"

    # BIGINT в SQLite не автоинкрементный, отсюда вариант для тестовой БД.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True
    )

    # Идемпотентность (спека 3.2): UNIQUE-индекс в БД, а не проверка в коде.
    natural_key: Mapped[str] = mapped_column(String(128))

    event_type: Mapped[EventType] = mapped_column(_enum_column(EventType, "event_type"))

    # Две даты: позиции считаются по trade_date, деньги — по settlement_date.
    trade_date: Mapped[date] = mapped_column(Date)
    settlement_date: Mapped[date | None] = mapped_column(Date)

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"))

    quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    price: Mapped[Decimal | None] = mapped_column(PRICE)
    accrued_int: Mapped[Decimal | None] = mapped_column(AMOUNT)
    fee: Mapped[Decimal | None] = mapped_column(AMOUNT)
    fee_kind: Mapped[FeeKind | None] = mapped_column(_enum_column(FeeKind, "fee_kind"))

    amount: Mapped[Decimal] = mapped_column(AMOUNT)
    currency: Mapped[str] = mapped_column(String(3))

    # Пустует на этапе 1: курсов ЦБ здесь нет по построению.
    cbr_rate: Mapped[Decimal | None] = mapped_column(RATE)

    # NULL означает «неизвестно» (спека 3.2). Восстанавливать gross делением
    # на (1 − ставка) запрещено.
    withheld_at_source: Mapped[bool | None] = mapped_column(Boolean)

    # Инструмент-предшественник для CONVERSION и SPIN_OFF. Пустует на этапе 1.
    predecessor_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"))

    broker_trade_no: Mapped[str | None] = mapped_column(String(64))
    source_report_id: Mapped[int | None] = mapped_column(ForeignKey("raw_reports.id"))
    # `id` строки декларативного CSV — по нему идёт дифф (спека 9).
    source_row_id: Mapped[str | None] = mapped_column(String(64))

    # Начальный остаток (спека 3).
    cost_basis: Mapped[Decimal | None] = mapped_column(PRICE)
    basis_quality: Mapped[BasisQuality | None] = mapped_column(
        _enum_column(BasisQuality, "basis_quality")
    )
    note: Mapped[str | None] = mapped_column(Text)

    reverses_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("natural_key", name="uq_transactions_natural_key"),
        # Операцию сторнируют один раз: второй сторно по той же записи —
        # ошибка диффа, а не сценарий.
        UniqueConstraint("reverses_id", name="uq_transactions_reverses_id"),
        CheckConstraint("length(currency) = 3", name="ck_transactions_currency"),
        Index("ix_transactions_account_trade_date", "account_id", "trade_date"),
        Index("ix_transactions_account_settlement_date", "account_id", "settlement_date"),
        Index("ix_transactions_instrument", "instrument_id"),
        Index("ix_transactions_source_report", "source_report_id"),
        Index("ix_transactions_source_row", "source_row_id"),
    )
