"""Хранилище трека: отдельный SQLite, нулевое пересечение с боевой БД.

Красная линия трека (`docs/PARALLEL-TRACK.md`): в боевую базу этот код не
пишет ничего и никогда. Здесь лежат только результаты сверки доходностей —
числа о рынке, не о вашем портфеле.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS verify_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot    TEXT NOT NULL,
    board       TEXT NOT NULL,
    settlement  TEXT NOT NULL,
    day_count   TEXT NOT NULL,
    checked     INTEGER NOT NULL,
    outliers    INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS yield_checks (
    run_id        INTEGER NOT NULL REFERENCES verify_runs(id),
    secid         TEXT NOT NULL,
    price_source  TEXT NOT NULL,
    price         TEXT,
    own_yield     TEXT,
    iss_yield     TEXT,
    diff_bp       TEXT,
    own_nkd       TEXT,
    iss_nkd       TEXT,
    nkd_settled   TEXT,
    own_duration  INTEGER,
    iss_duration  INTEGER,
    note          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS yield_checks_run ON yield_checks(run_id);
CREATE INDEX IF NOT EXISTS yield_checks_secid ON yield_checks(secid);
"""


@dataclass(frozen=True)
class CheckRow:
    """Одна строка сверки. Все числа строками: SQLite не знает `NUMERIC`."""

    secid: str
    price_source: str
    price: str | None
    own_yield: str | None
    iss_yield: str | None
    diff_bp: str | None
    own_nkd: str | None
    iss_nkd: str | None
    nkd_settled: str | None
    own_duration: int | None
    iss_duration: int | None
    note: str


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        yield connection
        connection.commit()
    finally:
        connection.close()


def save_run(
    connection: sqlite3.Connection,
    *,
    snapshot: str,
    board: str,
    settlement: str,
    day_count: str,
    rows: Iterable[CheckRow],
) -> int:
    """Записать прогон целиком. Возвращает идентификатор прогона."""
    materialized = list(rows)
    outliers = sum(1 for row in materialized if row.note and row.note != "ok")

    cursor = connection.execute(
        "INSERT INTO verify_runs (snapshot, board, settlement, day_count, checked, outliers)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (snapshot, board, settlement, day_count, len(materialized), outliers),
    )
    run_id = int(cursor.lastrowid or 0)

    connection.executemany(
        "INSERT INTO yield_checks (run_id, secid, price_source, price, own_yield, iss_yield,"
        " diff_bp, own_nkd, iss_nkd, nkd_settled, own_duration, iss_duration, note)"
        " VALUES (:run_id, :secid, :price_source, :price, :own_yield, :iss_yield, :diff_bp,"
        " :own_nkd, :iss_nkd, :nkd_settled, :own_duration, :iss_duration, :note)",
        [{"run_id": run_id, **asdict(row)} for row in materialized],
    )
    return run_id
