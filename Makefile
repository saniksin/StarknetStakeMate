.PHONY: all start help build up down restart logs ps sh-bot sh-api pull rebuild prune dev-bot dev-api test lint

# Default target — `make` with no args does the one-command bootstrap:
# creates .env from the example if missing, builds the image, starts both
# services, and prints the URLs.
all: start

start:          ## one-command bootstrap: .env + build + up + URLs
	@if [ ! -f .env ]; then \
		cp .env.example .env && \
		echo "→ .env created from .env.example — edit BOT_TOKEN / ADMINS_ID before first real use"; \
	fi
	docker compose up -d --build
	@echo ""
	@echo "✅ Stakemate is up"
	@echo "   API   → http://localhost:$${API_PORT:-8000}/docs"
	@echo "   App   → http://localhost:$${API_PORT:-8000}/app/"
	@echo "   Logs  → make logs"
	@echo "   Stop  → make down"

help:           ## show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---- Docker composition ----------------------------------------------------

build:          ## build the stakemate image once, cache-friendly
	docker compose build

up:             ## start bot + api in the background (detached)
	docker compose up -d
	@echo "API   → http://localhost:$${API_PORT:-8000}/docs"
	@echo "App   → http://localhost:$${API_PORT:-8000}/app/"

down:           ## stop and remove both containers (volumes kept)
	docker compose down

restart:        ## restart both services
	docker compose restart

rebuild:        ## wipe images + rebuild + bring back up (keeps data)
	docker compose down
	docker compose build --no-cache
	docker compose up -d

logs:           ## tail logs from both services (Ctrl+C to stop)
	docker compose logs -f --tail=100

ps:             ## show running services and health
	docker compose ps

sh-bot:         ## shell into the bot container
	docker compose exec bot /bin/bash

sh-api:         ## shell into the api container
	docker compose exec api /bin/bash

prune:          ## DANGER — remove containers AND volumes (wipes users.db)
	docker compose down -v

# ---- Local (no Docker) development ----------------------------------------

dev-bot:        ## run the bot locally via uv (no container)
	uv run stakemate-bot

dev-api:        ## run the API locally via uv (no container)
	uv run stakemate-api

test:           ## run the unit test suite
	uv run pytest -q

lint:           ## run ruff
	uv run ruff check .
