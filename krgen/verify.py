"""Многоуровневая верификация карточек доказательств.

Уровень 1 (детерминированный, без LLM) — дешёвые проверки, которым нельзя
«понравиться»: цитата реально есть на указанной странице, числа формулировки
есть в цитате, отрицание не потеряно и не добавлено, «рекомендация» содержит
рекомендательный маркер. Эти проверки ловят самые опасные ошибки — выдуманную
цитату, искажённую дозу, перевёрнутое отрицание.

Уровень 2 (LLM-рецензент) — смысловая проверка: подтверждает ли найденная цитата
формулировку (entailment), не расширена ли популяция, не выдан ли результат
исследования за рекомендацию. Рецензент — отдельная роль с отдельным промптом,
получает расширенный контекст вокруг цитаты и не видит рассуждений генератора.

Статус карточки:
  verified      — цитата найдена, все проверки пройдены, рецензент: supported;
  needs_review  — есть предупреждения (частичное совпадение, числа, отрицание…);
  unverified    — цитата не найдена или рецензент: not_supported.
В подтверждённую часть черновика попадают только verified и карточки,
подтверждённые экспертом вручную.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from collections import Counter
from functools import lru_cache

from rapidfuzz import fuzz
from rapidfuzz.distance import Indel

from . import db, prompts as P
from .config import SETTINGS
from .llm import CallCtx, LLMError, call_json
from .textnorm import NEG_EN, NEG_RU, NEG_IGNORE as NEG_IGNORE_RE, has_negation, negation_match, norm_hard_with_map, numbers

REC_MARKERS = re.compile(
    r"\b(recommend\w*|should|must|indicated|advis\w*|suggest\w*|guideline|is preferred|"
    r"consider\w*|warrant\w*|necessary|required|contraindicated)\b|"
    r"(рекоменд\w*|следует|показан\w*|необходим\w*|целесообразн\w*|противопоказ\w*|должн\w*)", re.I)


VERIFY_VERSION = "v2"  # меняется при изменении логики проверок → карточки перепроверяются

# Эвристика → поле вердикта рецензента, которое может её снять (эвристика — триггер,
# рецензент — арбитр). Если эвристика сработала и рецензент её НЕ снял — на проверку.
ADJUDICATED_BY = {"numbers": "numbers_ok", "negation": "negation_ok",
                  "recommendation_marker": "claim_type_ok"}


def _content_hash(c: dict) -> str:
    keys = ("statement_ru", "quote", "page", "polarity", "claim_type", "population", "dose",
            "timing", "thresholds", "conditions", "age_group")
    return hashlib.sha1(json.dumps([VERIFY_VERSION] + [c.get(k) for k in keys],
                                   ensure_ascii=False).encode()).hexdigest()[:16]


@lru_cache(maxsize=512)
def _norm_cached(text: str) -> tuple[str, tuple[int, ...]]:
    """Нормализация страницы кэшируется: раньше её пересчитывали для каждой карточки."""
    hay, hmap = norm_hard_with_map(text)
    return hay, tuple(hmap)


def _find_in(text: str, needle_n: str) -> tuple[float, int, int, tuple[int, ...]]:
    hay, hmap = _norm_cached(text)
    if not hay or not needle_n:
        return 0.0, -1, -1, hmap
    pos = hay.find(needle_n)
    if pos >= 0:
        return 100.0, pos, pos + len(needle_n), hmap
    al = fuzz.partial_ratio_alignment(needle_n, hay)
    if al is None:
        return 0.0, -1, -1, hmap
    return float(al.score), al.dest_start, al.dest_end, hmap


def check_quote(doc_id: str, card: dict) -> dict:
    """Ищет цитату на заявленной странице, затем на остальных и на стыке страниц.
    page_span — точный фрагмент страницы, с которым совпала цитата: по нему
    сверяются числа и отрицания, если совпадение не дословное."""
    quote = card.get("quote") or ""
    needle, _ = norm_hard_with_map(quote)
    pages = {p["page_no"]: p["text"] for p in
             db.q("SELECT page_no, text FROM pages WHERE doc_id=?", [doc_id])}
    claimed = int(card.get("page") or 1)
    res = dict(claimed_page=claimed, found_page=None, score=0.0, status="not_found",
               page_corrected=False, context="", page_span="")
    if len(needle) < 15:
        res["status"] = "too_short"
        return res

    def try_page(n, joined=False):
        if n not in pages:
            return None
        text = pages[n] + ("\n" + pages[n + 1] if joined and n + 1 in pages else "")
        score, s, e, hmap = _find_in(text, needle)
        ctx = span = ""
        if s >= 0 and hmap:
            a, b = hmap[s], hmap[min(e, len(hmap)) - 1]
            ctx = text[max(0, a - 600): b + 600]
            span = text[a: b + 1]
        return score, n, ctx, span

    best = try_page(claimed) or (0.0, claimed, "", "")
    if best[0] < SETTINGS.quote_fuzzy_threshold:
        # цитата может стоять на другой странице (переход фрагмента)
        for n in pages:
            if n == claimed:
                continue
            r = try_page(n)
            if r and r[0] > best[0] + 2:
                best = r
            if best[0] >= 100:
                break
    if best[0] < SETTINGS.quote_fuzzy_threshold:
        # цитата на стыке страниц
        for n in (claimed - 1, claimed):
            r = try_page(n, joined=True)
            if r and r[0] > best[0] + 2:
                best = r
    score, page, ctx, span = best
    res.update(score=round(score, 1), found_page=page, context=ctx, page_span=span)
    if score >= SETTINGS.quote_exact_threshold:
        res["status"] = "exact"
    elif score >= SETTINGS.quote_fuzzy_threshold:
        res["status"] = "fuzzy"
    elif score >= SETTINGS.quote_partial_threshold:
        res["status"] = "partial"
    else:
        res["status"] = "not_found"
        res["found_page"] = None
    if res["found_page"] and res["found_page"] != claimed:
        res["page_corrected"] = True
    return res


_NEG_TOK = re.compile(r"not|no|never|without|neither|нет|не|без")
_OCR_PAIRS = {("°", "1"), ("°", "0"), ("°", "o"), ("%", "7o"), ("%", "0/0"), ("%", "o/o"), ("%", "7"),
              ("=", "1⁄4"), ("=", "¼"), ("=", "5")}
_OCR_CHARS = {"0": "oо", "1": "liі|", "2": "z", "5": "s", "8": "b", "6": "б"}  # цифра ↔ похожие буквы


def _is_glyph(qa: str, pa: str, nspan: str, ds: int, de: int) -> bool:
    """Известные дефекты текстового слоя PDF (не искажение цитаты): минус → «2», тире → «e»,
    «=» → «¼», типичные ошибки OCR («°» → «1», «0» ↔ «o»)."""
    nxt = nspan[de:de + 1]
    prv = nspan[ds - 1:ds] if ds > 0 else ""
    if qa == "" and pa == "2" and nxt.isdigit():
        return True  # «−0.49» → «20.49» (минус в нормализации удалён)
    if qa == "" and pa == "e" and nxt.isdigit() and (prv.isdigit() or prv == "%"):
        return True  # «0.17–0.58» → «0.17e0.58»
    if (qa, pa) in _OCR_PAIRS or (pa, qa) in _OCR_PAIRS:
        return True
    # посимвольная OCR-путаница одинаковой длины: «II» ↔ «11», «O» ↔ «0»
    def same(a, b):
        return a == b or b in _OCR_CHARS.get(a, "") or a in _OCR_CHARS.get(b, "")
    return bool(qa) and len(qa) == len(pa) and all(same(a, b) for a, b in zip(qa, pa))


def _neg_words(text: str) -> Counter:
    """Слова-отрицания с КОЛИЧЕСТВОМ: одно удалённое «not» при втором таком же рядом иначе не видно."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)          # перенос слова
    t = NEG_IGNORE_RE.sub(" ", re.sub(r"\s+", " ", t))          # «lack\nof» → «lack of»
    norm = lambda w: re.sub(r"[\s-]", "", w)                    # «non-inferior» = «noninferior»
    return Counter(norm(m.group(0)) for m in NEG_EN.finditer(t)) + Counter(norm(m.group(0)) for m in NEG_RU.finditer(t))


