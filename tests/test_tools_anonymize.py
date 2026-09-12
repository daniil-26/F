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
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from _report_grid import ReportGrid, build_grid, parse_document  # noqa: E402
from anonymize_report import (  # noqa: E402
    AliasStore,
    Anonymizer,
    Options,
    collect_jobs,
    main,
    mask_number,
    verify_structure,
)

RAW = Path(__file__).parent / "fixtures" / "tools" / "broker_raw_sample.html"
# Отчёт с пятью бумагами в портфеле, четырьмя выпусками в сделках (3, 2, 4 и 1
# сделка) и шестью неторговыми операциями — на нём видно прореживание.
SAMPLING = Path(__file__).parent / "fixtures" / "tools" / "broker_sampling_sample.html"

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


# --- прореживание -----------------------------------------------------------


def _run(source: Path, tmp_path: Path, **options: object) -> tuple[ReportGrid, Anonymizer]:
    store = AliasStore.load(tmp_path / "map.json", seed=7)
    anonymizer = Anonymizer(store, Options(**options))  # type: ignore[arg-type]
    result = anonymizer.run(source.read_bytes())
    return build_grid(parse_document(result)), anonymizer


def _section_rows(grid: ReportGrid, title_prefix: str) -> list[list[str]]:
    """Непустые строки секции, кроме подписи секции и шапок."""
    return [
        [value for value in grid.row_text(index) if value]
        for index in range(len(grid.rows))
        if (grid.sections.get(index) or "").startswith(title_prefix)
        and not grid.is_section_row(index)
        and not grid.is_header_row(index)
        and any(grid.row_text(index))
    ]


def test_section_title_with_two_decimals_is_not_masked(tmp_path: Path) -> None:
    """«5.10 Исполнение обязательств по сделке» подходит под форму денежного
    числа и превращалось в «0 000.00 Исполнение обязательств по сделке»."""
    report = """<html><body><table>
     <tr><td colspan="3">5.10 Исполнение обязательств по сделке</td></tr>
     <tr><td>Дата</td><td>Тип операции</td><td>Сумма</td><td>Валюта</td></tr>
     <tr><td>05.08.2025</td><td>Покупка</td><td>1 234.56</td><td>RUR</td></tr>
    </table></body></html>"""

    store = AliasStore.load(tmp_path / "map.json", seed=7)
    result = Anonymizer(store, Options()).run(report.encode("utf-8")).decode()

    assert "5.10 Исполнение обязательств по сделке" in result
    assert "1 234.56" not in result


def test_sample_keeps_fraction_of_portfolio_rows(tmp_path: Path) -> None:
    """Секция 2: пять бумаг, 0.7 — остаётся четыре, «Итого» на месте."""
    grid, _ = _run(SAMPLING, tmp_path, sample=0.7)
    rows = _section_rows(grid, "2. Состояние портфеля")

    assert len([row for row in rows if row[0].startswith("БУМАГА")]) == 4
    assert any(row[0].startswith("Итого") for row in rows)


def test_sample_keeps_fraction_of_instruments_and_their_rows(tmp_path: Path) -> None:
    """Секция 5: четыре выпуска → три; внутри выпуска из четырёх сделок → три.

    Выпуск из двух сделок не трогается: правило «больше двух» применяется на
    каждом уровне отдельно.
    """
    grid, _ = _run(SAMPLING, tmp_path, sample=0.7)
    rows = _section_rows(grid, "5.1 Биржевые сделки")

    groups = [row for row in rows if len(row) == 1 and row[0].startswith("ПАО")]
    trades = [row for row in rows if row[0].startswith("B-")]
    assert len(groups) == 3
    assert len(trades) == 6  # 2 + 3 + 1

    # У каждого оставшегося выпуска остался его «Итого по выпуску».
    assert len([row for row in rows if row[0].startswith("Итого по выпуску")]) == 3
    assert any(row[0].startswith("Общий итог") for row in rows)


def test_dropped_instrument_leaves_no_trace(tmp_path: Path) -> None:
    """Выпуск уходит целиком: без подзаголовка, сделок и своего итога.

    Итог без сделок дал бы фикстуру, которой в природе не бывает.
    """
    grid, anonymizer = _run(SAMPLING, tmp_path, sample=0.7)
    rows = _section_rows(grid, "5.1 Биржевые сделки")

    headers = [row[0] for row in rows if len(row) == 1]
    assert len(headers) == len(set(headers))
    assert anonymizer.stats.dropped_groups["5.1 Биржевые сделки с ценными бумагами"] == 1


