"""Разбор строк источников: русские числа, даты, коды валют.

Модуль маленький, но от него зависит весь импорт: ошибка здесь даёт тихую порчу
сумм, а не падение. Поэтому ни одна функция не возвращает подставное значение —
нераспознанная строка всегда исключение или `None` (спека 5.4).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

__all__ = [
    "SPACE_CHARS",
    "FormatError",
    "format_decimal",
    "is_blank",
    "normalize_text",
    "parse_currency",
    "parse_date",
    "parse_decimal",
    "parse_optional_date",
    "parse_optional_decimal",
]


class FormatError(ValueError):
    """Строка источника не разобрана. Ноль вместо неё не подставляется."""


# Неразрывный, узкий неразрывный, тонкий и обычный пробелы — всё это разделители
# разрядов в отчётах брокера.
SPACE_CHARS = "           \t"

_SPACE_RE = re.compile(f"[{re.escape(SPACE_CHARS)}]+")

# Прочерки и заглушки, означающие «значения нет».
_BLANK_TOKENS = frozenset({"", "-", "--", "—", "–", "−", "n/a", "na", "нет", "null", "none"})

_MINUS_CHARS = "-−–—"

_DATE_FORMATS = (
    "%d.%m.%Y",
    "%d.%m.%y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%Y.%m.%d",
)

_CURRENCY_ALIASES = {
    "RUB": "RUB",
    "RUR": "RUB",
    "643": "RUB",
    "РУБ": "RUB",
    "РУБ.": "RUB",
    "РУБЛЬ": "RUB",
    "РУБЛИ": "RUB",
    "РОССИЙСКИЙ РУБЛЬ": "RUB",
    "₽": "RUB",
    "USD": "USD",
    "840": "USD",
    "$": "USD",
    "ДОЛЛАР": "USD",
    "ДОЛЛАР США": "USD",
    "ДОЛЛ.США": "USD",
    "EUR": "EUR",
    "978": "EUR",
    "€": "EUR",
    "ЕВРО": "EUR",
    "CNY": "CNY",
    "156": "CNY",
    "ЮАНЬ": "CNY",
    "КИТАЙСКИЙ ЮАНЬ": "CNY",
    "HKD": "HKD",
    "GBP": "GBP",
    "CHF": "CHF",
    "KZT": "KZT",
    "BYN": "BYN",
    "TRY": "TRY",
    "AED": "AED",
}


def normalize_text(raw: str | None) -> str:
    """Схлопывает любые пробелы (включая неразрывные) в один обычный, обрезает края."""
    if raw is None:
        return ""
    return _SPACE_RE.sub(" ", raw.replace("\r", " ").replace("\n", " ")).strip()


def is_blank(raw: str | None) -> bool:
    """Пустая ячейка или прочерк — «значения нет», а не ноль."""
    return normalize_text(raw).casefold() in _BLANK_TOKENS


def parse_decimal(raw: str | Decimal | int) -> Decimal:
    """Разбирает русское число: `1 234,56`, `-1\xa0234.56`, `(1 234,56)`, `1.234,56`.

    Скобки означают отрицательное число — так их печатает часть выгрузок.
    """
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):  # pragma: no cover - защита от вызова с float
        raise FormatError("число из источника не может приходить как float")

    text = normalize_text(raw)
    if text.casefold() in _BLANK_TOKENS:
        raise FormatError(f"пустое значение вместо числа: {raw!r}")

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    for minus in _MINUS_CHARS:
        if text.startswith(minus):
            negative = not negative
            text = text[1:].strip()
            break
    if text.startswith("+"):
        text = text[1:].strip()

    text = text.replace(" ", "").replace("'", "")
    text = _strip_currency_suffix(text)
    text = _normalize_separators(text)

    if not text or not re.fullmatch(r"\d*\.?\d+|\d+\.?\d*", text):
        raise FormatError(f"не разобрано как число: {raw!r}")

    try:
        value = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - отсечено регуляркой выше
        raise FormatError(f"не разобрано как число: {raw!r}") from exc

    return -value if negative else value


def parse_optional_decimal(raw: str | Decimal | int | None) -> Decimal | None:
    """То же, но прочерк и пустая ячейка дают `None`, а не исключение."""
    if raw is None:
        return None
    if isinstance(raw, str) and is_blank(raw):
        return None
    return parse_decimal(raw)


def parse_date(raw: str | date | datetime) -> date:
    """Разбирает дату отчёта. Время, если оно есть в ячейке, отбрасывается."""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw

    text = normalize_text(raw)
    if text.casefold() in _BLANK_TOKENS:
        raise FormatError(f"пустое значение вместо даты: {raw!r}")

    # «12.08.2025 17:43:01» и «12.08.2025T00:00:00»
    head = re.split(r"[ T]", text, maxsplit=1)[0]

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    raise FormatError(f"не разобрано как дата: {raw!r}")


def parse_optional_date(raw: str | date | datetime | None) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, str) and is_blank(raw):
        return None
    return parse_date(raw)


def parse_currency(raw: str) -> str:
    """Приводит обозначение валюты к коду ISO-4217."""
    text = normalize_text(raw).upper().rstrip(".")
    if not text:
        raise FormatError(f"пустое значение вместо валюты: {raw!r}")

    for candidate in (text, text + ".", text.replace(" ", "")):
        if candidate in _CURRENCY_ALIASES:
            return _CURRENCY_ALIASES[candidate]

    if re.fullmatch(r"[A-Z]{3}", text):
        return text
    raise FormatError(f"не разобрано как валюта: {raw!r}")


def format_decimal(value: Decimal, decimals: int | None = None) -> str:
    """Печатает число в том же виде, в каком оно приходит из отчёта.

    Обратна `parse_decimal`: `parse_decimal(format_decimal(x)) == x`.
    """
    quantized = value if decimals is None else value.quantize(Decimal(1).scaleb(-decimals))

    sign = "-" if quantized < 0 else ""
    digits = format(abs(quantized), "f")
    whole, _, frac = digits.partition(".")

    groups: list[str] = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    grouped = " ".join(groups)

    return f"{sign}{grouped},{frac}" if frac else f"{sign}{grouped}"


def _strip_currency_suffix(text: str) -> str:
    return re.sub(r"(?i)(₽|руб\.?|rub|rur|usd|\$|eur|€|cny)$", "", text).strip()


def _normalize_separators(text: str) -> str:
    """Сводит `,` и `.` к одной десятичной точке.

    Правило: последний из встретившихся разделителей десятичный, если за ним не
    ровно три цифры; если ровно три и разделитель один — это разряды.
    """
    has_comma = "," in text
    has_dot = "." in text

    if has_comma and has_dot:
        # `1.234,56` против `1,234.56` — десятичным считается последний.
        if text.rfind(",") > text.rfind("."):
            return text.replace(".", "").replace(",", ".")
        return text.replace(",", "")

    if has_comma:
        return _single_separator(text, ",")
    if has_dot:
        return _single_separator(text, ".")
    return text


def _single_separator(text: str, sep: str) -> str:
    parts = text.split(sep)
    if len(parts) > 2:
        # `1.234.567` и `1,234,567` — разделители разрядов, но только если все
        # группы после первой состоят ровно из трёх цифр. Иначе это не число:
        # `1 2 3,4,5` должно дать ошибку, а не 12345.
        if all(len(part) == 3 and part.isdigit() for part in parts[1:]):
            return "".join(parts)
        return ""
    tail = parts[1]
    if sep == "." and len(tail) == 3 and len(parts[0]) <= 3 and text.count(".") == 1:
        # Неоднозначный случай `1.234`. Точка как разделитель разрядов в русских
        # отчётах не используется — трактуем как десятичную.
        return text
    return f"{parts[0]}.{tail}"