# слова, замена которых меняет направление вывода: антонимы (по основам) и приставки отрицания
_ANTONYMS = [("increas", "decreas"), ("higher", "lower"), ("more", "less"), ("better", "worse"),
             ("superior", "inferior"), ("improv", "worsen"), ("greater", "smaller"), ("gain", "loss"),
             ("rais", "lower"), ("benefi", "harm"), ("above", "below"), ("maximum", "minimum"),
             ("increas", "reduc"), ("увелич", "уменьш"), ("увелич", "сниж"), ("повыш", "сниж"),
             ("выше", "ниже"), ("больше", "меньше"), ("лучше", "хуже"), ("улучш", "ухудш")]
_NEG_PREFIXES = ("non", "un", "in", "im", "il", "ir", "dis", "не", "без")


def _dir_words(text: str) -> Counter:
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)        # перенос слова
    t = re.sub(r"(?<=[a-zа-яё])-(?=[a-zа-яё])", "", t)       # «non-significant» = «nonsignificant»
    return Counter(re.findall(r"[a-zа-яё]{3,}", t))


def _direction_swaps(quote: str, page: str) -> list[str]:
    """Слова цитаты, которых нет на странице, при этом на странице есть антоним или то же слово
    с приставкой отрицания («significant» ↔ «non-significant», «decreased» ↔ «increased»)."""
    q, p = _dir_words(quote), _dir_words(page)
    q_only, p_only = q - p, p - q
    out = []
    for a in q_only:
        for b in p_only:
            pre = any((x == pf + y or y == pf + x) and len(min(x, y, key=len)) >= 4
                      for x, y in ((a, b),) for pf in _NEG_PREFIXES)
            ant = any((a.startswith(s1) and b.startswith(s2)) or (a.startswith(s2) and b.startswith(s1))
                      for s1, s2 in _ANTONYMS)
            if pre or ant:
                out.append(f"«{a}» в цитате ↔ «{b}» на странице")
    return sorted(out)


