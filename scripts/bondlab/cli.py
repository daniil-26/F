"""CLI трека: выгрузка снапшотов и сверка доходностей.

    python -m bondlab boards
    python -m bondlab pull TQOB --limit 30
    python -m bondlab schedule SU26238RMFS4
    python -m bondlab verify --board TQOB

Команда `verify` работает **с диска** и сети не требует: выгрузка и разбор
разделены намеренно (`docs/PARALLEL-TRACK.md`, A1).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from bondlab import iss, parse, snapshots, store, verify
from portfolio.calc.bonds import DayCount

app = typer.Typer(
    help="Одноразовый трек: данные по облигациям с MOEX ISS и сверка доходностей.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXIT_OUTLIERS = 1
EXIT_NO_DATA = 2


def _root() -> Path:
    return Path(os.environ.get("BONDLAB_DIR", PROJECT_ROOT / "data" / "bondlab"))


def _today() -> date:
    """Сегодня — только в CLI: в расчётном ядре текущей даты нет (спека 9)."""
    return datetime.now(tz=UTC).date()


def _client() -> iss.IssClient:
    return iss.IssClient(cache_dir=_root() / "cache")


@app.command("boards")
def pull_boards(
    day: str = typer.Option("", "--day", help="дата снапшота, по умолчанию сегодня"),
) -> None:
    """Снять перечень досок. Коды досок меняются — выписывать их руками нельзя."""
    snapshot = snapshots.SnapshotStore.for_day(_root() / "snapshots", day or _today())
    with _client() as client:
        payload = _fetch(lambda: iss.boards(client))
    path = snapshot.save(snapshots.BOARDS, "boards", payload)

    table = Table(title=f"Доски облигаций, {snapshot.day}")
    for column in ("boardid", "title", "is_traded"):
        table.add_column(column)
    for row in parse.rows(payload, "boards"):
        table.add_row(str(row.get("boardid")), str(row.get("title")), str(row.get("is_traded")))

    console.print(table)
    console.print(f"снимок: {path}")


@app.command("pull")
def pull_board(
    board: str = typer.Argument(..., help="код доски, например TQOB"),
    limit: int = typer.Option(0, "--limit", help="сколько бумаг снять, 0 — все"),
    day: str = typer.Option("", "--day", help="дата снапшота, по умолчанию сегодня"),
) -> None:
    """Снять доску и по каждой бумаге описание и `bondization`.

    Порядок обхода за один проход: доска → список `secid` → по каждой бумаге
    описание и график. Котировки берутся одним запросом на доску, не по бумаге.
    """
    snapshot = snapshots.SnapshotStore.for_day(_root() / "snapshots", day or _today())

    with _client() as client:
        quotes = _fetch(lambda: iss.board_securities(client, board))
        snapshot.save(snapshots.BOARD_SECURITIES, board, quotes)

        secids = list(iss.iter_secids(quotes))
        if limit:
            secids = secids[:limit]

        console.print(f"доска {board}: {len(secids)} бумаг")
        with typer.progressbar(secids, label="выгрузка") as progress:
            for secid in progress:
                if not snapshot.has(snapshots.SECURITIES, secid):
                    snapshot.save(snapshots.SECURITIES, secid, iss.security(client, secid))
                if not snapshot.has(snapshots.BONDIZATION, secid):
                    snapshot.save(snapshots.BONDIZATION, secid, iss.bondization(client, secid))

    console.print(f"снимок: {snapshot.directory}")


@app.command("schedule")
def show_schedule(
    secid: str = typer.Argument(..., help="код бумаги"),
    day: str = typer.Option("", "--day", help="дата снапшота, по умолчанию последний"),
) -> None:
    """Показать график платежей из снапшота. Первая проверка разбора — глазами."""
    snapshot = snapshots.SnapshotStore.for_day(_root() / "snapshots", day or None)
    schedule = parse.build_schedule(
        snapshot.load(snapshots.SECURITIES, secid),
        snapshot.load(snapshots.BONDIZATION, secid),
    )

    console.print(
        f"[bold]{schedule.secid}[/bold] {schedule.name or ''} — "
        f"номинал {schedule.initial_face_value}, погашение {schedule.maturity_date}"
    )

    table = Table(title="Купоны")
    for column in ("период с", "выплата", "сумма", "ставка, %"):
        table.add_column(column)
    for coupon in schedule.coupons:
        table.add_row(
            coupon.period_start.isoformat(),
            coupon.date.isoformat(),
            str(coupon.value) if coupon.value is not None else "[yellow]неизвестен[/yellow]",
            str(coupon.rate_pct) if coupon.rate_pct is not None else "—",
        )
    console.print(table)

    if len(schedule.amortizations) > 1:
        amortizations = Table(title="Амортизация")
        amortizations.add_column("дата")
        amortizations.add_column("сумма")
        for item in schedule.amortizations:
            amortizations.add_row(item.date.isoformat(), str(item.value))
        console.print(amortizations)

    if schedule.offers:
        offers = Table(title="Оферты")
        for column in ("дата", "тип", "цена, %"):
            offers.add_column(column)
        for offer in schedule.offers:
            offers.add_row(
                offer.date.isoformat(),
                offer.kind.value if offer.kind else "[yellow]не распознан[/yellow]",
                str(offer.price_pct) if offer.price_pct is not None else "—",
            )
        console.print(offers)


@app.command("verify")
def verify_yields(
    board: str = typer.Option(..., "--board", help="код доски из снапшота"),
    day: str = typer.Option("", "--day", help="дата снапшота, по умолчанию последний"),
    trade_date: str = typer.Option("", "--trade-date", help="дата торгов, иначе дата снимка"),
    day_count: str = typer.Option("ACT/365", "--day-count", help="база начисления дней"),
    top: int = typer.Option(20, "--top", help="сколько выбросов показать"),
) -> None:
    """Свой расчёт против биржевого по всей доске.

    Код возврата 1 при наличии выбросов: команда предназначена для cron
    (спека 6), а выброс — это повод посмотреть, а не фон.
    """
    snapshot = snapshots.SnapshotStore.for_day(_root() / "snapshots", day or None)
    quotes = parse.board_quotes(snapshot.load(snapshots.BOARD_SECURITIES, board))
    when = date.fromisoformat(trade_date) if trade_date else date.fromisoformat(snapshot.day)
    basis = DayCount(day_count)

    comparisons: list[verify.Comparison] = []
    unparsed: list[tuple[str, str]] = []

    for quote in quotes:
        secid = str(quote.get("SECID") or "")
        if not secid or not snapshot.has(snapshots.BONDIZATION, secid):
            continue
        try:
            schedule = parse.build_schedule(
                snapshot.load(snapshots.SECURITIES, secid),
                snapshot.load(snapshots.BONDIZATION, secid),
            )
        except (parse.ParseError, snapshots.SnapshotMissing) as error:
            unparsed.append((secid, str(error)))
            continue
        comparisons.extend(verify.compare_security(schedule, quote, when, day_count=basis))

    _print_distribution(verify.summarize(comparisons), when, basis)
    found = verify.outliers(comparisons)
    _print_outliers(found[:top])

    if unparsed:
        console.print(f"[yellow]не разобрано выпусков: {len(unparsed)}[/yellow]")
        for secid, reason in unparsed[:top]:
            console.print(f"  • {secid}: {reason}")

    with store.connect(_root() / "bondlab.sqlite") as connection:
        run_id = store.save_run(
            connection,
            snapshot=snapshot.day,
            board=board,
            settlement=when.isoformat(),
            day_count=basis.value,
            rows=[item.to_row() for item in comparisons],
        )
    console.print(f"прогон {run_id} записан в {_root() / 'bondlab.sqlite'}")

    if found:
        raise typer.Exit(EXIT_OUTLIERS)


def _fetch(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Запрос к ISS с человеческим сообщением вместо трассировки.

    Недоступный ISS — не повод для стека: это штатная ситуация, снапшоты на
    диске от неё не страдают.
    """
    try:
        return call()
    except iss.IssError as error:
        console.print(f"[red]ISS недоступен:[/red] {error}")
        raise typer.Exit(EXIT_NO_DATA) from error


