"""Починка отчётов с испорченной кодировкой (`tools/fix_encoding.py`).

Порча вида «UTF-8 прочитали как CP1251 и сохранили» обратима точно: обратное
преобразование возвращает исходный текст символ в символ. Тесты проверяют и
точность, и обратную сторону — что здоровые файлы остаются нетронутыми, а
порча с потерей символов честно объявляется неисправимой.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from fix_encoding import main, repair_file, repair_text  # noqa: E402

from conftest import FIXTURES  # noqa: E402
from portfolio.adapters.broker.tables import garbage_ratio  # noqa: E402

# Заглавная «И» — байт 0x98, не определённый в CP1251. Её присутствие роняет
# неверное чтение с ошибкой и случайно спасает файл; ломаются молча ровно те
# отчёты, где её не оказалось.
HEALTHY = FIXTURES.joinpath("report_2025-08.html").read_text("utf-8").replace("И", "Й")


def _broken(text: str, encoding: str) -> str:
    return text.encode("utf-8").decode(encoding)


@pytest.mark.parametrize("encoding", ["cp1251", "latin-1", "koi8-r"])
def test_repair_is_exact(encoding: str) -> None:
    damaged = _broken(HEALTHY, encoding)
    assert garbage_ratio(damaged) > 0.2

    repaired, rounds = repair_text(damaged)

    assert repaired == HEALTHY
    assert rounds == [encoding]


def test_repair_undoes_two_layers() -> None:
    """Файл мог пережить две неверные перекодировки подряд."""
    damaged = _broken(_broken(HEALTHY, "cp1251"), "latin-1")

    repaired, rounds = repair_text(damaged)

    assert repaired == HEALTHY
    assert len(rounds) == 2


def test_healthy_text_is_untouched() -> None:
    """Не эвристика: кодировщик отвергает кириллицу вне своей таблицы, поэтому
    правильный текст в принципе не может быть «улучшен»."""
    repaired, rounds = repair_text(HEALTHY)

    assert repaired == HEALTHY
    assert rounds == []


def test_lossy_damage_is_refused(tmp_path: Path) -> None:
    """Если при первом неверном чтении символы заменены на U+FFFD,
    восстанавливать нечего — файл нужно доставать заново."""
    original = FIXTURES.joinpath("report_2025-08.html").read_text("utf-8")
    lossy = original.encode("utf-8").decode("cp1251", errors="replace")
    assert "�" in lossy

    path = tmp_path / "lossy.html"
    path.write_text(lossy, encoding="utf-8")
    report = repair_file(path)

    assert not report.repaired
    assert report.broken
    assert "нужен исходник" in report.describe()


def test_declaration_is_brought_in_line(tmp_path: Path) -> None:
    """Починенный файл сохраняется в UTF-8, значит и объявлять должен UTF-8:
    иначе следующий читатель повторит ту же ошибку."""
    damaged = _broken(HEALTHY.replace("charset=utf-8", "charset=windows-1251"), "cp1251")
    path = tmp_path / "report.html"
    path.write_text(damaged, encoding="utf-8")

    report = repair_file(path)

    assert report.text is not None
    assert "charset=utf-8" in report.text
    assert "windows-1251" not in report.text


def test_declaration_is_added_when_missing(tmp_path: Path) -> None:
    """Файл без объявления браузер читает по локали, то есть снова неверно."""
    damaged = _broken(HEALTHY.replace(
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">', ""
    ), "cp1251")
    path = tmp_path / "report.html"
    path.write_text(damaged, encoding="utf-8")

    report = repair_file(path)

    assert report.text is not None
    assert 'charset="utf-8"' in report.text


def test_cli_writes_fixed_copy(tmp_path: Path) -> None:
    path = tmp_path / "report.html"
    path.write_text(_broken(HEALTHY, "cp1251"), encoding="utf-8")

    assert main([str(path), "--quiet"]) == 0

    fixed = tmp_path / "report.fixed.html"
    assert fixed.read_text("utf-8") == HEALTHY
    assert path.read_text("utf-8") != HEALTHY  # исходник не тронут


def test_cli_in_place_keeps_backup(tmp_path: Path) -> None:
    """Скрипт правит боевой архив — копия обязательна."""
    path = tmp_path / "report.html"
    damaged = _broken(HEALTHY, "cp1251")
    path.write_text(damaged, encoding="utf-8")

    assert main([str(path), "--in-place", "--quiet"]) == 0

    assert path.read_text("utf-8") == HEALTHY
    assert (tmp_path / "report.html.bak").read_text("utf-8") == damaged


def test_cli_dry_run_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "report.html"
    path.write_text(_broken(HEALTHY, "cp1251"), encoding="utf-8")

    assert main([str(path), "--dry-run", "--quiet"]) == 0
    assert list(tmp_path.iterdir()) == [path]


def test_cli_walks_a_directory(tmp_path: Path) -> None:
    (tmp_path / "2024").mkdir()
    (tmp_path / "2024" / "broken.html").write_text(_broken(HEALTHY, "cp1251"), encoding="utf-8")
    (tmp_path / "healthy.html").write_text(HEALTHY, encoding="utf-8")

    assert main([str(tmp_path), "-r", "--quiet"]) == 0

    assert (tmp_path / "2024" / "broken.fixed.html").read_text("utf-8") == HEALTHY
    assert not (tmp_path / "healthy.fixed.html").exists()


def test_cli_reports_hopeless_with_nonzero_code(tmp_path: Path) -> None:
    """Ненулевой код возврата: прогон по архиву не должен выглядеть успешным,
    если часть файлов осталась сломанной."""
    original = FIXTURES.joinpath("report_2025-08.html").read_text("utf-8")
    path = tmp_path / "lossy.html"
    path.write_text(original.encode("utf-8").decode("cp1251", errors="replace"), encoding="utf-8")

    assert main([str(path), "--quiet"]) == 1


def test_line_endings_survive_byte_for_byte(tmp_path: Path) -> None:
    """Отчёты брокера приходят с CRLF (`docs/BROKER-REPORT-FORMAT.md`).

    Запись через `write_text` на Windows транслирует каждый `\\n` в `\\r\\n` и
    превращает CRLF исходника в `\\r\\r\\n`: файл распухает пустыми строками, а
    обещание «починка точна» перестаёт выполняться. На Linux трансляции нет,
    поэтому проверка сторожит контракт там, где он ломается, — сравнением
    байтов, а не строк.
    """
    healthy = HEALTHY.replace("\n", "\r\n")
    path = tmp_path / "report.html"
    path.write_bytes(_broken(healthy, "cp1251").encode("utf-8"))

    assert main([str(path), "--quiet"]) == 0

    fixed = tmp_path / "report.fixed.html"
    assert fixed.read_bytes() == healthy.encode("utf-8")