_UNITS = {"mg", "mcg", "ug", "g", "kg", "ng", "ml", "l", "dl", "iu", "u", "units", "mmol", "mol", "meq", "h", "hr",
          "hrs", "hours", "hour", "d", "day", "days", "wk", "week", "weeks", "min", "mins", "minutes", "s", "sec",
          "cm", "mm", "m", "%", "мг", "мкг", "г", "кг", "нг", "мл", "л", "дл", "ед", "ме", "ммоль", "моль", "ч",
          "час", "часа", "часов", "сут", "сутки", "суток", "дн", "дня", "дней", "нед", "недели", "недель", "мин",
          "см", "мм", "м", "°", "градусов", "times", "раз", "раза"}
_NUM_UNIT = re.compile(r"([<>≤≥=]?)\s*(\d+(?:[.,]\d+)?)\s*(%|°|[a-zа-яµμ]+(?:\s?/\s?[a-zа-яµμ]+)*)?")


def _unit_pairs(text: str) -> dict[str, set]:
    """Число → набор (знак сравнения, единица). Единица учитывается, только если это слово из словаря
    единиц (иначе после числа идёт обычное слово, и сравнивать нечего)."""
    t = unicodedata.normalize("NFKC", text or "").lower().replace("µ", "u").replace("μ", "u").replace("mcg", "ug")
    t = re.sub(r"\s+", " ", t.replace("¼", "="))
    out: dict[str, set] = {}
    for m in _NUM_UNIT.finditer(t):
        cmp_, num, unit = m.group(1), m.group(2).replace(",", "."), (m.group(3) or "").replace(" ", "")
        parts = unit.split("/") if unit else []
        unit = unit if parts and all(p in _UNITS or p == "ug" for p in parts) else ""
        out.setdefault(num, set()).add((cmp_, unit))
    return out


def _unit_mismatches(quote: str, span: str) -> list[str]:
    """«10 mg» ↔ «10 mcg», «15%» ↔ «15», «<10» ↔ «10», «мг/кг/сут» ↔ «мг/кг» — у числа, которое
    есть и в цитате, и на странице, не совпадает ни один вариант (знак, единица)."""
    q, p = _unit_pairs(quote), _unit_pairs(span)
    res = []
    for num in q.keys() & p.keys():
        # вариант «знак + единица», который есть только с одной стороны (одно верное вхождение
        # числа не должно маскировать другое, искажённое)
        diff = {u for u in (q[num] ^ p[num]) if u != ("", "")}
        if not diff:
            continue
        fmt = lambda xs: ", ".join(sorted(c + num + (" " + u if u else "") for c, u in xs)) or num
        res.append(f"«{fmt(q[num])}» в цитате ↔ «{fmt(p[num])}» на странице")
    return res


