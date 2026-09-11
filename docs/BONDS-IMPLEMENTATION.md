# Облигации: что реализовано и как это работает

Технический разбор параллельного трека (`PARALLEL-TRACK.md`): состав файлов,
назначение каждого модуля, как функции вызывают друг друга, какие форматы
данных ходят между слоями и в каких единицах измеряется каждое число.

Документ описывает **реализацию**. Предметные решения — в
[`ASSUMPTIONS.md`](../ASSUMPTIONS.md) (A-12…A-19), внешние эталоны — в
[`BOND-REFERENCES.md`](BOND-REFERENCES.md), красные линии трека — в
[`PARALLEL-TRACK.md`](PARALLEL-TRACK.md).

---

## 1. Что сделано

| Трек из `PARALLEL-TRACK.md` | Состояние | Где |
|---|---|---|
| A1. Снапшоты ISS на диск | код готов, выгрузка за человеком | `scripts/bondlab/snapshots.py`, `cli.py` |
| A2. Эндпоинты, retry, лимит, кеш, пагинация | готово | `scripts/bondlab/iss.py` |
| A3. Разбор находок в допущения | готово | `ASSUMPTIONS.md` A-12…A-19 |
| A4. Эталонный набор, 8–12 выпусков | девять случаев, снапшоты синтетические | `tests/fixtures/iss/` |
| B. `calc/bonds.py` послойно | готово | `src/portfolio/calc/bonds.py` |
| B1. Прототип `verify-yields` | готово, работает с диска | `scripts/bondlab/verify.py` |
| C. Рейтинги | не начат | — |

Новые файлы:

```
src/portfolio/calc/bonds.py          расчётное ядро, постоянный модуль
scripts/bondlab/                     одноразовый код трека (7 файлов + README)
tests/test_bonds.py                  37 тестов ядра, послойно
tests/test_bondlab_iss.py            26 тестов разбора и сверки
tests/make_iss_fixtures.py           генератор синтетических снапшотов
tests/fixtures/iss/                  эталонный набор: 9 выпусков, 2 доски
docs/BOND-REFERENCES.md              таблица внешних эталонов (пустая, ждёт данных)
docs/BONDS-IMPLEMENTATION.md         этот файл
```

Изменённые: `README.md`, `ASSUMPTIONS.md`, `pyproject.toml` (httpx,
`pythonpath = ["src", "scripts"]`), `.gitignore`, `.env.example`,
`tests/test_layering.py` (охрана красной линии), `tests/test_properties.py`
(облигационные свойства из спеки 5.5).

---

## 2. Два контура и почему они разделены

```
   ┌─────────────────────────────────────────────┐
   │ src/portfolio/calc/bonds.py                 │  постоянный код
   │ чистые функции: ни сети, ни БД, ни today()  │  mypy --strict
   │ живёт по правилам спеки 9                   │  переживёт трек
   └───────────────▲─────────────────────────────┘
                   │ импорт только в эту сторону
   ┌───────────────┴─────────────────────────────┐
   │ scripts/bondlab/                            │  одноразовый код
   │ HTTP, файлы, SQLite, печать таблиц          │  будет выброшен
   └─────────────────────────────────────────────┘
```

Разделение не косметическое. Ядро тестируется офлайн и не может испортить
журнал, потому что ничего про него не знает. Это проверяется статически тестом
`tests/test_layering.py::test_bondlab_touches_only_the_calculation_core`: из
проекта `bondlab` разрешено импортировать **только** слой `calc`, а строки
`sqlalchemy` и `portfolio.db` в его исходниках запрещены. Красная линия «запись
в боевую БД» держится структурой, а не обещанием.

Обратное тоже верно: в `bondlab` нет ни одной функции, принимающей портфель.
Фильтровать выгруженную доску по своим позициям нечем.

---

## 3. Сквозной поток данных

### 3.1 Выгрузка (`bondlab pull`, требует сети)

