"""Выбор версии разбора по форме документа.

Версии не заменяют друг друга, а сосуществуют (спека 4.1): архив за десять лет
содержит отчёты обоих форматов, и старые файлы обязаны разбираться той версией,
для которой они написаны. Номер версии пишется в `raw_reports.parser_version`,
поэтому позже видно, чем разобран каждый файл.

Признак выбирается по документу, а не по дате отчёта: брокер менял выгрузку не в
один день для всех клиентов, а даты в имени файла может не быть вовсе.
"""

from __future__ import annotations

from portfolio.adapters.broker import mapping_v1, mapping_v2
from portfolio.adapters.broker.dto import (
    ParsedBalance,
    ParsedOperation,
    ParsedReport,
    UnparsedRow,
)
from portfolio.adapters.broker.tables import extract_tables

__all__ = ["MAPPING_VERSION", "VERSIONS", "parse", "parse_report"]

# Версия по умолчанию: ею помечается результат, если разбор не сказал иного.
MAPPING_VERSION = mapping_v1.MAPPING_VERSION

VERSIONS = (mapping_v1.MAPPING_VERSION, mapping_v2.MAPPING_VERSION)


def parse(content: bytes) -> ParsedReport:
    """Разбирает отчёт подходящей версией. Таблицы достаются один раз."""
    tables = extract_tables(content)
    if mapping_v2.looks_like_v2(content, tables):
        return mapping_v2.parse(content, tables)
    return mapping_v1.parse(content, tables)


def parse_report(
    content: bytes,
) -> tuple[list[ParsedOperation], list[ParsedBalance], list[UnparsedRow]]:
    """Контракт парсера (STAGE-1, T6): `bytes` → (операции, остатки, нераспознанное)."""
    report = parse(content)
    return list(report.operations), list(report.balances), list(report.unparsed)
