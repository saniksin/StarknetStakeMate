from data.languages import translate
from utils.format_decimal import format_decimal


def parse_delegator_info(data, user_locale: str):
    # Если `data` является кортежем, берем первый элемент
    if isinstance(data, tuple):
        if not data:  # Проверка на пустой кортеж
            return translate("invalid_delegator_address", user_locale)
        data = data[0]

    # Проверка на валидный формат `data`
    if not isinstance(data, dict):
        return translate("invalid_delegator_address", user_locale)

    # Преобразование чисел в hex
    def to_hex(address):
        return f"0x{address:x}"

    # Обработка данных делегатора
    reward_address = f"<code>{to_hex(data['reward_address'])}</code>"
    amount = f"{format_decimal(data['amount'])} STRK"  # Форматируем в удобный вид
    unclaimed_rewards = f"{format_decimal(data['unclaimed_rewards'])} STRK"
    commission = data["commission"] / 100  # Конвертируем в проценты
    unpool_amount = f"{format_decimal(data.get('unpool_amount', 0))} STRK"
    unpool_time = data.get("unpool_time", None)

    # Формирование сообщения для делегатора
    message = (
        f"{translate('reward_address', user_locale)} {reward_address}\n"
        
        f"{translate('delegator_stake_amount', user_locale)} {amount}\n"
        f"{translate('delegator_unclaimed_rewards', user_locale)} {unclaimed_rewards}\n"
        f"{translate('delegator_unpool_amount', user_locale)} {unpool_amount}\n"
        f"{translate('unstake_status', user_locale)} {f"{translate('delegator_cannot_unstake', user_locale)} - {unpool_time}\n" if unpool_time else translate('delegator_cannot_unstake', user_locale)}\n"
        f"{translate('pool_commission', user_locale)} {commission:.2f}%\n"
    )

    return message


def parse_validator_info(data, user_locale: str, status=True):
    message = ''

    # Если data является кортежем, берем первый элемент
    if isinstance(data, tuple):
        if not data:  # Проверка на пустой кортеж
            return translate("invalid_validator_address", user_locale)
        data = data[0]

    # Проверка на валидный формат data
    if not isinstance(data, dict):
        return translate("invalid_validator_address", user_locale)

    # Преобразование чисел в hex
    def to_hex(address):
        return f"0x{address:x}"

    # Обработка данных валидатора
    reward_address = f"<code>{to_hex(data['reward_address'])}</code>"
    operational_address = f"<code>{to_hex(data['operational_address'])}</code>"
    unstake_time = data["unstake_time"]
    unstake_status = (
        translate("can_unstake", user_locale) if unstake_time is None else f"{translate("cannot_unstake", user_locale)} - {unstake_time}"
    )
    amount_own = f"{format_decimal(data['amount_own'])} STRK"  # Форматируем в удобный вид
    unclaimed_rewards_own = f"{format_decimal(data['unclaimed_rewards_own'])} STRK"

    # Обработка данных пула
    pool_contract = f"<code>{to_hex(data['pool_info']['pool_contract'])}</code>"
    pool_unclaimed_rewards = f"{format_decimal(data['pool_info']['unclaimed_rewards'])} STRK"
    pool_commission = data["pool_info"]["commission"] / 100  # Конвертируем в проценты

    validator_info = f"{translate('validator_info', user_locale)}\n\n" if status else ""
    unclaimed_rewards_own = f"{translate('unclaimed_rewards_own', user_locale)} {unclaimed_rewards_own}\n\n" if status \
        else f"{translate('unclaimed_rewards_own', user_locale)} {unclaimed_rewards_own}\n"
    poll_info = f"{translate('pool_info', user_locale)}\n" if status else ""
    pool_contract = f"  {translate('pool_contract', user_locale)} {pool_contract}\n" if status \
        else f"{translate('pool_contract', user_locale)} {pool_contract}\n"
    pool_unclaimed_rewards = f"  {translate('pool_unclaimed_rewards', user_locale)} {pool_unclaimed_rewards}\n" if status \
        else f"{translate('pool_unclaimed_rewards', user_locale)} {pool_unclaimed_rewards}\n"
    pool_commission = f"  {translate('pool_commission', user_locale)} {pool_commission:.2f}%" if status \
        else f"{translate('pool_commission', user_locale)} {pool_commission:.2f}%"

    
    # Формирование сообщения
    message += validator_info
    message += (
        f"{translate('reward_address', user_locale)} {reward_address}\n"
        f"{translate('operational_address', user_locale)} {operational_address}\n"
        f"{translate('unstake_status', user_locale)} {unstake_status}\n"
        f"{translate('amount_own', user_locale)} {amount_own}\n"
    )
    message += unclaimed_rewards_own
    message += poll_info
    message += pool_contract
    message += pool_unclaimed_rewards
    message += pool_commission

    return message


def format_section(user_locale, task_type, task_result, address, pool, info_address_key, pool_address_key, no_data=False):
    """
    Функция для форматирования секции информации (делегатор или валидатор) с адресами и пулами.
    """
    # Формируем разделитель с отступами
    separator = "\n===================================\n"

    # Заголовок в зависимости от типа задачи
    if task_type == 'validator':
        section_title = translate('validator_info_2', user_locale)
        info_address = translate(info_address_key, user_locale)
        pool_address = translate(pool_address_key, user_locale)
    else:
        section_title = translate('delegator_info', user_locale)
        info_address = translate(info_address_key, user_locale)
        pool_address = translate(pool_address_key, user_locale)

    # Формируем контент секции
    if no_data:
        section_content = f"{separator}<b>{section_title}</b>{separator}{translate('no_data_for_' + task_type, user_locale)} {address} | {pool}\n"
    else:
        section_content = f"{separator}<b>{section_title}</b>{separator}{info_address} <code>{address}</code>\n{pool_address} <code>{pool}</code>\n"
        # Дополнительные данные (передаем для валидатора или делегатора)
        if task_result:
            section_content += parse_delegator_info(task_result, user_locale) if task_type == 'delegator' else parse_validator_info(task_result, user_locale, False)

    return section_content
