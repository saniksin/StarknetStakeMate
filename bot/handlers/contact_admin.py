from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from data.models import get_admins
from data.languages import translate
from data.tg_bot import bot
from db_api.database import get_account_by_username, get_account, Users


class ContactAdminState(StatesGroup):
    awaiting_message = State()


# Команда для начала обращения к админу
async def start_contact_admin(message: types.Message, state: FSMContext, user_locale: str):
    # Создаем клавиатуру с кнопкой отмены
    cancel_button = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=translate("cancel", user_locale))]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    # Отправляем сообщение пользователю с клавиатурой
    await message.reply(translate("contact_admin_prompt", user_locale), reply_markup=cancel_button, parse_mode="HTML")
    await state.set_state(ContactAdminState.awaiting_message)


# Отправка сообщения админу
async def send_message_to_admin(message: types.Message, state: FSMContext, user_locale: str):
    admins = get_admins()

    # Информация о пользователе
    user_info = (
        f"@{message.from_user.username}" if message.from_user.username
        else f"ID: {message.from_user.id}, {message.from_user.first_name or ''} {message.from_user.last_name or ''}"
    )
    admin_message = f"{translate('message_from_user', user_locale)} {user_info}:\n\n{message.text}"

    # Отправляем сообщение каждому админу
    for admin_id in admins:
        try:
            await bot.send_message(admin_id, admin_message, parse_mode="HTML")
        except Exception as e:
            print(f"Не удалось отправить сообщение админу {admin_id}: {e}")

    # Подтверждение пользователю
    await message.reply(translate("message_sent_to_admin", user_locale), parse_mode="HTML")
    await state.clear()


# Обработчик ответа администратора
async def admin_reply_handler(message: types.Message, user_locale: str, user_object: Users):
    # Проверяем, что сообщение является ответом на другое сообщение
    if not message.reply_to_message:
        return

    # Получаем текст оригинального сообщения
    original_message = message.reply_to_message
    if not original_message.text:
        await message.reply(translate("original_message_empty", user_locale))
        return

    # Извлекаем упоминание из entities
    user = None
    if original_message.entities:
        for entity in original_message.entities:
            if entity.type == "mention":  # Проверяем, что это упоминание пользователя
                username = original_message.text[entity.offset:entity.offset + entity.length]
                user = await get_account_by_username(username.strip("@"))
                break

    # Если не удалось найти пользователя через упоминание, проверяем на наличие ID
    if not user:
        if "ID:" in original_message.text:
            try:
                user_id = int(original_message.text.split("ID:")[1].split(",")[0].strip())
                user = await get_account(user_id)
            except ValueError:
                await message.reply(translate("invalid_user_id", user_locale))
                return

    if not user:
        await message.reply(translate("user_not_found", user_locale))
        return

    response_text = (
        f"📩 {translate('response_from_admin', user.user_language)} @{user_object.user_name}:\n\n"
        f"{message.text}\n\n"
        f"**{translate('reply_to_admin_prompt', user.user_language)}**"
    )

    try:
        # Отправляем сообщение пользователю
        await bot.send_message(user.user_id, response_text, parse_mode="HTML")
        # Подтверждение для администратора
        await message.reply(translate("message_sent_to_user", user_locale), parse_mode="HTML")
    except Exception as e:
        await message.reply(translate("failed_to_send_message", user_locale) + f": {e}")
        print(f"Не удалось отправить сообщение пользователю {user.user_id}: {e}")


# Обработчик ответа пользователя или администратора
async def reply_handler(message: types.Message, user_locale: str):
    # Получаем список администраторов
    admins = get_admins()

    # Проверяем, является ли сообщение ответом на другое сообщение
    if not message.reply_to_message:
        return

    # Получаем информацию об оригинальном сообщении
    original_message = message.reply_to_message
    if not original_message.text:
        await message.reply(translate("original_message_empty", user_locale))
        return

    # Попытаемся получить ID пользователя или username из текста оригинального сообщения
    original_author_id = None
    original_author_username = None

    if "@" in original_message.text:  # Проверяем наличие username
        try:
            original_author_username = original_message.text.split("@")[1].split(":")[0].strip()
            user = await get_account_by_username(original_author_username)
            if user:
                original_author_id = user.user_id
        except Exception as e:
            print(f"Ошибка при извлечении username: {e}")

    elif "ID:" in original_message.text:  # Проверяем наличие ID
        try:
            original_author_id = int(original_message.text.split("ID:")[1].split(",")[0].strip())
        except ValueError:
            await message.reply(translate("invalid_user_id", user_locale))
            return

    # Если не удалось извлечь ни ID, ни username
    if not original_author_id:
        await message.reply(translate("user_not_found", user_locale))
        return

    # Проверяем, является ли оригинальный автор администратором или пользователем
    original_author_role = "user" if original_author_id in admins else "admin"

    # Определяем, кому нужно направить ответ: пользователю или администратору
    if original_author_role == "admin":
        # Если оригинальное сообщение от администратора, то ответ идет к пользователю
        target_user_id = original_author_id if original_author_id != message.from_user.id else None

        # Проверка, чтобы не отправлять сообщение самому себе
        if not target_user_id:
            await message.reply(translate("cannot_reply_self", user_locale))
            return

        target_user = await get_account(target_user_id)
        if not target_user:
            await message.reply(translate("user_not_found", user_locale))
            return

        target_user_language = target_user.user_language

        # Формируем текст ответа для пользователя
        response_text = (
            f"📩 {translate('response_from_admin', target_user_language)} @{message.from_user.username}:\n\n"
            f"{message.text}\n\n"
            f"<i>{translate('reply_to_admin_prompt', target_user_language)}</i>"
        )

        try:
            # Отправляем сообщение пользователю
            await bot.send_message(target_user_id, response_text, parse_mode="HTML")
            # Подтверждение для администратора, что сообщение успешно отправлено пользователю
            await message.reply(translate("message_sent_to_user", user_locale), parse_mode="HTML")
        except Exception as e:
            await message.reply(translate("failed_to_send_message", user_locale) + f": {e}")
            print(f"Не удалось отправить сообщение пользователю {target_user_id}: {e}")

    elif original_author_role == "user":
        # Если оригинальное сообщение от пользователя, то ответ идет к администратору
        target_user_id = original_author_id

        # Проверка на случай, если ID администратора равен ID отправителя (исключение ответа самому себе)
        if target_user_id == message.from_user.id:
            await message.reply(translate("cannot_reply_self", user_locale))
            return

        target_user = await get_account(target_user_id)
        if not target_user:
            await message.reply(translate("user_not_found", user_locale))
            return

        target_user_language = target_user.user_language

        # Формируем текст ответа для администратора
        response_text = (
            f"📨 {translate('reply_from_user', target_user_language)} @{message.from_user.username}:\n\n"
            f"{message.text}\n\n"
            f"<i>{translate('reply_to_user_prompt', target_user_language)}</i>"
        )

        try:
            # Отправляем сообщение администратору
            await bot.send_message(target_user_id, response_text, parse_mode="HTML")
            # Подтверждение для пользователя, что сообщение успешно отправлено администратору
            await message.reply(translate("message_sent_to_admin", user_locale), parse_mode="HTML")
        except Exception as e:
            await message.reply(translate("failed_to_send_message", user_locale) + f": {e}")
            print(f"Не удалось отправить сообщение администратору {target_user_id}: {e}")
