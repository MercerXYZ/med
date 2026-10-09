"""Второй проход: объединение похожих тезисов внутри раздела КР.

Группировка карточек идёт пакетами, поэтому один и тот же тезис (например,
«дексаметазон периоперационно для снижения боли») может получиться из
нескольких кластеров. Здесь LLM по краткому списку тезисов раздела предлагает
группы-повторы, код проверяет «стражей» (одно направление, взрослые ≠ дети) и
пересобирает объединённый тезис через ту же функцию _finalize: УДД, УУР,
проверка чисел и популяции считаются заново по объединённым доказательствам.
Исходные тезисы не удаляются — помечаются merged_into и видны эксперту.
"""
from __future__ import annotations

import asyncio
import json

from . import db, prompts as P
from .llm import CallCtx, LLMError, call_json
from .synth import _evidence, _finalize
from .textnorm import numbers

MERGE_BATCH = 70  # тезисов раздела на один вызов (кратко: id + формулировка)


def _ages(ev: list[dict]) -> set[str]:
    return {c.get("age_group") for c in ev} - {"unspecified", "mixed", None}


async def _merge_section(project: dict, section: str, rows: list[dict], ctx: CallCtx) -> set[str]:
    produced: set[str] = set()
    for i in range(0, len(rows), MERGE_BATCH):
        part = rows[i:i + MERGE_BATCH]
        items = [dict(id=r["id"], statement=db.effective(r)["statement"][:260],
                      n_sources=len(r.get("evidence_ids") or [])) for r in part]
        prompt = (f"Раздел КР {section} «{P.SECTIONS.get(section, '')}». Тезисы:\n"
                  + json.dumps(items, ensure_ascii=False, indent=0))
        try:
            res = await call_json("merge", P.MERGE_SYSTEM, prompt, P.MERGE_SCHEMA, ctx)
        except LLMError:
            continue  # без объединения — тезисы остаются как есть
        by_id = {r["id"]: r for r in part}
        used: set[str] = set()
        for g in res.get("groups", []):
            ids = [t for t in dict.fromkeys(g.get("thesis_ids", [])) if t in by_id and t not in used]
            if len(ids) < 2:
                continue
            members = [by_id[t] for t in ids]
            auto = [db.effective(m) for m in members]
            if len({a.get("direction") for a in auto}) > 1:
                continue  # «делать» и «не делать» — это противоречие, а не повтор
            if len({frozenset(numbers(a.get("body") or "")) for a in auto}) > 1:
                continue  # разные дозы/сроки/пороги («1 г» и «2 г») — разные тезисы
            evs = {m["id"]: _evidence(project["id"], {"card_ids": m.get("evidence_ids") or []}) for m in members}
            ages = [_ages(e) for e in evs.values()]
            if any(a and b and a.isdisjoint(b) for a in ages for b in ages):
                continue  # взрослые и дети не объединяются
            canon = max(members, key=lambda m: (len(m.get("evidence_ids") or []),
                                                -(db.effective(m).get("udd") or 9)))
            synth_res = (canon.get("auto") or {}).get("_synth")
            if not synth_res:
                continue
            union, seen = [], set()
            for m in members:
                for c in evs[m["id"]]:
                    if c["_id"] not in seen:
                        seen.add(c["_id"])
                        union.append(c)
            if not union:
                continue
            rid = _finalize(project, {"id": canon.get("cluster_id"), "section": section}, union, synth_res, ctx, relink=False)
            row = db.q1("SELECT review_flags FROM theses WHERE id=?", [rid])
            flags = list(row["review_flags"] or [])
            flags.append(f"Объединено из {len(members)} тезисов: {g.get('reason', '')}"[:300])
            row = db.q1("SELECT auto FROM theses WHERE id=?", [rid])
            auto_m = dict(row["auto"] or {})
            auto_m["merged_from"] = [m["id"] for m in members]
            db.update("theses", rid, review_flags=flags, merged_into=None, auto=auto_m)
            for m in members:
                if m["id"] != rid:
                    db.update("theses", m["id"], merged_into=rid)
            used.update(ids)
            produced.add(rid)
    return produced


async def merge_project(project: dict, ctx: CallCtx) -> None:
    pid = project["id"]
    db.ex("UPDATE theses SET merged_into=NULL WHERE project_id=?", [pid])
    # результаты прошлого прохода объединения без правок эксперта пересобираются заново
    for t in db.q("SELECT id, auto, override, status FROM theses WHERE project_id=?", [pid]):
        if (t["auto"] or {}).get("merged_from") and not t["override"] and t["status"] == "draft":
            db.ex("DELETE FROM theses WHERE id=?", [t["id"]])
    # утверждённые и отредактированные экспертом тезисы не объединяем: объединённый тезис
    # строится из ответа модели, и правка эксперта была бы потеряна
    rows = db.q("SELECT * FROM theses WHERE project_id=? AND provenance!='manual' AND orphan=0 "
                "AND status='draft' AND override IS NULL ORDER BY section, id", [pid])
    by_sec: dict[str, list] = {}
    for r in rows:
        if (r.get("auto") or {}).get("kind") == "narrative":
            continue
        by_sec.setdefault(r["section"], []).append(r)
    by_sec = {s: rs for s, rs in by_sec.items() if len(rs) > 1}
    await asyncio.gather(*(_merge_section(project, s, rs, ctx) for s, rs in by_sec.items()))