```
cli.pull_board(board, limit, day)
  ├─ snapshots.SnapshotStore.for_day(root/snapshots, day)
  ├─ iss.board_securities(client, board)          GET .../boards/{board}/securities.json
  │    └─ IssClient.get → кеш? → _request (retry, лимит) → JSON
  ├─ store.save(BOARD_SECURITIES, board, payload) → snapshots/{дата}/board-securities/{board}.json
  ├─ iss.iter_secids(payload)                      SECID из блока securities
  └─ по каждому secid:
       ├─ iss.security(client, secid)              GET /securities/{secid}.json
       ├─ iss.bondization(client, secid)           три блока, каждый с пагинацией
       │    └─ IssClient.get_all_pages(path, block, iss.only=block)
       └─ snapshot.save(SECURITIES | BONDIZATION, secid, payload)
```

Уже сохранённые бумаги пропускаются (`snapshot.has`), поэтому прерванная
выгрузка продолжается с места остановки, а не с начала.

### 3.2 Разбор и сверка (`bondlab verify`, только диск)

```
cli.verify_yields(board, day, trade_date, day_count, top)
  ├─ snapshot.load(BOARD_SECURITIES, board)
  ├─ parse.board_quotes(payload)                   securities ⋈ marketdata по SECID
  └─ по каждой строке доски:
       ├─ parse.build_schedule(securities, bondization) ──► BondSchedule
       └─ verify.compare_security(schedule, quote, trade_date)
            ├─ verify.settlement_candidates ──► bonds.accrued_interest (перебор дат)
            ├─ bonds.face_value_at(schedule, settlement)
            ├─ bonds.full_price(clean_pct, face, accrued)
            ├─ bonds.future_cashflows(schedule, settlement)
            ├─ bonds.ytm(flows, price, settlement, day_count=…)
            ├─ bonds.ytp(schedule, price, settlement, offer)   ← если оферта впереди
            ├─ bonds.duration_days(flows, settlement, own_yield)
            └─ _structure_notes + _with_note ──► Comparison
  ├─ verify.summarize(comparisons) ──► [Distribution]   первая таблица
  ├─ verify.outliers(comparisons)  ──► [Comparison]     вторая таблица
  └─ store.save_run(connection, …, rows=[c.to_row()])   SQLite трека
```

Единственная точка, где встречаются рыночные данные и расчёт, —
`compare_security`. Всё, что выше неё, не считает; всё, что ниже, не ходит в
сеть и не пишет на диск.

---

## 4. Расчётное ядро: `src/portfolio/calc/bonds.py`

### 4.1 Типы данных

Все суммы — `Money` (`Decimal` + код валюты, `float` отвергается типом). Все
даты — `datetime.date`. Внутри датаклассов тип даты доступен под псевдонимом
`_Date`, потому что поле `date` перекрывает имя типа в теле класса.

| Тип | Поля | Инварианты и смысл |
|---|---|---|
| `Coupon` | `date`, `period_start`, `value: Money \| None`, `rate_pct: Decimal \| None` | `period_start < date`, иначе `ScheduleError`. `value is None` — купон **неизвестен**, а не равен нулю. Свойства: `is_known`, `period_days` |
| `Amortization` | `date`, `value: Money`, `is_maturity: bool` | частичное погашение номинала; финальное погашение — тоже амортизация, с `is_maturity=True` |
| `Offer` | `date`, `kind: OfferKind \| None`, `price_pct: Decimal \| None` | `kind is None` — тип не распознан источником. `price_pct is None` трактуется как 100% только в `ytp`, в самих данных не подменяется |
| `CashFlow` | `date`, `amount: Money \| None`, `kind: CashFlowKind` | элемент будущего потока; `amount is None` — платёж неизвестен |
| `BondSchedule` | `secid`, `currency`, `initial_face_value`, `maturity_date`, `coupons`, `amortizations`, `offers`, `name` | в `__post_init__`: валюта приводится к верхнему регистру, все платежи проверяются на совпадение валюты с выпуском, купоны/амортизации/оферты сортируются по дате, сумма амортизаций не может превысить номинал. Свойства: `has_unknown_coupons`, метод `offers_after(on, kind=None)` |

