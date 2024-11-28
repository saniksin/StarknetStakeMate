from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from sqlalchemy.ext.asyncio import AsyncSession

from data.languages import translate, possible_language
from db_api.database import db, get_account


# Состояние для выбора языка
class LanguageState(StatesGroup):
    choosing = State()


# Хендлер для команды /language
async def choose_language(message: types.Message, state: FSMContext, user_locale: str):
    # Создаем клавиатуру
    markup = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="English"), KeyboardButton(text="Русский")],
            [KeyboardButton(text="Українська")]
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
async def set_language(message: types.Message, state: FSMContext, user_locale: str):
    selected_language = message.text.lower()

    if selected_language in possible_language:
        if selected_language == "english":
            locale = "en"
        elif selected_language == "русский":
            locale = "ru"
        elif selected_language == "українська":
            locale = "ua"

        # Сохраняем язык
        await state.update_data(language=locale)
        user_id = message.from_user.id
        user = await get_account(user_id)
        async with AsyncSession(db.engine) as session:
            user.user_language = locale
            await session.merge(user)
            await session.commit()
    else:
        await message.reply(
            translate("invalid_language_choice", locale=user_locale), 
            parse_mode="HTML")
        await state.clear()
        return

    confirmation_message = translate("language_set", locale=locale)
    await message.reply(
        confirmation_message, 
        reply_markup=types.ReplyKeyboardRemove(), 
        parse_mode="HTML"
    )
    await state.clear()
