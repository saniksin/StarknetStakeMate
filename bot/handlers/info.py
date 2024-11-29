from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from data.languages import translate
from parse.parse_info import parse_validator_staking_info
from utils.msg_format import parse_validator_info
from utils.check_valid_addresses import is_valid_starknet_address


class ValidatorState(StatesGroup):
    awaiting_address = State()


# Хендлер команды /get_staker_info
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
        await message.reply(
            translate("operation_cancelled", locale=user_locale), 
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.clear()
        return

    try:
        check = is_valid_starknet_address(message.text)
        if check:
            answer = await parse_validator_staking_info(message.text)
        else:
            await state.clear()
            await message.reply(
                translate("invalid_validator_address", locale=user_locale), 
                parse_mode="HTML"
            )
            return
    except ValueError:
        await message.reply(
            translate("invalid_validator_address", locale=user_locale), 
            parse_mode="HTML"
        )
        return
        
    if answer:
        await message.reply(
            parse_validator_info(answer, user_locale), 
            parse_mode="HTML", 
            reply_markup=types.ReplyKeyboardRemove()
        )
    else:
        await message.reply(
            translate(
                "invalid_validator_address", user_locale), 
                parse_mode="HTML", 
                reply_markup=types.ReplyKeyboardRemove()
        )

    await state.clear()