Перечисления: `DayCount` (`ACT/365`, `ACT/ACT`, `30/360`), `Compounding`
(`EFFECTIVE`, `SIMPLE`), `OfferKind` (`PUT`, `CALL`), `CashFlowKind`
(`COUPON`, `AMORTIZATION`, `REDEMPTION`).

Константы: `RATE_LOWER = -0.99`, `RATE_UPPER = 10` (брекетинг солвера),
`RATE_PRECISION = 1E-10` (округление результата), `DAYS_IN_YEAR = 365`.

### 4.2 Функции по слоям

Порядок ровно тот, в котором они тестируются, и тот, в котором ошибка ищется
при расхождении с биржей.

**Слой 0 — конвенции.**

```python
year_fraction(start, end, basis=DayCount.ACT_365) -> Decimal
```
Доля года между датами; отрицательна, если `end` раньше `start`. `ACT/ACT`
считает каждый календарный год своей длиной (реализация `_act_act` идёт по
границам лет), `30/360` — по правилу US NASD (`_thirty_360`).

**Слой 1 — номинал.**

```python
face_value_at(schedule, on) -> Money
```
Исходный номинал минус амортизации с датой **не позже** `on` (A-14). На дату
финального погашения возвращает ноль.

**Слой 2 — НКД.**

```python
coupon_period_at(schedule, on) -> Coupon | None      # период, где period_start <= on < date
accrued_interest(schedule, on) -> Money | None
```
НКД линеен по фактическим дням периода и округляется до копейки на одну
облигацию, `ROUND_HALF_UP` (A-15). Три исхода различаются по смыслу:

* число — купон периода известен;
* `None` — купон периода **неизвестен** (флоатер до фиксации ставки);
* ноль — даты вне купонных периодов (до размещения, после погашения,
  дисконтный выпуск) и дата выплаты купона: в этот день купон уже достался
  продавцу.

**Слой 3 — полная цена.**

```python
full_price(clean_pct, face, accrued) -> Money | None
```
`face × clean_pct / 100 + accrued`. Процент считается от номинала **на дату**,
а не от исходного. `accrued is None` даёт `None`: цена бумаги с неизвестным НКД
неизвестна. Валюты `face` и `accrued` обязаны совпадать.

**Слой 4 — будущий поток.**

```python
future_cashflows(schedule, from_date, *, until=None) -> tuple[CashFlow, ...]
has_unknown(flows) -> bool
```
Платежи **строго после** `from_date` (платёж в дату расчётов достаётся
продавцу), отсортированные по дате и виду. `until` усекает поток по дату
включительно — этим пользуется `ytp`; погашение остатка номинала на дату
усечения здесь не добавляется, это забота `ytp`.

**Слой 5 — доходность.**

```python
present_value(flows, settlement, rate, *, day_count, compounding) -> Money | None
ytm(flows, price, settlement, *, day_count, compounding) -> Decimal | None
ytp(schedule, price, settlement, offer, *, day_count, compounding) -> Decimal | None
solve_brent(function, lower, upper, *, tolerance=1e-12, max_iterations=200) -> float | None
```

`ytm` — тонкая обёртка над солвером: собирает пары «годы до платежа, сумма»
(`_as_pairs`), дисконтирует (`_discount`) и ищет корень `PV(y) − price`.
Возвращает `None` в трёх случаях, и все три означают «неизвестно»: в потоке
есть неизвестный платёж (A-12), поток пуст, решения на отрезке нет. Цена в
чужой валюте — не `None`, а `CurrencyMismatch`: это ошибка вызывающего, а не
свойство данных.

