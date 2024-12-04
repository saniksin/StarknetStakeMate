from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from data.languages import translate
from db_api.database import Users, get_account, get_account_by_username, write_to_db
from data.models import get_admins
from bot.handlers.clear_state import finish_operation


# Состояние для выбора языка
class UserBlockingState(StatesGroup):
    waiting_ban_info = State()
    confirm_operation = State()


# Хендлер для команды /ban_user
async def start_block_user(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    # Проверяем, является ли пользователь администратором
    admin_list = get_admins()
    if user_object.user_id in admin_list:
        markup = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=translate("cancel", locale=user_locale))]
            ],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await message.reply(
            translate("give_user_id_or_username", locale=user_locale),
            parse_mode="HTML",
            reply_markup=markup
        )
        await state.set_state(UserBlockingState.waiting_ban_info)
    else:
        await message.reply(
            translate("operation_not_allowed", locale=user_locale),
            parse_mode="HTML"
        )


# Хендлер для получения информации о пользователе, которого нужно заблокировать
async def process_ban(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    # Проверка на отмену операции
    if message.text == translate("cancel", locale=user_locale):
        await message.reply(
            translate("operation_cancelled", locale=user_locale),
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.clear()
        return

    user_id_or_username = message.text.strip()

    # Проверка формата ввода
    if "@" in user_id_or_username:
        id = False
        user_id_or_username = user_id_or_username.split('@')[1]
    else:
        try:
            user_id_or_username = int(user_id_or_username)
            id = True
        except ValueError:
            await message.reply(
                translate("incorrect_user_id", locale=user_locale),
                parse_mode="HTML"
            )
            await state.clear()
            return 

    # Попытка найти пользователя по ID или username
    if id:
        user: Users = await get_account(user_id_or_username)
    else:
        user: Users = await get_account_by_username(user_id_or_username)

    # Если пользователь найден, спрашиваем подтверждение
    if user:
        if user.user_is_blocked:
            await message.reply(
                translate("user_already_banned", locale=user_locale),
                parse_mode="HTML"
            )
            await state.clear()
            return

        await state.update_data(target_user_id=user)
        markup = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(text=translate("yes", locale=user_locale)),
                    KeyboardButton(text=translate("no", locale=user_locale))
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await message.reply(
            translate("confirm_ban_user", locale=user_locale).format(user.user_name),
            parse_mode="HTML",
            reply_markup=markup
        )
        await state.set_state(UserBlockingState.confirm_operation)
    else:
        await message.reply(
            translate("user_not_found", locale=user_locale),
            parse_mode="HTML"
        )
        await state.clear()
        return  


# Хендлер для подтверждения блокировки пользователя
async def confirm_ban_user(message: types.Message, state: FSMContext, user_locale: str):
    user_response = message.text.lower()

    # Если администратор подтверждает блокировку
    if user_response == translate("yes", locale=user_locale).lower():
        user_data = await state.get_data()
        target_user = user_data.get("target_user_id")

        if target_user:
            target_user.user_is_blocked = True
            await write_to_db(target_user)

            await message.reply(
                translate("user_blocked_success", locale=user_locale),
                reply_markup=types.ReplyKeyboardRemove(),
                parse_mode="HTML"
            )
        else:
            await message.reply(
                translate("user_not_found", locale=user_locale),
                reply_markup=types.ReplyKeyboardRemove(),
                parse_mode="HTML"
            )

        await state.clear()
    
    # Если администратор отменяет блокировку
    elif user_response == translate("no", locale=user_locale).lower():
        await finish_operation(message, state, user_locale)
        return
    
    # Если администратор ввел некорректный ответ
    else:
        await message.reply(
            translate("invalid_response", locale=user_locale),
            parse_mode="HTML"
        )
        await state.clear()
        return