def test_sample_keeps_fraction_of_cash_operations(tmp_path: Path) -> None:
    """Секция 8: шесть операций → четыре."""
    grid, _ = _run(SAMPLING, tmp_path, sample=0.7)
    rows = _section_rows(grid, "8.1 Неторговые операции")

    operations = [row for row in rows if len(row) >= 4]
    assert len(operations) == 4


def test_sample_does_not_touch_two_rows_or_fewer(tmp_path: Path) -> None:
    """Правило из задачи: прореживать только там, где строк больше двух."""
    grid, _ = _run(RAW, tmp_path, sample=0.5)
    rows = _section_rows(grid, "2. Состояние портфеля")

    assert len([row for row in rows if row[0].startswith("БУМАГА")]) == 2


def test_sample_does_not_touch_other_sections(tmp_path: Path) -> None:
    """Секция 1 — разложение остатка, а не список операций: её не прореживаем."""
    grid, _ = _run(RAW, tmp_path, sample=0.5)
    rows = _section_rows(grid, "1. Состояние денежных средств")

    assert [row[0] for row in rows] == [
        "Входящий остаток (всего):",
        "0 000.00",
        "Уплаченная комиссия и сборы, в том числе:",
        "комиссия Брокера",
        "комиссия Депозитария Брокера",
        "Исходящий остаток:",
    ]


def test_sample_keeps_document_tail(tmp_path: Path) -> None:
    """Подписи и дата формирования формально принадлежат последней секции, но
    строками таблицы не являются."""
    grid, _ = _run(SAMPLING, tmp_path, sample=0.7)
    text = " ".join(
        " ".join(grid.row_text(index)) for index in range(len(grid.rows))
    )

    assert "Клиент" in text
    assert "Дата формирования отчета" in text


def test_sample_is_deterministic(tmp_path: Path) -> None:
    """Повторный прогон обязан дать ту же выборку: иначе фикстуру нельзя
    пересоздать, а golden-тест на ней станет плавающим."""
    options = {"sample": 0.7, "dates": "mask"}
    first = Anonymizer(AliasStore.load(tmp_path / "a.json", seed=7), Options(**options))  # type: ignore[arg-type]
    second = Anonymizer(AliasStore.load(tmp_path / "b.json", seed=99), Options(**options))  # type: ignore[arg-type]

    assert first.run(SAMPLING.read_bytes()) == second.run(SAMPLING.read_bytes())


def test_sample_accounting_matches_the_document(tmp_path: Path) -> None:
    """Счётчик выброшенных строк сверяется с документом: на нём же стоит
    проверка формы, и разошедшийся счётчик её ломает."""
    store = AliasStore.load(tmp_path / "map.json", seed=7)
    anonymizer = Anonymizer(store, Options(sample=0.7))
    result = anonymizer.run(SAMPLING.read_bytes())

    assert verify_structure(
        SAMPLING.read_bytes(), result, dropped_rows=anonymizer.stats.dropped_rows
    ) is None


def test_sample_fraction_is_validated(tmp_path: Path) -> None:
    code = main(
        [
            str(SAMPLING), "-o", str(tmp_path / "out.html"),
            "--mapping", str(tmp_path / "map.json"),
            "--sample", "1.5", "--quiet",
        ]
    )
    assert code == 2


def test_sample_via_cli(tmp_path: Path) -> None:
    target = tmp_path / "out.html"
    code = main(
        [
            str(SAMPLING), "-o", str(target),
            "--mapping", str(tmp_path / "map.json"),
            "--seed", "7", "--sample", "0.7", "--quiet",
        ]
    )

    assert code == 0
    grid = build_grid(parse_document(target.read_bytes()))
    rows = _section_rows(grid, "2. Состояние портфеля")
    assert len([row for row in rows if row[0].startswith("БУМАГА")]) == 4


# --- каталоги ---------------------------------------------------------------


def _archive(tmp_path: Path) -> Path:
    """Архив как он выглядит на диске: подкаталоги по годам и мусор рядом."""
    root = tmp_path / "архив"
    (root / "2024").mkdir(parents=True)
    (root / "2025").mkdir()

    (root / "2024" / "report_2024-01.html").write_bytes(RAW.read_bytes())
    # Брокер отдаёт ту же HTML-выгрузку и под расширением .xls.
    (root / "2024" / "report_2024-02.xls").write_bytes(RAW.read_bytes())
    (root / "2025" / "report_2025-08.html").write_bytes(SAMPLING.read_bytes())
    (root / "report_flat.html").write_bytes(RAW.read_bytes())

    (root / "заметки.txt").write_text("не отчёт", encoding="utf-8")
    (root / "notes.html").write_text(
        "<html><body><p>без таблиц</p></body></html>", encoding="utf-8"
    )
    return root


