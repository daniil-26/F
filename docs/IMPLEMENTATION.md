# Реализация этапа 1. Техническое описание

Что сделано, как устроен каждый файл, как модули вызывают друг друга и в каких
форматах между ними ходят данные.

Читается вместе с [`portfolio-spec.md`](portfolio-spec.md) (описание
функционала) и [`STAGE-1.md`](STAGE-1.md) (план этапа). Предметные решения — в
[`../ASSUMPTIONS.md`](../ASSUMPTIONS.md), ссылки вида «A-05» указывают туда.

Документ описывает состояние кода, а не намерения: если здесь написано «делает
X», это проверяется тестом.

---

## 1. Что было сделано

### 1.1 Перестройка репозитория

До: три markdown-файла в корне. После:

* `docs/` — спецификация и планы этапов (перенесены `git mv`, история сохранена);
* корень — конфигурация проекта: `pyproject.toml`, `docker-compose.yml`,
  `Dockerfile`, `alembic.ini`, `.env.example`, `.gitignore`, `README.md`,
  `ASSUMPTIONS.md`;
* `src/portfolio/` — код по слоям из раздела 9 спеки;
* `migrations/` — alembic;
* `data/{raw,input}` — сырьё и декларативные входы;
* `tests/` — фикстуры, юнит-тесты, property-тесты.

### 1.2 Выполненные задачи плана

| Задача | Результат | Где смотреть |
|---|---|---|
| T1 Каркас | зависимости, линтеры, БД в compose, конфиг, сессии, alembic | `pyproject.toml`, `config.py`, `db.py`, `migrations/` |
| T2 Разбор форматов | русские числа, даты, валюты | `adapters/formats.py` |
| T3 Деньги | `Money`, арифметика, округление, сравнение с допуском | `calc/money.py` |
| T4 Схема | 4 таблицы, полная схема журнала, UNIQUE на ключе | `models.py`, `migrations/versions/0001_stage1_journal.py` |
| T5 Стадия 1 парсера | HTML → таблицы как есть | `adapters/broker/tables.py` |
| T6 Стадия 2 парсера | таблицы → события и остатки | `adapters/broker/{anchors,mapping_v1,dto}.py` |
| T7 Домен | ключи, сырьё, справочник, журнал, проекции, сверка, инварианты, входящие | `domain/*.py` |
| T8 Импорт отчёта | оркестрация в одной транзакции | `jobs/import_broker.py` |
| T9 Импорт CSV | декларативный дифф со сторно | `adapters/csv_input.py`, `jobs/import_csv.py` |
| T10 CLI | четыре команды, коды возврата | `cli.py`, `jobs/{check,inbox}.py` |

Не выполнено: шаг 0 (разведка архива отчётов) и T11 (бэкфилл) — обе задачи
требуют реальных отчётов брокера. Следствия описаны в разделе 11.

### 1.3 Решения, принятые сверх плана

**`check` не хранит контрольные числа.** Остатки, с которыми сверяется журнал,
заново вычитываются из сохранённого сырья в `data/raw/`. Отдельная таблица
контрольных чисел нарушила бы требование 5.8 (полное восстановление из
`data/raw/` + `data/input/` + код) и добавила бы состояние, которое может
разойтись с сырьём. Побочный эффект полезный: сверка идёт по каждому отчёту
автоматически, а не только по последнему.

**Направление импортов проверяется тестом, а не линтером.** `ruff` умеет
запрещать импорт модуля, но не различает слои: запрет `portfolio.domain`
сработал бы и внутри самого `domain`. `tests/test_layering.py` разбирает AST
каждого модуля и сверяет с таблицей разрешённых зависимостей.

**Точный `Decimal` в SQLite.** Тесты ходят в SQLite, который хранит `NUMERIC`
как `float`. Чтобы тесты проверяли те же числа, что поедут в PostgreSQL, в
`models.py` заведён `_SqliteDecimal` — вариант типа, хранящий `Decimal` строкой.
Боевая БД этого не касается.

---

## 2. Слои и направление зависимостей

```
                    HTML-отчёт, CSV
                           │
                    ┌──────▼──────┐
                    │  adapters/  │  знание про HTML, кодировки, форматы файлов
                    │             │  возвращает DTO, в БД не пишет
                    └──────┬──────┘
                           │ ParsedOperation / ParsedBalance / UnparsedRow
                    ┌──────▼──────┐
       models.py ◄──┤   domain/   │  ключи, справочник, журнал, проекции, сверка
                    └──────┬──────┘
                           │ LedgerEntry / LedgerDiff / ReconcileResult
                    ┌──────▼──────┐
                    │    jobs/    │  единственный слой с транзакциями
                    └──────┬──────┘
                           │ ImportResult / CheckResult / InboxItem
                    ┌──────▼──────┐
                    │   cli.py    │  разбор аргументов, rich, коды возврата
                    └─────────────┘

        calc/  — чистые функции, не импортируют ничего из проекта
```

Разрешённые зависимости (таблица `ALLOWED` в `tests/test_layering.py`):

| Слой | Может импортировать |
|---|---|
| `calc` | ничего из проекта |
| `adapters` | `calc`, `adapters` |
| `domain` | `calc`, `domain`, `adapters`, `models` |
| `jobs` | всё, кроме `web` |
| `web` | `calc`, `domain`, `jobs`, `models`, `config`, `db` |

Дополнительно тест проверяет: `calc/` не импортирует `sqlalchemy`, `httpx`,
`psycopg` и не вызывает `date.today()` / `datetime.now()`; `session_scope()` и
`.commit()` встречаются только в `jobs/` и `db.py`.

---

## 3. Форматы данных

### 3.1 Схема БД

Четыре таблицы. Денежные типы: `AMOUNT = NUMERIC(24,6)`,
`QUANTITY = PRICE = NUMERIC(28,10)`, `RATE = NUMERIC(20,10)`.

**`accounts`** — брокерский счёт, ИИС или банковский вклад.

| Колонка | Тип | Примечание |
|---|---|---|
| `id` | integer PK | |
| `code` | varchar(64) UNIQUE | код из отчёта брокера, по нему ищется счёт |
| `name` | varchar(255) | |
| `kind` | enum `BROKER`/`IIS`/`BANK` | тип счёта меняет налоговую логику |
| `broker`, `bank` | varchar(128), NULL | «банк» нужен для контроля лимита АСВ |
| `currency` | varchar(3) | |
| `tax_regime` | varchar(32), NULL | |
| `opened_at`, `closed_at` | date, NULL | |