def _print_distribution(
    summaries: list[verify.Distribution],
    when: date,
    basis: DayCount,
) -> None:
    table = Table(title=f"Расхождение с биржей, {when}, база {basis.value}")
    columns = (
        "цена", "сверено", "пропущено",
        "медиана, б.п.", "p90, б.п.", "макс, б.п.", "в пороге",
    )
    for column in columns:
        table.add_column(column, justify="right")

    for summary in summaries:
        table.add_row(
            summary.price_source,
            str(summary.compared),
            str(summary.skipped),
            _format(summary.median_bp),
            _format(summary.p90_bp),
            _format(summary.max_bp),
            f"{summary.within_threshold}/{summary.compared}" if summary.compared else "—",
        )
    console.print(table)


def _print_outliers(items: list[verify.Comparison]) -> None:
    if not items:
        console.print("[green]выбросов нет[/green]")
        return

    table = Table(title=f"Выбросы (> {verify.THRESHOLD_BP} б.п.)")
    columns = ("бумага", "цена", "своя", "к оферте", "биржа", "Δ б.п.", "НКД свой/биржи", "причина")
    for column in columns:
        table.add_column(column, overflow="fold")

    for item in items:
        table.add_row(
            item.secid,
            f"{item.price_source} {_format(item.price)}",
            _percent(item.own_yield),
            _percent(item.own_yield_to_offer),
            _percent(item.iss_yield),
            _format(item.diff_bp),
            f"{_format(item.own_nkd)} / {_format(item.iss_nkd)}",
            item.note,
        )
    console.print(table)


def _format(value: Decimal | None) -> str:
    return "—" if value is None else str(value)


def _percent(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.4f}"
