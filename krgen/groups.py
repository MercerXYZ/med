"""Группировка доказательств внутри раздела КР и поиск противоречий."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

from . import db, prompts as P
from .config import SETTINGS
from .llm import CallCtx, LLMError, call_json

MERGE_BATCH = 70  # групп на один запрос group_merge


def source_key(doc_id: str, biblio: dict) -> str:
    """Независимый источник — публикация, а не файл: DOI, иначе название."""
    doi = (biblio.get("doi") or "").lower().strip()
    title = re.sub(r"[^a-zа-я0-9]", "", (biblio.get("title") or "").lower())
    return "doi:" + doi if doi else ("title:" + title if len(title) >= 30 else "doc:" + doc_id)


def live_facts(project_id: str) -> dict[str, dict]:
    """Доказательства, которые идут в группы: подтверждены, с разделом, не из дубликата."""
    docs = {d["id"]: d for d in db.q("SELECT id, filename, status, biblio FROM documents WHERE project_id=?",
                                     [project_id])}
    out = {}
    for r in db.q("SELECT * FROM cards WHERE project_id=? ORDER BY id", [project_id]):
        doc = docs[r["doc_id"]]
        c = db.effective(r)
        ok = r["user_status"] == "confirmed" or (r["status"] == "verified" and r["user_status"] is None)
        if not ok or doc["status"] == "duplicate" or c.get("topic_relevance") == "none" \
                or c.get("kr_section") in (None, P.UNASSIGNED):
            continue
        b = doc["biblio"] or {}
        c.update(_id=r["id"], _doc=r["doc_id"], _src=source_key(r["doc_id"], b),
                 # пересказ чужой работы: дизайн этой статьи к нему не относится
                 _design=None if c.get("secondary_citation") else b.get("study_design"),
                 _year=b.get("year"), _title=b.get("title") or doc["filename"])
        out[r["id"]] = c
    return out


def group_statement(row: dict) -> str:
    return (row.get("override") or {}).get("statement") or row.get("statement") or ""


def group_id(card_ids: list[str]) -> str:
    return "gr_" + hashlib.sha1("|".join(sorted(card_ids)).encode()).hexdigest()[:10]


def conflict_id(side_a: list[str], side_b: list[str]) -> str:
    return "cf_" + hashlib.sha1((",".join(side_a) + "|" + ",".join(side_b)).encode()).hexdigest()[:10]


def _mixed(ids: list[str], facts: dict) -> tuple[bool, bool]:
    """В одной группе не бывает взрослых вместе с детьми и разных направлений вывода."""
    ages = {facts[i].get("age_group") for i in ids}
    return {"adults", "children"} <= ages, len({bool(facts[i].get("negation")) for i in ids}) > 1


def _split(ids: list[str], statement: str, facts: dict) -> list[dict]:
    bad_age, bad_dir = _mixed(ids, facts)
    if not (bad_age or bad_dir):
        return [dict(ids=ids, statement=statement or facts[ids[0]]["statement_ru"])]
    parts: dict[tuple, list[str]] = {}
    for i in ids:
        key = (facts[i].get("age_group") if bad_age else "", bool(facts[i].get("negation")) if bad_dir else False)
        parts.setdefault(key, []).append(i)
    return [dict(ids=p, statement=facts[p[0]]["statement_ru"]) for p in parts.values()]


def _conflict(cf: dict, allowed: set[str]) -> dict | None:
    a = [i for i in dict.fromkeys(cf["side_a"]) if i in allowed]
    b = [i for i in dict.fromkeys(cf["side_b"]) if i in allowed and i not in a]
    if a and b:
        return dict(side_a=a, side_b=b, kind=cf["kind"], description=cf["description"].strip())
    return None


def _fact_item(i: str, c: dict) -> dict:
    keys = ("statement_ru", "subject", "predicate", "object", "negation", "population", "age_group", "dose", "timing")
    return {"id": i, **{k: c[k] for k in keys if c.get(k) not in (None, "")}}


async def _group_part(section: str, part: list[str], facts: dict, ctx: CallCtx) -> tuple[list, list]:
    if len(part) == 1:
        return [dict(ids=part, statement=facts[part[0]]["statement_ru"])], []
    prompt = (f"Раздел КР {section} «{P.SECTIONS[section]}». Факты:\n"
              + json.dumps([_fact_item(i, facts[i]) for i in part], ensure_ascii=False, indent=0))
    res = await call_json("group", P.GROUP_SYSTEM, prompt, P.GROUP_SCHEMA, ctx)
    allowed, used, groups = set(part), set(), []
    for g in res["groups"]:
        ids = [i for i in dict.fromkeys(g["card_ids"]) if i in allowed and i not in used]
        if ids:
            used.update(ids)
            groups += _split(ids, g["statement"].strip(), facts)
    groups += [dict(ids=[i], statement=facts[i]["statement_ru"]) for i in part if i not in used]
    conflicts = [c for c in (_conflict(cf, allowed) for cf in res["conflicts"]) if c]
    return groups, conflicts


async def _merge_parts(section: str, groups: list[dict], facts: dict, ctx: CallCtx) -> tuple[list, list]:
    """Слияние одинаковых групп из разных частей раздела."""
    by_id = {group_id(g["ids"]): g for g in groups}
    order = sorted(by_id, key=lambda gid: ((facts[by_id[gid]["ids"][0]].get("subject") or "").lower(), gid))
    batches = [order[i:i + MERGE_BATCH] for i in range(0, len(order), MERGE_BATCH)]

    async def call(batch):
        items = [dict(id=gid, statement=by_id[gid]["statement"][:300], part=by_id[gid]["part"] + 1) for gid in batch]
        prompt = (f"Раздел КР {section} «{P.SECTIONS[section]}». Группы:\n"
                  + json.dumps(items, ensure_ascii=False, indent=0))
        return await call_json("group_merge", P.GROUP_MERGE_SYSTEM, prompt, P.GROUP_MERGE_SCHEMA, ctx)

    results = await asyncio.gather(*(call(b) for b in batches))
    dropped, conflicts = set(), []
    for batch, res in zip(batches, results):
        allowed = set(batch)
        for lst in res["merge"]:
            cand = [x for x in dict.fromkeys(lst) if x in allowed and x not in dropped]
            if len(cand) < 2:
                continue
            head = by_id[cand[0]]
            parts = {head["part"]}
            for x in cand[1:]:  # внутри одной части решение уже принято
                g = by_id[x]
                if g["part"] not in parts and not any(_mixed(head["ids"] + g["ids"], facts)):
                    head["ids"] = head["ids"] + g["ids"]
                    parts.add(g["part"])
                    dropped.add(x)
        for cf in res["conflicts"]:
            ga = [x for x in cf["side_a"] if x in allowed]
            gb = [x for x in cf["side_b"] if x in allowed]
            if len({by_id[x]["part"] for x in ga + gb}) < 2:
                continue  # противоречия внутри части уже найдены
            c = _conflict(dict(cf, side_a=[i for x in ga for i in by_id[x]["ids"]],
                               side_b=[i for x in gb for i in by_id[x]["ids"]]), set(facts))
            if c:
                conflicts.append(c)
    return [g for gid, g in by_id.items() if gid not in dropped], conflicts


def _store(project_id: str, section: str, groups: list[dict], conflicts: list[dict], run_id: str | None) -> None:
    """Записывает группы раздела; правки эксперта переносятся на группу с теми же фактами."""
    old = db.q("SELECT * FROM fact_groups WHERE project_id=? AND section=?", [project_id, section])
    rows = [dict(id=group_id(g["ids"]), project_id=project_id, section=section, statement=g["statement"],
                 card_ids=g["ids"], override=None, excluded=0, run_id=run_id) for g in groups]
    # группа, где осталась хотя бы половина прежних фактов, сохраняет id и правки эксперта:
    # от id группы зависит id тезиса, поэтому тезис и его статус тоже сохраняются
    taken = set()
    for o in sorted(old, key=lambda o: -len(o["card_ids"])):
        overlap = lambda r: len(set(r["card_ids"]) & set(o["card_ids"]))  # noqa: E731
        best = max((r for r in rows if r["id"] not in taken), key=overlap, default=None)
        if best and 2 * overlap(best) >= len(o["card_ids"]):
            best.update(id=o["id"], override=o["override"], excluded=o["excluded"])
            taken.add(o["id"])

    # противоречия, решённые экспертом, остаются; остальные пересоздаются
    expert = [(set(c["side_a"]), set(c["side_b"])) for c in
              db.q("SELECT side_a, side_b FROM conflicts WHERE project_id=? AND decided_by='expert'", [project_id])]
    with db.tx():
        db.ex("DELETE FROM fact_groups WHERE project_id=? AND section=?", [project_id, section])
        for r in rows:
            db.upsert("fact_groups", r)
        db.ex("DELETE FROM conflicts WHERE project_id=? AND section=? AND COALESCE(decided_by, '')!='expert'",
              [project_id, section])
        for cf in conflicts:
            a, b = sorted([sorted(cf["side_a"]), sorted(cf["side_b"])])
            if any(set(a) <= x and set(b) <= y or set(a) <= y and set(b) <= x for x, y in expert):
                continue
            db.upsert("conflicts", dict(id=conflict_id(a, b), project_id=project_id, section=section, side_a=a,
                                        side_b=b, kind=cf["kind"], description=cf["description"], decision=None,
                                        decided_by=None, reason="", run_id=run_id, created_at=time.time()))


async def _group_section(project_id: str, section: str, facts: dict, ctx: CallCtx) -> None:
    ids = sorted(facts, key=lambda i: ((facts[i].get("subject") or "").lower(), i))
    n = SETTINGS.cluster_batch
    parts = [ids[i:i + n] for i in range(0, len(ids), n)]
    try:
        results = await asyncio.gather(*(_group_part(section, p, facts, ctx) for p in parts))
        groups, conflicts = [], []
        for k, (gs, cfs) in enumerate(results):
            groups += [dict(g, part=k) for g in gs]
            conflicts += cfs
        if len(parts) > 1:
            groups, more = await _merge_parts(section, groups, facts, ctx)
            conflicts += more
    except LLMError:
        return  # при сбое модели остаются прежние группы раздела
    _store(project_id, section, groups, conflicts, ctx.run_id)


async def group_project(project_id: str, ctx: CallCtx) -> None:
    by_sec: dict[str, dict[str, dict]] = {}
    for cid, c in live_facts(project_id).items():
        by_sec.setdefault(c["kr_section"], {})[cid] = c
    await asyncio.gather(*(_group_section(project_id, s, fs, ctx) for s, fs in by_sec.items()))
    # разделы, где подтверждённых доказательств не осталось
    marks = ",".join("?" * len(by_sec))
    db.ex(f"DELETE FROM fact_groups WHERE project_id=? AND section NOT IN ({marks})", [project_id, *by_sec])
    db.ex(f"DELETE FROM conflicts WHERE project_id=? AND section NOT IN ({marks}) "
          "AND COALESCE(decided_by, '')!='expert'", [project_id, *by_sec])