**`instruments`** — минимальный справочник (этап 1).

| Колонка | Тип | Примечание |
|---|---|---|
| `id` | integer PK | |
| `ticker` | varchar(64) UNIQUE, NULL | |
| `isin` | varchar(12) UNIQUE, NULL | |
| `name` | varchar(255), NULL | |
| `kind` | enum `SHARE`/`BOND`/`ETF`/`CURRENCY`/`OTHER` | догадка по форме кода |
| `currency` | varchar(3), NULL | |

CHECK `ticker IS NOT NULL OR isin IS NOT NULL` — запись без обоих идентификаторов
неразрешима и не нужна.

**`raw_reports`** — сохранённое сырьё.

| Колонка | Тип | Примечание |
|---|---|---|
| `id` | integer PK | |
| `sha256` | varchar(64) UNIQUE | хеш содержимого; повторная загрузка видна до разбора |
| `source` | varchar(32) | `broker` |
| `original_filename` | varchar(255) | |
| `stored_path` | varchar(512) | `data/raw/<sha256><расширение>` |
| `parser_version` | varchar(32), NULL | `v1` — какой `mapping_*` разобрал файл |
| `period_start`, `period_end` | date, NULL | вынуты из текста отчёта |
| `note` | text, NULL | причина импорта с несошедшейся сверкой |
| `imported_at` | timestamptz, `now()` | |

**`transactions`** — журнал операций, полная схема по спеке 3.2.

| Колонка | Тип | Примечание |
|---|---|---|
| `id` | bigint PK | |
| `natural_key` | varchar(128) UNIQUE | ключ идемпотентности (A-01) |
| `event_type` | enum, 21 значение | `OPENING_BALANCE`, `CASH_IN`, …, `DEPOSIT_CLOSE` |
| `trade_date` | date | по ней считаются позиции |
| `settlement_date` | date, NULL | по ней считаются деньги |
| `account_id` | FK `accounts` | |
| `instrument_id` | FK `instruments`, NULL | NULL у денежных операций |
| `quantity` | numeric(28,10), NULL | изменение позиции со знаком (A-05) |
| `price` | numeric(28,10), NULL | |
| `accrued_int` | numeric(24,6), NULL | НКД |
| `fee` | numeric(24,6), NULL | справочно, в остаток не входит (A-06) |
| `fee_kind` | enum `BROKER`/`DEPOSITARY`/`EXCHANGE`/`WITHDRAWAL`/`OTHER`, NULL | |
| `amount` | numeric(24,6) NOT NULL | изменение остатка со знаком (A-05) |
| `currency` | varchar(3) NOT NULL | CHECK `length(currency) = 3` |
| `cbr_rate` | numeric(20,10), NULL | **пустует на этапе 1**: курсов нет |
| `withheld_at_source` | boolean, NULL | NULL = «неизвестно» (A-04) |
| `predecessor_id` | FK `instruments`, NULL | **пустует**: для `CONVERSION`/`SPIN_OFF` |
| `broker_trade_no` | varchar(64), NULL | |
| `source_report_id` | FK `raw_reports`, NULL | |
| `source_row_id` | varchar(64), NULL | `<файл>:<id строки>` для диффа CSV |
| `cost_basis` | numeric(28,10), NULL | цена приобретения начального остатка |
| `basis_quality` | enum `known`/`estimated`/`unknown`, NULL | |
| `note` | text, NULL | |
| `reverses_id` | FK `transactions`, UNIQUE, NULL | ссылка на сторнируемую запись |
| `recorded_at` | timestamptz, `now()` | |

UNIQUE на `reverses_id` означает: операцию сторнируют не более одного раза.
Второй сторно по той же записи — ошибка диффа, а не сценарий.

Индексы: `(account_id, trade_date)`, `(account_id, settlement_date)`,
`(instrument_id)`, `(source_report_id)`, `(source_row_id)`.

### 3.2 DTO парсера (`adapters/broker/dto.py`)

Тикер и валюта строками, без `instrument_id` и `natural_key`: разрешение
идентификаторов — дело домена.

```python
@dataclass(frozen=True)
class ParsedOperation:
    kind: str                    # строка, не EventType: маппинг в domain
    trade_date: date
    settlement_date: date | None
    ticker: str | None
    quantity: Decimal | None     # знак = направление (A-05)
    price: Decimal | None
    amount: Decimal              # знак = движение денег (A-05)
    currency: str
    fee_kind: str | None = None
    broker_trade_no: str | None = None
    withheld_at_source: bool | None = None
    isin: str | None = None
    accrued_int: Decimal | None = None
    fee: Decimal | None = None
    instrument_name: str | None = None
    note: str | None = None
    raw_row: dict[str, str] = field(default_factory=dict)   # для разбора расхождений

@dataclass(frozen=True)
class ParsedBalance:
    kind: str                    # cash | security
    ticker: str | None
    quantity: Decimal
    currency: str
    isin: str | None = None
    as_of: date | None = None    # проставляется концом периода отчёта
    raw_row: dict[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class UnparsedRow:
    table: str                   # подпись таблицы, из которой строка
    row: dict[str, str]
    reason: str

@dataclass(frozen=True)
class ParsedReport:
    operations: tuple[ParsedOperation, ...]
    balances: tuple[ParsedBalance, ...]
    unparsed: tuple[UnparsedRow, ...]
    mapping_version: str
    period_start: date | None = None
    period_end: date | None = None
    account_code: str | None = None
```

Контракт парсера из плана сохранён отдельной функцией:

```python
parse_report(content: bytes) -> tuple[list[ParsedOperation],
                                      list[ParsedBalance],
                                      list[UnparsedRow]]
parse(content: bytes) -> ParsedReport     # то же плюс реквизиты отчёта
```

### 3.3 Промежуточное представление таблиц (`tables.py`)

```python
@dataclass(frozen=True)
class RawTable:
    index: int                            # порядковый номер в документе
    caption: str                          # <caption>, если есть
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]     # все ячейки строками
    preceding_text: tuple[str, ...]       # текст перед таблицей — по нему якорение
```

