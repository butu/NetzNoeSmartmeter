"""
Utility functions and convenience methods to avoid boilerplate
"""
from __future__ import annotations
from functools import reduce
import datetime as dt
import logging
from types import UnionType


def today() -> dt.datetime:
    """
    today's timestamp (start of day)
    """
    return dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def before(datetime=None, days=1) -> dt.datetime:
    """
    subtract {days} days from given datetime (default: 1)
    """
    if datetime is None:
        datetime = today()
    return datetime - datetime.timedelta(days=days)


def strint(string: str) -> UnionType[str, int]:
    """
    convenience function for easily convert None-able str to in
    """
    if string is not None and string.isdigit():
        return int(string)
    return string


def is_valid_access(data: UnionType[list, dict], accessor: UnionType[str, int]) -> bool:
    """
    convenience function for double checking if attribute of list or dict can be accessed
    """
    if isinstance(accessor, int) and isinstance(data, list):
        return accessor < len(data)
    if isinstance(accessor, str) and isinstance(data, dict):
        return accessor in data
    else:
        return False


def dict_path(path: str, dictionary: dict) -> str:
    """
    convenience function for accessing nested attributes within a dict
    """
    try:
        return reduce(
            lambda acc, i: acc[i] if is_valid_access(acc, i) else None,
            [strint(s) for s in path.split(".")],
            dictionary,
        )
    except KeyError as exception:
        logging.warning("Could not find key '%s' in response", exception.args[0])
    except Exception as exception:  # pylint: disable=broad-except
        logging.exception(exception)
    return None


def translate_dict(
    dictionary: dict, attrs_list: list[tuple[str, str]]
) -> dict[str, str]:
    """
    Given a response dictionary and an attribute mapping (with nested accessors separated by '.')
    returns a dictionary including all "picked" attributes addressed by attrs_list
    """
    result = {}
    for src, dest in attrs_list:
        value = dict_path(src, dictionary)
        if value is not None:
            result[dest] = value
    return result