`ytp` строит усечённый поток, добавляет выкуп остатка номинала на дату оферты
по `price_pct` (по умолчанию 100%) и вызывает тот же `ytm`. Поэтому доходность
к оферте считается и тогда, когда купоны за её горизонтом неизвестны — что и
требует правило 5.4 спеки.

`solve_brent` — метод Брента с брекетингом, ~60 строк без внешних
зависимостей. Требует разных знаков на концах; иначе `None`. Для
`Compounding.SIMPLE` нижняя граница поднимается функцией `_lower_bound`:
множитель `1 + y·t` обращается в ноль на `y = −1/t`, и левее самого дальнего
платежа функция не определена.

**Слой 6 — дюрация и спреды.**

```python
duration(flows, settlement, rate, *, day_count) -> Decimal | None          # Маколея, годы
duration_days(flows, settlement, rate, *, day_count) -> int | None         # те же, в днях
modified_duration(flows, settlement, rate, *, day_count) -> Decimal | None # Маколея / (1 + y)
current_yield(schedule, on, price, *, horizon_days=365) -> Decimal | None
g_spread(bond_yield, curve, years) -> Decimal | None
basis_points(value) -> Decimal | None
```

`duration_days` существует потому, что доска ISS печатает дюрацию в днях, и
сравнивать надо в тех же единицах. `current_yield` не выбирает за вызывающего
между чистой и полной ценой — обе конвенции встречаются, цена передаётся явно.
`g_spread` линейно интерполирует кривую и **не экстраполирует**: за пределами
кривой возвращает `None`.

### 4.3 Где кончается `Decimal` и начинается `float`

Деньги, номиналы, цены и НКД — всегда `Decimal`: этого требует спека 2.
Дисконтирование — `float`: возведение в дробную степень в `Decimal` не
определено, а точности double хватает с запасом на порог сравнения в 1 б.п.
Граница проходит по `_as_pairs` (вход солвера) и по `Decimal(repr(root))` на
выходе. Результат квантуется до `1E-10`, то есть до 10⁻⁶ базисного пункта.

---

## 5. Трек A: `scripts/bondlab/`

### 5.1 `iss.py` — клиент

| Что | Как |
|---|---|
| Базовый URL | `https://iss.moex.com/iss`, к пути добавляется `.json` |
| Общие параметры | `iss.meta=off` в каждом запросе (`DEFAULT_PARAMS`) |
| Ограничение частоты | `RateLimiter`, по умолчанию 5 запросов в секунду, интервал выдерживается по `time.monotonic` |
| Повторы | 4 повтора, пауза 1→2→4→8 с, на таймаут, сетевую ошибку, 5xx и 429; после исчерпания — `IssError` |
| Кеш | файл `{sha1(url+сортированные параметры)}.json` в `cache_dir`; попадание в кеш означает ноль сетевых запросов |
| Пагинация | `get_all_pages(path, block)` крутит `start` шагом в размер страницы, пока страница полная (`PAGE_SIZE = 100`) |
| Жизненный цикл | контекстный менеджер `with IssClient() as client`; вне `with` — `IssError` |

Функции-эндпоинты (`PARALLEL-TRACK.md`, A2):

| Функция | Путь ISS |
|---|---|
| `boards(client)` | `/engines/stock/markets/bonds/boards` |
| `board_securities(client, board)` | `/engines/stock/markets/bonds/boards/{board}/securities` |
| `security(client, secid)` | `/securities/{secid}` |
| `bondization(client, secid)` | `/statistics/engines/stock/markets/bonds/bondization/{secid}`, три блока по очереди с `iss.only` |
| `history(client, board, secid)` | `/history/.../boards/{board}/securities/{secid}`, обязательно с пагинацией |
| `iter_secids(payload)` | не запрос: коды бумаг из блока `securities` в порядке ответа |

### 5.2 `snapshots.py` — снапшоты на диске

```
{root}/{дата}/boards/boards.json
{root}/{дата}/board-securities/{board}.json
{root}/{дата}/securities/{secid}.json
{root}/{дата}/bondization/{secid}.json
{root}/{дата}/history/{secid}.json      ← зарезервировано: `iss.history` есть, команды выгрузки пока нет
```

