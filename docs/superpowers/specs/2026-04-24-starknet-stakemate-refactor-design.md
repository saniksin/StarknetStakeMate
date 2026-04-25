# StarknetStakeMate — Refactor для Staking V2 + подготовка под Mini App

**Дата:** 2026-04-24
**Автор:** Claude Opus 4.7 (сессия с пользователем)
**Статус:** draft
**Scope:** обновление бота под Staking V2 (v3.0.0), улучшение UX, подготовка архитектуры под фронтенд/Mini App

---

## 1. Диагноз — почему бот сломан

Текущий код бьёт в `l2_staking_contract` методы `get_staker_info(staker)` и в пул-контракт `get_pool_member_info(pool_member)`. После апгрейда контракта на **v3.0.0** (Staking V2, активирован в Q3 2025) эти имена удалены из публичного ABI.

Подтверждение через live RPC (`https://rpc.starknet.lava.build`, `starknet_getClassAt` для `0x00ca1702e64c81d9a07b86bd2c540188d92a2c73cf5cc0e508d949015e7e84a7`):

- `version()` → `"3.0.0"`.
- Публичный ABI содержит `staker_info_v1`, `get_staker_info_v1`, `staker_pool_info`, `contract_parameters_v1`, `get_active_tokens`, `get_tokens`, `get_total_stake_for_token`, `get_current_epoch`, `get_epoch_info`.
- `get_staker_info`, `contract_parameters` отсутствуют — любой текущий вызов бота падает с "entrypoint does not exist".

На mainnet сейчас **5 активных staking-токенов**: STRK + четыре BTC-обёртки (WBTC, LBTC, tBTC, SolvBTC). Один валидатор может держать до **5 отдельных пулов**, по одному на каждый токен. Текущая модель бота (одна запись `[staker_address, pool_address]` на пользователя) не отражает эту множественность.

Дополнительные архитектурные изменения V2, которые влияют на UX:
- Эпохи: балансы обновляются только на границе эпохи, текущая и "projected" суммы различаются.
- Attestation-контракт: валидаторы обязаны аттестовать блоки, иначе теряют rewards — полезная метрика для пользователя.
- Unstake window: с 21 дня сокращён до 7 — сообщения бота про "21-day wait" устарели.

## 2. Цели рефакторинга

1. **Восстановить работоспособность** — обновить ABI, имена методов, парсинг DTO.
2. **Поддержать multi-pool-per-staker** — показывать все пулы (STRK + BTC) по одному валидатору.
3. **Улучшить UX** — человекочитаемые суммы (STRK/BTC вместо felt wei), эмодзи, эпохи, aattestation-статус, relative time для `unstake_time`.
4. **Выделить чистый service-layer** — контрактные вызовы и DTO вне Telegram, переиспользуемые и в боте, и в будущем REST API.
5. **Scaffold REST API** — FastAPI поверх service-layer с HMAC-аутентификацией через Telegram WebApp `initData`, готовый к подключению Mini App или отдельного фронта.
6. **Добавить resilience** — retry с backoff на RPC-клиент, health-check, structured logging.
7. **Покрыть критические пути тестами** — service-layer + форматтеры с моками RPC.

## 3. Декомпозиция на итерации (sub-projects)

Общая работа слишком велика для одного плана. Разбиваем на независимые фазы; каждая фаза — отдельный implementation plan, отдельный набор коммитов, отдельный merge-gate.