`RawTable.as_dicts()` → `list[dict[str, str]]`: строка как «заголовок → ячейка».
Колонки без заголовка получают позиционное имя `col_3`, повторяющиеся заголовки
— суффикс `__1`. Терять безымянные колонки нельзя: в них у брокеров приезжают
признаки операции.

### 3.4 Записи журнала (`domain/ledger.py`)

```python
@dataclass(frozen=True)
class LedgerEntry:          # событие, готовое к записи: id разрешены, ключ построен
    natural_key: str
    event_type: EventType
    trade_date: date
    settlement_date: date | None
    account_id: int
    amount: Decimal
    currency: str
    instrument_id: int | None = None
    quantity / price / accrued_int / fee: Decimal | None = None
    fee_kind: FeeKind | None = None
    withheld_at_source: bool | None = None
    broker_trade_no / source_row_id / note: str | None = None
    source_report_id: int | None = None
    cost_basis: Decimal | None = None
    basis_quality: BasisQuality | None = None

@dataclass(frozen=True)
class LedgerDiff:           # что импорт сделает с журналом
    new: tuple[LedgerEntry, ...]
    unchanged: tuple[LedgerEntry, ...]
    reversals: tuple[tuple[Transaction, LedgerEntry | None], ...]
    notes: tuple[str, ...]
```

`reversals` — пары «запись журнала, которую сторнируем» и «новая версия события».
Второй элемент `None`, если строка просто исчезла из декларативного файла.

### 3.5 Декларативные CSV

Первая колонка каждого файла — `id`, стабильный и не переиспользуемый. Файлы
описывают желаемое состояние, а не события.

`data/input/opening_balances.csv`:

```
id,account,date,ticker,isin,quantity,cost_basis,basis_quality,currency,note
ob-001,12345-АБВ,2025-07-31,,,50000.00,,known,RUB,остаток денег
ob-002,12345-АБВ,2025-07-31,SBER,RU0009029540,100,255.40,estimated,RUB,средняя
```

Пустые `ticker` и `isin` означают денежный остаток: сумма идёт в `amount`.
Заполненные — бумажный: количество идёт в `quantity`, а `amount = 0`, потому что
начальный остаток бумаг денег не двигает. `basis_quality` ∈
`known | estimated | unknown`.

`data/input/cash_flows.csv`:

```
id,account,date,kind,amount,currency,note
cf-001,12345-АБВ,2025-08-01,CASH_IN,100000,RUB,довнесение
cf-002,12345-АБВ,2025-08-29,CASH_OUT,20000,RUB,изъятие
```

`kind` ∈ `CASH_IN | CASH_OUT | TRANSFER`. Знак суммы задаётся типом события, а не
колонкой: `CASH_OUT` всегда уменьшает остаток, как бы ни была записана сумма.

Числа и даты в CSV разбираются тем же `adapters/formats.py`, что и HTML, поэтому
`1 234,56` и `31.07.2025` допустимы наравне с `1234.56` и `2025-07-31`.

### 3.6 Golden-эталоны

К каждому HTML-отчёту в `tests/fixtures/` два JSON:

* `*.tables.json` — форма документа после стадии 1: количество таблиц, подписи,
  предшествующий текст, заголовки, число строк, ширина;
* `*.expected.json` — результат стадии 2: операции, остатки, нераспознанное,
  период, код счёта.

Все числа в эталонах — строки (`"-10529.45"`), чтобы сравнение не зависело от
представления `float` в JSON.

### 3.7 Конфигурация (`.env`)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://portfolio:portfolio@localhost:5432/portfolio` | |
| `DATA_RAW_DIR` | `data/raw` | сырьё, имя файла = хеш |
| `DATA_INPUT_DIR` | `data/input` | декларативные CSV |
| `TAX_RATE` | `0.13` | для оценки налога (этап 3), **не** для восстановления gross |
| `RECONCILE_CASH_TOLERANCE` | `0.01` | деньги сверяются до копейки |
| `RECONCILE_QUANTITY_TOLERANCE` | `0` | бумаги — точно |

Относительные пути разворачиваются от корня проекта (`Settings.raw_dir`,
`Settings.input_dir`).

---

## 4. Корневые модули

### `config.py`

`Settings` на pydantic-settings, читает `.env`. Логики нет. `get_settings()`
кеширован `lru_cache`; тесты сбрасывают кеш через `get_settings.cache_clear()`.

### `db.py`

`get_engine()`, `get_session_factory()` (оба кешированы), `session_scope()` —
контекстный менеджер: `commit` при успешном выходе, `rollback` при исключении,
`close` всегда. Запросов к данным здесь нет.

### `models.py`

Все таблицы одним файлом, бизнес-методов на моделях нет. Здесь же перечисления
`EventType`, `AccountKind`, `InstrumentKind`, `BasisQuality`, `FeeKind` —
перечисления живут рядом со схемой, потому что от них зависят CHECK-ограничения;
признак «внешний/внутренний» и построение ключа при этом лежат в
`domain/events.py`.

Перечисления пишутся в БД как `VARCHAR + CHECK`, а не нативным типом PostgreSQL:
добавление значения в нативный enum — отдельная миграция с блокировкой, а
значений мало.

### `cli.py`

Typer + rich. Вычислений нет — только вызовы `jobs/` и печать таблиц.

| Команда | Вызывает | Код возврата |
|---|---|---|
| `import-broker <file> [--dry-run] [--account] [--force]` | `jobs.import_broker.import_broker_report` | `1`, если сверка не сошлась или есть нераспознанные строки |
| `import-csv <file> [--dry-run]` | `jobs.import_csv.import_csv_file` | `2` при ошибке валидации (`CsvValidationError`) |
| `check` | `jobs.check.run_check` | `1` при любом расхождении или нарушенном инварианте |
| `inbox` | `jobs.inbox.collect_inbox` | `1`, если очередь не пуста |

---

## 5. `calc/` — чистые функции

### `money.py`

`Money` — замороженный датакласс `(Decimal, валюта ISO-4217)` с собственным
`__init__`.

