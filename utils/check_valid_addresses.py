import re

def is_valid_starknet_address(address: str) -> bool:
    # Регулярное выражение для проверки адреса StarkNet
    pattern = r"^0x[a-fA-F0-9]{63,67}$"

    # Проверка на соответствие регулярному выражению
    if re.match(pattern, address):
        return True
    else:
        return False