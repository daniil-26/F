"""stage 1: accounts, instruments, transactions, raw_reports

Схема этапа 1 (STAGE-1, T4). Миграции применяются только к PostgreSQL —
тестовая SQLite создаётся из метаданных, поэтому вариантов типов здесь нет.

Revision ID: 1295064538cb
Revises: 
Create Date: 2026-09-11 14:34:20.983837
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '1295064538cb'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.Enum('BROKER', 'IIS', 'BANK', name='account_kind', native_enum=False, length=32), nullable=False),
    sa.Column('broker', sa.String(length=128), nullable=True),
    sa.Column('bank', sa.String(length=128), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('tax_regime', sa.String(length=32), nullable=True),
    sa.Column('opened_at', sa.Date(), nullable=True),
    sa.Column('closed_at', sa.Date(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_table('instruments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ticker', sa.String(length=64), nullable=True),
    sa.Column('isin', sa.String(length=12), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('kind', sa.Enum('SHARE', 'BOND', 'ETF', 'CURRENCY', 'OTHER', name='instrument_kind', native_enum=False, length=32), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.CheckConstraint('ticker IS NOT NULL OR isin IS NOT NULL', name='ck_instruments_identified'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('isin', name='uq_instruments_isin'),
    sa.UniqueConstraint('ticker', name='uq_instruments_ticker')
    )
    op.create_table('raw_reports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('original_filename', sa.String(length=255), nullable=False),
    sa.Column('stored_path', sa.String(length=512), nullable=False),
    sa.Column('parser_version', sa.String(length=32), nullable=True),
    sa.Column('period_start', sa.Date(), nullable=True),
    sa.Column('period_end', sa.Date(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('imported_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('sha256')
    )
    op.create_table('transactions',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('natural_key', sa.String(length=128), nullable=False),
    sa.Column('event_type', sa.Enum('OPENING_BALANCE', 'CASH_IN', 'CASH_OUT', 'TRANSFER', 'BUY', 'SELL', 'COUPON', 'DIVIDEND', 'AMORTIZATION', 'MATURITY', 'FX_CONVERT', 'FEE', 'TAX', 'CONVERSION', 'SPIN_OFF', 'SPLIT', 'DEFAULT', 'PARTIAL_REDEMPTION', 'DEPOSIT_OPEN', 'DEPOSIT_INTEREST', 'DEPOSIT_CLOSE', name='event_type', native_enum=False, length=32), nullable=False),
    sa.Column('trade_date', sa.Date(), nullable=False),
    sa.Column('settlement_date', sa.Date(), nullable=True),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=28, scale=10), nullable=True),
    sa.Column('price', sa.Numeric(precision=28, scale=10), nullable=True),
    sa.Column('accrued_int', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('fee', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('fee_kind', sa.Enum('BROKER', 'DEPOSITARY', 'EXCHANGE', 'WITHDRAWAL', 'OTHER', name='fee_kind', native_enum=False, length=32), nullable=True),
    sa.Column('amount', sa.Numeric(precision=24, scale=6), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('cbr_rate', sa.Numeric(precision=20, scale=10), nullable=True),
    sa.Column('withheld_at_source', sa.Boolean(), nullable=True),
    sa.Column('predecessor_id', sa.Integer(), nullable=True),
    sa.Column('broker_trade_no', sa.String(length=64), nullable=True),
    sa.Column('source_report_id', sa.Integer(), nullable=True),
    sa.Column('source_row_id', sa.String(length=64), nullable=True),
    sa.Column('cost_basis', sa.Numeric(precision=28, scale=10), nullable=True),
    sa.Column('basis_quality', sa.Enum('known', 'estimated', 'unknown', name='basis_quality', native_enum=False, length=32), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('reverses_id', sa.BigInteger(), nullable=True),
    sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.CheckConstraint('length(currency) = 3', name='ck_transactions_currency'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], ),
    sa.ForeignKeyConstraint(['predecessor_id'], ['instruments.id'], ),
    sa.ForeignKeyConstraint(['reverses_id'], ['transactions.id'], ),
    sa.ForeignKeyConstraint(['source_report_id'], ['raw_reports.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('natural_key', name='uq_transactions_natural_key'),
    sa.UniqueConstraint('reverses_id', name='uq_transactions_reverses_id')
    )
    op.create_index('ix_transactions_account_settlement_date', 'transactions', ['account_id', 'settlement_date'], unique=False)
    op.create_index('ix_transactions_account_trade_date', 'transactions', ['account_id', 'trade_date'], unique=False)
    op.create_index('ix_transactions_instrument', 'transactions', ['instrument_id'], unique=False)
    op.create_index('ix_transactions_source_report', 'transactions', ['source_report_id'], unique=False)
    op.create_index('ix_transactions_source_row', 'transactions', ['source_row_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_transactions_source_row', table_name='transactions')
    op.drop_index('ix_transactions_source_report', table_name='transactions')
    op.drop_index('ix_transactions_instrument', table_name='transactions')
    op.drop_index('ix_transactions_account_trade_date', table_name='transactions')
    op.drop_index('ix_transactions_account_settlement_date', table_name='transactions')
    op.drop_table('transactions')
    op.drop_table('raw_reports')
    op.drop_table('instruments')
    op.drop_table('accounts')