def quote_vs_page_checks(quote: str, span: str, context: str) -> list[dict]:
    """Сверка цитаты с местом страницы, куда она легла (запускается и для дословных совпадений)."""
    fix = lambda t: (t or "").replace("¼", "=")  # «¼» в научных PDF — испорченный «=»
    out = []
    # --- границы чисел: дословное совпадение «5 mg» внутри «15 mg» или «0.5 mg» — не совпадение ---
    i = (context or "").find(span or "")
    q0 = (quote or "").strip()
    if i >= 0 and span and q0:
        before, after = context[max(0, i - 2):i], context[i + len(span):i + len(span) + 2]
        cut_start = q0[0].isdigit() and (before[-1:].isdigit() or (before[-1:] in ".," and before[:1].isdigit()))
        cut_end = q0.rstrip(".")[-1:].isdigit() and (after[:1].isdigit() or (after[:1] in ".," and after[1:2].isdigit()))
        if cut_start or cut_end:
            # обрезанное начало («5» внутри «15» / «0.5») — ошибка; продолжение в конце чаще всего
            # приклеенный номер ссылки («3.2⁶¹»), поэтому только на проверку
            out.append(dict(name="quote_number_boundary", ok=False, severity="error" if cut_start else "warning",
                            detail="Число на краю цитаты на странице продолжается («"
                                   + (before + "|" + span[:12] if cut_start else span[-12:] + "|" + after)
                                   + "») — цитата обрезала число"))
        elif q0[0].isdigit() and before[-1:] and before[-1:] in "−-–" and not before[:1].isalnum():
            out.append(dict(name="quote_sign", ok=False, severity="warning",
                            detail="Перед числом в начале цитаты на странице стоит знак минус"))
    # --- отрицания: сравниваем НАБОРЫ слов-отрицаний (посимвольный diff «разбирает» not/без по
    # соседнему тексту). Сторона страницы — найденный фрагмент плюс 12 символов перед ним,
    # чтобы не потерять «not» прямо перед началом цитаты.
    if i >= 0:
        prefix = re.split(r"[.!?;:]", context[max(0, i - 15): i])[-1]  # только в том же предложении
        near = context[max(0, i - 15): i + len(span) + 15]
    else:
        prefix, near = "", span
    # сторона страницы для «пропущено» — не длиннее цитаты: выравнивание может захватить
    # следующее предложение («There was no…»)
    core = (span or "")[: len(quote or "") + 5]
    q_neg = _neg_words(quote)
    added = q_neg - _neg_words(near)                  # в цитате есть, рядом на странице нет
    lost = _neg_words(prefix + " " + core) - q_neg    # на странице в этом месте есть, в цитате нет
    if added or lost:
        out.append(dict(name="quote_negation_vs_page", ok=False, severity="warning",
                        detail="Отрицание в цитате расходится с текстом страницы: "
                               + ("в цитате лишнее «" + ", ".join(sorted(added)) + "»" if added else "")
                               + ("; " if added and lost else "")
                               + ("в цитате пропущено «" + ", ".join(sorted(lost)) + "»" if lost else "")))
    whole = core
    if i >= 0:  # края фрагмента — до границ слов: «ineffective», обрезанное до «effective», не замена
        st, en = i, i + len(core)
        while st > 0 and context[st - 1].isalpha():
            st -= 1
        while en < len(context) and context[en].isalpha():
            en += 1
        whole = context[st:en]
    swaps = _direction_swaps(quote, prefix + " " + whole)
    if swaps:
        out.append(dict(name="quote_direction_vs_page", ok=False, severity="warning",
                        detail="Слово в цитате заменено на противоположное по смыслу (смена направления "
                               "вывода?): " + "; ".join(swaps[:3])))

    # знак минус перед числом: в нормализации дефисы удаляются, поэтому «−0.49» и «0.49» совпали бы
    signed = lambda t: Counter(re.findall(r"(?<![\w.,])[\u2212\u2013-]\s?(\d+(?:[.,]\d+)?)", t or ""))
    q_signed, p_signed = signed(quote), signed(span)
    q_nums = numbers(quote)
    lost_sign = [x for x in (p_signed - q_signed) if numbers(x) <= q_nums]
    extra_sign = [x for x in (q_signed - p_signed) if numbers(x) <= numbers(span)]
    if lost_sign:
        out.append(dict(name="quote_sign", ok=False, severity="error",
                        detail="На странице перед числом стоит минус, в цитате — нет (смена направления "
                               "эффекта?): " + ", ".join(lost_sign[:4])))
    elif extra_sign:
        out.append(dict(name="quote_sign", ok=False, severity="warning",
                        detail="В цитате перед числом минус, на странице — нет (испорченный шрифт PDF?): "
                               + ", ".join(extra_sign[:4])))

    nq = norm_hard_with_map(fix(quote))[0].rstrip(".")  # точка в конце цитаты на месте ссылки
    ns = norm_hard_with_map(fix(span))[0]
    nctx = norm_hard_with_map(fix(context))[0]
    if not nq or not ns:
        return out
    off = nctx.find(ns)
    if off >= 0:
        # сравниваем с расширенным фрагментом: выравнивание могло «откусить» край цитаты,
        # а лишний текст страницы по краям — просто вставки, которые ниже игнорируются
        pad = max(20, len(nq) - len(ns) + 20)
        ns = nctx[max(0, off - pad): off + len(ns) + pad]
    # только вставки/удаления: «замены» Левенштейна склеивали пропущенные ссылки с соседними буквами
    raw_ops = [o for o in Indel.opcodes(nq, ns) if o.tag != "equal"]
    # соседние «удалить X» + «вставить Y» в одной точке — это замена X→Y (нужно для белого списка OCR)
    ops = []
    for o in raw_ops:
        if ops and ops[-1][1] == o.src_start and ops[-1][3] == o.dest_start:
            prev_ss, _, prev_ds, _ = ops[-1]
            ops[-1] = (prev_ss, o.src_end, prev_ds, o.dest_end)
        else:
            ops.append((o.src_start, o.src_end, o.dest_start, o.dest_end))

    def numeric_at(k: int, step: int) -> bool:
        """Позиция k на странице — часть числа (цифра или десятичный разделитель между цифрами)."""
        ch = ns[k:k + 1] if 0 <= k < len(ns) else ""
        if ch.isdigit():
            return True
        nb = ns[k + step:k + step + 1] if 0 <= k + step < len(ns) else ""
        return ch in ".," and nb.isdigit()

    num_err, glyph, layout, units = [], [], [], []
    for ss, se, ds, de in ops:
        qa, pa = nq[ss:se], ns[ds:de]
        # десятичный разделитель между цифрами появился/пропал: «1.5» ↔ «15» — ошибка дозы
        q_dec = qa and set(qa) <= set(".,") and nq[ss - 1:ss].isdigit() and nq[se:se + 1].isdigit()
        p_dec = pa and set(pa) <= set(".,") and ns[ds - 1:ds].isdigit() and ns[de:de + 1].isdigit()
        if q_dec or p_dec:
            num_err.append(f"«{qa or '∅'}» в цитате ↔ «{pa or '∅'}» на странице (десятичный разделитель)")
            continue
        if not qa.strip(".,;:"):
            qa = ""  # в цитате на этом месте только знак препинания (модель поставила точку вместо ссылки)
        if not qa and (ss == 0 or se == len(nq)):
            continue  # текст страницы до начала / после конца цитаты
        if qa and (ss == 0 or se == len(nq)) and len(qa) >= 6 and (
                qa in nctx or (numbers(qa) <= numbers(nctx) and fuzz.partial_ratio(qa, nctx) >= 85)):
            continue  # край цитаты есть рядом на странице (выравнивание его «откусило», ссылки разорвали)
        adjacent = numeric_at(ds - 1, -1) or numeric_at(de, 1)
        if any(ch.isdigit() for ch in qa + pa):
            if not qa and (pa[:1] in "[(" or not adjacent):
                pass  # текст только на странице: ссылки «[24]», «day 10[1]», «(Fig 6)», «surgery8 9 11»
            elif _is_glyph(qa, pa, ns, ds, de):
                glyph.append(f"«{qa}» ↔ «{pa}»")
            elif not qa and not (pa[:1].isdigit() or pa[:1] in ".,"):
                layout.append(f"«∅» ↔ «{pa}»")  # вставка рядом с числом, но начинается не с цифры
            elif not any(ch.isdigit() for ch in qa) and qa:
                # цифры только на стороне страницы: вклинившийся колонтитул, подпись таблицы
                layout.append(f"«{qa}» ↔ «{pa}»")
            else:
                num_err.append(f"«{qa or '∅'}» в цитате ↔ «{pa or '∅'}» на странице")

    if num_err:
        out.append(dict(name="quote_numbers_vs_page", ok=False, severity="error",
                        detail="Цифры цитаты расходятся с текстом страницы (цитата искажена?): " + "; ".join(num_err[:4])))
    if glyph:
        out.append(dict(name="pdf_text_layer", ok=False, severity="warning",
                        detail="Цифры цитаты отличаются от текстового слоя PDF (испорченный шрифт или ошибка "
                               "распознавания скана): сверьте по изображению страницы — " + "; ".join(glyph[:4])))
    if layout and not num_err:
        out.append(dict(name="quote_layout_vs_page", ok=False, severity="warning",
                        detail="В тексте страницы на месте цитаты есть посторонние цифры (колонтитул, подпись "
                               "таблицы?): сверьте по изображению — " + "; ".join(layout[:3])))
    units = _unit_mismatches(quote, span)
    if units:
        out.append(dict(name="quote_units_vs_page", ok=False, severity="warning",
                        detail="У одного и того же числа в цитате и на странице разные единицы или знаки "
                               "сравнения: " + "; ".join(units[:3])))
    return out