`SnapshotStore(root, day)` — каталог одной даты. `for_day(root, day=None)`
берёт указанную дату, `latest(root)` — последнюю по имени каталога. Методы:
`save`, `load`, `has`, `names`, `path`, свойство `directory`.

Отсутствующий файл — исключение `SnapshotMissing`, а не пустой результат и не
поход в сеть. Разбор из сети не подхватывается никогда: сначала выгрузка,
потом разбор.

### 5.3 `parse.py` — форма ISS и построение графика

**Формат блоков ISS.** Ответ — словарь блоков, каждый блок — колонки отдельно,
данные отдельно:

```json
{"coupons": {"columns": ["isin", "coupondate", "value", "valueprc"],
             "data": [["RU000A0GN9A7", "2021-08-11", 34.41, 6.9]]}}
```

`rows(payload, block)` превращает это в список словарей. `description(payload)`
разбирает блок `description`, который устроен иначе — парами «имя поля →
значение» в строках:

```json
{"description": {"columns": ["name", "title", "value", "type", "sort_order", "is_hidden", "precision"],
                 "data": [["SECID", "Код инструмента", "SU46020RMFS2", "string", 1, 0, null]]}}
```

**Колонки, которые читаются.**

| Блок | Колонки | Во что превращаются |
|---|---|---|
| `description` | `SECID`, `ISIN`, `SHORTNAME`/`NAME`, `MATDATE`, `INITIALFACEVALUE`, `FACEVALUE`, `FACEUNIT` | идентификаторы, дата погашения, исходный номинал, валюта номинала |
| `boards` | `currencyid` | валюта расчётов — читается отдельно от валюты номинала (A-18) |
| `coupons` | `coupondate`, `startdate`, `value`, `valueprc` | `Coupon` |
| `amortizations` | `amortdate`, `value` | `Amortization`, `is_maturity` ставится по совпадению с `MATDATE` |
| `offers` | `offerdate`, `offertype`, `price` | `Offer` |
| доска, `securities` | `SECID`, `PREVWAPRICE`, `YIELDATPREVWAPRICE`, `ACCRUEDINT`, `FACEVALUE`, `FACEUNIT`, `MATDATE`, `OFFERDATE` | строка котировки |
| доска, `marketdata` | `LAST`, `YIELD`, `DURATION`, `WAPRICE`, `YIELDATWAPRICE` | та же строка после `board_quotes` |

**Примитивы разбора.** `parse_decimal` не превращает `None` и пустую строку в
ноль; `float` переводится через `repr`, чтобы не тащить двоичную погрешность в
`Decimal`. `parse_date` считает пустую строку и `0000-00-00` отсутствием даты.
`currency_of` приводит рублёвый код ISS `SUR` к ISO-4217 `RUB`.

**Главное правило разбора** (A-12, A-13). Купон считается известным, если
`value` непустое и либо положительно, либо равно нулю при явно нулевом
`valueprc`:

| `value` | `valueprc` | Результат | Почему |
|---|---|---|---|
| `35.4` | `7.1` | известен | обычный купон |
| `null` | `null` | `None` | флоатер до фиксации ставки |
| `0` | `null` | `None` | плейсхолдер: нулевая сумма без ставки |
| `0` | `0` | известен, ноль | настоящий нулевой купон: ставка указана |

**`build_schedule(security_payload, bondization_payload) -> BondSchedule`**
собирает всё вместе. Номинал берётся исходный (`INITIALFACEVALUE`), текущий
восстанавливается амортизациями — иначе `face_value_at` не работал бы на даты
в прошлом. Если блок `amortizations` пуст, финальное погашение конструируется
из `MATDATE` и номинала: погашение существует всегда, даже когда ISS не
отдаёт его строкой. Противоречивый график даёт `ParseError` с кодом бумаги в
тексте.