| Фаза | Что делает | Ценность | Зависимости |
| --- | --- | --- | --- |
| **P1. Contract-layer ремонт** | Обновить ABI, data/contracts.py, parse/parse_info.py под `staker_info_v1` + `staker_pool_info` + `pool_member_info_v1`, поддержать multi-token пулы | Бот перестаёт падать; users снова получают данные | — |
| **P2. Service-layer + DTO** | Выделить `services/staking_service.py` с чистыми Pydantic-моделями. Функции принимают `FullNodeClient`, возвращают DTO. Никакого Telegram | Подготовка к REST API, проще тесты | P1 |
| **P3. UX-рефактор сообщений** | Перевести `utils/msg_format.py` на DTO из P2; улучшить вывод: эмодзи-сетки, inline-кнопки, STRK/BTC-форматирование, relative time, текущая эпоха, pool breakdown | Сообщения становятся понятнее | P2 |
| **P4. Bot-handlers polish** | Унифицировать FSM (общий cancel-pattern), добавить поддержку добавления multi-pool стейкеров, inline-клавиатуры в FullInfo | UX и поддерживаемость | P3 |
| **P5. REST API scaffold** | `api/` с FastAPI: `/api/v1/validators/{addr}`, `/api/v1/delegators/{addr}/pools/{pool}`, `/api/v1/users/{tg_id}/tracking` (read+write), OpenAPI docs, HMAC-проверка Telegram initData | Фронт / Mini App могут подключаться | P2 |
| **P6. Resilience layer** | Retry/backoff (tenacity), structured logging с correlation IDs, `/health` эндпоинт | Надёжность в проде | P2 |
| **P7. Тесты** | pytest с моками RPC: service-layer (happy+edge), форматтеры, API endpoints, FSM smoke | Регрессионная защита | P2, P3, P5 |
| **P8. Mini App / Frontend скелет** | `webapp/` — минимальный React или vanilla HTML+JS с `window.Telegram.WebApp`, fetch к REST API, показ одного validator view | Визуальный прототип | P5 |

### Приоритет в рамках **текущей сессии**

Выполнимо за один заход: **P1 → P2 → P3 → фрагмент P6 (retry) → scaffold P5/P8 (эндпоинт + placeholder)**. P4, расширенный P5, P7, полный P8 — планируются, но остаются в TODO следующим итерациям (реализм объёма).

## 4. Архитектура после рефакторинга

```
┌──────────────────────────────────────────────────────────┐
│  Telegram (aiogram 3)               HTTP (FastAPI)       │
│  ├─ handlers/ (UI/FSM)              └─ api/v1/* (REST)   │
│  └─ middlewares                      └─ WebApp auth      │
└────────────────┬─────────────────────────┬───────────────┘
                 │                         │
                 ▼                         ▼
         ┌────────────────────────────────────────┐
         │  services/ (чистый слой, pydantic DTO) │
         │  ├─ staking_service.py  (validator,    │
         │  │   delegator, pools, tokens, epoch)  │
         │  ├─ tracking_service.py (user ↔ addrs) │
         │  └─ notification_service.py            │
         └────────────────┬───────────────────────┘
                          │
      ┌───────────────────┼─────────────────┐
      ▼                   ▼                 ▼
  ┌────────┐       ┌──────────────┐   ┌──────────┐
  │ starknet│       │ SQLite (aio  │   │  Cache   │
  │ client  │       │ sqlite/SQLA) │   │          │
  │ + retry │       │              │   │          │
  └─────────┘       └──────────────┘   └──────────┘
```

## 5. Решения по P1–P3 (делаем в этой сессии)

### P1. Contract-layer

- **Не держать dummy Account** — для read-only достаточно `FullNodeClient` + `Contract.from_address` / прямых `.call()`. Убираем `0x4321` / `KeyPair(654,321)`.
- **Обновить `smart_contracts_abi/l2_staking_contract.json`** — перезалить live-ABI из v3.0.0 (уже скачан в `/tmp/stakemate/staking_abi_live.json`).
- **Обновить `smart_contracts_abi/l2_pool_contract.json`** — тот же приём, используя адрес одного живого пула (через `staker_pool_info`).
- **Добавить сетевой конфиг** — `STARKNET_NETWORK=mainnet|sepolia` в .env, соответствующие адреса стейкинг-контракта. Mainnet: `0x00ca1702...`, sepolia: `0x03745ab...` (добавим, даже если сейчас не используется).
- **Отрефакторить `data/contracts.py`** в сторону простого реестра: `get_staking_contract_address(network)`, `load_staking_abi()`, `build_full_node_client(rpc_url)`.

