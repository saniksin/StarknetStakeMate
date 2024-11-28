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


class DeleteInfoState(StatesGroup):
    choose_delete_type = State()
    awaiting_selection = State()
    awaiting_confirmation = State()


async def start_delete_info(message: types.Message, state: FSMContext, user_locale: str):
    options_buttons = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=translate("delete_all_addresses", user_locale))],
            [KeyboardButton(text=translate("delete_specific_address", user_locale))],
            [KeyboardButton(text=translate("cancel", user_locale))]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.reply(
        translate("choose_delete_option", user_locale),
        reply_markup=options_buttons,
        parse_mode="HTML"
    )
    await state.set_state(DeleteInfoState.choose_delete_type)


async def process_delete_choice(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    if message.text.lower() == translate("delete_all_addresses", user_locale).lower():
        # Удаляем все адреса
        user_object.tracking_data = json.dumps({"data_pair": []})

        async with AsyncSession(db.engine) as session:
            await session.merge(user_object)
            await session.commit()

        await message.reply(translate("all_info_deleted", user_locale), parse_mode="HTML")
        await state.clear()
        return

    elif message.text.lower() == translate("delete_specific_address", user_locale).lower():
        # Переход в состояние ожидания выбора адреса
        user_data = await get_user_tracking(user_object.user_id)

        # Проверяем, есть ли что удалять
        if not user_data['data_pair']:
            await message.reply(translate("no_addresses_to_delete", user_locale), parse_mode="HTML")
            await state.clear()
            return

        # Определяем контракты для стейкинга
        staking_contracts = {Contracts.L2_STAKING_CONTRACT.hex_address, 
                             Contracts.L2_STAKING_CONTRACT.hex_address_2, 
                             Contracts.L2_STAKING_CONTRACT.hex_address_3}

        # Создаем словарь для хранения коротких и длинных адресов
        address_map = {}

        # Создаем кнопки для каждой пары адрес + пул/контракт
        address_buttons = []
        for pair in user_data['data_pair']:
            address, pool = pair

            # Сокращаем адрес и пул для отображения
            short_address = f"{address[:6]}...{address[-6:]}"
            short_pool = f"{pool[:6]}...{pool[-6:]}"

            # Определяем текст кнопки в зависимости от типа адреса
            if pool in staking_contracts:
                # Это валидатор и адрес контракта стейкинга
                button_text = (
                    f"{translate('validator_address', user_locale)}: {short_address} | "
                    f"{translate('staking_contract', user_locale)}: {short_pool}"
                )
            else:
                # Это делегатор и пул
                button_text = (
                    f"{translate('delegator_address', user_locale)}: {short_address} | "
                    f"{translate('pool_address', user_locale)}: {short_pool}"
                )

            # Сохраняем короткий и полный адрес в словарь
            address_map[button_text] = {"full_address": address, "full_pool": pool}

            address_buttons.append([KeyboardButton(text=button_text)])

        # Добавляем кнопку отмены
        address_buttons.append([KeyboardButton(text=translate("cancel", user_locale))])

        # Сохраняем адреса в стейт
        await state.update_data(address_map=address_map)

        await state.set_state(DeleteInfoState.awaiting_selection)

        await message.reply(
            translate("choose_address_to_delete", user_locale),
            reply_markup=ReplyKeyboardMarkup(
                keyboard=address_buttons,
                resize_keyboard=True,
                one_time_keyboard=True
            ),
            parse_mode="HTML"
        )
    else:
        await cancel_operation(message, state, user_locale)


async def delete_specific_address(message: types.Message, state: FSMContext, user_locale: str, user_object: Users):
    user_data = await get_user_tracking(user_object.user_id)
    data = await state.get_data()

    # Получаем адрес для удаления из текста сообщения
    address_to_delete_text = message.text.strip()

    # Проверяем, существует ли словарь с адресами в данных состояния
    address_map = data.get("address_map", {})

    # Получаем полный адрес и пул из сохраненного словаря
    pair_to_delete = address_map.get(address_to_delete_text)

    if not pair_to_delete:
        await message.reply(translate("address_not_found", user_locale), parse_mode="HTML")
        await state.clear()
        return

    full_address = pair_to_delete["full_address"]
    full_pool = pair_to_delete["full_pool"]

    # Удаляем найденную пару из списка
    try:
        user_data['data_pair'].remove([full_address, full_pool])
    except ValueError as e:
        await message.reply(translate("address_not_found", user_locale), parse_mode="HTML")
        await state.clear()
        return

    # Обновляем данные пользователя
    user_object.tracking_data = json.dumps(user_data)

    async with AsyncSession(db.engine) as session:
        await session.merge(user_object)
        await session.commit()

    await message.reply(translate("address_deleted", user_locale), parse_mode="HTML")
    
    # Очистка состояния после завершения операции
    await state.clear()