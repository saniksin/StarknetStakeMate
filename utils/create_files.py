import os
from typing import Union
from pathlib import Path

from data.all_paths import FILES_DIR


def join_path(path: Union[str, tuple, list, Path]) -> str:
    if isinstance(path, (str, Path)):
        return path

    return os.path.join(*path)


def touch(path: Union[str, tuple, list], file: bool = False) -> bool:
    path = join_path(path)
    if file:
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write('')
            print(f'Создан файл {path}')
            return True
        else:
            return False
    else:
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            return True
        else:
            return False


def create_files():
    touch(FILES_DIR)
    