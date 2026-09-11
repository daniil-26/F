"""Разбор снапшотов ISS и прогон `verify-yields` — без сети.

Снапшоты в `tests/fixtures/iss/` синтетические и покрывают структурные случаи
эталонного набора (`docs/PARALLEL-TRACK.md`, A4). Они проверяют **проводку
данных**: что купон-плейсхолдер не стал нулём, что номинал меняется
амортизацией, что валюта номинала доезжает до расчёта.

Чего эти тесты не проверяют — конвенций Мосбиржи: поля доски в фикстурах
посчитаны тем же ядром. Конвенции проверяются только против реальных ответов
ISS (спека 5.6, `docs/BOND-REFERENCES.md`).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bondlab import iss, parse, snapshots, store, verify
from portfolio.calc.bonds import accrued_interest, face_value_at

FIXTURES = Path(__file__).parent / "fixtures" / "iss"
TRADE_DATE = date(2025, 9, 10)
SETTLEMENT = date(2025, 9, 11)


@pytest.fixture
def snapshot() -> snapshots.SnapshotStore:
    return snapshots.SnapshotStore.for_day(FIXTURES / "snapshots", TRADE_DATE)


def schedule_of(snapshot: snapshots.SnapshotStore, secid: str) -> Any:
    return parse.build_schedule(
        snapshot.load(snapshots.SECURITIES, secid),
        snapshot.load(snapshots.BONDIZATION, secid),
    )


# -- примитивы разбора ------------------------------------------------------


def test_missing_numbers_stay_none() -> None:
    """Пустое значение ISS остаётся `None` и не превращается в ноль (спека 5.4)."""
    assert parse.parse_decimal(None) is None
    assert parse.parse_decimal("") is None
    assert parse.parse_decimal(0) == 0
    assert parse.parse_decimal(35.4) == Decimal("35.4")


def test_missing_dates_stay_none() -> None:
    assert parse.parse_date(None) is None
    assert parse.parse_date("") is None
    assert parse.parse_date("0000-00-00") is None
    assert parse.parse_date("2025-09-10") == date(2025, 9, 10)


def test_rouble_is_normalised_to_iso() -> None:
    """ISS пишет рубль как `SUR`, журнал и `Money` — как `RUB`."""
    assert parse.currency_of("SUR") == "RUB"
    assert parse.currency_of("usd") == "USD"
    with pytest.raises(parse.ParseError):
        parse.currency_of("")


def test_placeholder_coupon_is_unknown_not_zero() -> None:
    """Ключевое различение трека: плейсхолдер флоатера против настоящего нуля."""
    base = {"coupondate": "2026-01-01", "startdate": "2025-01-01"}

    unfixed = parse.coupon_from_row({**base, "value": None, "valueprc": None}, "RUB")
    assert unfixed is not None and unfixed.value is None

    placeholder_zero = parse.coupon_from_row({**base, "value": 0, "valueprc": None}, "RUB")
    assert placeholder_zero is not None and placeholder_zero.value is None

    genuine_zero = parse.coupon_from_row({**base, "value": 0, "valueprc": 0}, "RUB")
    assert genuine_zero is not None and genuine_zero.value is not None
    assert genuine_zero.value.amount == 0


# -- эталонный набор --------------------------------------------------------


def test_plain_ofz_schedule(snapshot: snapshots.SnapshotStore) -> None:
    """ОФЗ с фиксированным купоном: базовый случай, обязан разбираться целиком."""
    schedule = schedule_of(snapshot, "SU26238RMFS4")

    assert schedule.currency == "RUB"
    assert schedule.initial_face_value.amount == 1000
    assert schedule.maturity_date == date(2041, 5, 15)
    assert not schedule.has_unknown_coupons
    assert not schedule.offers
    assert len(schedule.amortizations) == 1
    assert schedule.amortizations[0].is_maturity
    assert accrued_interest(schedule, SETTLEMENT) is not None


def test_amortized_ofz_changes_face_value(snapshot: snapshots.SnapshotStore) -> None:
    """Номинал не всегда 1000 и меняется во времени (A-14)."""
    schedule = schedule_of(snapshot, "SU46020RMFS2")

    assert face_value_at(schedule, date(2025, 9, 11)).amount == 1000
    assert face_value_at(schedule, date(2026, 2, 11)).amount == 800
    assert face_value_at(schedule, date(2031, 2, 12)).amount == 500
    assert face_value_at(schedule, schedule.maturity_date).amount == 0


def test_amortized_coupon_falls_with_face(snapshot: snapshots.SnapshotStore) -> None:
    schedule = schedule_of(snapshot, "SU46020RMFS2")
    before = next(c for c in schedule.coupons if c.date > date(2026, 2, 11))
    after = next(c for c in schedule.coupons if c.date > date(2031, 2, 12))
    assert before.value is not None and after.value is not None
    assert after.value.amount < before.value.amount


def test_floater_has_unknown_coupons(snapshot: snapshots.SnapshotStore) -> None:
    """ОФЗ-флоатер: за горизонтом установления ставки купоны неизвестны."""
    schedule = schedule_of(snapshot, "SU29014RMFS6")

    assert schedule.has_unknown_coupons
    unknown = [c for c in schedule.coupons if c.value is None]
    assert unknown and all(c.date > date(2025, 12, 10) for c in unknown)


def test_put_offer_is_parsed_with_kind(snapshot: snapshots.SnapshotStore) -> None:
    """Put и call — разные вещи, тип оферты обязан доезжать до расчёта (A-17)."""
    schedule = schedule_of(snapshot, "RU000A105SD9")

    assert len(schedule.offers) == 1
    offer = schedule.offers[0]
    assert offer.date == date(2026, 4, 17)
    assert offer.kind is not None and offer.kind.value == "put"
    assert offer.price_pct == 100


def test_unknown_offer_type_is_not_guessed() -> None:
    row = {"offerdate": "2026-04-17", "offertype": "Неизвестная формулировка"}
    offer = parse.offer_from_row(row)
    assert offer is not None and offer.kind is None


def test_foreign_currency_face_values(snapshot: snapshots.SnapshotStore) -> None:
    """Валюта номинала у замещающих и юаневых — не рубль (A-18)."""
    assert schedule_of(snapshot, "RU000A105RH2").currency == "USD"
    assert schedule_of(snapshot, "RU000A105NH9").currency == "CNY"


def test_settlement_currency_differs_from_face_currency(
    snapshot: snapshots.SnapshotStore,
) -> None:
    """Видно в описании выпуска, не на доске: номинал USD, расчёты в рублях."""
    payload = snapshot.load(snapshots.SECURITIES, "RU000A105RH2")
    boards = parse.rows(payload, "boards")
    assert parse.currency_of(parse.description(payload)["FACEUNIT"]) == "USD"
    assert parse.currency_of(boards[0]["currencyid"]) == "RUB"


def test_discount_bond_has_no_coupons(snapshot: snapshots.SnapshotStore) -> None:
    """Вырожденный случай: купонов нет, НКД равен нулю, погашение одно."""
    schedule = schedule_of(snapshot, "RU000A106H62")

    assert schedule.coupons == ()
    assert len(schedule.amortizations) == 1
    accrued = accrued_interest(schedule, SETTLEMENT)
    assert accrued is not None and accrued.amount == 0


def test_every_reference_issue_parses(snapshot: snapshots.SnapshotStore) -> None:
    """Весь эталонный набор разбирается: девять структурных случаев из A4."""
    secids = snapshot.names(snapshots.BONDIZATION)
    assert len(secids) == 9
    for secid in secids:
        assert schedule_of(snapshot, secid).secid == secid


# -- доска и сверка ---------------------------------------------------------


def test_board_quotes_merge_two_blocks(snapshot: snapshots.SnapshotStore) -> None:
    """`securities` и `marketdata` — разные блоки одного ответа, нужны оба."""
    quotes = parse.board_quotes(snapshot.load(snapshots.BOARD_SECURITIES, "TQOB"))
    assert quotes
    row = next(quote for quote in quotes if quote["SECID"] == "SU26238RMFS4")
    assert "ACCRUEDINT" in row and "PREVWAPRICE" in row
    assert "DURATION" in row and "YIELD" in row


def test_settlement_is_found_by_accrued_interest(snapshot: snapshots.SnapshotStore) -> None:
    """Дата расчётов не угадывается, а подбирается по совпадению НКД (A-19)."""
    schedule = schedule_of(snapshot, "SU26238RMFS4")
    quotes = parse.board_quotes(snapshot.load(snapshots.BOARD_SECURITIES, "TQOB"))
    quote = next(row for row in quotes if row["SECID"] == "SU26238RMFS4")

    candidates = verify.settlement_candidates(
        schedule, TRADE_DATE, Decimal(str(quote["ACCRUEDINT"]))
    )
    assert candidates == [SETTLEMENT]


def test_zero_accrued_interest_does_not_identify_settlement(
    snapshot: snapshots.SnapshotStore,
) -> None:
    """У дисконтного выпуска НКД нулевой в любой день и дату расчётов не задаёт."""
    schedule = schedule_of(snapshot, "RU000A106H62")
    candidates = verify.settlement_candidates(schedule, TRADE_DATE, Decimal(0))
    assert len(candidates) > 1


def test_plain_ofz_converges_with_the_board(snapshot: snapshots.SnapshotStore) -> None:
    """Проводка данных сквозная: снапшот → график → доходность → сравнение."""
    schedule = schedule_of(snapshot, "SU26238RMFS4")
    quotes = parse.board_quotes(snapshot.load(snapshots.BOARD_SECURITIES, "TQOB"))
    quote = next(row for row in quotes if row["SECID"] == "SU26238RMFS4")

    results = verify.compare_security(schedule, quote, TRADE_DATE)
    assert results
    for item in results:
        assert item.settlement == SETTLEMENT
        assert item.diff_bp is not None
        assert abs(item.diff_bp) <= verify.THRESHOLD_BP
        assert item.note == "ok"


def test_floater_yield_is_not_compared(snapshot: snapshots.SnapshotStore) -> None:
    """Биржа доходность флоатера печатает, мы — нет. Расхождение по правилу A-12."""
    schedule = schedule_of(snapshot, "SU29014RMFS6")
    quotes = parse.board_quotes(snapshot.load(snapshots.BOARD_SECURITIES, "TQOB"))
    quote = next(row for row in quotes if row["SECID"] == "SU29014RMFS6")

    results = verify.compare_security(schedule, quote, TRADE_DATE)
    assert results
    for item in results:
        assert item.iss_yield is not None
        assert item.own_yield is None
        assert "купоны неизвестны" in item.note


def test_offer_explains_the_outlier(snapshot: snapshots.SnapshotStore) -> None:
    """Биржа считает к оферте, мы к погашению — пятый пункт списка выбросов B1."""
    schedule = schedule_of(snapshot, "RU000A105SD9")
    quotes = parse.board_quotes(snapshot.load(snapshots.BOARD_SECURITIES, "TQCB"))
    quote = next(row for row in quotes if row["SECID"] == "RU000A105SD9")

    results = verify.compare_security(schedule, quote, TRADE_DATE)
    item = results[0]
    assert item.own_yield_to_offer is not None
    assert item.diff_to_offer_bp is not None
    assert abs(item.diff_to_offer_bp) <= verify.THRESHOLD_BP
    assert "сходится к оферте" in item.note


def test_summary_splits_compared_and_skipped(snapshot: snapshots.SnapshotStore) -> None:
    quotes = parse.board_quotes(snapshot.load(snapshots.BOARD_SECURITIES, "TQOB"))
    comparisons = []
    for quote in quotes:
        schedule = schedule_of(snapshot, str(quote["SECID"]))
        comparisons.extend(verify.compare_security(schedule, quote, TRADE_DATE))

    summaries = {item.price_source: item for item in verify.summarize(comparisons)}
    prevwap = summaries["PREVWAPRICE"]
    assert prevwap.compared == 2  # флоатер в сравнение не попадает
    assert prevwap.skipped == 1
    assert prevwap.max_bp is not None and prevwap.max_bp <= verify.THRESHOLD_BP
    assert verify.mean_bp(comparisons) is not None


def test_run_is_stored_in_its_own_sqlite(
    snapshot: snapshots.SnapshotStore, tmp_path: Path
) -> None:
    """Хранилище трека отдельное: в боевую БД этот код не пишет ничего."""
    schedule = schedule_of(snapshot, "SU26238RMFS4")
    quotes = parse.board_quotes(snapshot.load(snapshots.BOARD_SECURITIES, "TQOB"))
    quote = next(row for row in quotes if row["SECID"] == "SU26238RMFS4")
    comparisons = verify.compare_security(schedule, quote, TRADE_DATE)

    database = tmp_path / "bondlab.sqlite"
    with store.connect(database) as connection:
        run_id = store.save_run(
            connection,
            snapshot=snapshot.day,
            board="TQOB",
            settlement=SETTLEMENT.isoformat(),
            day_count="ACT/365",
            rows=[item.to_row() for item in comparisons],
        )
        saved = connection.execute(
            "SELECT secid, note FROM yield_checks WHERE run_id = ?", (run_id,)
        ).fetchall()

    assert database.exists()
    assert {row[0] for row in saved} == {"SU26238RMFS4"}


# -- клиент ISS (без сети) --------------------------------------------------


def test_pagination_collects_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пагинация по 100 записей: без цикла по `start` теряется всё после сотой."""
    pages = {
        0: [[index] for index in range(iss.PAGE_SIZE)],
        iss.PAGE_SIZE: [[index] for index in range(iss.PAGE_SIZE, iss.PAGE_SIZE + 7)],
    }
    seen: list[int] = []

    def fake_request(url: str, query: dict[str, str]) -> dict[str, Any]:
        start = int(query["start"])
        seen.append(start)
        return {"history": {"columns": ["value"], "data": pages[start]}}

    client = iss.IssClient()
    monkeypatch.setattr(client, "_request", fake_request)

    payload = client.get_all_pages("history/whatever", "history")
    assert seen == [0, iss.PAGE_SIZE]
    assert len(payload["history"]["data"]) == iss.PAGE_SIZE + 7


