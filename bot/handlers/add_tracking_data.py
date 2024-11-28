import json

from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from bot.handlers.clear_state import cancel_operation
from sqlalchemy.ext.asyncio import AsyncSession

from data.languages import translate
from db_api.database import get_user_tracking, Users, db
from data.contracts import Contracts


class AddInfoState(StatesGroup):
    choose_type = State()
    awaiting_validator_address = State()
    awaiting_delegate_address = State()
    awaiting_pool_address = State()
    awaiting_prepere_confirmation = State()
    awaiting_confirmation = State()


# Начало процесса добавления информации
async def add_info(message: types.Message, state: FSMContext, user_locale: str):
    # Создаем клавиатуру для выбора типа добавляемой информации
    options_buttons = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=translate("add_delegate_address", user_locale))],
            [KeyboardButton(text=translate("add_validator_address", user_locale))],
            [KeyboardButton(text=translate("cancel", user_locale))]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    # Отправляем сообщение и переводим в состояние выбора типа
    await message.reply(
        translate("choose_add_type", user_locale),
        reply_markup=options_buttons,
        parse_mode="HTML"
    )
    await state.set_state(AddInfoState.choose_type)


# Обработка выбора пользователя (валидатор или делегатор)
async def process_add_type(message: types.Message, state: FSMContext, user_locale: str):
    if message.text.lower() == translate("add_validator_address", user_locale).lower():
        # Переход в состояние ожидания адреса валидатора
        await message.reply(translate("enter_validator_address", user_locale), parse_mode="HTML")
        await state.set_state(AddInfoState.awaiting_validator_address)

    elif message.text.lower() == translate("add_delegate_address", user_locale).lower():
        # Переход в состояние ожидания адреса делегатора
        await message.reply(translate("enter_delegate_address", user_locale), parse_mode="HTML")
        await state.set_state(AddInfoState.awaiting_delegate_address)

    else:
        await cancel_operation(message, state, user_locale)


# Ввод адреса валидатора
async def process_validator_address(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):

    # TODO: check address
    validator_address = message.text.strip()
    
    # Проверяем, есть ли уже сохраненные адреса и достигает ли лимита
    user_data = await get_user_tracking(user_object.user_id)
    if len(user_data['data_pair']) >= 3:
        await message.reply(translate("info_limit_reached", user_locale))
        await state.clear()
        return

    await state.update_data(validator_address=validator_address)
    await state.update_data(pool_address=Contracts.L2_STAKING_CONTRACT.hex_address)
    await state.update_data(add_validator=True)
    await state.set_state(AddInfoState.awaiting_prepere_confirmation)
    await confirm_tracking_data(message, state, user_locale)


# Ввод адреса делегатора
async def process_delegator_address(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    # TODO: check address
    delegator_address = message.text.strip()

    user_data = await get_user_tracking(user_object.user_id)
    if len(user_data['data_pair']) >= 3:
        await message.reply(translate("info_limit_reached", user_locale))
        await state.clear()
        return

    # Переход в состояние ожидания адреса пула
    await state.update_data(delegetor_address=delegator_address)
    await state.update_data(add_delegator=True)
    await state.set_state(AddInfoState.awaiting_pool_address)
    await message.reply(translate("enter_pool_address", user_locale), parse_mode="HTML")


# Ввод адреса пула
async def process_pool_address(message: types.Message, state: FSMContext, user_locale: str):
    # TODO: check address
    pool_address = message.text.strip()
    await state.update_data(pool_address=pool_address)
    await state.set_state(AddInfoState.awaiting_prepere_confirmation)
    await confirm_tracking_data(message, state, user_locale)

# Функция для подтверждения введенной информации
async def confirm_tracking_data(message: types.Message, state: FSMContext, user_locale: str):
    data = await state.get_data()
    # Формируем сообщение для подтверждения на основе того, что добавляется
    confirm_message = ""
    if data.get("add_validator"):
        confirm_message = translate("confirm_validator_info", user_locale).format(
            validator_address=data.get('validator_address'),
            pool_address=data.get('pool_address')
        )
    elif data.get("add_delegator"):
        confirm_message = translate("confirm_delegate_info", user_locale).format(
            delegate_address=data.get('delegetor_address'),
            pool_address=data.get('pool_address')
        )
    else:
        await message.reply(translate("operation_cancelled", user_locale), parse_mode="HTML")
        await state.clear()
        return

    # Кнопки подтверждения и отмены
    confirmation_buttons = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=translate("save", user_locale))],
            [KeyboardButton(text=translate("cancel", user_locale))]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

    await message.reply(confirm_message, reply_markup=confirmation_buttons, parse_mode="HTML")
    await state.set_state(AddInfoState.awaiting_confirmation)

# Функция для обработки подтверждения или отмены
async def process_confirmation(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    data = await state.get_data()

    if message.text.lower() == translate("save", user_locale).lower():
        # Сохраняем данные в базе в зависимости от того, что добавлялось
        user_data = await get_user_tracking(user_object.user_id)

        if data.get("add_validator"):
            print(data.get("validator_address"))
            user_data['data_pair'].append([data.get("validator_address"), data.get("pool_address")])
        elif data.get("add_delegator"):
            user_data['data_pair'].append([data.get("delegetor_address"), data.get("pool_address")])

        user_object.tracking_data = json.dumps(user_data)

        # Сохраняем обновленные данные в базу
        async with AsyncSession(db.engine) as session:
            await session.merge(user_object)
            await session.commit()

        # Отправляем сообщение пользователю о том, что данные сохранены
        if data.get("add_validator"):
            await message.reply(translate("validator_info_saved", user_locale), parse_mode="HTML")
        elif data.get("add_delegator"):
            await message.reply(translate("delegate_info_saved", user_locale), parse_mode="HTML")

        # Очищаем состояние после успешного сохранения
        await state.clear()

    elif message.text.lower() == translate("cancel", user_locale).lower():
        await cancel_operation(message, state, user_locale)