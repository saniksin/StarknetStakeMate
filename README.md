# StarknetStakeMate

A Starknet staking companion: Telegram bot + REST API + Telegram Mini App / local dashboard. Staking **V2 (v3.0.0)** aware — tracks STRK and BTC-wrapper (WBTC / LBTC / tBTC / SolvBTC) delegation pools, attestation health, and reward thresholds.

---

## Features

| Feature | Bot | Mini App | Notes |
| --- | --- | --- | --- |
| Validator view (STRK + BTC pools, attestation) | ✅ | ✅ | `staker_info_v1` + `staker_pool_info` |
| Delegator view (any pool, token-aware) | ✅ | ✅ | `pool_member_info_v1` |
| Reward-threshold notifications | ✅ | planned | background notifier, hourly |
| Missed-attestation alerts | ✅ | planned | `get_last_epoch_attestation_done` |
| Multi-language UI (8 locales) | ✅ | fallback en | EN / RU / UA hand-translated for new keys |
| REST API (`/api/v1/*`) | — | ✅ | FastAPI + OpenAPI `/docs` |
| Telegram WebApp HMAC auth | — | ✅ | standard `initData` validation |
| Local dashboard mode | — | ✅ | no HMAC, uses `?tg_id=` |
| 8 commands (/start, /help, /add_info, …) | ✅ | — | — |

---

## Quick start