def test_disk_cache_prevents_a_second_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Кеш — не про трафик, а про то, чтобы разбор не ходил в сеть вовсе."""
    calls: list[str] = []

    def fake_request(url: str, query: dict[str, str]) -> dict[str, Any]:
        calls.append(url)
        return {"description": {"columns": [], "data": []}}

    client = iss.IssClient(cache_dir=tmp_path / "cache")
    monkeypatch.setattr(client, "_request", fake_request)

    first = client.get("securities/SU26238RMFS4")
    second = client.get("securities/SU26238RMFS4")

    assert first == second
    assert len(calls) == 1


def test_requests_carry_iss_meta_off(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_request(url: str, query: dict[str, str]) -> dict[str, Any]:
        captured.update(query)
        return {}

    client = iss.IssClient()
    monkeypatch.setattr(client, "_request", fake_request)
    client.get("securities/TEST")

    assert captured["iss.meta"] == "off"


def test_missing_snapshot_is_an_error_not_an_empty_result(tmp_path: Path) -> None:
    """Разбор из сети не подхватывается: сначала выгрузка, потом разбор."""
    empty = snapshots.SnapshotStore(root=tmp_path, day="2025-09-10")
    with pytest.raises(snapshots.SnapshotMissing):
        empty.load(snapshots.SECURITIES, "SU26238RMFS4")
    with pytest.raises(snapshots.SnapshotMissing):
        snapshots.SnapshotStore.latest(tmp_path)
