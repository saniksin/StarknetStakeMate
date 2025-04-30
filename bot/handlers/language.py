from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from data.languages import translate, possible_language
from db_api.database import db, Users, write_to_db
from bot.handlers.clear_state import finish_operation
from utils.cache import clear_user_cache


# Состояние для выбора языка
class LanguageState(StatesGroup):
    choosing = State()


# Хендлер для команды /language
async def choose_language(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    # Проверяем есть ли уже запрос в очереди
    if user_object.request_queue:
        await message.answer(
            translate("language_change_blocked", user_locale),
            parse_mode="HTML"
        )
        return

    # Создаем клавиатуру
    markup = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="English"), KeyboardButton(text="Русский")],
            [KeyboardButton(text="Українська"), KeyboardButton(text="中文")],
            [KeyboardButton(text="한국어"), KeyboardButton(text=translate("cancel", locale=user_locale))]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    # Отправляем сообщение с клавиатурой
    await message.reply(
        translate("choose_language", locale=user_locale), 
        reply_markup=markup, 
        parse_mode="HTML"
    )
    await state.set_state(LanguageState.choosing)


# Хендлер для установки языка
async def set_language(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    selected_language = message.text.lower()

    if selected_language in possible_language:
        if selected_language == "english":
            locale = "en"
        elif selected_language == "русский":
            locale = "ru"
        elif selected_language == "українська":
            locale = "ua"
        elif selected_language == "中文":
            locale = "zh"
        elif selected_language == "한국어":
            locale = "ko"

        # Сохраняем язык
        user_object.user_language = locale
        await write_to_db(user_object)
        
        # Очищаем кеш пользователя
        await clear_user_cache(message.from_user.id)
    else:
        await finish_operation(
            message, 
            state, 
            user_locale,
            privious_msg=translate("invalid_language_choice", locale=user_locale),
            cancel_msg=False
        )
        return

    await finish_operation(
        message, 
        state, 
        user_object.user_language,
        privious_msg=translate("language_set", locale=locale),
        cancel_msg=False
    )