**`board_quotes(payload)`** склеивает `securities` и `marketdata` по `SECID` в
одну строку: доходность и цена, к которой она относится, лежат в разных
блоках, а сравнивать их нужно вместе.

### 5.4 `verify.py` — сверка

**Пары «цена → доходность»** (`PRICE_SOURCES`). Сравнение допустимо только
внутри пары; доходность по одной цене против цены из другой строки
методологически бессмысленна:

| Цена | Доходность биржи |
|---|---|
| `PREVWAPRICE` | `YIELDATPREVWAPRICE` |
| `WAPRICE` | `YIELDATWAPRICE` |
| `LAST` | `YIELD` |

**Подбор даты расчётов** (A-19). `settlement_candidates(schedule, trade_date,
iss_accrued)` перебирает даты `trade_date … trade_date + 4` и возвращает те, на
которых собственный НКД совпал с `ACCRUEDINT` с точностью до копейки. Дальше:

| Совпадений | Что значит | Что делает `compare_security` |
|---|---|---|
| ровно одно | график платежей подтверждён | считает по этой дате, `settlement` в результате заполнен |
| ноль | ошибка в графике уже найдена | считает по `trade_date + 1` и помечает «НКД не сошёлся» |
| несколько | НКД нулевой и ничего не различает (дисконтная бумага, дата купона) | считает по `trade_date + 1` и помечает «дата расчётов не определяется» |

**`compare_security(schedule, quote, trade_date, *, day_count, fallback_offset=1)
-> list[Comparison]`** — по одной `Comparison` на доступную пару цен. Внутри:
номинал на дату → НКД → полная цена → поток → YTM (или `None` при неизвестных
купонах) → YTP к ближайшей оферте → дюрация в днях → пометки.

**`Comparison`** — результат сверки одной бумаги по одному источнику цены:
`secid`, `price_source`, `price`, `own_yield`, `iss_yield`, `own_nkd`,
`iss_nkd`, `settlement`, `own_duration`, `iss_duration`, `own_yield_to_offer`,
`note`. Вычисляемые свойства: `diff_bp`, `diff_to_offer_bp`, `is_outlier`
(расхождение больше `THRESHOLD_BP = 5` **или** сравнение не состоялось).
`to_row()` переводит в `CheckRow` для SQLite.

**Пометки.** Короткие, потому что колонку причин читают по всему списку сразу:
`НКД не сошёлся: ошибка в графике`, `НКД нулевой: дата расчётов не
определяется`, `купоны неизвестны, только YTP (A-12)`, `оферта раньше
погашения`, `амортизация`, `номинал в USD`, `биржа не отдала доходность`,
`решения нет`, `сходится к оферте`. Последняя — самая полезная: она означает,
что расхождение объясняется тем, что биржа считает к оферте, а не ошибкой
расчёта, и ставится, когда YTP попадает в порог при выпавшей из порога YTM.

**`summarize(comparisons) -> list[Distribution]`** — по источнику цены:
`compared`, `skipped`, `median_bp`, `p90_bp`, `max_bp`, `within_threshold`.
Квантили считаются линейной интерполяцией по отсортированным абсолютным
расхождениям. `outliers(comparisons)` сортирует выбросы по убыванию
расхождения, несравнённые идут первыми. `mean_bp` даёт знаковое среднее:
систематический сдвиг указывает на конвенцию, а не на отдельные бумаги.

### 5.5 `store.py` — SQLite трека

Отдельная база, никакого пересечения с PostgreSQL проекта. Числа хранятся
строками: в SQLite нет `NUMERIC`, а превращать `Decimal` в `float` ради
хранения — терять то, ради чего он выбран.

