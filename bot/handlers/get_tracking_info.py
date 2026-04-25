"""Handlers for /get_full_info, /get_reward_info, /get_validator_info.

All three just enqueue a request and reply with the queue position; the
heavy lifting happens in the background processors, which now delegate
to :mod:`services.tracking_service` for rendering.
"""
from __future__ import annotations

import json

import aiohttp
from aiogram import types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data.languages import translate
from data.tg_bot import BOT_TOKEN
from db_api.database import db, get_account, write_to_db
from db_api.models import Users
from services.tracking_service import render_user_tracking
from utils.cache import cache, get_cache_key
from utils.logger import logger


TELEGRAM_API_BASE = "https://api.telegram.org/bot"


async def send_message(chat_id: int, text: str) -> None:
    """Send a plain HTML message through the Telegram Bot API.

    Kept as a module helper because the background processors run in a
    separate process and can't share the aiogram Bot instance from main.
    """
    url = f"{TELEGRAM_API_BASE}{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            if response.status != 200:
                logger.error(f"sendMessage failed: {await response.text()}")


async def _position_in_queue(user_id: int) -> int:
    async with AsyncSession(db.engine) as session:
        query = select(Users).where(Users.request_queue.isnot(None))
        result = await session.execute(query)
        users = result.scalars().all()
    return next((i + 1 for i, u in enumerate(users) if u.user_id == user_id), 0)


async def _enqueue_simple(message: types.Message, command: str) -> None:
    """Shared path for /get_full_info and /get_reward_info."""
    user = await get_account(str(message.from_user.id))
    if not user:
        return
    if user.request_queue:
        await message.answer(
            translate("request_already_processing", user.user_language), parse_mode="HTML"
        )
        return
    user.request_queue = json.dumps({"command": command})
    await write_to_db(user)
    position = await _position_in_queue(user.user_id)
    await message.answer(
        translate("queue_position", user.user_language, position=position),
        parse_mode="HTML",
    )


async def get_tracking_full_info(message: types.Message) -> None:
    try:
        await _enqueue_simple(message, "full_info")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"get_tracking_full_info failed: {exc}")


async def get_tracking_reward_info(message: types.Message) -> None:
    try:
        await _enqueue_simple(message, "rewards_info")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"get_tracking_reward_info failed: {exc}")


async def get_tracking_validator_info(message: types.Message) -> None:
    try:
        user = await get_account(str(message.from_user.id))
        if not user:
            return
        if user.request_queue:
            await message.answer(
                translate("request_already_processing", user.user_language),
                parse_mode="HTML",
            )
            return
        user.request_queue = json.dumps({"command": "validator_info"})
        await write_to_db(user)
        position = await _position_in_queue(user.user_id)
        await message.answer(
            translate("queue_position", user.user_language, position=position),
            parse_mode="HTML",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"get_tracking_validator_info failed: {exc}")


# ---------------------------------------------------------------------------
# Background processors (invoked by tasks.request_queue worker process).
# ---------------------------------------------------------------------------

async def _emit(user: Users, body: str, *, cache_key: str | None = None) -> None:
    """Cache the rendered body (if requested) and deliver it to the user."""
    if cache_key:
        await cache.set(cache_key, body)
    await send_message(user.user_id, body)


async def process_full_info(user: Users) -> None:
    # Caching disabled here on purpose: this code runs in the
    # ``strk_bot_parsing`` worker process, whose ``cache`` is a per-process
    # dict. ``clear_user_cache`` runs in MainProcess (where add/edit handlers
    # live) and never reaches the worker's dict, so a cached body would mask
    # fresh tracking edits for up to 5 minutes. RPC is fast enough to render
    # on demand; reintroduce caching only with a real cross-process backing
    # store (e.g. SQLite or shared Manager dict passed via initializer).
    try:
        body = await render_user_tracking(user.tracking_data, user.user_language, mode="full")
        await send_message(user.user_id, body)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"process_full_info({user.user_id}): {exc}")
        await send_message(user.user_id, translate("error_processing_request", user.user_language))


async def process_reward_info(user: Users) -> None:
    try:
        body = await render_user_tracking(user.tracking_data, user.user_language, mode="reward")
        await send_message(user.user_id, body)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"process_reward_info({user.user_id}): {exc}")
        await send_message(user.user_id, translate("error_processing_request", user.user_language))
