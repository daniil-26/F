"""Выжимка архива (`report_structure.py digest`).

Единственный режим инструментов, который предназначен **для передачи наружу**:
владелец архива присылает форму своих отчётов, не присылая сами отчёты. Поэтому
главный тест здесь — про то, чего в выжимке быть не должно.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from anonymize_report import AliasStore, Anonymizer, Options  # noqa: E402
from report_structure import build_digest, main  # noqa: E402

RAW = Path(__file__).parent / "fixtures" / "tools" / "broker_raw_sample.html"
SAMPLING = Path(__file__).parent / "fixtures" / "tools" / "broker_sampling_sample.html"

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
    "Сбербанк",
    "RU0009029540",
    "10301481B",
    "ОФЗ 26238",
    "1 234 567.89",
    "1234567.89",
    "318.45",
    "Национальный расчетный депозитарий",
    "05.08.2025",
)


def test_digest_carries_no_secrets() -> None:
    """Ни одна выдуманная персональная строка фикстуры не должна доехать."""
    text = json.dumps(build_digest([RAW]), ensure_ascii=False)

    assert [secret for secret in SECRETS if secret in text] == []


def test_digest_keeps_what_mapping_needs() -> None:
    """Форма при этом сохраняется полностью: без неё выжимка бесполезна."""
    digest = build_digest([RAW])
    payload = json.dumps(digest, ensure_ascii=False)

    for marker in (
        "2. Состояние портфеля ценных бумаг",
        "8.1 Неторговые операции с ДС",
        "Наименование ЦБ",
        "Дата поставки",
        "Погашение купона",
        "Вывод ДС",
        "RUR",
    ):
        assert marker in payload, marker

    assert digest["number_shapes"]
    assert digest["date_shapes"]


def test_dates_are_coarsened_to_months() -> None:
    """Точная дата отчёта — тоже след владельца; для разбора формата довольно
    года и месяца."""
    files = build_digest([RAW])["files"]
    assert isinstance(files, list)

    assert files[0]["period"] == "2025-08 … 2025-08"


def test_file_names_lose_digits() -> None:
    """В имени файла у брокера бывает номер счёта."""
    files = build_digest([RAW])["files"]
    assert isinstance(files, list)

    assert files[0]["file"] == "broker_raw_sample.html"  # цифр в имени нет
    assert not any(char.isdigit() for char in str(files[0]["file"]))


def test_comment_templates_are_collapsed_to_shapes() -> None:
    """Нужна форма комментария, а не перечень бумаг: иначе «Погашение купона №…»
    дробится на сотню строк по одной."""
    templates = build_digest([RAW])["comment_templates"]
    assert isinstance(templates, dict)

    assert any("<бумага>" in template or "<isin>" in template for template in templates)
    assert not any("ОФЗ 26238" in template for template in templates)


def test_person_in_issuer_name_is_masked() -> None:
    """«им. В.Д. Шашина» — инициалы перед фамилией.

    Найдено на реальном архиве: по имени в названии бумага узнаётся однозначно,
    а прежний шаблон ловил только обратный порядок «Фамилия И. О.».
    """
    store = AliasStore.load(Path("/tmp/never-written.json"), seed=1)
    masked = Anonymizer(store, Options()).mask_text(
        'Акции привилегированные ПАО "Лукойл" им. В.Д. Шашина'
    )

    assert "Шашина" not in masked
    assert "В.Д." not in masked


def test_venue_names_are_not_mistaken_for_people() -> None:
    store = AliasStore.load(Path("/tmp/never-written.json"), seed=1)
    text = "Московская биржа (СПОТ: МБ T+)"

    assert Anonymizer(store, Options()).mask_text(text) == text


def test_signature_rows_do_not_reach_the_vocabulary() -> None:
    """«Руководитель компании | Петров П. П.» стоит в колонке, которая по сетке
    пришлась на «Валюта», и однажды уехала в словарь валют."""
    digest = build_digest([RAW])
    payload = json.dumps(digest, ensure_ascii=False)

    assert "Петров" not in payload
    assert "Сидорова" not in payload


def test_cli_writes_digest(tmp_path: Path) -> None:
    target = tmp_path / "digest.json"

    assert main(["digest", str(RAW), str(SAMPLING), "-o", str(target)]) == 0

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert len(payload["files"]) == 2
