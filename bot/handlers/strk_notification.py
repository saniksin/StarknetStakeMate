import json

from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from db_api.database import Users, write_to_db

from data.languages import translate
from bot.handlers.clear_state import finish_operation


# Состояние для ввода порога клейма
class RewardClaimState(StatesGroup):
    waiting_for_threshold = State()


def create_strk_notification_menu(locale: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=translate("set_strk_reward_notification", locale))],
            [KeyboardButton(text=translate("disable_strk_reward_notification", locale))],
            [KeyboardButton(text=translate("show_strk_reward_notification", locale))],
            [KeyboardButton(text=translate("cancel", locale))]
        ],
        resize_keyboard=True
    )


# Хендлер для открытия меню Notification
async def open_strk_notification_menu(message: types.Message, state: FSMContext, user_locale: str):
    notification_menu_kb = create_strk_notification_menu(user_locale)
    await message.reply(
        text=translate("strk_notification", locale=user_locale),
        reply_markup=notification_menu_kb,
        parse_mode="HTML"
    )


# Хендлер для начала ввода порога
async def start_set_threshold(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):

    tracking_data = json.loads(user_object.tracking_data)

    if len(tracking_data['data_pair']) == 0:
        await message.reply(translate("no_addresses_to_parse", user_locale), parse_mode="HTML")
        return

    markup = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=translate("cancel", locale=user_locale))]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.reply(
        translate("enter_claim_threshold", locale=user_locale),
        parse_mode="HTML",
        reply_markup=markup
    )
    await state.set_state(RewardClaimState.waiting_for_threshold)


# Хендлер для обработки введенного порога
async def set_claim_threshold(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    if message.text == translate("cancel", locale=user_locale):
        await finish_operation(
            message, 
            state, 
            user_locale
        )
        return

    try:
        threshold = float(message.text.strip())
        if threshold < 0:
            await finish_operation(
                message, 
                state, 
                user_locale,
                privious_msg=translate("invalid_threshold", locale=user_locale)
            )
            return
    except ValueError:
        await message.reply(
            translate("invalid_input", locale=user_locale),
            parse_mode="HTML"
        )
        return

    user_object.claim_reward_msg = threshold
    await write_to_db(user_object)

    await finish_operation(
        message, 
        state, 
        user_locale,
        privious_msg= translate("threshold_set_success", locale=user_locale).format(threshold),
        cancel_msg=False
    )


async def clear_claim_threshold(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    if user_object.claim_reward_msg == 0:
        await message.reply(
            translate("claim_threshold_is_zero", locale=user_locale),
            parse_mode="HTML"
        )
        return
    
    user_object.claim_reward_msg = 0
    await write_to_db(user_object)

    await finish_operation(
        message, 
        state, 
        user_locale, 
        privious_msg=f"{translate("claim_notification_success_disable", locale=user_locale)}",
        cancel_msg=False
    )
    return


async def show_claim_treshold_info(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    if user_object.claim_reward_msg != 0:
        await finish_operation(
            message, 
            state, 
            user_locale, 
            privious_msg=translate("show_notification_info", locale=user_locale).format(amount=user_object.claim_reward_msg),
            cancel_msg=False
        )
        return
    await message.reply(
        translate("notification_disabled", locale=user_locale),
        parse_mode="HTML"
    )


