"""Нормализация текста PDF для сравнения цитат.

Оговорённая нормализация (ТЗ, «Проверяемость цитат»): Unicode NFKC, лигатуры,
склейка переносов «-\\n», единые тире/кавычки/апострофы, нижний регистр,
удаление мягких переносов и всех пробельных символов. Пробелы убираются целиком,
потому что в PDF с выключкой по ширине («Orthopaedics  &  Traumatology»,
«нет \\nдостоверных») пробелы и разрывы строк расставлены произвольно.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

_TRANS = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "−": "-", "­": None, "‘": "'", "’": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "´": "'", "`": "'", " ": " ", " ": " ", " ": " ",
    "×": "x", "≤": "<=", "≥": ">=", "±": "+-", "•": " ",
    "": " ", "●": " ",
})


def norm_soft(text: str) -> str:
    """Нормализация с сохранением пробелов (для показа и поиска слов)."""
    t = unicodedata.normalize("NFKC", text or "").translate(_TRANS)
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)  # перенос слова
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()


def norm_hard(text: str) -> str:
    """Нормализация для поиска цитаты: без пробелов, дефисов и кавычек.
    Дефисы убираются целиком, т.к. «anti-
inflammatory» в PDF и
    «anti-inflammatory» в цитате модели иначе не совпадут."""
    return norm_hard_with_map(text)[0]


_DROP = set("\"'*-")


def norm_hard_with_map(text: str) -> tuple[str, list[int]]:
    """norm_hard + карта «индекс в нормализованной строке → индекс в исходной».
    Нужна, чтобы подсветить найденный фрагмент на странице PDF."""
    out, idx = [], []
    src = unicodedata.normalize("NFKC", text or "")
    for i, ch in enumerate(src):
        t = ch.translate(_TRANS)
        for c in (t or "").lower():
            if not (c.isspace() or c in _DROP):
                out.append(c)
                idx.append(i)
    return "".join(out), idx


_NUM_RE = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)")


_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i > 1}
_UNITS.update({"single": 1, "once": 1, "twice": 2, "half": 0.5, "dozen": 12, "hundred": 100})
_WORDNUM = re.compile(
    r"\b(" + "|".join(sorted(_TENS, key=len, reverse=True)) + r")(?:[-\s](" + "|".join(
        k for k in _UNITS if _UNITS[k] < 10 and k != "zero") + r"))?\b|\b(" +
    "|".join(sorted(_UNITS, key=len, reverse=True)) + r")(?:fold)?\b", re.I)


def words_to_digits(text: str) -> str:
    """«forty-two» → 42, «ninefold» → 9, «five to six times» → 5 to 6: в англоязычных
    статьях числа часто пишутся словами, а в формулировке — цифрами."""
    def repl(m):
        if m.group(1):
            return str(_TENS[m.group(1).lower()] + (_UNITS[m.group(2).lower()] if m.group(2) else 0))
        v = _UNITS[m.group(3).lower()]
        return str(v)
    return _WORDNUM.sub(repl, re.sub(r"(\w)fold\b", r"\1", text or "", flags=re.I))


def numbers(text: str) -> set[str]:
    """Числа из текста в каноническом виде (запятая → точка, без хвостовых нулей).
    Год публикации и номера ссылок вида [12] тоже числа — сравнение идёт только
    между формулировкой и её цитатой, поэтому лишнее в цитате не мешает."""
    res = set()
    # «¼» в научных PDF — почти всегда испорченный «=» (P¼0.05); NFKC превратил бы его в «1⁄4»
    # и склеил с соседним числом («P¼0.7» → «1⁄40.7»)
    # разделитель тысяч «1 000» (неразрывный/узкий пробел) — до NFKC, который делает его обычным пробелом
    t = re.sub(r"(?<=\d)[\u00a0\u202f\u2009](?=\d{3}(?!\d))", "", (text or "").replace("¼", "="))
    t = words_to_digits(unicodedata.normalize("NFKC", t))
    # разделитель тысяч «1,000» — одно число; «0,005» и «1,5» — дроби
    t = re.sub(r"(?<![\d.,])([1-9]\d{0,2}),(?=\d{3}(?![\d.,]))", r"\1", t)
    t = re.sub(r"(?<=[\d%])e(?=\d)", "-", t)  # тире, закодированное как «e»: 0.17e0.58
    t = re.sub(r"\[[\d,\s\-–]+\]", " ", t)  # ссылки на литературу [1, 28]
    for m in _NUM_RE.finditer(t):
        v = format(Decimal(m.group(1).replace(",", ".")).normalize(), "f")
        res.add(v)
    return res


NEG_EN = re.compile(
    r"\b(not|no|without|neither|nor|never|none|cannot|failed to|fail to|non-?inferior|"
    r"absence of|absent|insufficient|lack of|unable|discouraged|against)\b|n't",
    re.I)
NEG_RU = re.compile(
    r"(?<![а-яё])(не|нет|без|ни|никак\w*|отсутств\w*|недостаточн\w*|нецелесообразн\w*|"
    r"избега\w*|противопоказ\w*)(?![а-яё])", re.I)


NEG_IGNORE = re.compile(r"whether or not|not only|no\. ?\d|не только|нетравм\w*|независимо", re.I)


def negation_match(text: str):
    t = NEG_IGNORE.sub(" ", unicodedata.normalize("NFKC", text or ""))  # лигатуры: insufﬁcient
    return NEG_EN.search(t) or NEG_RU.search(t)


def has_negation(text: str) -> bool:
    return bool(negation_match(text))
