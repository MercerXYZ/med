"""Раздел КР для каждого доказательства: отдельный запрос с описанием разделов и темой КР."""
from __future__ import annotations

import asyncio
import json

from . import db, prompts as P
from .llm import CallCtx, LLMError, call_json

BATCH = 40
AGE = {"adults": "взрослые", "children": "дети", "mixed": "взрослые и дети"}


def _facts_by_doc(project_id: str) -> dict[str, list[tuple[str, dict]]]:
    out: dict[str, list] = {}
    for r in db.q("SELECT c.id, c.doc_id, c.auto, c.override FROM cards c JOIN documents d ON d.id=c.doc_id "
                  "WHERE c.project_id=? AND d.status!='duplicate' AND COALESCE(c.user_status, '')!='rejected' "
                  "ORDER BY c.id", [project_id]):
        c = db.effective(r)
        if c.get("topic_relevance") != "none":
            out.setdefault(r["doc_id"], []).append((r["id"], c))
    return out


async def _classify(project: dict, part: list[tuple[str, dict]], ctx: CallCtx) -> None:
    items = [dict(id=i, statement=c.get("statement_ru"), subject=c.get("subject"), population=c.get("population"))
             for i, c in part]
    prompt = (f"КР: «{project['title']}». Коды МКБ-10: {', '.join(project.get('icd_codes') or [])}. "
              f"Возрастная группа: {AGE.get(project.get('age_category'), 'не указана')}.\n"
              f"Факты:\n{json.dumps(items, ensure_ascii=False, indent=0)}")
    try:
        res = await call_json("classify", P.CLASSIFY_SYSTEM, prompt, P.CLASSIFY_SCHEMA, ctx)
    except LLMError:
        return  # у доказательств остаётся прежний раздел
    ids = {i for i, _ in part}
    for r in res["results"]:
        if r["id"] in ids:
            db.update("cards", r["id"], section=r["section"])


async def classify_project(project: dict, ctx: CallCtx) -> None:
    # пачки внутри документа: новая статья не сбивает кэш ответов по уже обработанным
    parts = [fs[i:i + BATCH] for fs in _facts_by_doc(project["id"]).values() for i in range(0, len(fs), BATCH)]
    await asyncio.gather(*(_classify(project, p, ctx) for p in parts))