def test_directory_is_expanded_to_reports(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    out = tmp_path / "out"

    code = main(
        [str(root), "-r", "--out-dir", str(out), "--seed", "7", "--quiet"]
    )

    assert code == 0
    produced = sorted(path.relative_to(out).as_posix() for path in out.rglob("*.anon.html"))
    assert produced == [
        "2024/report_2024-01.anon.html",
        "2024/report_2024-02.anon.html",
        "2025/report_2025-08.anon.html",
        "report_flat.anon.html",
    ]


def test_recursive_keeps_subdirectories(tmp_path: Path) -> None:
    """Архив разложен по годам; плоская выгрузка теряет разметку и рискует
    совпадением имён."""
    root = _archive(tmp_path)
    out = tmp_path / "out"

    main([str(root), "-r", "--out-dir", str(out), "--seed", "7", "--quiet"])

    assert (out / "2024" / "report_2024-01.anon.html").exists()
    assert (out / "2025" / "report_2025-08.anon.html").exists()


def test_without_recursive_only_top_level(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    out = tmp_path / "out"

    main([str(root), "--out-dir", str(out), "--seed", "7", "--quiet"])

    assert sorted(path.name for path in out.rglob("*.anon.html")) == ["report_flat.anon.html"]


def test_non_reports_are_skipped(tmp_path: Path) -> None:
    """Каталог архива редко стерилен: pdf, скриншоты, случайные выгрузки.

    Молча превратить такой файл в «обезличенный отчёт» хуже, чем пропустить.
    """
    root = _archive(tmp_path)
    out = tmp_path / "out"

    main([str(root), "-r", "--out-dir", str(out), "--seed", "7", "--quiet"])

    names = {path.name for path in out.rglob("*")}
    assert "заметки.anon.html" not in names
    assert "notes.anon.html" not in names


def test_previous_results_are_not_reprocessed(tmp_path: Path) -> None:
    """Результат кладётся рядом с исходником — второй прогон не должен
    обезличивать собственный вывод."""
    root = _archive(tmp_path)

    main([str(root), "-r", "--seed", "7", "--quiet"])
    first = sorted(path.name for path in root.rglob("*.anon.html"))

    jobs, _ = collect_jobs(
        _namespace(files=[root], recursive=True, out_dir=None, out=None, pattern=None)
    )

    assert first
    assert all(not job.source.name.endswith(".anon.html") for job in jobs)
    # Четыре отчёта плюс notes.html: он с виду html и отсеивается уже при
    # обработке — с сообщением, какой именно файл пропущен.
    assert len(jobs) == 5


def test_pattern_narrows_the_selection(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    out = tmp_path / "out"

    main(
        [
            str(root), "-r", "--pattern", "report_2024*",
            "--out-dir", str(out), "--seed", "7", "--quiet",
        ]
    )

    assert sorted(path.name for path in out.rglob("*.anon.html")) == [
        "report_2024-01.anon.html",
        "report_2024-02.anon.html",
    ]


def test_aliases_are_shared_across_the_whole_run(tmp_path: Path) -> None:
    """Одна бумага — один псевдоним во всех месяцах архива, иначе сквозной
    импорт распадётся на несвязанные позиции."""
    root = _archive(tmp_path)
    out = tmp_path / "out"

    main([str(root), "-r", "--out-dir", str(out), "--seed", "7", "--quiet"])

    first = (out / "2024" / "report_2024-01.anon.html").read_bytes()
    second = (out / "2024" / "report_2024-02.anon.html").read_bytes()
    assert first == second  # одинаковые исходники дали одинаковый результат

    mapping = json.loads((out / ".anonymize-mapping.json").read_text(encoding="utf-8"))
    assert mapping["aliases"]["instrument"]


def test_missing_path_is_reported(tmp_path: Path) -> None:
    assert main([str(tmp_path / "нет-такого.html"), "--quiet"]) == 2


def test_empty_directory_is_reported(tmp_path: Path) -> None:
    empty = tmp_path / "пусто"
    empty.mkdir()
    assert main([str(empty), "--quiet"]) == 2


def test_out_is_refused_for_many_files(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    assert main([str(root), "-r", "-o", str(tmp_path / "one.html"), "--quiet"]) == 2


def _namespace(**values: object) -> Any:
    from argparse import Namespace

    return Namespace(**values)
