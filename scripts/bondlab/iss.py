"""Клиент MOEX ISS: retry, ограничение частоты, дисковый кеш, пагинация.

ISS отвечает медленно и не любит параллельных пачек, поэтому порядок работы
обратный привычному: **сначала ответы сохраняются на диск, разбор идёт уже из
файлов**. Перезапускать разбор придётся десятки раз, выгрузку — один раз.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://iss.moex.com/iss"

# `iss.meta=off` заметно сокращает ответ: метаданные колонок не нужны, типы
# приводятся при разборе.
DEFAULT_PARAMS: dict[str, str] = {"iss.meta": "off"}

# ISS отдаёт исторические блоки страницами по 100 записей.
PAGE_SIZE = 100

JsonDict = dict[str, Any]


class IssError(RuntimeError):
    """ISS не ответил или ответил не тем."""


@dataclass
class RateLimiter:
    """Не более `per_second` запросов в секунду. Пачками ISS отвечает отказом."""

    per_second: float = 5.0
    _last: float = field(default=0.0, init=False)

    def wait(self) -> None:
        if self.per_second <= 0:
            return
        interval = 1.0 / self.per_second
        elapsed = time.monotonic() - self._last
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last = time.monotonic()


@dataclass
class IssClient:
    """Тонкая обёртка над ISS.

    `cache_dir` — дисковый кеш по хешу запроса. Он не про экономию трафика, а
    про то, чтобы повторный прогон разбора не ходил в сеть вообще.
    """

    base_url: str = BASE_URL
    cache_dir: Path | None = None
    rate_limit: float = 5.0
    retries: int = 4
    timeout: float = 30.0
    _client: httpx.Client | None = field(default=None, init=False, repr=False)
    _limiter: RateLimiter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._limiter = RateLimiter(self.rate_limit)

    def __enter__(self) -> IssClient:
        self._client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- запросы ------------------------------------------------------------

    def get(self, path: str, **params: str | int) -> JsonDict:
        """Один запрос. Путь без префикса `/iss`, например `securities/SU26238RMFS4`."""
        query = {**DEFAULT_PARAMS, **{key: str(value) for key, value in params.items()}}
        url = f"{self.base_url}/{path.lstrip('/')}.json"

        cached = self._read_cache(url, query)
        if cached is not None:
            return cached

        payload = self._request(url, query)
        self._write_cache(url, query, payload)
        return payload

    def get_all_pages(self, path: str, block: str, **params: str | int) -> JsonDict:
        """Все страницы одного блока, склеенные в один ответ.

        Пагинация по 100 записей на исторических эндпоинтах — цикл по `start`.
        Без него берётся первая сотня строк и молча теряется остальное.
        """
        columns: list[str] | None = None
        rows: list[list[Any]] = []
        start = 0

        while True:
            payload = self.get(path, start=start, **params)
            chunk = payload.get(block)
            if not isinstance(chunk, dict):
                raise IssError(f"в ответе нет блока {block!r}: {sorted(payload)}")

            columns = columns or list(chunk.get("columns", []))
            data = list(chunk.get("data", []))
            rows.extend(data)

            if len(data) < PAGE_SIZE:
                break
            start += len(data)

        return {block: {"columns": columns or [], "data": rows}}

    def _request(self, url: str, query: dict[str, str]) -> JsonDict:
        if self._client is None:
            raise IssError("клиент используется вне контекстного менеджера `with`")

        delay = 1.0
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            self._limiter.wait()
            try:
                response = self._client.get(url, params=query)
                if response.status_code >= 500 or response.status_code == 429:
                    raise IssError(f"{response.status_code} на {response.url}")
                response.raise_for_status()
                parsed: JsonDict = response.json()
                return parsed
            except (httpx.HTTPError, IssError, json.JSONDecodeError) as error:
                last_error = error
                if attempt == self.retries:
                    break
                time.sleep(delay)
                delay *= 2

        raise IssError(f"ISS не ответил после {self.retries + 1} попыток: {url}") from last_error

    # -- кеш ----------------------------------------------------------------

    def _cache_path(self, url: str, query: dict[str, str]) -> Path | None:
        if self.cache_dir is None:
            return None
        key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(query.items()))
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, url: str, query: dict[str, str]) -> JsonDict | None:
        path = self._cache_path(url, query)
        if path is None or not path.exists():
            return None
        cached: JsonDict = json.loads(path.read_text(encoding="utf-8"))
        return cached

    def _write_cache(self, url: str, query: dict[str, str], payload: JsonDict) -> None:
        path = self._cache_path(url, query)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# -- эндпоинты (`docs/PARALLEL-TRACK.md`, A2) ------------------------------


def boards(client: IssClient) -> JsonDict:
    """Перечень досок. Коды досок меняются — получать их программно надёжнее."""
    return client.get("engines/stock/markets/bonds/boards")


def board_securities(client: IssClient, board: str) -> JsonDict:
    """Текущие котировки по доске: один запрос на доску, не по бумаге."""
    return client.get(f"engines/stock/markets/bonds/boards/{board}/securities")


def security(client: IssClient, secid: str) -> JsonDict:
    """Описание выпуска: номинал, валюта номинала, дата погашения."""
    return client.get(f"securities/{secid}")


def bondization(client: IssClient, secid: str) -> JsonDict:
    """Купоны, амортизации, оферты. Каждый блок пагинируется отдельно."""
    merged: JsonDict = {}
    path = f"statistics/engines/stock/markets/bonds/bondization/{secid}"
    for block in ("coupons", "amortizations", "offers"):
        merged.update(client.get_all_pages(path, block, **{"iss.only": block}))
    return merged


def history(client: IssClient, board: str, secid: str, **params: str | int) -> JsonDict:
    """История цен по бумаге. Пагинация обязательна: по 100 записей на страницу."""
    path = f"history/engines/stock/markets/bonds/boards/{board}/securities/{secid}"
    return client.get_all_pages(path, "history", **params)


def iter_secids(payload: JsonDict, block: str = "securities") -> Iterator[str]:
    """Коды бумаг из ответа доски, в порядке ответа."""
    chunk = payload.get(block)
    if not isinstance(chunk, dict):
        return
    columns = list(chunk.get("columns", []))
    if "SECID" not in columns:
        return
    index = columns.index("SECID")
    for row in chunk.get("data", []):
        value = row[index]
        if isinstance(value, str):
            yield value
