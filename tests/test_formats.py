"""Разбор строк источника. Ошибка здесь даёт тихую порчу сумм, а не падение."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portfolio.adapters.formats import (
    FormatError,
    format_decimal,
    is_blank,
    normalize_text,
    parse_currency,
    parse_date,
    parse_decimal,
    parse_optional_decimal,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 234,56", Decimal("1234.56")),          # неразрывный пробел
        ("1 234,56", Decimal("1234.56")),          # узкий неразрывный
        ("1 234,56", Decimal("1234.56")),               # обычный пробел
        ("1234,56", Decimal("1234.56")),
        ("1234.56", Decimal("1234.56")),
        ("1.234,56", Decimal("1234.56")),               # точка как разряды
        ("1,234.56", Decimal("1234.56")),               # английская запись
        ("-1 234,56", Decimal("-1234.56")),
        ("−1 234,56", Decimal("-1234.56")),   # типографский минус
        ("(1 234,56)", Decimal("-1234.56")),       # скобки как минус
        ("+15,5", Decimal("15.5")),
        ("15,5 руб.", Decimal("15.5")),
        ("100 000 000,01", Decimal("100000000.01")),
        ("0,00", Decimal("0.00")),
        ("12", Decimal("12")),
    ],
)
def test_parse_decimal_handles_report_spellings(raw: str, expected: Decimal) -> None:
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["", "—", "-", "н/д", "абв", "1 2 3,4,5"])
def test_parse_decimal_refuses_to_guess(raw: str) -> None:
    """Отсутствие данных не подменяется нулём (спека 5.4)."""
    with pytest.raises(FormatError):
        parse_decimal(raw)


def test_parse_decimal_rejects_float() -> None:
    with pytest.raises(FormatError):
        parse_decimal(1.5)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", ["", "—", "  ", "-"])
def test_optional_decimal_gives_none_not_zero(raw: str) -> None:
    assert parse_optional_decimal(raw) is None
    assert is_blank(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12.08.2025", date(2025, 8, 12)),
        ("12.08.2025 17:43:01", date(2025, 8, 12)),
        ("2025-08-12", date(2025, 8, 12)),
        ("12.08.25", date(2025, 8, 12)),
    ],
)
def test_parse_date(raw: str, expected: date) -> None:
    assert parse_date(raw) == expected


def test_parse_date_refuses_unknown_format() -> None:
    with pytest.raises(FormatError):
        parse_date("август 2025")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("руб.", "RUB"), ("RUR", "RUB"), ("₽", "RUB"), ("Российский рубль", "RUB"),
        ("USD", "USD"), ("$", "USD"), ("евро", "EUR"), ("CNY", "CNY"),
    ],
)
def test_parse_currency(raw: str, expected: str) -> None:
    assert parse_currency(raw) == expected


def test_parse_currency_refuses_unknown() -> None:
    with pytest.raises(FormatError):
        parse_currency("тугрик")


def test_normalize_text_collapses_nbsp() -> None:
    assert normalize_text(" Итого  по\nсделкам ") == "Итого по сделкам"


def test_format_decimal_matches_report_style() -> None:
    assert format_decimal(Decimal("-1234567.5")) == "-1 234 567,5"
    assert format_decimal(Decimal("1234.5"), decimals=2) == "1 234,50"
