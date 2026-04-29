# StarknetStakeMate

A Starknet staking companion: Telegram bot + REST API + Telegram Mini App / local dashboard. Staking **V2 (v3.0.0)** aware — tracks STRK and BTC-wrapper (WBTC / LBTC / tBTC / SolvBTC) delegation pools, attestation health, and reward thresholds.

---

## Features

| Feature | Bot | Mini App | Notes |
| --- | --- | --- | --- |
| Validator view (STRK + BTC pools, attestation) | ✅ | ✅ | `staker_info_v1` + `staker_pool_info` |
| Delegator view (any pool, token-aware) | ✅ | ✅ | `pool_member_info_v1`; shows the staker's status banner too |
| Total stake hero (own + delegations, USD-aggregated) | — | ✅ | per-token breakdown line below |
| Extended attestation status (block-level) | ✅ | ✅ | waiting state shows `current_block` / `target_block` / sign window / time left; every state appends a "next epoch in N blocks (~M min)" tail |
| Reward-threshold notifications (single-mode USD ⊻ token) | ✅ | ✅ | background notifier, wall-clock-aligned |
| Missed-attestation alerts | ✅ | ✅ | `get_last_epoch_attestation_done`, per-minute scan |
| Per-epoch operator-balance alert | ✅ | n/a | fires once at the start of every new epoch with the right transition message (low / recovered); `was_below` flag persisted per-staker |
| Auto-split long Telegram digests | ✅ | n/a | `/get_full_info` / `/get_reward_info` chunk on card boundaries to stay under Telegram's 4096-char cap |
| Tap-to-copy addresses (with HapticFeedback) | — | ✅ | every staker / delegator / pool address |
| Confirm-before-delete (Yes/No on remove flows) | ✅ | ✅ | bot uses FSM; Mini App uses native `showConfirm` |
| Reject duplicate validator/delegator add | ✅ | n/a | bot returns "already in your tracking list" |
| Multi-language UI with CLDR plural rules | ✅ | fallback en | 8 locales (EN / RU / UA / DE / ES / KO / PL / ZH); `services/i18n_plural.py` server-side, `Intl.PluralRules` in Mini App |
| REST API (`/api/v1/*`) | — | ✅ | FastAPI + OpenAPI `/docs` |
| Telegram WebApp HMAC auth | — | ✅ | standard `initData` validation |
| Local dashboard mode | — | ✅ | no HMAC, uses `?tg_id=` |
| Asset cache-busting (`?v=<mtime>`) | — | ✅ | survives Telegram WebView's aggressive cache |

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

### Production deployment (Docker + Caddy + DuckDNS)

The repo ships with a 3-service `docker-compose.yml` (`bot` + `api` + `caddy`) and a `Caddyfile` that auto-issues Let's Encrypt certs for any hostname you put in the `DOMAIN` env var. Tested live on a Contabo VPS (Ubuntu 22.04) with a free DuckDNS subdomain.

```bash
# .env (only the prod-specific bits — bot vars same as local)
DOMAIN=yourname.duckdns.org
API_AUTH_MODE=telegram

docker compose up -d --build
```

Caddy listens on 80/443 (and 443/udp for HTTP/3) and reverse-proxies to the `api` container on the internal Docker network — the API itself is **not** published to the host. SQLite + logs live in named volumes (`stakemate-data`, `stakemate-logs`) and survive `docker compose down`. Cert cache lives in `caddy-data` (don't lose it — Let's Encrypt rate-limits at 50 issuances per domain per week).

Resource fences: each container caps at 1 CPU + 1 GB RAM. The bot needs the full gigabyte because it warms up starknet-py `Contract` objects across multiprocessing workers — 512 MB tripped the OOM killer (exit 137) and looped restarts every ~25 s.

### Mini App entry point

The reliable entry on every Telegram client is the **Menu Button** (the blue button left of the message field), configured via `@BotFather → Bot Settings → Configure Mini App → Menu Button URL`. ReplyKeyboard `web_app` buttons are buggy on Telegram Desktop (initData isn't passed) — we don't ship those.

The bare `/` URL is served by FastAPI directly and on the fly rewrites `/app/style.css` and `/app/app.js` references with `?v=<mtime>` cache-busters, so a redeploy invalidates Telegram's WebView asset cache automatically.

### Dev tools

```bash
uv sync --extra dev
uv run pytest                 # 153 tests covering DTO / formatting / plurals /
                              # per-epoch alerts / message-split / locale parity
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
       │  │                     EpochTimeline   │
       │  ├─ staking_service.py contracts V2    │
       │  ├─ attestation_service.py missed eps  │
       │  │                     + block window  │
       │  ├─ token_service.py   decimals/symbol │
       │  ├─ tracking_service.py user digest    │
       │  ├─ formatting.py      telegram HTML   │
       │  ├─ i18n_plural.py     CLDR plurals    │
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
├── locales/               # 8 JSON locale bundles, CLDR-plural-aware
│                          #   en/de/es: 319 keys (one + other forms)
│                          #   ru/ua/pl: 326 keys (one + few + many)
│                          #   ko/zh:    312 keys (other only)
├── parse/                 # Legacy shim — forwards to services.*
├── services/              # ✨ new clean domain layer
├── smart_contracts_abi/   # l2_staking, l2_pool, l2_attestation ABIs (live)
├── tasks/                 # Background loops (queue processor, notifier)
├── utils/                 # Caching, rate-limit, filters, logging
├── webapp/                # Mini App / local dashboard (vanilla HTML/JS)
├── Dockerfile             # uv-based slim image, single image runs both bot + api
├── docker-compose.yml     # bot + api + caddy (HTTPS reverse proxy)
├── Caddyfile              # auto Let's Encrypt for ${DOMAIN}
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

## Breaking changes from v1

- `requirements.txt` → `pyproject.toml` (managed by `uv`)
- `starknet-py` 0.24.3 → **0.30.0** (RPC 0.10 ready)
- `aiogram` 3.15 → **3.27**
- Contract ABIs replaced with live v3.0.0 snapshots
- New module layout: `services/`, `api/`, `webapp/`
- `parse/parse_info.py` kept as a thin compatibility shim — new code imports from `services.staking_service`
- Locale bundles split countable nouns into CLDR plural variants (`_one` / `_few` / `_many` / `_other`); render through `services.i18n_plural.t_n(...)` instead of `translate(...)` whenever a number is interpolated next to a noun. The legacy `(s)` suffix hack is gone.
- Operator-balance state in `notification_config` migrated from the flip-triggered `_operator_balance_state` to the per-epoch `_operator_balance_was_below`; `db_api/models.Users.get_notification_config` auto-converts existing rows on read.