### P2. Service-layer DTO

Новая директория `services/` с чистыми async-функциями и Pydantic-моделями:

```python
# services/staking_dto.py
class PoolInfo(BaseModel):
    pool_contract: str         # hex 0x...
    token_address: str
    token_symbol: str | None   # STRK / WBTC / LBTC / tBTC / SolvBTC
    amount_raw: int            # u128
    amount_decimal: Decimal    # приведённая сумма с учётом decimals токена
    
class ValidatorInfo(BaseModel):
    staker_address: str
    reward_address: str
    operational_address: str
    is_unstaking: bool
    unstake_time_utc: datetime | None
    unstake_eta: timedelta | None       # относительно now
    amount_own_raw: int
    amount_own_strk: Decimal
    unclaimed_rewards_own_raw: int
    unclaimed_rewards_own_strk: Decimal
    commission_bps: int | None          # 0..10000
    pools: list[PoolInfo]               # может быть пусто или 1..5 пулов
    current_epoch: int

class DelegatorInfo(BaseModel):
    pool_contract: str
    delegator_address: str
    reward_address: str
    amount_raw: int
    amount_decimal: Decimal
    token_symbol: str | None
    unclaimed_rewards_raw: int
    unclaimed_rewards_decimal: Decimal
    commission_bps: int
    unpool_amount_raw: int
    unpool_time_utc: datetime | None
    unpool_eta: timedelta | None
```

`services/staking_service.py` предоставляет:

- `async def fetch_validator(client, staker_addr) -> ValidatorInfo | None`
- `async def fetch_delegator(client, pool_addr, delegator_addr) -> DelegatorInfo | None`
- `async def fetch_active_tokens(client) -> list[TokenInfo]` — кэшировать
- `async def fetch_current_epoch(client) -> int`

Внутри: вызываем `staker_info_v1` + `staker_pool_info` одновременно (gather) для получения полного multi-pool вида.

### P3. UX-рефактор сообщений

Переписываем `utils/msg_format.py` на чистую функцию `render_validator(info: ValidatorInfo, locale: str) -> str`. Формат (пример):

```
🛡️ Валидатор 0x00ca…84a7
├─ 🏦 Reward address: 0x05a2…f091
├─ ⚙️ Operational: 0x07b2…cd1a
├─ 💰 Own stake: 120 000 STRK
├─ 🎁 Unclaimed own rewards: 450.2 STRK
├─ 🔁 Commission: 5.00%
├─ ⏳ Unstake: не запрошен
├─ 🌐 Текущая эпоха: 9474
└─ 🎱 Пулы (3):
   ├─ STRK Pool 0x01f8… — 2 500 000 STRK
   ├─ WBTC Pool 0x02c9… — 12.5 WBTC
   └─ tBTC Pool 0x04fa… — 3.0 tBTC
```

Локали обновляются (новые ключи: `pools_header`, `token_strk`, `token_btc_wrapper`, `epoch_current`, `unstake_not_requested`, `unstake_eta_format`). Fallback: если перевода нет, берём английский, а не сырой ключ (устраняем баг текущего `translate` fallback на ключ).

## 6. Решения по P5 (scaffold в этой сессии — минимум для подключения фронта)

- **Фреймворк:** FastAPI + uvicorn, отдельный процесс (не вплетаем в bot event-loop).
- **Эндпоинты v1:**
  - `GET /api/v1/status` — health + version + current epoch
  - `GET /api/v1/validators/{address}` — `ValidatorInfo` DTO
  - `GET /api/v1/delegators/{address}?pool={pool_address}` — `DelegatorInfo`
  - `GET /api/v1/users/me/tracking` — список адресов пользователя (требует auth)
  - `POST /api/v1/users/me/tracking` — добавить; `DELETE /api/v1/users/me/tracking/{id}` — удалить