def deterministic_checks(card: dict, quote_res: dict) -> list[dict]:
    checks = []
    q = card.get("quote") or ""
    qs = quote_res["status"]
    checks.append(dict(
        name="quote_found", ok=qs in ("exact", "fuzzy"),
        severity="error" if qs in ("not_found", "too_short") else ("warning" if qs == "partial" else "ok"),
        detail={"exact": "Цитата найдена дословно",
                "fuzzy": f"Цитата найдена после нормализации (сходство {quote_res['score']}%)",
                "partial": f"Найдено лишь частичное совпадение ({quote_res['score']}%)",
                "too_short": "Цитата слишком короткая для проверки",
                "not_found": "Цитата не найдена ни на одной странице документа"}[qs]
        + (f"; страница исправлена {quote_res['claimed_page']} → {quote_res['found_page']}"
           if quote_res.get("page_corrected") else "")))

    # Нестрогое совпадение (fuzzy/partial) допускает отличие цитаты от страницы на несколько
    # символов — ровно столько нужно, чтобы «5 mg» стало «50 mg» или исчезло «not». Поэтому
    # цитата посимвольно сравнивается с фрагментом страницы, на который она легла.
    if qs in ("exact", "fuzzy", "partial") and quote_res.get("page_span"):
        checks += quote_vs_page_checks(q, quote_res["page_span"], quote_res.get("context") or "")

    stmt_fields = " ".join(card.get(k) or "" for k in
                           ("statement_ru", "dose", "timing", "thresholds", "conditions"))
    missing = numbers(stmt_fields) - numbers(q)
    in_ctx = missing & numbers(quote_res.get("context") or "")
    nowhere = sorted(missing - in_ctx, key=float)
    if nowhere:
        checks.append(dict(name="numbers_absent", ok=False, severity="warning",
                           detail="Числа формулировки не найдены ни в цитате, ни рядом с ней: " + ", ".join(nowhere)))
    elif in_ctx:
        # доза, срок, порог должны стоять в самой цитате: «рядом» рецензент снять не может
        key = in_ctx & numbers(" ".join(card.get(k) or "" for k in ("dose", "timing", "thresholds")))
        checks.append(dict(name="numbers", ok=False, severity="warning" if key else "soft",
                           detail=("Доза/срок/порог есть рядом с цитатой, но не в ней — цитата не подтверждает "
                                   "параметр: " if key else "Числа есть рядом с цитатой, но не в ней: ")
                                  + ", ".join(sorted(in_ctx, key=float))))
    else:
        checks.append(dict(name="numbers", ok=True, severity="ok", detail="Все числа формулировки есть в цитате"))

    q_neg, s_neg = has_negation(q), has_negation(card.get("statement_ru"))
    pol = card.get("polarity")
    neg_detail, neg_ok = "Отрицания согласованы", True
    if q_neg and not s_neg:
        neg_ok, neg_detail = False, ("В цитате есть отрицание («" +
                                     negation_match(q).group(0) +
                                     "»), в формулировке — нет")
    elif s_neg and not q_neg:
        neg_ok, neg_detail = False, "В формулировке есть отрицание, в цитате — нет"
    elif pol == "negative" and not (q_neg or s_neg):
        neg_ok, neg_detail = False, "Полярность negative, но отрицания нет ни в цитате, ни в формулировке"
    checks.append(dict(name="negation", ok=neg_ok, severity="ok" if neg_ok else "soft",
                       detail=neg_detail))

    if card.get("claim_type") == "source_recommendation":
        ok = bool(REC_MARKERS.search(q))
        checks.append(dict(name="recommendation_marker", ok=ok, severity="ok" if ok else "soft",
                           detail="В цитате есть рекомендательная формулировка" if ok else
                           "Тип «рекомендация источника», но в цитате нет рекомендательного маркера "
                           "(возможно, результат исследования выдан за рекомендацию)"))
    if not (card.get("intervention") or "").strip() and card.get("claim_type") != "background_fact":
        checks.append(dict(name="intervention_empty", ok=False, severity="info",
                           detail="Не указано вмешательство/метод"))
    return checks


