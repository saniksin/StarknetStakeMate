from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from data.languages import translate
from parse.parse_info import parse_validator_staking_info
from utils.msg_format import parse_validator_info
from utils.check_valid_addresses import is_valid_starknet_address
from bot.handlers.clear_state import finish_operation
from utils.queue_manager import queue_manager
import logging

logger = logging.getLogger(__name__)


class ValidatorState(StatesGroup):
    awaiting_address = State()


async def process_validator_info(user_id: int, task_data: dict):
    """
    Обработчик для информации о валидаторе
    """
    message = task_data['message']
    state = task_data['state']
    user_locale = task_data['user_locale']

    try:
        check = is_valid_starknet_address(message.text)
        if check:
            answer = await parse_validator_staking_info(message.text)
        else:
            await finish_operation(
                message, state, user_locale, privious_msg=f"{translate("invalid_validator_address", user_locale)}"
            )
            return
    except ValueError:
        await finish_operation(
            message, state, user_locale, privious_msg=f"{translate("invalid_validator_address", user_locale)}"
        )
        return
        
    if answer:
        await finish_operation(
            message, 
            state, 
            user_locale, 
            privious_msg=f"{parse_validator_info(answer, user_locale)}",
            cancel_msg=False
        )
    else:
        await finish_operation(
            message, 
            state, 
            user_locale, 
            privious_msg=f"{translate("invalid_validator_address", user_locale)}",
            cancel_msg=False
        )


# Хендлер команды /get_validator_info
async def get_validator_info(message: types.Message, state: FSMContext, user_locale: str):
    markup = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="0x0475a1ba31db59f0eda3b3b260ad3abb30a2a67983cd51d753fdb4adad92a524")],
            [KeyboardButton(text=translate("cancel", locale=user_locale))]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

    # Запит адреси валідатора
    await message.reply(
        translate("enter_validator_address_2", locale=user_locale),
        reply_markup=markup,
        parse_mode="HTML"
    )
    await state.set_state(ValidatorState.awaiting_address)


async def handle_validator_address(message: types.Message, state: FSMContext, user_locale: str):
    if message.text == translate("cancel", locale=user_locale):
        await finish_operation(
            message, state, user_locale, privious_msg=f"{translate("operation_cancelled", user_locale)}"
        )
        return

    # Добавляем задачу в очередь
    position, success = await queue_manager.add_to_queue(
        "validator_info",
        message.from_user.id,
        {
            "message": message,
            "state": state,
            "user_locale": user_locale
        }
    )

    if not success:
        await message.reply(translate("queue_full", user_locale), parse_mode="HTML")
        return

    # Отправляем сообщение о позиции в очереди
    await message.reply(
        translate("queue_position", user_locale).format(position=position),
        parse_mode="HTML"
    )

    # Запускаем обработку очереди
    await queue_manager.process_queue("validator_info", process_validator_info)
