import re
import unicodedata


def search_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value[:100]).lower().replace("ё", "е")
    return " ".join(re.findall(r"[^\W_]+", value))


def russian_layout(value: str) -> str:
    if not re.fullmatch(r"[a-z ]+", value):
        return value
    return search_text(
        value.translate(str.maketrans("qwertyuiopasdfghjklzxcvbnm", "йцукенгшщзфывапролдячсмить"))
    )