| Элемент | Поведение |
|---|---|
| `Money(amount, currency)` | `float` и `bool` — `TypeError`; `int`/`str`/`Decimal` принимаются; валюта проверяется регуляркой `^[A-Z]{3}$` |
| `+`, `-`, `<`, `<=`, `>`, `>=` | требуют одинаковой валюты, иначе `CurrencyMismatch` |
| `*`, `/` | только на `Decimal`/`int`; `Money * Money` и `Money / Money` — `TypeError` |
| `money_sum(items, currency=None)` | проверяет валюты; сумма пустого списка без явной валюты — `ValueError` |
| `is_close(a, b, tol)` | сравнение с допуском для сверки; отрицательный допуск — `ValueError` |
| `round_amount(m, places=CENT)` | `ROUND_HALF_UP`, по умолчанию до копейки |
| `zero(currency)` | явный ноль в известной валюте |

Почему сумма пустого списка без валюты — ошибка: «ноль неизвестно чего» — то
самое подставное значение, которое растворяется в итоге портфеля и больше не
обнаруживается (спека 5.4).

`convert` и распределение остатка на этапе 1 не нужны: курсов нет, амортизаций
нет. `mypy --strict` на этом каталоге проходит.

---

## 6. `adapters/` — внешний мир

### `formats.py`

Единственное место, где живёт знание о том, как источник печатает числа.
Используется и HTML-парсером, и CSV.

| Функция | Вход → выход | Особенности |
|---|---|---|
| `normalize_text(str\|None) -> str` | схлопывает любые пробелы, включая `\xa0`, ` ` | |
| `is_blank(str\|None) -> bool` | `""`, `-`, `—`, `н/д`, `null` … | «значения нет», а не ноль |
| `parse_decimal(str\|Decimal\|int) -> Decimal` | `1 234,56`, `1.234,56`, `1,234.56`, `(1 234,56)`, `−1 234,56`, `15,5 руб.` | пустое значение и мусор → `FormatError`; `float` → `FormatError` |
| `parse_optional_decimal(...) -> Decimal \| None` | прочерк → `None` | |
| `parse_date(...) -> date` | `12.08.2025`, `12.08.2025 17:43:01`, `2025-08-12`, `12.08.25` | время отбрасывается |
| `parse_currency(str) -> str` | `руб.`, `RUR`, `₽`, `$`, `евро`, `156` → ISO-код | незнакомое → `FormatError` |
| `format_decimal(Decimal, decimals=None) -> str` | `-1234567.5` → `-1 234 567,5` | обратна `parse_decimal` (property-тест) |

Разрешение неоднозначности разделителей (`_normalize_separators`):

* есть и `,` и `.` — десятичным считается последний: `1.234,56` → `1234.56`,
  `1,234.56` → `1234.56`;
* один вид разделителя, больше одного вхождения — это разряды, **но только если
  все группы после первой ровно из трёх цифр**: `1.234.567` → `1234567`, а
  `1 2 3,4,5` → `FormatError`. Без этой оговорки мусор молча превращался бы в
  число;
* `1.234` трактуется как дробное: точка как разделитель разрядов в русских
  отчётах не используется.

### `broker/tables.py` — стадия 1

`extract_tables(content: bytes) -> list[RawTable]`.

1. `decode_report(content)` — кодировка берётся из `<meta charset>` первых 4 КБ,
   затем перебираются `utf-8`, `cp1251`, `koi8-r`. Ошибка здесь превращает
   кириллицу в мусор, и якоря перестают находиться;
2. `lxml.html.fromstring`, обход всех `<table>` в порядке появления;
3. для каждой таблицы строки берутся **только свои**: `tr` относится к
   ближайшему предку `table`, ячейка — к ближайшей `tr`. Отчёты брокеров
   верстаются вложенными таблицами, иначе строки обёртки и данных смешались бы;
3а. ячейки раскладываются в **сетку с учётом `colspan` и `rowspan`**: каждая
   получает номер колонки, а не порядковый номер в строке. В выгрузке Excel
   заголовок «Дата оплаты» занимает две колонки, и без сетки «Место совершения
   сделки» из шапки оказывается над «Дата поставки фактическая» в данных.
   Шапка бывает многострочной — имена колонок склеиваются сверху вниз
   («Дата оплаты · Фактическая»), иначе две колонки назывались бы одинаково;
4. заголовок — строка из одних `<th>`; если `<th>` нет, заголовком считается
   первая строка при условии, что в ней нет чисел;
5. `preceding_text` — до трёх текстовых узлов перед таблицей, включая соседей
   предков: заголовок секции часто лежит вне таблицы и даже вне её `div`.

Модуль ничего не знает про сделки. Его golden-эталон (`*.tables.json`) меняется
только при смене движка отчётов.

### `broker/anchors.py` — подписи

Таблицы соответствий, вынесенные отдельно, чтобы смена формулировки правилась
одной строкой.

| Объект | Что описывает |
|---|---|
| `Section` | `TRADES`, `CASH_FLOW`, `CASH_BALANCES`, `SECURITY_BALANCES`, `REPORT_HEADER` |
| `SECTION_SIGNATURES` | подписи секций: «заключенные в отчетном периоде сделки», «движение денежных средств», «состояние портфеля» … |
| `COLUMNS` | логическое имя колонки → варианты заголовка (21 имя) |
| `TOTAL_ROW_MARKERS` | «итого», «всего», «в том числе», «оборот» |
| `OPERATION_SIGNATURES` | текст описания → (тип события, вид комиссии) |

Функции:

* `normalize_signature(text)` — регистр, пробелы, пунктуация, `ё` → `е`;
* `find_sections(tables) -> dict[Section, list[RawTable]]` — сначала по подписи
  (`caption`, затем текст перед таблицей), при неудаче — по набору колонок
  (`_section_by_headers`);
* `resolve_columns(table) -> dict[str, str]` — логическое имя → фактическое имя
  колонки;
* `is_total_row(row) -> bool` — «Итого», подзаголовки, пустые строки;
* `match_operation_kind(text) -> tuple[str, str | None] | None`;
* `match_direction(text) -> str | None` — `BUY` / `SELL`.

**Правило самого длинного совпадения** применяется трижды и в каждом случае
закрывает конкретную ошибку:

