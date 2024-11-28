import os

import json
from typing import Optional, Union


def read_json(path: Union[str, tuple, list], encoding: Optional[str] = None) -> Union[list, dict]:
    """
    Read a JSON file and return a Python list or dictionary.

    :param Union[str, tuple, list] path: path to the JSON file
    :param Optional[str] encoding: the name of the encoding used to decode or encode the file
    :return Union[list, dict]: the Python list or dictionary
    """
    if isinstance(path, (str, tuple, list)):
        path = join_path(path)
    return json.load(open(path, encoding=encoding))


def join_path(path: Union[str, tuple, list]) -> str:
    return os.path.join(*path)