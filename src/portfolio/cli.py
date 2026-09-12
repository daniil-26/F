"""CLI: разбор аргументов, вызов джоб, печать таблиц через rich.

Вычислений здесь нет — только вызовы `jobs/` (спека 9). Коды возврата важны:
`check` запускается по cron, и ненулевой код — это уведомление.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from portfolio.adapters.csv_input import CsvValidationError
from portfolio.jobs.check import CheckResult, run_check
from portfolio.jobs.import_broker import (
    ArchiveImportResult,
    ImportResult,
    import_broker_archive,
)
from portfolio.jobs.import_csv import CsvImportResult, import_csv_file
from portfolio.jobs.inbox import collect_inbox

app = typer.Typer(
    help="Учёт инвестиций: журнал, импорт отчётов брокера, сверка.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

EXIT_DISCREPANCY = 1
EXIT_INPUT_ERROR = 2


@app.command("import-broker")
def import_broker(
    paths: list[Path] = typer.Argument(
        ..., exists=True, help="HTML-отчёты брокера или каталоги с ними"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="показать изменения и ничего не писать"),
    account: str | None = typer.Option(None, "--account", help="код счёта, если его нет в отчёте"),
    force: bool = typer.Option(
        False,
        "--force",
        help="записать, даже если сверка не сошлась (причина попадёт в примечание)",
    ),
    recursive: bool = typer.Option(
        False, "-r", "--recursive", help="заходить во вложенные каталоги"
    ),
    pattern: str | None = typer.Option(
        None, "--pattern", help="маска имён внутри каталога, например 'report_2024*'"
    ),
    keep_going: bool = typer.Option(
        False,
        "--keep-going",
        help="не останавливаться на первом расхождении (только для разбора)",
    ),
) -> None:
    """Импортировать отчёт брокера или весь архив из каталога.

    Каталог обходится **в хронологическом порядке по периоду отчёта**: позиции и
    остатки накопительны, и отчёт за март после майского даст верный итог и
    неверную историю.
    """
    result = import_broker_archive(
        paths,
        account_code=account,
        dry_run=dry_run,
        force=force,
        recursive=recursive,
        pattern=pattern,
        stop_on_error=not keep_going,
    )

    for note in result.notes:
        console.print(f"[yellow]{note}[/yellow]")

    if not result.results and not result.failures:
        console.print("[red]Не найдено ни одного отчёта.[/red]")
        raise typer.Exit(EXIT_INPUT_ERROR)

    # Один отчёт — прежний подробный вывод. Оборванный прогон одним отчётом не
    # считается: там важно, что остальные файлы каталога остались нетронутыми.
    single = len(result.results) == 1 and not result.failures and not result.stopped_early
    if single:
        _print_import(result.results[0])
    else:
        _print_archive(result)

    if not result.ok:
        raise typer.Exit(EXIT_DISCREPANCY)


@app.command("import-csv")
def import_csv(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="декларативный CSV"),
    dry_run: bool = typer.Option(False, "--dry-run", help="показать изменения и ничего не писать"),
) -> None:
    """Импортировать декларативный CSV (начальные остатки, денежные потоки)."""
    try:
        result = import_csv_file(file, dry_run=dry_run)
    except CsvValidationError as error:
        console.print(f"[red]Файл не прошёл валидацию:[/red] {error.path}")
        for problem in error.problems:
            console.print(f"  • {problem}")
        raise typer.Exit(EXIT_INPUT_ERROR) from error

    _print_csv_import(result)


@app.command("check")
def check() -> None:
    """Сверить остатки по каждому отчёту и прогнать жёсткие инварианты."""
    result = run_check()
    _print_check(result)

    if not result.ok:
        raise typer.Exit(EXIT_DISCREPANCY)


@app.command("inbox")
def inbox() -> None:
    """Показать очередь: нераспознанные строки и расхождения сверки."""
    items = collect_inbox()
    if not items:
        console.print("[green]Входящие пусты.[/green]")
        return

    table = Table(title="Входящие", show_lines=False)
    table.add_column("Вид", style="cyan", no_wrap=True)
    table.add_column("Что")
    table.add_column("Подробности", overflow="fold")
    for item in items:
        table.add_row(item.kind, item.title, item.detail)
    console.print(table)
    raise typer.Exit(EXIT_DISCREPANCY)


def _print_import(result: ImportResult) -> None:
    header = Table.grid(padding=(0, 2))
    header.add_row("Файл", result.source)
    header.add_row("Счёт", result.account_code)
    header.add_row("Период", f"{result.period_start} — {result.period_end}")
    header.add_row("Версия парсера", result.mapping_version)
    if result.is_duplicate:
        header.add_row("Сырьё", "файл уже загружался (совпал хеш)")
    console.print(header)

    summary = result.diff.summary()
    table = Table(title="Дифф с журналом")
    table.add_column("Новых", justify="right")
    table.add_column("Уже в журнале", justify="right")
    table.add_column("Сторно", justify="right")
    table.add_row(str(summary["new"]), str(summary["unchanged"]), str(summary["reversals"]))
    console.print(table)

    if result.unparsed:
        unparsed = Table(title="Нераспознанные строки", style="yellow")
        unparsed.add_column("Таблица")
        unparsed.add_column("Причина")
        unparsed.add_column("Строка", overflow="fold")
        for row in result.unparsed:
            unparsed.add_row(row.table, row.reason, str(row.row))
        console.print(unparsed)

    _print_reconcile(result.reconcile.discrepancies)

    if result.dry_run:
        console.print("[yellow]--dry-run: ничего не записано.[/yellow]")
    elif result.committed:
        console.print(f"[green]Записано событий: {result.written}.[/green]")
    else:
        console.print(
            "[red]Не записано: сверка не сошлась.[/red] "
            "Повторить с --force, чтобы подтвердить импорт с расхождением."
        )


def _print_archive(result: ArchiveImportResult) -> None:
    """Сводка по архиву: одна строка на отчёт, в порядке импорта."""
    table = Table(title="Импорт архива")
    table.add_column("Отчёт")
    table.add_column("Период")
    table.add_column("Новых", justify="right")
    table.add_column("Уже в журнале", justify="right")
    table.add_column("Нераспознано", justify="right")
    table.add_column("Расхождений", justify="right")
    table.add_column("Записано", justify="right")

    for item in result.results:
        summary = item.diff.summary()
        broken = len(item.reconcile.discrepancies)
        style = "green" if item.ok else "red"
        table.add_row(
            Path(item.source).name,
            f"{item.period_start} — {item.period_end}",
            str(summary["new"]),
            str(summary["unchanged"]),
            str(len(item.unparsed)),
            str(broken),
            str(item.written),
            style=style,
        )
    console.print(table)

    for path, error in result.failures:
        console.print(f"[red]{path}: не обработан — {error}[/red]")

    first_bad = next((item for item in result.results if not item.ok), None)
    if first_bad is not None:
        console.print(f"\n[red]Первый отчёт с расхождением: {first_bad.source}[/red]")
        for row in first_bad.unparsed[:10]:
            console.print(f"  • нераспознано: {row.reason} — {row.row}")
        _print_reconcile(first_bad.reconcile.discrepancies)

    if result.stopped_early:
        console.print(
            "Остальные отчёты каталога не импортированы: расхождение накапливается, "
            "и разбирать его нужно с первого. Пройти каталог целиком — --keep-going."
        )

    console.print(
        f"Отчётов: {len(result.results)}, подтверждено: {result.committed}, "
        f"не обработано: {len(result.failures)}"
    )


def _print_csv_import(result: CsvImportResult) -> None:
    summary = result.diff.summary()
    table = Table(title=f"{result.source} ({result.kind})")
    table.add_column("Строк в файле", justify="right")
    table.add_column("Новых", justify="right")
    table.add_column("Без изменений", justify="right")
    table.add_column("Сторно", justify="right")
    table.add_row(
        str(result.rows),
        str(summary["new"]),
        str(summary["unchanged"]),
        str(summary["reversals"]),
    )
    console.print(table)

    for note in result.diff.notes:
        console.print(f"  • {note}")

    if result.dry_run:
        console.print("[yellow]--dry-run: ничего не записано.[/yellow]")
    else:
        console.print(f"[green]Записано событий: {result.written}.[/green]")


def _print_check(result: CheckResult) -> None:
    table = Table(title="Сверка по отчётам")
    table.add_column("Отчёт")
    table.add_column("Конец периода")
    table.add_column("Счёт")
    table.add_column("Проверено", justify="right")
    table.add_column("Расхождений", justify="right")
    table.add_column("Нераспознано", justify="right")

    for report in result.reports:
        broken = len(report.result.discrepancies)
        style = "red" if broken or report.unparsed or report.missing_file else "green"
        table.add_row(
            report.filename,
            str(report.period_end),
            report.account_code,
            str(report.result.checked),
            str(broken),
            str(len(report.unparsed)),
            style=style,
        )
    console.print(table)

    for report in result.reports:
        if report.result.discrepancies:
            console.print(f"[red]Расхождения: {report.filename}[/red]")
            _print_reconcile(report.result.discrepancies)

    if result.violations:
        violations = Table(title="Нарушенные инварианты", style="red")
        violations.add_column("Проверка")
        violations.add_column("Нарушение", overflow="fold")
        for violation in result.violations:
            violations.add_row(violation.check, violation.message)
        console.print(violations)

    console.print(f"Событий в журнале: {result.transactions}")
    if result.ok:
        console.print("[green]Сверка сошлась по всем отчётам, инварианты выполнены.[/green]")


def _print_reconcile(discrepancies: tuple) -> None:  # type: ignore[type-arg]
    if not discrepancies:
        return
    table = Table(title="Расхождения сверки", style="red")
    table.add_column("Вид")
    table.add_column("Объект")
    table.add_column("В отчёте", justify="right")
    table.add_column("В журнале", justify="right")
    table.add_column("Разница", justify="right")
    for item in discrepancies:
        table.add_row(
            item.kind,
            item.label,
            str(item.expected),
            str(item.actual),
            str(item.difference),
        )
    console.print(table)


if __name__ == "__main__":  # pragma: no cover
    app()
