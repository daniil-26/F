"""Разрешение тикеров и ISIN в `instrument_id`, ведение справочника.

Справочник минимальный (этап 1): тикер, ISIN, тип, валюта. Эмитенты, секторы и
графики платежей появятся на этапе 2 вместе с MOEX ISS.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio.models import Instrument, InstrumentKind

__all__ = ["InstrumentResolver", "guess_kind", "normalize_ticker"]

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
# Коды ОФЗ и большинства корпоративных выпусков опознаются по форме кода.
_BOND_TICKER_RE = re.compile(r"^(SU\d{5}RMFS\d|RU000[A-Z0-9]{8})$")


def normalize_ticker(ticker: str | None) -> str | None:
    if ticker is None:
        return None
    cleaned = ticker.strip().upper()
    return cleaned or None


def guess_kind(ticker: str | None, isin: str | None, name: str | None = None) -> InstrumentKind:
    """Тип инструмента по форме кода.

    Догадка сознательно грубая: точный справочник приезжает с MOEX на этапе 2.
    Ошибка здесь не портит журнал — количество и деньги от типа не зависят.
    """
    code = normalize_ticker(ticker) or ""
    isin_code = (isin or "").upper()

    if _BOND_TICKER_RE.match(code) or isin_code.startswith("RU000A"):
        return InstrumentKind.BOND
    if name and any(marker in name.lower() for marker in ("облигац", "офз", "бонд")):
        return InstrumentKind.BOND
    if name and any(marker in name.lower() for marker in ("фонд", "етф", "etf", "пиф")):
        return InstrumentKind.ETF
    if code:
        return InstrumentKind.SHARE
    return InstrumentKind.OTHER


class InstrumentResolver:
    """Разрешает тикер и ISIN в запись справочника, создавая недостающие.

    Кеширует внутри импорта: один отчёт упоминает одну бумагу десятки раз, и без
    кеша каждая строка порождает запрос.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._by_ticker: dict[str, Instrument] = {}
        self._by_isin: dict[str, Instrument] = {}

    def resolve(
        self,
        ticker: str | None = None,
        isin: str | None = None,
        name: str | None = None,
        currency: str | None = None,
    ) -> Instrument | None:
        """`None`, если строка не несёт ни тикера, ни ISIN, — денежная операция."""
        code = normalize_ticker(ticker)
        isin_code = (isin or "").strip().upper() or None
        if isin_code is not None and not _ISIN_RE.match(isin_code):
            isin_code = None
        if code is None and isin_code is None:
            return None

        found = self._lookup(code, isin_code)
        if found is None:
            found = Instrument(
                ticker=code,
                isin=isin_code,
                name=name,
                kind=guess_kind(code, isin_code, name),
                currency=currency,
            )
            self._session.add(found)
            self._session.flush()
        else:
            self._enrich(found, code, isin_code, name, currency)

        if found.ticker:
            self._by_ticker[found.ticker] = found
        if found.isin:
            self._by_isin[found.isin] = found
        return found

    def find(self, ticker: str | None = None, isin: str | None = None) -> Instrument | None:
        """Только поиск, без создания. Нужен проверкам: `check` ничего не пишет."""
        code = normalize_ticker(ticker)
        isin_code = (isin or "").strip().upper() or None
        if code is None and isin_code is None:
            return None
        return self._lookup(code, isin_code)

    def _lookup(self, code: str | None, isin_code: str | None) -> Instrument | None:
        if isin_code is not None:
            cached = self._by_isin.get(isin_code)
            if cached is not None:
                return cached
        if code is not None:
            cached = self._by_ticker.get(code)
            if cached is not None:
                return cached

        # ISIN устойчивее тикера: тикер брокер может писать по-разному.
        if isin_code is not None:
            found = self._session.scalar(
                select(Instrument).where(Instrument.isin == isin_code)
            )
            if found is not None:
                return found
        if code is not None:
            return self._session.scalar(select(Instrument).where(Instrument.ticker == code))
        return None

    def _enrich(
        self,
        instrument: Instrument,
        code: str | None,
        isin_code: str | None,
        name: str | None,
        currency: str | None,
    ) -> None:
        """Дозаполняет пустые поля. Заполненные не перезаписываются."""
        if instrument.ticker is None and code is not None:
            instrument.ticker = code
        if instrument.isin is None and isin_code is not None:
            instrument.isin = isin_code
        if instrument.name is None and name:
            instrument.name = name
        if instrument.currency is None and currency:
            instrument.currency = currency
        if instrument.kind == InstrumentKind.OTHER:
            instrument.kind = guess_kind(instrument.ticker, instrument.isin, instrument.name)