| Где | Конфликт | Что было бы при выборе «первого попавшегося» |
|---|---|---|
| секции | «сделки» ⊂ «заключенные в отчетном периоде сделки» | секция определялась бы по случайному вхождению |
| колонки | «дата» ⊂ «дата расчетов» | обе даты съехали бы в одну колонку |
| операции | «комиссия» ⊂ «комиссия депозитария»; «перевод» в «пополнение счета, перевод из банка» | депозитарная комиссия стала бы прочей, довнесение — переводом |

`_drop_duplicate_targets` дополнительно следит, чтобы одна физическая колонка не
обслуживала два логических имени: «Сумма зачисления» достаётся `credit`, а не
`amount`, потому что совпавший вариант длиннее. Проигравшее имя не подставляется
никуда — лучше `UnparsedRow`, чем сумма из чужой колонки.

### `broker/mapping_v1.py` — стадия 2

`parse(content) -> ParsedReport` делает следующее:

```
extract_tables(content)                      → list[RawTable]
find_sections(tables)                        → {Section: [RawTable]}
  TRADES             → _parse_trades         → ParsedOperation | UnparsedRow
  CASH_FLOW          → _parse_cash_flow      → ParsedOperation | UnparsedRow
  CASH_BALANCES      → _parse_cash_balances  → ParsedBalance   | UnparsedRow
  SECURITY_BALANCES  → _parse_security_balances
таблицы вне секций   → _unknown_tables       → UnparsedRow
_extract_period, _extract_account            → реквизиты отчёта
```

**Сделки** (`_trade_row`). Обязательны дата и количество. Направление берётся из
колонки вида сделки, при её отсутствии выводится из знаков (`_direction_from_signs`);
если не выводится — строка уходит в `UnparsedRow`, а не угадывается.

Денежный эффект (`_trade_gross`, допущение A-07): берётся сумма сделки из
отчёта — это авторитетное число брокера с его округлением; `количество × цена`
считается, только если колонки суммы нет. НКД прибавляется, если он приходит
отдельной колонкой: наличие колонки означает, что сумма сделки его не включает.
Знак: покупка `−`, продажа `+`.

Комиссии сделки (`_fee_operations`, A-06) становятся отдельными событиями `FEE`
со своим `fee_kind` и теми же датами. Поле `fee` самой сделки заполняется суммой
по модулю справочно и в денежный остаток не входит — иначе комиссия посчиталась
бы дважды.

**Движение денежных средств** (`_cash_flow_row`). Тип определяется по описанию;
не опознан — `UnparsedRow` с текстом описания в причине. Сумма (`_cash_amount`):
если есть колонки «зачисление»/«списание» — `credit − |debit|`; если одна общая
сумма — знак берётся из типа события (`FEE`, `TAX`, `CASH_OUT` уменьшают
остаток).

Расщепление gross/net (A-04): если в строке есть колонка суммы налога, в журнал
идёт сумма **до** налога (`amount + |tax|`) и отдельное событие `TAX` на
`−|tax|`; `withheld_at_source = False`. Если колонки налога нет,
`_withheld_flag` ищет в описании «за вычетом», «нетто», «брутто»; не нашёл —
`None`, «неизвестно». Деление на `(1 − ставка)` не делается нигде.

**Остатки.** Денежный — валюта и исходящий остаток; бумажный — тикер/ISIN и
исходящий остаток. Всем остаткам без собственной даты проставляется конец
периода отчёта.

**Нераспознанное.** Таблицы вне секций попадают в `UnparsedRow` построчно, но
только если похожи на данные (`_looks_like_data`: не меньше двух колонок и есть
строка с числом). Верстальные обёртки и текстовые шапки пропускаются молча —
иначе очередь «Входящих» забилась бы шумом и её перестали бы читать.

Версия парсера — константа `MAPPING_VERSION = "v1"`, пишется в
`raw_reports.parser_version`. При смене формата рядом появляется `mapping_v2.py`,
v1 остаётся рабочей для старых файлов.

### `csv_input.py`

Чтение и валидация декларативных файлов через pydantic.

* `sniff_kind(path) -> str` — вид файла по имени, затем по набору колонок;
* `read_opening_balances(path) -> list[OpeningBalanceRow]`;
* `read_cash_flows(path) -> list[CashFlowRow]`.

Проверяется: первая колонка называется `id`; `id` не пуст и не повторяется;
`basis_quality` и `kind` из допустимого множества; числа и даты разбираются
`formats`. Ошибки собираются **по всему файлу** и поднимаются одним
`CsvValidationError` со списком строк вида
`строка 2, поле quantity: не разобрано как число: 'не число'`. Падать на первой
ошибке значило бы чинить файл по одной строке за прогон.

---

## 7. `domain/` — журнал и проекции

### `events.py`

* `EXTERNAL_EVENT_TYPES` — `OPENING_BALANCE`, `CASH_IN`, `CASH_OUT`, `TRANSFER`.
  Классификация — свойство типа, а не решение на лету: от неё зависит
  корректность всех доходностей;
* `is_external(event_type)`, `to_event_type(kind)` — незнакомый тип даёт
  `ValueError`, а не «прочее»;
* `KeyInput` — реквизиты, из которых строится ключ: код счёта, тип, дата,
  `instrument_ref`, количество, цена, сумма, вид комиссии, номер сделки, `id`
  строки CSV;
* `natural_key(item, index=0) -> str` — sha256 первых 40 символов;
* `assign_natural_keys(items) -> list[str]` — ключи для набора операций одного
  импорта.

Порядок предпочтения (A-01):

| Условие | Состав ключа |
|---|---|
| есть `source_row_id` | `csv \| счёт \| дата \| тип \| инструмент \| id строки` |
| есть `broker_trade_no` | `trade \| счёт \| дата \| тип \| вид комиссии \| номер сделки` |
| иначе | `hash \| счёт \| дата \| тип \| инструмент \| кол-во \| цена \| сумма \| вид комиссии \| индекс` |

`instrument_ref` — ISIN либо тикер, но не `instrument_id`: идентификатор зависит
от порядка создания справочника, и ключ поехал бы при пересоздании базы.

Индекс считается в пределах **календарного дня**: `assign_natural_keys` ведёт
счётчик по группе `(счёт, дата, тип, инструмент, количество, цена, сумма, вид
комиссии)`. Именно поэтому два отчёта с перекрывающимся периодом дают одни и те
же ключи для одних и тех же операций, а две части одной заявки получают разные.

