"""Объединение повторов и выявление противоречий.

LLM предлагает кластеры, дубликаты и конфликты внутри раздела КР, а
детерминированные «стражи» запрещают опасные слияния, о которых говорит ТЗ:
взрослые ≠ дети, разные числа (доза/порог/срок) ≠ повтор, разное направление ≠
повтор (это кандидат в противоречие). Объединение не удаляет карточки:
дубликат помечается merged_into=<каноническая>, все источники сохраняются.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from . import db, prompts as P
from .config import SETTINGS
from .llm import CallCtx, LLMError, call_json
from .textnorm import numbers

ELIGIBLE = ("verified", "needs_review")


def _age_compatible(a: str, b: str) -> bool:
    """Взрослые и дети не объединяются никогда; «не указано» — только со взрослыми/смешанной."""
    return a == b or ("unspecified" in (a, b) and "children" not in (a, b))


def _direction(c: dict) -> str:
    return "neg" if c.get("polarity") == "negative" else "pos"


def guard_duplicate(a: dict, b: dict) -> str | None:
    """Причина, по которой две карточки НЕЛЬЗЯ считать повтором (или None)."""
    if not _age_compatible(a.get("age_group", "unspecified"), b.get("age_group", "unspecified")):
        return f"разные возрастные группы ({a.get('age_group')} / {b.get('age_group')})"
    if _direction(a) != _direction(b):
        return "разное направление утверждения"
    na = numbers(" ".join(a.get(k) or "" for k in ("statement_ru", "dose", "timing", "thresholds")))
    nb = numbers(" ".join(b.get(k) or "" for k in ("statement_ru", "dose", "timing", "thresholds")))
    if na and nb and na != nb:
        return f"разные числовые параметры ({', '.join(sorted(na))} / {', '.join(sorted(nb))})"
    return None


def _cid(prefix: str, ids: list[str]) -> str:
    return prefix + hashlib.sha1("|".join(sorted(ids)).encode()).hexdigest()[:10]


async def _cluster_section(project_id: str, section: str, cards: dict[str, dict], ctx: CallCtx) -> None:
    ids = sorted(cards, key=lambda i: (cards[i].get("intervention") or "", i))
    parts = [ids[i:i + SETTINGS.cluster_batch] for i in range(0, len(ids), SETTINGS.cluster_batch)]
    results = []
    for part in parts:
        if len(part) == 1:
            results.append((part, [{"topic": cards[part[0]].get("intervention") or "",
                                    "card_ids": part, "duplicate_groups": []}], []))
            continue
        items = [dict(id=i, statement=cards[i].get("statement_ru"),
                      intervention=cards[i].get("intervention"),
                      population=cards[i].get("population"), age=cards[i].get("age_group"),
                      polarity=cards[i].get("polarity"), type=cards[i].get("claim_type"),
                      dose=cards[i].get("dose"), timing=cards[i].get("timing"))
                 for i in part]
        prompt = (f"Раздел КР {section} «{P.SECTIONS.get(section, '')}». Карточки:\n"
                  + json.dumps(items, ensure_ascii=False, indent=0))
        try:
            res = await call_json("cluster", P.CLUSTER_SYSTEM, prompt, P.CLUSTER_SCHEMA, ctx)
        except LLMError:
            return  # сбой API: прежняя группировка раздела сохраняется, ошибка — в журнале вызовов
        results.append((part, res.get("clusters", []), res.get("conflicts", [])))
    # пересборка автоматической группировки раздела; ручные объединения эксперта сохраняются
    db.ex("DELETE FROM clusters WHERE project_id=? AND section=? AND origin='auto'", [project_id, section])
    # открытые автоматические противоречия раздела пересобираются; разрешённые экспертом остаются
    sec_ids = set(ids)
    for cf in db.q("SELECT id, card_ids FROM conflicts WHERE project_id=? AND origin='auto' AND status='open'",
                   [project_id]):
        if sec_ids & set(cf["card_ids"] or []):
            db.ex("DELETE FROM conflicts WHERE id=?", [cf["id"]])
    for i in ids:
        db.ex("UPDATE cards SET merged_into=NULL WHERE id=? AND (merge_lock IS NULL OR merge_lock!='merged')", [i])
    for part, clusters, conflicts in results:
        _store(project_id, section, part, cards, clusters, conflicts, ctx.run_id)


def _store(project_id, section, part, cards, clusters, conflicts, run_id):
    assigned = set()
    for cl in clusters:
        members = [i for i in cl.get("card_ids", []) if i in cards and i not in assigned]
        if not members:
            continue
        assigned.update(members)
        dup_groups, rejected = [], []
        for grp in cl.get("duplicate_groups", []):
            grp = [i for i in grp if i in members]
            if len(grp) < 2:
                continue
            canon = max(grp, key=lambda i: (cards[i].get("_status") == "verified",
                                            len(cards[i].get("quote") or "")))
            ok = [canon]
            for i in grp:
                if i == canon:
                    continue
                why = guard_duplicate(cards[canon], cards[i])
                if why:
                    rejected.append({"card_ids": [canon, i], "reason": why})
                else:
                    ok.append(i)
            if len(ok) > 1:
                dup_groups.append(ok)
        cid = _cid("cl_", members)
        db.upsert("clusters", dict(id=cid, project_id=project_id, section=section,
                                   topic=cl.get("topic", ""), card_ids=members,
                                   duplicate_groups={"groups": dup_groups, "rejected": rejected},
                                   origin="auto", run_id=run_id))
        for g in dup_groups:
            for i in g[1:]:
                row = db.q1("SELECT merge_lock FROM cards WHERE id=?", [i])
                if row and row["merge_lock"] != "unmerged":  # эксперт мог разъединить
                    db.update("cards", i, merged_into=g[0], updated_at=time.time())
    # карточки, которые модель потеряла, — отдельными кластерами (ничего не пропадает)
    for i in part:
        if i not in assigned:
            db.upsert("clusters", dict(id=_cid("cl_", [i]), project_id=project_id, section=section,
                                       topic=cards[i].get("intervention") or "", card_ids=[i],
                                       duplicate_groups={"groups": [], "rejected": []},
                                       origin="auto", run_id=run_id))
    for cf in conflicts:
        ids = [i for i in cf.get("card_ids", []) if i in cards]
        if len(ids) < 2:
            continue
        cfid = _cid("cf_", ids)
        old = db.q1("SELECT status, resolution FROM conflicts WHERE id=?", [cfid])
        db.upsert("conflicts", dict(id=cfid, project_id=project_id, card_ids=ids,
                                    description=cf.get("description", ""), kind=cf.get("kind", "other"),
                                    status=old["status"] if old else "open",
                                    resolution=old["resolution"] if old else None,
                                    origin="auto", run_id=run_id, created_at=time.time()))


async def cluster_project(project_id: str, ctx: CallCtx) -> None:
    rows = db.q("SELECT * FROM cards WHERE project_id=? AND (user_status IS NULL OR user_status!='rejected') "
                "AND doc_id NOT IN (SELECT id FROM documents WHERE project_id=? AND status='duplicate')",
                [project_id, project_id])
    by_sec: dict[str, dict[str, dict]] = {}
    for r in rows:
        c = db.effective(r)
        st = "verified" if r.get("user_status") == "confirmed" else r["status"]
        if st not in ELIGIBLE or c.get("topic_relevance") == "none":
            continue
        c["_status"] = st
        by_sec.setdefault(c.get("kr_section") or "7", {})[r["id"]] = c
    await asyncio.gather(*(_cluster_section(project_id, s, cs, ctx) for s, cs in by_sec.items()))
    # разделы, где больше нет пригодных карточек
    for row in db.q("SELECT DISTINCT section FROM clusters WHERE project_id=? AND origin='auto'", [project_id]):
        if row["section"] not in by_sec:
            db.ex("DELETE FROM clusters WHERE project_id=? AND section=? AND origin='auto'", [project_id, row["section"]])
