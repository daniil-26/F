"""Проверка обезличивания отчётов (`tools/anonymize_report.py`).

Фикстура `tests/fixtures/tools/broker_raw_sample.html` — синтетический отчёт в
форме выгрузки этого брокера с **выдуманными** персональными данными: ФИО, номер
счёта, телефон, суммы, названия бумаг, подпись картинкой, `x:num` с точными
числами. Тест проверяет, что ни одна из этих строк не доживает до результата.

Иначе проверять нечем: на уже обезличенном отчёте скрипт молча «работает», и
пропущенная утечка обнаружится только после публикации фикстуры.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from anonymize_report import (  # noqa: E402
    AliasStore,
    Anonymizer,
    Options,
    main,
    mask_number,
    verify_structure,
)

RAW = Path(__file__).parent / "fixtures" / "tools" / "broker_raw_sample.html"

# Всё, что в фикстуре изображает персональные данные.
SECRETS = (
    "Иванов Иван Иванович",
    "Иванова И. И.",
    "БС-12345",
    "40701810012345678901",
    "+7 916 123-45-67",
    "ivanov.ii@example.com",
    "Петров Петр Петрович",
    "Сидорова Анна Сергеевна",
    "Брокерский дом Пример",
    "045-12345-100000",
    "Сбербанк",
    "RU0009029540",
    "RU000A1038V6",
    "10301481B",
    "SU26238RMFS4",
    "ОФЗ 26238",
    "Национальный расчетный депозитарий",
    "1 234 567.89",
    "1234567.89",       # то же число в атрибуте x:num
    "318.45",
    "iVBORw0KGgo",      # подпись картинкой в data:URI
)


@pytest.fixture
def anonymized(tmp_path: Path) -> str:
    store = AliasStore.load(tmp_path / "map.json", seed=7)
    anonymizer = Anonymizer(store, Options())
    return anonymizer.run(RAW.read_bytes()).decode("utf-8")


def test_no_secret_survives(anonymized: str) -> None:
    assert [secret for secret in SECRETS if secret in anonymized] == []


def test_excel_numeric_attributes_are_cleared(anonymized: str) -> None:
    """`x:num` несёт точное число мимо видимого текста.

    Обнулить текст и забыть про атрибут — самая дорогая ошибка этого скрипта:
    суммы остались бы в файле, просто перестали быть видимыми.
    """
    assert 'x:num=""' in anonymized
    assert "x:num=\"1" not in anonymized


def test_images_lose_source(anonymized: str) -> None:
    """В `src` приезжают факсимиле подписей и штампы."""
    assert "data:image" not in anonymized
    assert "<img" in anonymized  # сама картинка остаётся, чтобы форма не менялась


def test_document_metadata_is_dropped(anonymized: str) -> None:
    assert 'name="Author"' not in anonymized
    assert 'name="Company"' not in anonymized
    assert 'name="Generator"' in anonymized  # версия Excel — признак формата, не личное


def test_structure_is_preserved(anonymized: str) -> None:
    """Обезличивание правит текст, но не форму: иначе golden-тест на фикстуре
    проверял бы не то, что в отчёте."""
    assert verify_structure(RAW.read_bytes(), anonymized.encode("utf-8")) is None


def test_section_titles_and_headers_survive(anonymized: str) -> None:
    for marker in (
        "2. Состояние портфеля ценных бумаг",
        "5.1 Биржевые сделки с ценными бумагами",
        "8.1 Неторговые операции с ДС",
        "Наименование ЦБ",
        "Номер гос. регистрации",
        "Дата поставки",
        "Тип операции",
        "Комментарий",
    ):
        assert marker in anonymized, marker


def test_operation_vocabulary_survives(anonymized: str) -> None:
    """Типы операций — то, по чему парсер узнаёт событие. Их терять нельзя."""
    for marker in ("Покупка", "Продажа", "Погашение купона", "Налог", "Вывод ДС", "RUR"):
        assert marker in anonymized, marker


def test_total_rows_are_not_renamed(anonymized: str) -> None:
    """«Итого:» стоит в колонке названий бумаг и однажды стало «БУМАГА-03»."""
    assert "Итого:" in anonymized
    assert "Итого по выпуску:" in anonymized


def test_dates_are_shifted_keeping_intervals(tmp_path: Path) -> None:
    """Сдвиг сохраняет интервалы, порядок и T+1 — без этого фикстура не годится
    для проверки сверки остатков, ради которой она и нужна."""
    store = AliasStore.load(tmp_path / "map.json", seed=7)
    anonymizer = Anonymizer(store, Options(dates="shift"))

    trade = anonymizer._date("05.08.2025")
    settlement = anonymizer._date("06.08.2025")

    assert trade != "05.08.2025"
    assert _delta(trade, settlement) == 1
    assert _delta(anonymizer._date("01.08.2025"), anonymizer._date("31.08.2025")) == 30
    # Кратность семи сохраняет дни недели: торговый день остаётся торговым.
    assert store.date_offset_days % 7 == 0
    assert store.date_offset_days < 0  # в прошлое: даты в будущем читаются как ошибка


def test_mask_mode_is_idempotent(tmp_path: Path) -> None:
    store = AliasStore.load(tmp_path / "map.json", seed=7)
    options = Options(dates="mask")
    once = Anonymizer(store, options).run(RAW.read_bytes())
    twice = Anonymizer(store, options).run(once)
    assert once == twice


def test_same_instrument_gets_one_pseudonym(anonymized: str, tmp_path: Path) -> None:
    """Бумага названа и в портфеле, и в шапке группы сделок, и в комментарии к
    купону. Разные псевдонимы развалили бы сквозной импорт архива."""
    store = AliasStore.load(tmp_path / "map.json", seed=7)
    anonymizer = Anonymizer(store, Options())
    result = anonymizer.run(RAW.read_bytes()).decode("utf-8")

    bond = store.aliases["instrument"]["ОФЗ 26238"]
    assert result.count(bond) >= 2           # портфель и комментарий к купону
    issuer = store.aliases["issuer"]['ПАО "Сбербанк России"']
    assert store.aliases["issuer"]["Сбербанк России"] == issuer
    assert result.count(issuer) >= 3         # портфель, шапка группы, дивиденды


def test_aliases_are_stable_across_runs(tmp_path: Path) -> None:
    """Файл соответствий нужен, чтобы месяцы архива обезличились согласованно."""
    mapping = tmp_path / "map.json"
    first = Anonymizer(AliasStore.load(mapping, seed=7), Options())
    first.run(RAW.read_bytes())
    first.store.save()

    reloaded = AliasStore.load(mapping, seed=999)
    assert reloaded.date_offset_days == first.store.date_offset_days
    assert reloaded.aliases == first.store.aliases


FOREIGN = """<html><body><table>
 <tr><td>Наименование ЦБ</td><td>Эмитент</td><td>Номер гос. регистрации</td><td>ISIN</td>
     <td>Количество ЦБ на конец периода, шт.</td><td>Цена закрытия одной ЦБ</td></tr>
 <tr><td>Apple</td><td>Apple Inc.</td><td>1-02-00100-A</td><td>US0378331005</td>
     <td>10</td><td>190.50</td></tr>
 <tr><td>Сбербанк</td><td>ПАО "Сбербанк России"</td><td>10301481B</td><td>RU0009029540</td>
     <td>100</td><td>318.45</td></tr>
