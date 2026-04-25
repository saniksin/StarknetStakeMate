"""Shared pytest fixtures — no network, no Telegram."""
from __future__ import annotations

import os

# Set BEFORE any project import so data.contracts / data.tg_bot don't abort.
os.environ.setdefault("BOT_TOKEN", "12345:fake-test-token")
os.environ.setdefault("STARKNET_RPC_URL", "https://rpc.starknet.lava.build")
os.environ.setdefault("STARKNET_NETWORK", "mainnet")
os.environ.setdefault("API_AUTH_MODE", "local")

import pytest  # noqa: E402


@pytest.fixture
def strk_token_address() -> str:
    return "0x04718f5a0fc34cc1af16a1cdee98ffb20c31f5cd61d6ab07201858f4287c938d"