```sql
CREATE TABLE verify_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot TEXT, board TEXT, settlement TEXT, day_count TEXT,
    checked INTEGER, outliers INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE yield_checks (
    run_id INTEGER REFERENCES verify_runs(id),
    secid TEXT, price_source TEXT, price TEXT,
    own_yield TEXT, iss_yield TEXT, diff_bp TEXT,
    own_nkd TEXT, iss_nkd TEXT, nkd_settled TEXT,
    own_duration INTEGER, iss_duration INTEGER,
    note TEXT
);
```

`connect(path)` — контекстный менеджер, создаёт схему и коммитит на выходе.
`save_run(...) -> int` пишет прогон и все строки, возвращает `id` прогона.
Прогоны накапливаются: сравнение двух прогонов до и после правки конвенции —
основной способ увидеть, что изменилось.

### 5.6 `cli.py` — команды

| Команда | Сеть | Что делает | Коды возврата |
|---|---|---|---|
| `boards` | да | снимает перечень досок и печатает его | 2 — ISS недоступен |
| `pull BOARD [--limit N]` | да | доска, затем описание и `bondization` по каждой бумаге | 2 — ISS недоступен |
| `schedule SECID` | нет | печатает график платежей из снапшота: купоны с пометкой «неизвестен», амортизации, оферты с типом | — |
| `verify --board BOARD` | нет | распределение расхождений, выбросы, запись прогона | 1 — есть выбросы |

Общие опции: `--day` (дата снапшота, по умолчанию последний), у `verify` ещё
`--trade-date`, `--day-count` (`ACT/365`, `ACT/ACT`, `30/360`) и `--top`.
Каталог хранилища задаётся переменной `BONDLAB_DIR`, по умолчанию
`data/bondlab`.

Недоступный ISS печатается строкой, а не трассировкой: снапшоты на диске от
этого не страдают, ситуация штатная. Текущая дата берётся здесь и только здесь
— в `calc/` её нет по построению, и это проверяется тестом слоёв.

---

## 6. Единицы измерения

Половина расхождений с внешними источниками — это перепутанные единицы, поэтому
таблица явная.

| Величина | Единица | Где |
|---|---|---|
| Цена бумаги | проценты от номинала **на дату** | `clean_pct`, поля `PREVWAPRICE`, `LAST` |
| Номинал, купон, НКД, полная цена | деньги в валюте номинала | `Money` |
| Доходность в ядре | доли единицы (`0.1535` = 15,35%) | `ytm`, `ytp`, `current_yield`, `g_spread` |
| Доходность на доске ISS | проценты | делится на 100 в `compare_security` |
| Расхождение доходностей | базисные пункты | `basis_points`, колонка `Δ б.п.` |
| Дюрация | годы (`duration`) и дни (`duration_days`) | ISS печатает дни |
| Округление НКД | копейка, `ROUND_HALF_UP` | `accrued_interest` |
| Порог сравнения | 5 б.п. по доходности, копейка по НКД | `THRESHOLD_BP`, `KOPECK` |

---

## 7. Тесты

213 тестов, ни одного сетевого запроса и ни одного обращения к БД.

| Файл | Что проверяет |
|---|---|
| `tests/test_bonds.py` | ядро послойно: конвенции дат, номинал с амортизацией, НКД (включая `None` у флоатера и ноль вне периодов), полная цена, строгость границы потока, усечение на оферте, YTM и её отсутствие, YTP, дюрация с эталоном 1,909 года, G-спред без экстраполяции, отказ солвера |
| `tests/test_bondlab_iss.py` | разбор снапшотов: плейсхолдер против нуля, амортизация меняет номинал и купон, валюта номинала USD и CNY, валюта расчётов из другого блока, дисконтный выпуск, нераспознанный тип оферты; подбор даты расчётов; сквозной прогон сверки; пагинация и кеш клиента без сети |
| `tests/test_properties.py` | свойства из спеки 5.5: YTM бумаги по номиналу равна купонной ставке, цена монотонно убывает по доходности, сумма купонов не зависит от порядка обхода |
| `tests/test_layering.py` | `bondlab` видит только слой `calc`, не знает про ORM и боевую БД |