### Prerequisites
- **Python ≥ 3.10** (project targets 3.12; `uv` will fetch it automatically)
- [**uv**](https://docs.astral.sh/uv/) — we no longer use `requirements.txt`; the lockfile lives in `uv.lock`
- A **Starknet mainnet RPC URL**. Public options:
  - `https://rpc.starknet.lava.build` (RPC 0.8.1, still works — emits a deprecation warning)
  - `https://starknet-mainnet.g.alchemy.com/v2/<KEY>` (RPC 0.10+, recommended)
- A **Telegram bot token** from @BotFather

### Install

```bash
git clone https://github.com/saniksin/StarknetStakeMate.git
cd StarknetStakeMate
cp .env.example .env          # then fill BOT_TOKEN / STARKNET_RPC_URL / ADMINS_ID
uv sync                       # creates .venv and installs everything from pyproject.toml
```

### Run

**Bot:**
```bash
uv run stakemate-bot          # or: uv run python main.py
```

**API + Mini App / dashboard:**
```bash
uv run stakemate-api          # or: uv run uvicorn api.app:app --host 127.0.0.1 --port 8000
```
Then open:
- `http://127.0.0.1:8000/docs` — OpenAPI docs
- `http://127.0.0.1:8000/app/?tg_id=<your_id>` — local dashboard (local auth mode)
- `https://<your_public_host>/app/` — Mini App target (set `WebAppInfo(url=…)` in a bot button)

For Mini App production: host `webapp/` behind HTTPS and set `API_AUTH_MODE=telegram` so every API call requires a valid Telegram `initData` HMAC signature.

### Dev tools

```bash
uv sync --extra dev
uv run pytest                 # once tests are in place
uv run ruff check .
uv run ruff format .
uv run mypy services api
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Telegram (aiogram 3.27)          HTTP (FastAPI 0.136)   │
│  ├─ bot/handlers/ (UI/FSM)        └─ api/routers/*       │
│  └─ bot/middlewares/              └─ api/auth.py (HMAC)  │
└────────────────┬──────────────────────┬──────────────────┘
                 │                      │
                 ▼                      ▼
       ┌────────────────────────────────────────┐
       │  services/ (DTO + pure async)          │
       │  ├─ staking_dto.py     ValidatorInfo   │
       │  │                     DelegatorInfo   │
       │  │                     AttestationStatus
       │  ├─ staking_service.py contracts V2    │
       │  ├─ attestation_service.py missed eps  │
       │  ├─ token_service.py   decimals/symbol │
       │  ├─ tracking_service.py user digest    │
       │  ├─ formatting.py      telegram HTML   │
       │  └─ rpc_client.py      retry+backoff   │
       └────────────────┬───────────────────────┘
                        │
      ┌─────────────────┼────────────────────────┐
      ▼                 ▼                        ▼
 ┌──────────┐    ┌──────────────┐     ┌──────────────────┐
 │ starknet │    │ SQLite (aio  │     │ aiocache /       │
 │ Full-node│    │ sqlite+SQLA) │     │ in-memory cache  │
 │  + tenacity│   │ users.db     │     │ (TTL 5min)       │
 └──────────┘    └──────────────┘     └──────────────────┘
```

Key idea: every contract read and every user-visible string originates in `services/`. Handlers and routers are thin; they fetch a DTO and render it. The Mini App and local dashboard talk to the same REST layer.

---

## Directory layout

```
.
├── api/                   # FastAPI application
│   ├── app.py            #   entry point
│   ├── auth.py           #   Telegram WebApp HMAC verification
│   └── routers/          #   /status, /validators, /delegators, /users/me/*
├── bot/                   # Telegram UI (aiogram handlers, middlewares)
├── data/                  # Config: contracts, bot init, locales, paths
├── db_api/                # SQLAlchemy models + DB helpers (SQLite)
├── docs/superpowers/specs # Design documents
├── locales/               # 8 JSON locale bundles (169 keys each)
├── parse/                 # Legacy shim — forwards to services.*
├── services/              # ✨ new clean domain layer
├── smart_contracts_abi/   # l2_staking, l2_pool, l2_attestation ABIs (live)
├── tasks/                 # Background loops (queue processor, notifier)
├── utils/                 # Caching, rate-limit, filters, logging
├── webapp/                # Mini App / local dashboard (vanilla HTML/JS)
└── main.py                # Bot entry point (aiogram polling + mp workers)
```

---

## Commands (Telegram)

| Command | What it does |
| --- | --- |
| `/start` | Main menu |
| `/help` | Command list |
| `/language` | Switch UI language |
| `/add_info` | Add a validator or delegator address |
| `/delete_info` | Remove tracked addresses |
| `/get_full_info` | Validator + delegator digest (rendered service-layer HTML) |
| `/get_reward_info` | Rewards summary |
| `/get_validator_info` | One-off lookup for a staker address |
| `/set_reward_notification` | STRK threshold that triggers an alert |
| `/disable_reward_notification` | Stop alerts |
| `/show_reward_notification` | Show current threshold |
| `/contact_admin` | DM the bot admins |
| `/ban_user`, `/unban_user` | Admins only |

---

## REST API (selected)

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/v1/status` | Protocol + service health |
| GET | `/api/v1/validators/{addr}` | Full validator view (multi-pool + attestation) |
| GET | `/api/v1/delegators/{addr}?pool=…` | Delegator position in one pool |
| GET | `/api/v1/users/me/tracking` | List tracked pairs |
| PUT | `/api/v1/users/me/tracking` | Replace the tracking list |
| GET | `/api/v1/users/me/digest?mode=full|reward` | Pre-rendered HTML digest |
| PUT | `/api/v1/users/me/threshold` | Update STRK reward threshold |
| GET | `/api/v1/users/me/entries` | Typed DTO entries (for JS renderers) |

Every `/users/me/*` endpoint accepts either the `X-Telegram-Init-Data` header (Mini App mode) or a `?tg_id=<id>` query parameter (local dashboard mode), controlled by `API_AUTH_MODE`.

---

## Design documents

- `docs/superpowers/specs/2026-04-24-starknet-stakemate-refactor-design.md` — the full Staking V2 refactor plan with decomposition into P1–P8 phases.

---

## Breaking changes from v1

- `requirements.txt` → `pyproject.toml` (managed by `uv`)
- `starknet-py` 0.24.3 → **0.30.0** (RPC 0.10 ready)
- `aiogram` 3.15 → **3.27**
- Contract ABIs replaced with live v3.0.0 snapshots
- New module layout: `services/`, `api/`, `webapp/`
- `parse/parse_info.py` kept as a thin compatibility shim — new code imports from `services.staking_service`
