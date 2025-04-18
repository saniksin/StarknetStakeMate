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
    reward_address = to_hex(data['reward_address'])
    amount = format_decimal(data['amount'])
    unclaimed_rewards = format_decimal(data['unclaimed_rewards'])
    commission = data["commission"] / 100
    unpool_amount = format_decimal(data.get('unpool_amount', 0))
    unpool_time = data.get("unpool_time", None)

    # Формирование сообщения для делегатора
    message = (
        
        f"┌ {translate('basic_info', user_locale)}\n"
        f"├ • {translate('delegator_address', user_locale)}:\n"
        f"├{reward_address}\n"
        f"├ • {translate('pool_address', user_locale)}:\n"
        f"└{to_hex(data['pool_info']['pool_contract'])}\n\n"
        
        f"┌ {translate('staking', user_locale)}\n"
        f"├ • {translate('delegated', user_locale)}: {amount} STRK\n"
        f"├ • {translate('unclaimed', user_locale)}: {unclaimed_rewards} STRK\n"
        f"└ • {translate('withdrawing', user_locale)}: {unpool_amount} STRK\n\n"
        
        f"• 🔄 {translate('status', user_locale)}: {translate('delegator_cannot_unstake', user_locale)} {f'- {unpool_time}' if unpool_time else ''} ✅\n"
        f"• 📈 {translate('commission', user_locale)}: {commission:.2f}%\n"
        f"──────────────────────────────"
    )

    return message


def parse_validator_info(data, user_locale: str, status=True):
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
    reward_address = to_hex(data['reward_address'])
    operational_address = to_hex(data['operational_address'])
    unstake_time = data["unstake_time"]
    unstake_status = (
        translate("can_unstake", user_locale) if unstake_time is None else f"{translate("cannot_unstake", user_locale)} - {unstake_time}"
    )
    amount_own = format_decimal(data['amount_own'])
    unclaimed_rewards_own = format_decimal(data['unclaimed_rewards_own'])

    # Обработка данных пула
    pool_contract = to_hex(data['pool_info']['pool_contract'])
    pool_unclaimed_rewards = format_decimal(data['pool_info']['unclaimed_rewards'])
    pool_commission = data["pool_info"]["commission"] / 100

    # Формирование сообщения
    message = (
        f"──────────────────────────────\n"
        f"{translate('validator_info', user_locale)}\n"
        f"──────────────────────────────\n\n"
        
        f"┌ {translate('basic_info', user_locale)}\n"
        f"├ • {translate('validator_address', user_locale)}:\n"
        f"├{reward_address}\n"
        f"├ • {translate('reward_address', user_locale)}:\n"
        f"├{reward_address}\n"
        f"├ • {translate('operational_address', user_locale)}:\n"
        f"├{operational_address}\n"
        f"├ • {translate('contract_address', user_locale)}:\n"
        f"└{pool_contract}\n\n"
        
        f"┌ {translate('staking', user_locale)}\n"
        f"├ • {translate('personal_stake', user_locale)}: {amount_own} STRK\n"
        f"└ • {translate('unclaimed', user_locale)}: {unclaimed_rewards_own} STRK\n\n"
        
        f"• 🔄 {translate('status', user_locale)}: {unstake_status} ✅\n"
        f"• 📈 {translate('commission', user_locale)}: {pool_commission:.2f}%\n\n"
        
        f"┌ {translate('pool_info', user_locale)}\n"
        f"└ • {translate('unclaimed_in_pool', user_locale)}: {pool_unclaimed_rewards} STRK\n"
        f"──────────────────────────────"
    )

    return message


def format_section(user_locale, task_type, task_result, address, pool, info_address_key, pool_address_key, no_data=False):
    """
    Функция для форматирования секции информации (делегатор или валидатор) с адресами и пулами.
    """
    # Заголовок в зависимости от типа задачи
    if task_type == 'validator':
        section_title = translate('validator_info', user_locale)
        info_address = translate(info_address_key, user_locale)
        pool_address = translate(pool_address_key, user_locale)
    else:
        section_title = translate('delegator_info', user_locale)
        info_address = translate(info_address_key, user_locale)
        pool_address = translate(pool_address_key, user_locale)

    # Формируем контент секции
    if no_data:
        section_content = f"──────────────────────────────\n{section_title}\n──────────────────────────────\n\n{translate('no_data_for_' + task_type, user_locale)} {address} | {pool}\n"
    else:
        section_content = f"──────────────────────────────\n{section_title}\n──────────────────────────────\n\n┌ {translate('basic_info', user_locale)}\n├ • {translate(f'{task_type}_address', user_locale)}:\n├{address}\n├ • {translate('pool_address', user_locale)}:\n└{pool}\n\n"
        # Дополнительные данные (передаем для валидатора или делегатора)
        if task_result:
            section_content += parse_delegator_info(task_result, user_locale) if task_type == 'delegator' else parse_validator_info(task_result, user_locale, False)

    return section_content
