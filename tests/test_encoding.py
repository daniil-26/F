"""Определение кодировки отчёта.

Ошибка здесь не падает, а тихо портит всё дальше: UTF-8, прочитанный как
CP1251, превращает «Состояние денежных средств» в «РЎРѕСЃС‚РѕСЏРЅРёРµ
РґРµРЅРµР¶РЅС‹С…». Якоря перестают находиться, секции не опознаются, а
обезличенная фикстура уносит мусор в git.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FIXTURES
from portfolio.adapters.broker.tables import (
    decode_report,
    detect_encoding,
    extract_tables,
    garbage_ratio,
)

CP1251_REPORT = FIXTURES / "report_2019-03.html"
UTF8_REPORT = FIXTURES / "report_2025-08.html"


def _document(text: str, charset: str | None = None) -> str:
    meta = f'<meta charset="{charset}">' if charset else ""
    return f"<html><head>{meta}</head><body><table><tr><td>{text}</td></tr></table></body></html>"


def test_honest_utf8() -> None:
    choice = detect_encoding(UTF8_REPORT.read_bytes())
    assert choice.name == "utf-8"
    assert not choice.declaration_lies
    assert "Остатки денежных средств" in decode_report(UTF8_REPORT.read_bytes())


def test_honest_cp1251() -> None:
    choice = detect_encoding(CP1251_REPORT.read_bytes())
    assert choice.name == "windows-1251"
    assert not choice.declaration_lies
    assert "Отчет брокера" in decode_report(CP1251_REPORT.read_bytes())


def test_cp1251_without_declaration() -> None:
    text = CP1251_REPORT.read_bytes().decode("cp1251")
    stripped = text.replace(
        '<meta http-equiv="Content-Type" content="text/html; charset=windows-1251">', ""
    )

    assert "Отчет брокера" in decode_report(stripped.encode("cp1251"))


def test_utf8_with_lying_declaration() -> None:
    """Файл сохранили в UTF-8, а мету от старой версии оставили.

    CP1251 определён почти на всех байтах, поэтому такой файл читается «успешно»
    и молча превращается в мусор. Верить объявлению нельзя.
    """
    # Без заглавной «И»: её байт 0x98 в CP1251 не определён и случайно спасает
    # разбор, роняя его с ошибкой. Здесь спасать нечему.
    raw = _document("Состояние денежных средств").replace(
        "<head>", '<head><meta charset="windows-1251">'
    )
    content = raw.encode("utf-8")

    choice = detect_encoding(content)

    assert choice.name == "utf-8"
    assert choice.declaration_lies
    assert "Состояние денежных средств" in decode_report(content)


def test_utf8_bom_is_stripped() -> None:
    content = b"\xef\xbb\xbf" + _document("Итого").encode("utf-8")

    text = decode_report(content)

    assert detect_encoding(content).name == "utf-8-sig"
    assert not text.startswith("﻿")


def test_already_broken_file_is_flagged() -> None:
    """Файл, испорченный до нас, скрипт не чинит — но обязан сказать о нём.

    Байты сохраняются как есть: чинить вслепую значит добавить второй слой
    перекодировки к первому.
    """
    content = _document("РЎРѕСЃС‚РѕСЏРЅРёРµ РґРµРЅРµР¶РЅС‹С…").encode("utf-8")

    choice = detect_encoding(content)

    assert choice.name == "utf-8"
    assert choice.suspicious
    assert choice.garbage_ratio > 0.3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Состояние денежных средств", 0.0),
        ("Итого: 1 234,56 ₽ «Брокер» — отчёт", 0.0),
        ("Report total 1234.56 USD", 0.0),
        ("РЎРѕСЃС‚РѕСЏРЅРёРµ", 0.3),
    ],
)
def test_garbage_ratio(text: str, expected: float) -> None:
    ratio = garbage_ratio(text)
    assert (ratio > expected) if expected else (ratio == 0.0)


def test_tables_are_readable_in_both_encodings() -> None:
    """Сквозная проверка: до таблиц доезжает читаемая кириллица."""
    for report in (CP1251_REPORT, UTF8_REPORT):
        tables = extract_tables(report.read_bytes())
        text = " ".join(" ".join(row) for table in tables for row in table.rows)
        assert garbage_ratio(text) == 0.0


def test_decoding_never_raises_on_junk(tmp_path: Path) -> None:
    """Каталог архива не стерилен: скрипт не должен падать на бинарном файле."""
    junk = bytes(range(256)) * 4

    assert isinstance(decode_report(junk), str)
