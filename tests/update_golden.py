"""Пересоздание golden-файлов. Запускается руками после осознанной правки парсера.

    python tests/update_golden.py

Каждое изменение здесь обязано быть объяснимо: golden-тест на фикстурах —
самый ценный тест в проекте (спека 5.5), и молча переписанный эталон
превращает его в тавтологию.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from golden import dump, report_to_dict, tables_to_dict
from portfolio.adapters.broker.mapping_v1 import parse
from portfolio.adapters.broker.tables import extract_tables

FIXTURES = Path(__file__).parent / "fixtures"


def main() -> None:
    for report in sorted(FIXTURES.glob("*.html")):
        content = report.read_bytes()
        dump(report.with_suffix(".tables.json"), tables_to_dict(extract_tables(content)))
        dump(report.with_suffix(".expected.json"), report_to_dict(parse(content)))
        print(f"обновлено: {report.name}")


if __name__ == "__main__":
    main()
