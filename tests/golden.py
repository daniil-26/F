"""Сериализация результата разбора для golden-тестов.

Отдельный модуль, чтобы одно и то же представление использовали и тест, и
скрипт обновления фикстур: расхождение между ними прятало бы регрессии.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from portfolio.adapters.broker.dto import ParsedReport
from portfolio.adapters.broker.tables import RawTable


def report_to_dict(report: ParsedReport) -> dict[str, Any]:
    return {
        "mapping_version": report.mapping_version,
        "period_start": _text(report.period_start),
        "period_end": _text(report.period_end),
        "account_code": report.account_code,
        "operations": [
            {
                "kind": operation.kind,
                "trade_date": _text(operation.trade_date),
                "settlement_date": _text(operation.settlement_date),
                "ticker": operation.ticker,
                "isin": operation.isin,
                "quantity": _text(operation.quantity),
                "price": _text(operation.price),
                "accrued_int": _text(operation.accrued_int),
                "fee": _text(operation.fee),
                "fee_kind": operation.fee_kind,
                "amount": _text(operation.amount),
                "currency": operation.currency,
                "broker_trade_no": operation.broker_trade_no,
                "withheld_at_source": operation.withheld_at_source,
            }
            for operation in report.operations
        ],
        "balances": [
            {
                "kind": balance.kind,
                "ticker": balance.ticker,
                "isin": balance.isin,
                "quantity": _text(balance.quantity),
                "currency": balance.currency,
                "as_of": _text(balance.as_of),
            }
            for balance in report.balances
        ],
        "unparsed": [
            {"table": row.table, "reason": row.reason, "row": row.row}
            for row in report.unparsed
        ],
    }


def tables_to_dict(tables: list[RawTable]) -> dict[str, Any]:
    """Форма документа: сколько таблиц, какие заголовки, какие размерности."""
    return {
        "count": len(tables),
        "tables": [
            {
                "index": table.index,
                "caption": table.caption,
                "preceding_text": list(table.preceding_text),
                "headers": list(table.headers),
                "rows": len(table.rows),
                "width": table.width,
            }
            for table in tables
        ],
    }


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _text(value: Decimal | Any) -> str | None:
    if value is None:
        return None
    return format(value, "f") if isinstance(value, Decimal) else str(value)
