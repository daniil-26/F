"""Чтение и валидация входных CSV через pydantic.

Файлы описывают **желаемое состояние**, а не события (спека 6): импортёр диффит
файл с журналом и сам генерирует сторно. Первая колонка каждого файла — `id`,
стабильный и не переиспользуемый: без явного ключа правка строки неотличима от
удаления плюс добавления.

Ошибки собираются по всему файлу и печатаются в виде «строка 14, поле rate:
ожидалось число», а не бросаются на первой.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from portfolio.adapters.formats import (
    FormatError,
    is_blank,
    parse_currency,
    parse_date,
    parse_decimal,
)

__all__ = [
    "CashFlowRow",
    "CsvValidationError",
    "OpeningBalanceRow",
    "read_cash_flows",
    "read_opening_balances",
    "sniff_kind",
]


class CsvValidationError(ValueError):
    """Ошибки валидации файла целиком, а не первая попавшаяся."""

    def __init__(self, path: Path, problems: list[str]) -> None:
        self.path = path
        self.problems = problems
        super().__init__(f"{path}: " + "; ".join(problems))


class _Row(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    id: str

    @field_validator("id")
    @classmethod
    def _id_not_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("пустой id: дифф идёт по нему, пустым он быть не может")
        return value


class OpeningBalanceRow(_Row):
    """Начальный остаток. Без него сверка не сойдётся никогда (STAGE-1, T9)."""

    account: str
    date: date
    ticker: str | None = None
    isin: str | None = None
    quantity: Decimal
    cost_basis: Decimal | None = None
    basis_quality: str = "unknown"
    currency: str = "RUB"
    note: str | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _parse_date(cls, value: Any) -> Any:
        return parse_date(value) if isinstance(value, str) else value

    @field_validator("quantity", "cost_basis", mode="before")
    @classmethod
    def _parse_number(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and is_blank(value)):
            return None
        return parse_decimal(value) if isinstance(value, str) else value

    @field_validator("currency", mode="before")
    @classmethod
    def _parse_currency(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and is_blank(value)):
            return "RUB"
        return parse_currency(value) if isinstance(value, str) else value

    @field_validator("ticker", "isin", "note", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and is_blank(value):
            return None
        return value

    @field_validator("basis_quality")
    @classmethod
    def _known_quality(cls, value: str) -> str:
        allowed = {"known", "estimated", "unknown"}
        if value not in allowed:
            raise ValueError(f"ожидалось одно из {sorted(allowed)}, получено {value!r}")
        return value

    @property
    def is_cash(self) -> bool:
        return self.ticker is None and self.isin is None


class CashFlowRow(_Row):
    """Довнесения и изъятия, если они не видны из отчёта брокера."""

    account: str
    date: date
    kind: str
    amount: Decimal
    currency: str = "RUB"
    note: str | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _parse_date(cls, value: Any) -> Any:
        return parse_date(value) if isinstance(value, str) else value

    @field_validator("currency", mode="before")
    @classmethod
    def _parse_currency(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and is_blank(value)):
            return "RUB"
        return parse_currency(value) if isinstance(value, str) else value

    @field_validator("amount", mode="before")
    @classmethod
    def _parse_amount(cls, value: Any) -> Any:
        return parse_decimal(value) if isinstance(value, str) else value

    @field_validator("note", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and is_blank(value):
            return None
        return value

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        allowed = {"CASH_IN", "CASH_OUT", "TRANSFER"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"ожидалось одно из {sorted(allowed)}, получено {value!r}")
        return upper


@dataclass(frozen=True)
class _ParsedFile:
    rows: list[dict[str, str]]
    fieldnames: list[str]


def sniff_kind(path: Path) -> str:
    """Определяет вид файла по имени, затем по набору колонок."""
    stem = path.stem.lower()
    if "opening" in stem or "остат" in stem:
        return "opening_balances"
    if "cash" in stem or "flow" in stem:
        return "cash_flows"

    parsed = _read(path)
    columns = {name.strip().lower() for name in parsed.fieldnames}
    if {"quantity"} <= columns and {"cost_basis", "basis_quality"} & columns:
        return "opening_balances"
    if {"amount", "kind"} <= columns:
        return "cash_flows"
    raise CsvValidationError(path, ["вид файла не опознан ни по имени, ни по колонкам"])


def read_opening_balances(path: Path) -> list[OpeningBalanceRow]:
    return _read_rows(path, OpeningBalanceRow)


def read_cash_flows(path: Path) -> list[CashFlowRow]:
    return _read_rows(path, CashFlowRow)


def _read_rows(path: Path, model: type[_Row]) -> list[Any]:
    parsed = _read(path)
    rows: list[Any] = []
    problems: list[str] = []
    seen: dict[str, int] = {}

    for number, raw in enumerate(parsed.rows, start=2):  # строка 1 — заголовок
        if all(is_blank(value) for value in raw.values()):
            continue
        try:
            row = model(**{key.strip(): value for key, value in raw.items() if key})
        except ValidationError as error:
            problems.extend(_describe(number, error))
            continue
        except (FormatError, ValueError) as error:
            problems.append(f"строка {number}: {error}")
            continue

        if row.id in seen:
            problems.append(
                f"строка {number}, поле id: {row.id!r} уже встречался в строке {seen[row.id]}"
            )
            continue
        seen[row.id] = number
        rows.append(row)

    if problems:
        raise CsvValidationError(path, problems)
    return rows


def _describe(number: int, error: ValidationError) -> Iterator[str]:
    for item in error.errors():
        field = ".".join(str(part) for part in item["loc"]) or "?"
        message = item["msg"].removeprefix("Value error, ")
        yield f"строка {number}, поле {field}: {message}"


def _read(path: Path) -> _ParsedFile:
    text = path.read_text(encoding="utf-8-sig")
    dialect = _dialect(text)
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    fieldnames = list(reader.fieldnames or [])
    if not fieldnames:
        raise CsvValidationError(path, ["файл пуст: нет строки заголовка"])
    if fieldnames[0].strip().lower() != "id":
        raise CsvValidationError(
            path, [f"первая колонка должна называться id, а не {fieldnames[0]!r}"]
        )
    return _ParsedFile(rows=[dict(row) for row in reader], fieldnames=fieldnames)


def _dialect(text: str) -> type[csv.Dialect] | csv.Dialect:
    header = text.splitlines()[0] if text.splitlines() else ""
    return csv.excel_tab if "\t" in header else csv.excel