def _status(checks: list[dict], judge: dict | None) -> tuple[str, list[str]]:
    """Итоговый статус. severity: error — блокирует всегда (цитата не найдена);
    warning — на проверку; soft — эвристика, которую может снять рецензент;
    info — только для сведения."""
    if any(c["severity"] == "error" and not c["ok"] for c in checks):
        return "unverified", [c["detail"] for c in checks if not c["ok"]]
    if judge is None or judge.get("error"):
        why = f"Смысловая проверка не выполнена: {judge['error']}" if judge else "Смысловая проверка не выполнялась"
        return "needs_review", [c["detail"] for c in checks if not c["ok"] and c["severity"] != "info"] + [why]
    blocking = []
    for c in checks:
        if c["ok"] or c["severity"] == "info":
            continue
        if c["severity"] == "soft" and judge.get(ADJUDICATED_BY.get(c["name"], "")) is True:
            c["resolved_by_judge"] = True  # эвристика сработала, рецензент подтвердил корректность
            continue
        blocking.append(c["detail"])
    issues = blocking + list(judge.get("issues") or [])
    if judge["entailment"] == "not_supported":
        return "unverified", issues or ["Рецензент: цитата не подтверждает формулировку"]
    bad_flags = [k for k in ("numbers_ok", "negation_ok", "population_ok", "claim_type_ok") if judge.get(k) is False]
    for k in bad_flags:
        issues.append({"numbers_ok": "Рецензент: числа не совпадают",
                       "negation_ok": "Рецензент: искажено отрицание/направление",
                       "population_ok": "Рецензент: искажена группа пациентов",
                       "claim_type_ok": "Рецензент: результат исследования выдан за рекомендацию"}[k])
    if judge["entailment"] == "partial" or bad_flags or blocking:
        return "needs_review", list(dict.fromkeys(issues))
    return "verified", []