- **Auth для Mini App:** HMAC-SHA256 валидация `initData` от Telegram WebApp (`secret = HMAC(key='WebAppData', msg=BOT_TOKEN)`). Это стандартная схема Telegram WebApp — документирована. FastAPI-dependency `current_user(initData: Header)`.
- **OpenAPI:** автоматически через FastAPI, `/docs`.
- **Пока (скелет):** поднимаем только `/status` и `/validators/{address}`. Остальное — TODO-заглушки с `501 Not Implemented`, чтобы не разрастать объём сессии.

## 7. Решения по P6 (retry/backoff)

- Добавляем зависимость `tenacity`.
- Обёртка `services/rpc_client.py::ResilientClient` декорирует все `.call()` через `@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4))` с предикатом `retry_if_exception_type((ClientError, asyncio.TimeoutError))` — **не ретраим** `InvalidValueException` (это контрактная ошибка типа "staker not exists").
- Логи через loguru с `logger.bind(correlation_id=...)`.

## 8. Что НЕ делаем в этой сессии (в TODO на будущее)

- Полный P4 (FSM унификация, мульти-пул UX в `/add_info`). Пока оставляем старую one-pool модель, но предупреждаем пользователя если у валидатора >1 пула.
- Полный P7 (комплекс тестов). Делаем только минимум — 3–5 ключевых юнит-тестов для service-layer.
- Полноценный фронт. Сделаем только `webapp/index.html` с fetch к `/api/v1/status` и `/api/v1/validators/{addr}` как placeholder, чтобы было видно путь.
- Миграция схемы БД под мульти-пул на один staker. Текущая `tracking_data` структура `{data_pair: [[addr, pool]]}` пока оставлена — мульти-пул добавим позже.
- Events/historical data парсинг. Не трогаем, read-only state достаточно.

## 9. Риски и компромиссы

- **RPC rate-limits на публичных ендпоинтах** — в service-layer кэшируем `fetch_active_tokens` (TTL 1 час), не ходим за токенами на каждый запрос.
- **Token decimals** — BTC-обёртки обычно 8, STRK — 18. Формируем мапу `token_address → decimals` через `IERC20.decimals()` вызов, тоже кэшируем.
- **Старые тестовые данные в БД** — `tracking_data` у пользователей ссылается на старые пулы (актуальные пул-контракты для V2 валидаторов не изменились в адресации, но могли быть закрыты). На `/get_full_info` обрабатываем gracefully: если валидатор больше не существует → сообщаем пользователю, не роняем процесс.
- **Breaking changes в сериализации** — старые кэш-записи от `aiocache` несовместимы с новыми DTO. При релизе очищаем кэш (`SharedCache.clear_all()` при старте).

## 10. Deliverables для этой сессии

1. Обновлённые ABI файлы (`smart_contracts_abi/l2_staking_contract.json`, `l2_pool_contract.json`).
2. Новый `services/` модуль (dto, staking_service, rpc_client с retry).
3. Рефактор `parse/parse_info.py` → тонкая обёртка над `services/staking_service.py` (для обратной совместимости со старыми импортами).
4. Обновлённый `utils/msg_format.py` на DTO.
5. Минимальный `api/` пакет с FastAPI (`/status`, `/validators/{addr}`) и инструкцией по запуску.
6. Заглушка `webapp/index.html` с простым UI.
7. Обновлённый `README.md` с новыми командами, версией и заметкой о V2.
8. Обновлённый `.env_exampl` (корректный typo → `.env.example`) с `STARKNET_NETWORK`, `API_HOST`, `API_PORT`.
9. Папка `tests/` с 3–5 минимальными юнит-тестами для service-layer.
10. Запись в Obsidian (`Projects/claude-global/index.md`) + `decisions.md` о принятых решениях.