</table></body></html>"""


def test_foreign_isin_pseudonym_keeps_country_code(tmp_path: Path) -> None:
    """Код страны сохраняется: по нему видно, что бумага иностранная."""
    store = AliasStore.load(tmp_path / "map.json", seed=7)
    result = Anonymizer(store, Options()).run(FOREIGN.encode("utf-8")).decode()

    assert "US000Z" in result
    assert "US0378331005" not in result


def test_foreign_isin_pseudonym_is_not_a_residual_suspicion(tmp_path: Path) -> None:
    """Собственный псевдоним не должен попадать в остаточные подозрения.

    Проверка на литерал «RU000Z» объявляла подозрительными псевдонимы всех
    иностранных бумаг, и в отчёте по архиву их были десятки — шум, из-за
    которого настоящую находку не заметить.
    """
    store = AliasStore.load(tmp_path / "map.json", seed=7)
    anonymizer = Anonymizer(store, Options())
    anonymizer.run(FOREIGN.encode("utf-8"))

    assert anonymizer.stats.residual == Counter()


def test_foreign_isin_pseudonym_is_not_realiased(tmp_path: Path) -> None:
    """Повторный прогон не должен переименовывать US000Z… в новый псевдоним:
    иначе фикстуры разных месяцев расходятся."""
    store = AliasStore.load(tmp_path / "map.json", seed=7)
    options = Options(dates="mask")
    once = Anonymizer(store, options).run(FOREIGN.encode("utf-8"))
    twice = Anonymizer(store, options).run(once)

    assert once == twice
    assert len(store.aliases["isin"]) == 2


def test_keep_instruments_keeps_isin(tmp_path: Path) -> None:
    store = AliasStore.load(tmp_path / "map.json", seed=7)
    result = Anonymizer(store, Options(keep_instruments=True)).run(RAW.read_bytes()).decode()

    assert "RU0009029540" in result      # состав портфеля оставлен осознанно
    assert "Иванов Иван Иванович" not in result   # личное вычищается всё равно
    assert "ФИО-01" in result


def test_amounts_keep_mode(tmp_path: Path) -> None:
    store = AliasStore.load(tmp_path / "map.json", seed=7)
    result = Anonymizer(store, Options(amounts="keep")).run(RAW.read_bytes()).decode()
    assert "1 234 567.89" in result
    assert "Иванов Иван Иванович" not in result


def test_residual_scan_reports_what_it_could_not_handle(tmp_path: Path) -> None:
    """Свободный текст обезличивается эвристиками, и скрипт обязан сказать, где
    он мог не справиться: результат всё равно смотрят глазами."""
    store = AliasStore.load(tmp_path / "map.json", seed=7)
    anonymizer = Anonymizer(store, Options())

    raw = RAW.read_text(encoding="utf-8").replace(
        "Списание НДФЛ. НДС не облагается.",
        "Перевод в пользу Общество Дальний Горизонт по заявлению",
    )
    anonymizer.run(raw.encode("utf-8"))

    assert any("ФИО" in item for item in anonymizer.stats.residual)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 234 567.89", "0 000.00"),
        ("318.45", "0 000.00"),        # группа разрядов добавляется: иначе видно порядок
        ("-20 000.00", "-0 000.00"),
        ("300.1000", "0 000.0000"),
        ("50", "00"),
        ("0 000.00", "0 000.00"),      # идемпотентность
    ],
)
def test_mask_number(raw: str, expected: str) -> None:
    assert mask_number(raw) == expected


def test_cli_writes_file_and_mapping(tmp_path: Path) -> None:
    target = tmp_path / "out" / "report.html"
    mapping = tmp_path / "map.json"

    code = main(
        [str(RAW), "-o", str(target), "--mapping", str(mapping), "--seed", "7", "--quiet"]
    )

    assert code == 0
    assert "Иванов Иван Иванович" not in target.read_text(encoding="utf-8")
    assert json.loads(mapping.read_text(encoding="utf-8"))["date_offset_days"] % 7 == 0


def test_cli_dry_run_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "out" / "report.html"
    code = main(
        [
            str(RAW), "-o", str(target),
            "--mapping", str(tmp_path / "map.json"),
            "--seed", "7", "--dry-run", "--quiet",
        ]
    )
    assert code == 0
    assert not target.exists()


def _delta(left: str, right: str) -> int:
    return (_parse(right) - _parse(left)).days


def _parse(value: str) -> date:
    day, month, year = (int(part) for part in value.split("."))
    return date(year, month, day)
