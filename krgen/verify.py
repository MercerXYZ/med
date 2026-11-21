"""Проверка доказательств: цитата на странице PDF, числа, отрицание, затем модель-рецензент."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from rapidfuzz import fuzz

from . import db, prompts as P
from .config import SETTINGS
from .llm import CallCtx, LLMError, call_json
from .textnorm import negation_word, norm_hard_with_map, numbers

# поля, от которых зависит вердикт рецензента: пока они те же, вердикт переиспользуется
JUDGE_KEYS = ("statement_ru", "quote", "subject", "predicate", "object", "negation", "population", "age_group",
              "dose", "timing", "conditions")
PARAM_KEYS = ("statement_ru", "dose", "timing", "conditions")


def _find(text: str, needle: str) -> tuple[float, int, int, list[int]]:
    """Сходство цитаты с текстом страницы и её положение в исходном тексте."""
    hay, hmap = norm_hard_with_map(text)
    if not hay or not needle:
        return 0.0, -1, -1, hmap
    pos = hay.find(needle)
    if pos >= 0:
        return 100.0, pos, pos + len(needle), hmap
    if len(hay) < len(needle):  # короткая страница: частичное сравнение нашло бы её внутри цитаты
        return fuzz.ratio(needle, hay), 0, len(hay), hmap
    al = fuzz.partial_ratio_alignment(needle, hay)
    return al.score, al.dest_start, al.dest_end, hmap


def check_quote(doc_id: str, card: dict) -> dict:
    """Ищет цитату на указанной странице, потом на остальных и на стыке двух страниц."""
    needle = norm_hard_with_map(card.get("quote") or "")[0]
    pages = {p["page_no"]: p["text"] for p in db.q("SELECT page_no, text FROM pages WHERE doc_id=?", [doc_id])}
    claimed = int(card.get("page") or 1)
    res = dict(claimed_page=claimed, found_page=None, score=0.0, status="not_found", context="")
    if len(needle) < 15:
        res["status"] = "too_short"
        return res

    def try_text(n: int, text: str) -> tuple[float, int, str]:
        score, s, e, hmap = _find(text, needle)
        ctx = ""
        if s >= 0 and hmap:
            ctx = text[max(0, hmap[s] - 600): hmap[max(s, e - 1)] + 600]
        return score, n, ctx

    order = [claimed] + [n for n in pages if n != claimed]
    best = (0.0, None, "")
    for n in order:
        if n in pages:
            best = max(best, try_text(n, pages[n]), key=lambda x: x[0])
        if best[0] >= SETTINGS.quote_fuzzy_threshold:
            break
    else:
        for n in (claimed - 1, claimed):  # цитата на стыке страниц
            if n in pages and n + 1 in pages:
                best = max(best, try_text(n, pages[n] + "\n" + pages[n + 1]), key=lambda x: x[0])
    score, page, ctx = best
    res.update(score=round(score, 1), found_page=page, context=ctx)
    if score >= 100:
        res["status"] = "exact"
    elif score >= SETTINGS.quote_fuzzy_threshold:
        res["status"] = "fuzzy"
    elif score >= SETTINGS.quote_partial_threshold:
        res["status"] = "partial"
    else:
        res["found_page"] = None
    return res


QUOTE_TEXT = {"exact": "Цитата найдена в документе дословно",
              "fuzzy": "Цитата найдена в документе (сходство {score}%)",
              "partial": "Цитата найдена лишь частично (сходство {score}%)",
              "too_short": "Цитата слишком короткая для проверки",
              "not_found": "Цитата не найдена в документе"}


def checks_for(card: dict, quote: dict) -> list[dict]:
    """Проверки без модели. judge — поле вердикта рецензента, которое может снять замечание."""
    out = [dict(name="quote", ok=quote["status"] in ("exact", "fuzzy"),
                text=QUOTE_TEXT[quote["status"]].format(score=quote["score"])
                + (f", страница {quote['found_page']}" if quote["found_page"] not in (None, quote["claimed_page"])
                   else ""))]
    q = card.get("quote") or ""
    missing = numbers(" ".join(card.get(k) or "" for k in PARAM_KEYS)) - numbers(q)
    out.append(dict(name="numbers", ok=not missing, judge="numbers_ok",
                    text="Числа формулировки есть в цитате" if not missing
                    else "Числа формулировки не найдены в цитате: " + ", ".join(sorted(missing, key=float))))
    q_neg, s_neg = negation_word(q), negation_word(card.get("statement_ru"))
    text = "Отрицания в цитате и формулировке совпадают"
    if q_neg and not s_neg:
        text = f"В цитате есть отрицание («{q_neg}»), в формулировке нет"
    elif s_neg and not q_neg:
        text = "В формулировке есть отрицание, в цитате нет"
    elif card.get("negation") and not q_neg:
        text = "Доказательство отмечено как отрицательное, но в цитате нет отрицания"
    out.append(dict(name="negation", ok=text.startswith("Отрицания"), judge="negation_ok", text=text))
    return out


JUDGE_ISSUES = {"numbers_ok": "Рецензент: числа не совпадают с источником",
                "negation_ok": "Рецензент: искажено отрицание или направление вывода",
                "population_ok": "Рецензент: искажена группа пациентов"}


def status_for(quote: dict, checks: list[dict], judge: dict | None) -> tuple[str, list[str]]:
    """Статус доказательства: verified, needs_review или unverified, и список замечаний."""
    if quote["status"] in ("not_found", "too_short"):
        return "unverified", [checks[0]["text"]]
    if judge is None or judge.get("error"):
        issues = [c["text"] for c in checks if not c["ok"]]
        return "needs_review", issues + [f"Смысловая проверка не выполнена: {judge['error']}" if judge
                                         else "Смысловая проверка не выполнялась"]
    issues = []
    for c in checks:
        c["cleared"] = not c["ok"] and judge.get(c.get("judge")) is True  # рецензент ошибки не нашёл
        if not c["ok"] and not c["cleared"]:
            issues.append(c["text"])
    issues += [text for k, text in JUDGE_ISSUES.items() if judge.get(k) is False]
    issues += judge.get("issues") or []
    if judge["entailment"] == "not_supported":
        return "unverified", issues or ["Рецензент: цитата не подтверждает формулировку"]
    if judge["entailment"] == "partial" or issues:
        return "needs_review", list(dict.fromkeys(issues))
    return "verified", []


def _judge_hash(card: dict) -> str:
    return hashlib.sha1(json.dumps([card.get(k) for k in JUDGE_KEYS], ensure_ascii=False).encode()).hexdigest()[:16]


def recheck(row: dict) -> tuple[dict, bool]:
    """Перепроверка без модели → (результат, нужен ли рецензент)."""
    card = db.effective(row)
    old = row.get("verification") or {}
    quote = check_quote(row["doc_id"], card)
    checks = checks_for(card, quote)
    jh = _judge_hash(card)
    judge = old.get("judge") if old.get("judge_hash") == jh and not (old.get("judge") or {}).get("error") else None
    status, issues = status_for(quote, checks, judge)
    ver = dict(judge_hash=jh, quote=quote, checks=checks, judge=judge, issues=issues, ts=time.time())
    db.update("cards", row["id"], verification=ver, status=status, updated_at=time.time())
    need = judge is None and quote["status"] in ("exact", "fuzzy", "partial") and card.get("topic_relevance") != "none"
    return ver, need


async def verify_cards(project_id: str, ctx: CallCtx, use_judge: bool = True) -> None:
    to_judge = []
    for row in db.q("SELECT * FROM cards WHERE project_id=?", [project_id]):
        ver, need = recheck(row)
        if use_judge and need:
            to_judge.append((row, ver))
    n = SETTINGS.judge_batch
    await asyncio.gather(*(_judge_batch(to_judge[i:i + n], ctx) for i in range(0, len(to_judge), n)))


async def _judge_batch(batch: list[tuple[dict, dict]], ctx: CallCtx) -> None:
    docs = {d["id"]: d.get("biblio") or {} for d in db.q("SELECT id, biblio FROM documents WHERE id IN (%s)" % ",".join(
        "?" * len(batch)), [row["doc_id"] for row, _ in batch])}
    items = []
    for row, ver in batch:
        card, b = db.effective(row), docs.get(row["doc_id"]) or {}
        ctx_text = ver["quote"]["context"]
        if len(ctx_text) > 1000:  # цитата в середине контекста
            ctx_text = ctx_text[len(ctx_text) // 2 - 500: len(ctx_text) // 2 + 500]
        items.append(dict(card_id=row["id"], **{k: card.get(k) for k in JUDGE_KEYS}, context=ctx_text,
                          study=f"{b.get('title', '')}; дизайн {b.get('study_design', '?')}; "
                                f"популяция: {b.get('population', '?')}"))
    prompt = "Проверь доказательства:\n" + json.dumps(items, ensure_ascii=False, indent=1)
    try:
        res = await call_json("judge", P.JUDGE_SYSTEM, prompt, P.JUDGE_SCHEMA, ctx)
        by_id = {r["card_id"]: r for r in res["results"]}
    except LLMError as e:
        by_id = {row["id"]: {"error": str(e)} for row, _ in batch}
    for row, ver in batch:
        cur = db.q1("SELECT * FROM cards WHERE id=?", [row["id"]])
        if not cur or _judge_hash(db.effective(cur)) != ver["judge_hash"]:
            continue  # доказательство изменили, пока шла проверка
        ver["judge"] = by_id.get(row["id"]) or {"error": "рецензент не вернул вердикт"}
        status, ver["issues"] = status_for(ver["quote"], ver["checks"], ver["judge"])
        db.update("cards", row["id"], verification=ver, status=status, updated_at=time.time())
