"""Раздел КР для каждого факта: отдельный запрос с описанием разделов и темой КР."""
from __future__ import annotations

import asyncio
import json

from . import db, prompts as P
from .llm import CallCtx, LLMError, call_json

BATCH = 40
AGE = {"adults": "взрослые", "children": "дети", "mixed": "взрослые и дети"}


def _facts_by_doc(project_id: str) -> dict[str, list[tuple[str, dict]]]:
    dup = {r["id"] for r in db.q("SELECT id FROM documents WHERE project_id=? AND status='duplicate'", [project_id])}
    out: dict[str, list] = {}
    for r in db.q("SELECT id, doc_id, auto, override, user_status FROM cards WHERE project_id=? ORDER BY id",
                  [project_id]):
        c = db.effective(r)
        if r["doc_id"] in dup or r.get("user_status") == "rejected" or c.get("topic_relevance") == "none":
            continue
        out.setdefault(r["doc_id"], []).append((r["id"], c))
    return out


async def _classify(project: dict, part: list[tuple[str, dict]], ctx: CallCtx) -> None:
    items = [dict(id=i, statement=c.get("statement_ru") or "", type=c.get("claim_type"),
                  intervention=c.get("intervention") or "", population=c.get("population") or "")
             for i, c in part]
    prompt = (f"КР: «{project['title']}». Коды МКБ-10: {', '.join(project.get('icd_codes') or []) or 'не указаны'}. "
              f"Возрастная группа: {AGE.get(project.get('age_category'), 'не указана')}.\n"
              f"Факты:\n{json.dumps(items, ensure_ascii=False, indent=0)}")
    try:
        res = await call_json("classify", P.CLASSIFY_SYSTEM, prompt, P.CLASSIFY_SCHEMA, ctx)
    except LLMError:
        return  # остаётся прежний раздел (или раздел из извлечения)
    ids = {i for i, _ in part}
    for r in res.get("results", []):
        sec = r.get("section")
        if r.get("id") in ids and (sec in P.SECTIONS or sec == P.UNASSIGNED):
            db.update("cards", r["id"], cls_section=sec)


async def classify_project(project: dict, ctx: CallCtx) -> None:
    # пачки внутри документа: новая статья не сбивает кэш ответов по уже обработанным
    parts = [fs[i:i + BATCH] for fs in _facts_by_doc(project["id"]).values() for i in range(0, len(fs), BATCH)]
    await asyncio.gather(*(_classify(project, p, ctx) for p in parts))