Числа нормализуются (`Decimal.normalize`) перед хешированием: `10` и `10.00`
обязаны давать один ключ.

### `raw.py`

* `content_hash(content) -> str` — sha256;
* `store_report(session, content, *, original_filename, raw_dir, source, parser_version, period_start, period_end, note) -> StoredReport`.

Файл кладётся в `raw_dir` под именем `<hash><расширение>`; если запись с таким
хешем уже есть, возвращается существующая с `is_duplicate=True` — импорт
остаётся идемпотентным, а не падает.

### `instruments.py`

`InstrumentResolver(session)`:

* `resolve(ticker, isin, name, currency) -> Instrument | None` — ищет, при
  отсутствии создаёт, при находке дозаполняет пустые поля (заполненные не
  перезаписывает). `None`, если нет ни тикера, ни ISIN — это денежная операция;
* `find(ticker, isin) -> Instrument | None` — только поиск. Нужен `check`:
  проверка ничего не пишет, и бумага, которой нет в базе, — это расхождение, а
  не повод завести запись.

Поиск идёт сначала по ISIN, затем по тикеру: ISIN устойчивее, тикер брокер может
писать по-разному. Внутри импорта работает кеш — один отчёт упоминает бумагу
десятки раз.

`guess_kind(ticker, isin, name)` — грубая догадка по форме кода (`SU…RMFS…`,
`RU000A…` → облигация). Ошибка типа не портит журнал: количества и деньги от
него не зависят, а точный справочник приедет с MOEX на этапе 2.

### `ledger.py`

```python
load_entries(session, account_id, *, upto=None) -> list[Transaction]
build_diff(session, entries, *, source_row_scope=None) -> LedgerDiff
apply_diff(session, diff) -> list[Transaction]
```

**Дифф по ключу** (отчёты брокера, `source_row_scope=None`): запись с
существующим `natural_key` попадает в `unchanged`, остальные — в `new`.

**Декларативный дифф** (`_declarative_diff`, CSV) устроен иначе, и это
существенно. Ключ строки CSV не включает сумму, поэтому правка количества дала
бы тот же ключ и молча не записалась. Алгоритм:

1. выбрать записи журнала с `source_row_id LIKE '<файл>:%'`;
2. отбросить сторнирующие записи и уже сторнированные — остаются «живые» версии,
   по одной на `id` строки;
3. для каждой строки файла: нет живой версии → `new`; есть и содержимое
   совпадает (`_same_content`) → `unchanged`; есть и отличается → пара
   «сторно + новая запись», причём у новой записи к ключу добавляется суффикс
   ревизии `:r<N>`, иначе UNIQUE-индекс отверг бы вторую версию;
4. живая версия, которой нет в файле → сторно без замены.

`_same_content` сравнивает только предметные поля (тип, даты, инструмент,
количество, цена, сумма, валюта, `cost_basis`, `basis_quality`). `recorded_at` и
ссылка на сырьё меняются при каждом импорте и изменением строки не являются.

`_reversal_of(original)` строит сторно: те же реквизиты, противоположные знаки
`amount`, `quantity`, `accrued_int`, `fee`, ключ `reversal:<ключ оригинала>`,
`reverses_id` на оригинал, примечание «сторно записи #N». Оригинал не трогается:
журнал append-only.

`apply_diff` пишет в порядке «сторно → замена → новые» и делает `flush`, но не
`commit`: транзакцией управляет `jobs/`.

### `positions.py`

Только количества, без оценки. Рыночная стоимость не считается — цен на этапе
нет и не должно быть.

```python
positions_on(transactions, on_date=None, *, keep_empty=False) -> dict[int, Position]
cash_balances(transactions, on_date=None) -> dict[str, Decimal]
settled_cash(transactions, currency, on_date=None) -> Decimal
```

`Position` — `instrument_id`, `quantity` и **список лотов**, а не только агрегат:
дата и цена каждой партии нужны для FIFO и для срока владения по ЛДВ, а задним
числом не восстанавливаются.

```python
@dataclass(frozen=True)
class Lot:
    trade_date: date
    quantity: Decimal
    price: Decimal | None        # price сделки либо cost_basis начального остатка
    basis_quality: str | None
    transaction_id: int | None
```

Две даты: позиции считаются по `trade_date`, деньги — по `settlement_date`
(при её отсутствии — по `trade_date`). Именно это различие даёт сходимость
сверки, когда сделка прошла в последний торговый день периода.

Количество меняют только события из `_QUANTITY_EVENTS`: `OPENING_BALANCE`,
`BUY`, `SELL`, `MATURITY`, `CONVERSION`, `SPIN_OFF`, `SPLIT`,
`PARTIAL_REDEMPTION`. Списание идёт по FIFO (`_consume_fifo`) — тем же порядком,
каким потом будет считаться налоговая база; два разных порядка списания в одной
системе держать нельзя. Списание сверх известных лотов не «съедается»:
количество уходит в минус, и это ловит жёсткий инвариант.

### `reconcile.py`

```python
reconcile(transactions, expected, *, as_of=None,
          cash_tolerance=Decimal("0.01"),
          quantity_tolerance=Decimal(0)) -> ReconcileResult
```

Вход — журнал и список `ExpectedBalance` (вид, количество, валюта,
`instrument_id`, подпись). Выход — `ReconcileResult` со свойством `ok` и
кортежем `Discrepancy` (вид, подпись, «в отчёте», «в журнале», `difference`).

Проверяется в обе стороны:

* каждый контрольный остаток отчёта сравнивается с расчётным;
* каждый ненулевой остаток журнала, **не** подтверждённый отчётом, — тоже
  расхождение (`_missing_in_report`). Так выглядит лишняя или задвоенная
  операция, и без этой половины она осталась бы незамеченной;
* бумага из отчёта, которой нет в справочнике, даёт расхождение, а не пропуск.

### `invariants.py`

Возвращают списки `Violation(check, message, level)`. Падает вызывающая сторона.