## 11. Дополнительные требования (добавлено после первичного design)

Пользователь дополнительно запросил:

### 11.1 Attestation monitoring (пропущенные эпохи)
Starknet V2 валидаторы обязаны аттестовать блоки в каждой эпохе. Пропуск → штраф rewards.
**Интеграция:**
- `AttestationContract = 0x10398fe631af9ab2311840432d507bf7ef4b959ae967f1507928f5afe888a99` (получено через `contract_parameters_v1` live).
- `get_last_epoch_attestation_done(staker)` + `is_attestation_done_in_curr_epoch(staker)` → сравниваем с `get_current_epoch()`.
- DTO: `AttestationStatus { last_epoch: int, current_epoch: int, missed_epochs: int, is_attesting_this_epoch: bool }`.
- Включается в `ValidatorInfo` (поле `attestation: AttestationStatus | None`).

Добавляем в **Phase P1.5** (после ABI, перед service-layer).

### 11.2 Расширенные уведомления (event-based)
Текущая нотификация — только reward threshold. Расширяем notification_service:
- **Missed attestation alert** — если staker не аттестовал текущую эпоху к концу attestation window → пинг.
- **Commission change** — мониторим `CommissionChanged` event (опционально).
- **Stake balance change** — если у пользователя отслеживается валидатор и его `amount_own` резко меняется → пинг.
- **Unstake intent** — если валидатор подал `unstake_intent` → пинг делегатору (защита от потери средств).

**Архитектура:** добавляем `notifications/` пакет с отдельными классами-чекерами (`AttestationChecker`, `RewardThresholdChecker`, ...). Общий `NotificationDispatcher` периодически бежит по всем чекерам для каждого пользователя.

В текущей сессии — добавляем только `AttestationChecker` как пример + инфраструктуру для остальных.

### 11.3 Telegram Mini App — красивый UI
Поднимаем приоритет. Минимальный функционал в этой сессии:
- `webapp/` — SPA (vanilla HTML+CSS+JS, без билд-шага для простоты).
- Страницы: `index.html` (dashboard со списком отслеживаемых), `validator.html?addr=...` (детали), `settings.html` (уведомления).
- Подключение к REST API через `fetch`.
- Auth через Telegram `initData` в header `X-Telegram-Init-Data`.
- Стилистика: Telegram theme variables (`var(--tg-theme-bg-color)`, `--tg-theme-text-color`) для нативного вида.
- Inline-кнопка в боте: "🖥 Open Dashboard" → `WebAppInfo(url=...)`.

### 11.4 Локальный dashboard (standalone)
Тот же `webapp/` поднимается отдельно локально (`python -m http.server` или uvicorn static mount). Отличие — не требует HMAC: используется либо локально только на `127.0.0.1`, либо basic auth через env.

Настройка через `API_AUTH_MODE=telegram|local|both`.

### 11.5 Итого обновлённый scope для текущей сессии
- P1 + P1.5 (ABI + attestation)
- P2 (service-layer с attestation DTO)
- P3 (UX сообщений с missed epochs)
- P5 (FastAPI с эндпоинтами validators / attestation / tracking)
- P6 (retry)
- **NEW:** webapp/ скелет с 2-3 страницами и Telegram theme integration
- **NEW:** AttestationChecker как пример нового notification-типа

## 12. Следующие итерации (после сессии)

- Собрать обратную связь, приоритизировать P4, P7, расширенный P8.
- Полноценный Mini App с React + State management (или SvelteKit).
- Расширить `tracking_data` схему до `{staker: [pools]}` и UI `/add_info` под multi-pool.
- Настроить CI (GitHub Actions) с линтом + тестами.
- Observability: Prometheus exporter + Grafana dashboard.
- Историческая аналитика: epoch-by-epoch rewards graph, attestation uptime %.
- Event listener (Torii / starknet-py event filter) для real-time alert'ов вместо polling.