Отдельный тест `test_leap_day_shifts_par_yield_slightly` фиксирует, что через
високосный год равенство «доходность по номиналу = купонная ставка» выполняется
лишь приближённо: при ACT/365 период в 366 дней перестаёт быть ровно годом,
расхождение около 1 б.п. Это конвенция, а не дефект, и знать о ней надо до
разбора выбросов, а не во время.

**Чего эти тесты не проверяют.** Конвенций Мосбиржи. Эталонные поля
синтетических снапшотов (`ACCRUEDINT`, `YIELDATPREVWAPRICE`, `DURATION`)
посчитаны тем же ядром, которое их потом сверяет, поэтому совпадение проверяет
проводку данных, а не правильность базы начисления дней и округлений.
Закрывается это только заполнением `BOND-REFERENCES.md` по реальной доске и
калькулятору Мосбиржи.

Синтетический набор пересоздаётся командой `python tests/make_iss_fixtures.py`
и покрывает девять структурных случаев: фиксированный купон, амортизация,
флоатер RUONIA, флоатер на ключевую ставку, put-оферта, оферта с амортизацией,
замещающая USD, юаневая, дисконтная. Генератор умеет продлевать неизвестные
купоны последним известным — так доходность флоатера считает биржа, и фикстура
воспроизводит именно то расхождение с нашим правилом, которое ожидается.

---

## 8. Допущения, зафиксированные этой работой

| ID | О чём | Статус |
|---|---|---|
| A-12 | неизвестный купон не равен нулю; YTM к погашению не показывается, только YTP | подтверждено |
| A-13 | отличение плейсхолдера от настоящего нуля при разборе `bondization` | требует подтверждения |
| A-14 | номинал на дату: амортизация вычитается с даты включительно | требует подтверждения |
| A-15 | НКД линеен по фактическим дням, округляется до копейки, вне периодов ноль | требует подтверждения |
| A-16 | база ACT/365 и эффективная ставка; Брент на `[-0.99, 10]`, иначе `None` | требует подтверждения |
| A-17 | put и call — разные вещи, нераспознанный тип остаётся неизвестным | требует подтверждения |
| A-18 | валюта номинала и валюта расчётов различаются, ядро считает в валюте номинала | требует подтверждения |
| A-19 | дата расчётов подбирается по НКД, торговый календарь не заводится | подтверждено |

Полные формулировки с датами и списком затронутых модулей — в
[`ASSUMPTIONS.md`](../ASSUMPTIONS.md). Таблица «что сломается, если гипотеза
неверна» — там же, в конце.

---

## 9. Запуск

```bash
pip install -e ".[dev]"
export PYTHONPATH=src:scripts

python -m bondlab boards
python -m bondlab pull TQOB --limit 30
python -m bondlab schedule SU26238RMFS4
python -m bondlab verify --board TQOB

# то же на эталонном наборе, без сети
BONDLAB_DIR=tests/fixtures/iss python -m bondlab verify --board TQCB

pytest
ruff check .
mypy --strict src/portfolio/calc
```

---

## 10. Границы сделанного

Что в периметр **не** входило и не сделано намеренно:

* скринер и любое ранжирование бумаг — запрещено до сверки доходностей;
* любое пересечение с журналом: ни одной функции, принимающей позиции;
* запись в боевую БД;
* трек C (рейтинги);
* кривая ОФЗ: `g_spread` кривую принимает, но строить её пока не из чего.

Что сделано, но ждёт реальных данных:

* выгрузка с ISS: код написан и на недоступный сервис реагирует штатно, но ни
  одного реального ответа через него не прошло;
* эталонные YTM с калькулятора Мосбиржи: таблица заведена и пуста.

До заполнения этой таблицы расчётные доходности — рабочая гипотеза, а не
знание. Принимать по ним решения о покупке нельзя: скринер опаснее дашборда,
дашборд врёт о прошлом, скринер — о будущем, и на нём тратятся деньги.