| Функция | Что ловит |
|---|---|
| `check_no_negative_positions` | отрицательное количество на любую дату; чаще всего это незаполненные начальные остатки |
| `check_reversals_net_to_zero` | сторно и оригинал не дают ноль по сумме или по количеству; сторно ссылается на запись вне выборки |
| `check_natural_keys_unique` | дубль ключа в выборке (в БД это UNIQUE-индекс; проверка нужна для выборок без БД) |
| `check_currency_filled` | пустая валюта или не три буквы — иначе рубли сложатся с долларами |
| `check_all` | всё перечисленное разом |

`external_flow_by_day(transactions) -> dict[date, Decimal]` — сумма внешних
событий по дням, по `settlement_date`. Классификация берётся из типа события, без
исключений и ручных поправок. Понадобится на этапе 4 для TWR и XIRR; здесь она
уже посчитана правильно, чтобы позже не переизобретать.

Инварианты, требующие цен («сумма позиций по ценам равна `nav`»), на этапе 1
отсутствуют осознанно: шумная жёсткая проверка хуже отсутствующей.

### `inbox.py`

`from_unparsed(rows, source)` и `from_discrepancies(discrepancies, source, as_of)`
превращают результаты парсера и сверки в единый `InboxItem(kind, title, detail,
source, as_of)`.

---

## 8. `jobs/` — оркестрация

Единственный слой, где открываются транзакции.

### `import_broker.py`

```python
import_broker_report(path, *, account_code=None, dry_run=False,
                     force=False, settings=None) -> ImportResult
```

Последовательность вызовов:

| Шаг | Вызов | Результат |
|---|---|---|
| 1 | `path.read_bytes()` | `bytes` |
| 2 | `mapping_v1.parse(content)` | `ParsedReport` |
| 3 | `session_scope()` | открыта транзакция |
| 4 | `_resolve_account(session, account_code or report.account_code)` | `Account` (заводится, если кода нет в базе — A-09) |
| 5 | `raw.store_report(...)` | `StoredReport`; пропускается при `--dry-run` |
| 6 | `_to_entries(...)`: `InstrumentResolver.resolve` по каждой операции → `events.assign_natural_keys` | `list[LedgerEntry]` |
| 7 | `ledger.build_diff(session, entries)` | `LedgerDiff` |
| 8 | `ledger.apply_diff(session, diff)` | записи во `flush`, но не в `commit` |
| 9 | `_to_expected(report.balances, resolver, period_end)` | `list[ExpectedBalance]` |
| 10 | `reconcile(load_entries(session, account.id), expected, as_of=period_end, …)` | `ReconcileResult` |
| — | **граница `--dry-run`** | |
| 11 | `committed = not dry_run and (result.ok or force)`; иначе `session.rollback()` | |
| 12 | при `force` и несошедшейся сверке — причина в `raw_reports.note` | |

Сверка выполняется **после** записи в незакоммиченной транзакции: проверяется
состояние журнала, которое получится, а не прогноз этого состояния. Реквизиты
(`sha256`, `is_duplicate`, код счёта) считываются до отката — после него объекты
сессии просрочены.

`ImportResult` содержит источник, счёт, период, версию парсера, хеш, флаг
дубля, `LedgerDiff`, `ReconcileResult`, нераспознанные строки, число записанных
событий и флаги `dry_run` / `committed`.

### `import_csv.py`

```python
import_csv_file(path, *, dry_run=False, settings=None) -> CsvImportResult
```

`sniff_kind` → чтение и валидация → построение `LedgerEntry` → `build_diff` с
`source_row_scope=<вид файла>` → `apply_diff` → `commit` либо `rollback` при
`--dry-run`.

`source_row_id` записей формируется как `<вид файла>:<id строки>` —
по этому префиксу декларативный дифф находит «свои» записи и не трогает чужие.

### `check.py`

```python
run_check(settings=None) -> CheckResult
```

1. выбрать все `transactions` → `invariants.check_all`;
2. для каждого `raw_reports` с `source='broker'`, по возрастанию `period_end`:
   найти счёт (по первой записи журнала с этим `source_report_id`), прочитать
   файл из `stored_path`, разобрать `mapping_v1.parse`, построить
   `ExpectedBalance` через `InstrumentResolver.find`, сверить
   `reconcile(load_entries(...), …, as_of=period_end)`;
3. собрать `CheckResult(reports, violations, transactions)`.

`CheckResult.ok` ложно, если есть хоть одно нарушение инварианта, расхождение,
нераспознанная строка или пропавший файл сырья. Пропавший файл — отдельный
случай: запись в `raw_reports` есть, файла нет, и это сигнал восстанавливать
резервную копию, а не повод считать отчёт сошедшимся.

Джоба ничего не пишет: `InstrumentResolver.find` вместо `resolve`.

### `inbox.py`

`collect_inbox()` вызывает `run_check()` и превращает результат в
`list[InboxItem]`: пропавшее сырьё, нераспознанные строки, расхождения, нарушения
инвариантов.

---

## 9. Сквозные соглашения

| Соглашение | Формулировка | Где закреплено |
|---|---|---|
| Знаки | `quantity` — изменение позиции, `amount` — изменение остатка | A-05; `positions.py`, `mapping_v1.py` |
| Две даты | позиции по `trade_date`, деньги по `settlement_date` | `positions.py`, `reconcile.py` |
| Комиссии | отдельные события `FEE` по типам; поле `fee` справочное | A-06 |
| Gross | суммы до налога, удержание отдельным `TAX`; не восстанавливается — `None` | A-04 |
| Идемпотентность | `natural_key`, UNIQUE в БД; индекс по календарному дню | A-01 |
| Append-only | исправление = сторно + новая запись | `ledger.py` |
| Нет данных ≠ ноль | `None`, `FormatError`, `UnparsedRow` | `formats.py`, `csv_input.py`, `mapping_v1.py` |
| Транзакции | только в `jobs/` | `tests/test_layering.py` |

---

## 10. Тесты

246 тестов (из них 178 по этапу 1 и вспомогательным скриптам), без сети и без поднятого PostgreSQL. Тестовая БД — SQLite во
временном каталоге, схема создаётся из тех же метаданных.

