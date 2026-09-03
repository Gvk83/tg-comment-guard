"""Приведение текста к виду, устойчивому к типовым способам обхода фильтра."""

import re
import unicodedata

# Невидимые символы, которыми спамеры разрывают слова внутри.
INVISIBLE = re.compile(
    "[​‌‍‎‏⁠⁡⁢⁣⁤﻿­]"
)

# Латинские буквы и цифры, визуально неотличимые от кириллических букв.
HOMOGLYPHS = str.maketrans(
    {
        "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х",
        "y": "у", "k": "к", "m": "м", "h": "н", "t": "т", "b": "в",
        "u": "и", "n": "п", "3": "з", "0": "о", "4": "ч", "6": "б",
    }
)

NON_ALNUM = re.compile(r"[^0-9a-zа-я]+")


def normalize(text: str) -> str:
    """Основной вид: единый регистр, без невидимых символов, одиночные пробелы."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = INVISIBLE.sub("", text)
    text = text.replace("ё", "е").lower()  # ё -> е
    # Пробелы схлопываем, но переводы строк сохраняем: по ним видно,
    # что «Пиши» стоит отдельной строкой, а не внутри предложения.
    text = re.sub(r"[^\S\n]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


# Признаки того, что текст намеренно исказили.
LATIN_IN_CYRILLIC = re.compile(r"[а-я][a-z]|[a-z][а-я]")
SPACED_LETTERS = re.compile(r"(?:(?<=\s)|^)[а-яa-z](?:\s[а-яa-z]){2,}")
DIGIT_IN_WORD = re.compile(r"[а-я][0346][а-я]")


def looks_obfuscated(original: str, normalized: str) -> bool:
    """Текст похож на попытку обойти фильтр.

    Только в этом случае имеет смысл искать слова в «сжатом» виде: там нет
    границ слов, и обычный текст даёт ложные совпадения («заработался»
    содержит «работа»).
    """
    if INVISIBLE.search(original):
        return True
    return bool(
        LATIN_IN_CYRILLIC.search(normalized)
        or SPACED_LETTERS.search(normalized)
        or DIGIT_IN_WORD.search(normalized)
    )


def squash(normalized: str) -> str:
    """Сжатый вид: только буквы и цифры, латиница заменена на похожую кириллицу.

    Нужен, чтобы поймать «р а б о т а», «рaботa» (с латинскими буквами)
    и «раб0та» — в сжатом виде все они превращаются в «работа».
    """
    return NON_ALNUM.sub("", normalized.translate(HOMOGLYPHS))
