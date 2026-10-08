"""Shared conservative claim boundaries for learning dedupe and reply R13."""
import re
from itertools import groupby


def normalize_claim(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def same_claim_sequences(a: str, b: str) -> bool:
    """Numbers (including Chinese numerals) and negations must stay in order."""
    left, right = normalize_claim(a), normalize_claim(b)
    def numbers(text: str) -> list[str]:
        return ["".join(chars) for numeric, chars in groupby(text, str.isnumeric) if numeric]
    return (numbers(left) == numbers(right) and
            re.findall(r"不|没|无|未|否", left) == re.findall(r"不|没|无|未|否", right))