JUDGE_KEYS = ("statement_ru", "quote", "polarity", "claim_type", "population", "dose",
              "timing", "thresholds", "conditions", "age_group")


def _judge_hash(c: dict) -> str:
    """Отпечаток полей, которые видит рецензент (без номера страницы): вердикт
    переиспользуется, только если эти поля не менялись."""
    return hashlib.sha1(json.dumps([c.get(k) for k in JUDGE_KEYS], ensure_ascii=False).encode()).hexdigest()[:16]


def _reusable_judge(old: dict, card: dict) -> dict | None:
    j = (old or {}).get("judge")
    if not j or j.get("error"):
        return None
    if old.get("judge_hash"):
        return j if old["judge_hash"] == _judge_hash(card) else None
    # проверки, сделанные до появления judge_hash: старый отпечаток включал номер страницы,
    # которую прежняя логика могла исправить в данных модели, — сравниваем с обоими вариантами
    claimed = (old.get("quote") or {}).get("claimed_page")
    if old.get("hash") in (_content_hash(card), _content_hash({**card, "page": claimed})):
        return j
    return None


def recheck(row: dict) -> tuple[dict, bool]:
    """Детерминированная перепроверка карточки (без LLM). Возвращает (verification,
    нужен ли рецензент). Номер страницы в данных модели НЕ меняется: найденная страница
    хранится в verification.quote.found_page."""
    card = db.effective(row)
    old = row.get("verification") or {}
    qres = check_quote(row["doc_id"], card)
    checks = deterministic_checks(card, qres)
    judge = _reusable_judge(old, card)
    ver = dict(hash=_content_hash(card), judge_hash=_judge_hash(card), quote=qres, checks=checks,
               judge=judge, ts=time.time())
    status, issues = _status(checks, judge)
    if judge is None and old.get("judge") and row.get("override"):
        issues = issues + ["Карточка изменена экспертом после смысловой проверки — нужна перепроверка"]
    ver["issues"] = issues
    db.update("cards", row["id"], verification=ver, status=status, updated_at=time.time())
    need = judge is None and qres["status"] in ("exact", "fuzzy", "partial") and \
        card.get("topic_relevance") != "none" and \
        not any(c["severity"] in ("error",) and not c["ok"] for c in checks)
    return ver, need


