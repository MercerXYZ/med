"""Нормализация текста PDF для сравнения цитат, числа и отрицания."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

_TRANS = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", "­": None,
    "‘": "'", "’": "'", "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    " ": " ", " ": " ", " ": " ", "×": "x", "≤": "<=", "≥": ">=", "±": "+-", "•": " ",
})
_DROP = set("\"'*-")


def norm_soft(text: str) -> str:
    """Нижний регистр, одинарные пробелы, склеенные переносы."""
    t = unicodedata.normalize("NFKC", text or "").translate(_TRANS)
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def norm_hard_with_map(text: str) -> tuple[str, list[int]]:
    """Без пробелов, дефисов и кавычек + индекс каждого символа в исходной строке."""
    out, idx = [], []
    for i, ch in enumerate(unicodedata.normalize("NFKC", text or "")):
        for c in (ch.translate(_TRANS) or "").lower():
            if not (c.isspace() or c in _DROP):
                out.append(c)
                idx.append(i)
    return "".join(out), idx


def norm_hard(text: str) -> str:
    return norm_hard_with_map(text)[0]


_NUM_RE = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)")


def numbers(text: str) -> set[str]:
    """Числа из текста: запятая → точка, без хвостовых нулей, без номеров ссылок [1, 2]."""
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"\[[\d,\s\-–]+\]", " ", t)
    return {format(Decimal(m.group(1).replace(",", ".")).normalize(), "f") for m in _NUM_RE.finditer(t)}


NEG_EN = re.compile(r"\b(not|no|without|neither|nor|never|none|cannot|failed to|non-?inferior|absence of|"
                    r"lack of|insufficient|unable|against)\b|n't", re.I)
NEG_RU = re.compile(r"(?<![а-яё])(не|нет|без|ни|отсутств\w*|недостаточн\w*|нецелесообразн\w*|противопоказ\w*)"
                    r"(?![а-яё])", re.I)
NEG_IGNORE = re.compile(r"whether or not|not only|no\. ?\d|не только|независимо", re.I)


def negation_word(text: str) -> str | None:
    t = NEG_IGNORE.sub(" ", unicodedata.normalize("NFKC", text or ""))
    m = NEG_EN.search(t) or NEG_RU.search(t)
    return m.group(0) if m else None
