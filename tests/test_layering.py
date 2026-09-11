"""Направление импортов: `jobs` → `domain` → `calc`; `adapters` → `calc`.

Обратных импортов нет (спека 9). Проверяется статически по AST: линтер не
различает слои, а правило — ровно про слои. Нарушение этого правила разрушает
главное свойство архитектуры: расчётное ядро остаётся тестируемым офлайн только
пока оно ничего не знает про БД и сеть.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "portfolio"

# слой → что ему разрешено импортировать из проекта
ALLOWED: dict[str, set[str]] = {
    "calc": set(),
    "adapters": {"calc", "adapters"},
    "domain": {"calc", "domain", "adapters", "models"},
    "jobs": {"calc", "domain", "adapters", "jobs", "models", "config", "db"},
    "web": {"calc", "domain", "jobs", "models", "config", "db", "web"},
}

MODULES = sorted(SOURCE.rglob("*.py"))


def _layer(path: Path) -> str:
    relative = path.relative_to(SOURCE)
    return relative.parts[0] if len(relative.parts) > 1 else relative.stem


def _imported_layers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("portfolio."):
                    found.add(alias.name.split(".")[1])
            continue
        else:
            continue

        if module.startswith("portfolio."):
            found.add(module.split(".")[1])
    return found


@pytest.mark.parametrize("module", MODULES, ids=lambda path: str(path.name))
def test_import_direction(module: Path) -> None:
    layer = _layer(module)
    if layer not in ALLOWED:
        return

    forbidden = _imported_layers(module) - ALLOWED[layer] - {layer}
    assert not forbidden, f"{module.relative_to(SOURCE)} импортирует {sorted(forbidden)}"


def test_calc_knows_nothing_about_io() -> None:
    """`calc/` не знает ни про БД, ни про сеть, ни про текущую дату."""
    banned = {"sqlalchemy", "httpx", "requests", "psycopg"}

    for module in (SOURCE / "calc").rglob("*.py"):
        imported = {
            node.module.split(".")[0]
            for node in ast.walk(ast.parse(module.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not imported & banned, f"{module.name} импортирует {sorted(imported & banned)}"

        source = module.read_text(encoding="utf-8")
        assert "date.today()" not in source, f"{module.name} знает текущую дату"
        assert "datetime.now(" not in source, f"{module.name} знает текущее время"


def test_bondlab_touches_only_the_calculation_core() -> None:
    """Красная линия параллельного трека: одноразовый код не знает про журнал.

    `scripts/bondlab/` (`docs/PARALLEL-TRACK.md`) считает числа о рынке и не
    должен иметь возможности записать что-либо в боевую БД или прочитать из
    неё позиции. Проверяется статически: из проекта ему доступен только слой
    `calc`, а SQLAlchemy недоступна вовсе.
    """
    bondlab = Path(__file__).resolve().parents[1] / "scripts" / "bondlab"

    for module in sorted(bondlab.rglob("*.py")):
        forbidden = _imported_layers(module) - {"calc"}
        assert not forbidden, f"bondlab/{module.name} импортирует {sorted(forbidden)}"

        source = module.read_text(encoding="utf-8")
        assert "sqlalchemy" not in source, f"bondlab/{module.name} знает про ORM боевой БД"
        assert "portfolio.db" not in source, f"bondlab/{module.name} знает про боевую БД"


def test_transactions_are_opened_only_in_jobs() -> None:
    """Транзакции открываются только в `jobs/` (спека 9)."""
    for module in SOURCE.rglob("*.py"):
        if _layer(module) in {"jobs", "db"}:
            continue
        source = module.read_text(encoding="utf-8")
        assert "session_scope(" not in source, f"{module.name} открывает транзакцию"
        assert ".commit()" not in source, f"{module.name} коммитит транзакцию"