async def verify_cards(project_id: str, ctx: CallCtx, use_judge: bool = True,
                       force: bool = False) -> None:
    # проверяются все карточки, включая повторы: их числа и дизайн участвуют в синтезе тезисов
    rows = db.q("SELECT * FROM cards WHERE project_id=?", [project_id])
    to_judge = []
    for row in rows:
        if force:
            row = dict(row, verification=None)
        ver, need = recheck(row)
        if use_judge and need:
            to_judge.append((row["id"], db.effective(row), ver))
    batches = [to_judge[i:i + SETTINGS.judge_batch] for i in range(0, len(to_judge), SETTINGS.judge_batch)]
    await asyncio.gather(*(_judge_batch(b, ctx) for b in batches))


def _judge_context(ctx: str, size: int = 1000) -> str:
    """Рецензенту — окно 1000 символов (~±500) вокруг цитаты (цитата в середине сохранённого
    контекста ±600). Полный контекст нужен только детерминированной проверке чисел;
    рецензенту он почти не добавлял качества, но был главной статьёй входных токенов."""
    if len(ctx) <= size:
        return ctx
    start = max(0, len(ctx) // 2 - size // 2)
    return ctx[start:start + size]


async def _judge_batch(batch: list, ctx: CallCtx) -> None:
    items = []
    for cid, card, ver in batch:
        doc = db.q1("SELECT biblio FROM documents WHERE id=(SELECT doc_id FROM cards WHERE id=?)", [cid])
        b = (doc or {}).get("biblio") or {}
        items.append(dict(
            study=f"{b.get('title', '')}; дизайн {b.get('study_design', '?')}; популяция: {b.get('population', '?')}",
            card_id=cid, statement_ru=card.get("statement_ru"), claim_type=card.get("claim_type"),
            polarity=card.get("polarity"), population=card.get("population"),
            age_group=card.get("age_group"), dose=card.get("dose"), timing=card.get("timing"),
            thresholds=card.get("thresholds"), conditions=card.get("conditions"),
            quote=card.get("quote"), context=_judge_context(ver["quote"].get("context") or "")))
    prompt = "Проверь карточки:\n" + json.dumps(items, ensure_ascii=False, indent=1)
    try:
        res = await call_json("judge", P.JUDGE_SYSTEM, prompt, P.JUDGE_SCHEMA, ctx,
                              model=SETTINGS.judge_model)
        by_id = {r["card_id"]: r for r in res.get("results", [])}
    except LLMError as e:
        by_id = {cid: {"error": str(e)} for cid, _, _ in batch}
    for cid, card, ver in batch:
        j = by_id.get(cid) or {"error": "рецензент не вернул вердикт по карточке"}
        row = db.q1("SELECT * FROM cards WHERE id=?", [cid])
        if not row or _judge_hash(db.effective(row)) != ver["judge_hash"]:
            continue  # карточку изменили, пока шла проверка, — вердикт устарел, не записываем
        ver["judge"] = j
        status, issues = _status(ver["checks"], j)
        ver["issues"] = issues
        db.update("cards", cid, verification=ver, status=status, updated_at=time.time())