| Файл | Что проверяет |
|---|---|
| `conftest.py` | фикстуры `database`, `session`, `account`; подмена `DATABASE_URL` и каталогов, сброс кешей настроек |
| `golden.py` | сериализация разбора для эталонов — общая у теста и у скрипта обновления |
| `update_golden.py` | пересоздание эталонов, запускается руками |
| `test_formats.py` | 15 написаний чисел, отказ от догадок, даты, валюты, форматирование |
| `test_money.py` | запрет `float`, разные валюты, `Money * Money`, округление, сумма пустого списка |
| `test_properties.py` | hypothesis: обратимость разбора и форматирования, независимость суммы от порядка, идемпотентность округления |
| `test_broker_html.py` | golden на трёх фикстурах: форма таблиц и результат разбора; пустое нераспознанное; согласованность остатков с операциями; кодировка CP1251; частичное исполнение; комиссии по типам; gross-купон; НКД; отброшенные «Итого» |
| `test_positions.py` | две даты, лоты с ценой и качеством, FIFO, расхождения в обе стороны, допуск в копейку |
| `test_invariants.py` | каждый инвариант на подготовленном нарушении; `external_flow` только по внешним событиям |
| `test_idempotency.py` | сверка сходится; двойной импорт не меняет журнал; перекрытие периодов (2 новых, 8 существующих); частичное исполнение двумя записями; `--dry-run`; отказ без начальных остатков; `--force` с записью причины |
| `test_csv_import.py` | сообщения валидации, дубль `id`, дифф по `id`, сторно при правке и при удалении строки, знак по типу события |
| `test_cli.py` | коды возврата: ноль после каждого месяца, ненулевой при расхождении, `2` при битом CSV |
| `test_layering.py` | направление импортов, чистота `calc/`, транзакции только в `jobs/` |
| `test_tools_anonymize.py` | ни одна выдуманная персональная строка не доживает до результата; `x:num` и `<img src>` очищены; форма, подписи секций и словарь операций сохранены; сдвиг дат сохраняет интервалы и T+1; идемпотентность; один псевдоним на бумагу |
| `test_tools_structure.py` | секции и словарь операций собираются; строки «Итого» не попадают в словарь; объединённые шапки совпадают с данными |

Фикстуры (`tests/fixtures/`): `report_2025-08.html` — основной месяц,
`report_2025-08-16_09-15.html` — перекрывающийся период, `report_2019-03.html` —
ранний формат в CP1251. Все три синтетические; подробности и план замены — в
`tests/fixtures/README.md`.

Команды проверки:

```bash
pytest                              # 246 тестов
ruff check .
mypy --strict src/portfolio/calc
mypy                                # весь пакет
python tests/update_golden.py       # пересоздать эталоны
```

---

## 10а. `tools/` — вспомогательные скрипты

Не часть приложения: в пакет `portfolio` не входят, запускаются напрямую,
импортируют из проекта только `adapters` (разбор HTML и форматов), чтобы не
заводить вторую реализацию декодирования. Подробности — в
[`../tools/README.md`](../tools/README.md).

| Файл | Назначение |
|---|---|
| `_report_grid.py` | разбор отчёта в сетку с разворачиванием `colspan`/`rowspan`; привязка строк к секциям и шапкам; словарь заголовков и маркеров служебных строк |
| `report_structure.py` | инвентарь структуры отчёта (`dump`) и сравнение нескольких отчётов (`compare`) — инструмент шага 0: какие типы операций есть в архиве, а каких нет в отдельных файлах |
| `anonymize_report.py` | обезличивание отчётов для `tests/fixtures/`: личное → псевдонимы, суммы → форма без величины, даты → сдвиг на скрытое число дней, `x:num` и `<img src>` → очистка; структура не меняется и проверяется сравнением до/после |

Ключевые решения обезличивания и их обоснование — в `tools/README.md`; важное
для понимания кода: сдвиг дат вместо обнуления (иначе фикстура не проверяет
сверку на дату), обнуление сумм (иначе деньги уезжают в git), стабильный файл
соответствий (иначе месяцы архива обезличиваются несогласованно и сквозной импорт
распадается).

---

## 11. Ограничения текущего состояния

**Шаг 0 выполнен частично.** Форма одного реального отчёта разобрана
([`BROKER-REPORT-FORMAT.md`](BROKER-REPORT-FORMAT.md)): это выгрузка Excel, где
весь отчёт — одна таблица, секции лежат строками, шапки двухстрочные с
объединёнными ячейками, а инструмент сделки берётся из строки-подзаголовка
группы. Под такую раскладку нужен отдельный `mapping`, и стадия 1 парсера должна
научиться разворачивать `colspan`/`rowspan` — логика уже написана и проверена в
`tools/_report_grid.py`.

Словарь операций по всему архиву не собран: для него нужны остальные отчёты
(`tools/report_structure.py compare`). Пока он не собран:

* фикстуры синтетические — `mapping_v1` описывает гипотезу о форме отчёта;
* допущения A-01, A-02, A-03, A-04, A-07, A-08, A-10 помечены как «требует
  подтверждения»;
* T11 (бэкфилл архива) не начат.

**Что почти наверняка придётся поправить после первого прогона по архиву** —
в порядке вероятности, по опыту из плана этапа: неучтённый тип комиссии;
операции последнего торгового дня месяца из-за T+1; корпоративные действия;
купон нетто там, где ожидался брутто; неполные начальные остатки.

Правки локализованы: формулировки секций и колонок — в `anchors.py`, правила
разбора — в `mapping_v1.py`, предметные решения — в `ASSUMPTIONS.md`. Ранние
отчёты другого формата разбираются добавлением `mapping_v0.py` рядом с v1 и
выбором по дате отчёта — механизм версионирования уже заложен в
`raw_reports.parser_version`.

**Проверено на PostgreSQL не было.** В окружении разработки не запускался
docker-демон, поэтому миграция проверена применением и откатом на SQLite, а
`docker-compose.yml` — только валидацией конфигурации. Типы, специфичные для
PostgreSQL, в схеме не используются, но первый запуск `alembic upgrade head` на
настоящей БД стоит считать частью проверки.

**Чего в коде нет и не должно быть на этом этапе:** обращений к ISS, ЦБ и любым
внешним API; цен и рыночной стоимости; доходностей; облигационной логики; вкладов;
веб-интерфейса. Каталог `web/` создан пустым, `calc/` содержит только `money.py`.
