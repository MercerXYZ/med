"""Группировка фактов внутри раздела КР и поиск противоречий."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

from . import db, prompts as P
from .config import SETTINGS
from .llm import CallCtx, LLMError, call_json

MERGE_BATCH = 70  # групп на один вызов group_merge


def source_key(doc_id: str, biblio: dict) -> str:
    """Независимый источник = публикация, а не файл: DOI, иначе нормализованное название."""
    doi = (biblio.get("doi") or "").lower().strip()
    title = re.sub(r"[^a-zа-я0-9]", "", (biblio.get("title") or "").lower())
    return "doi:" + doi if doi else ("title:" + title if len(title) >= 30 else "doc:" + doc_id)


def live_facts(project_id: str) -> dict[str, dict]:
    docs = {d["id"]: d for d in db.q("SELECT id, filename, status, biblio FROM documents WHERE project_id=?",
                                     [project_id])}
    out = {}
    for r in db.q("SELECT * FROM cards WHERE project_id=? ORDER BY id", [project_id]):
        if r.get("user_status") == "rejected":
            continue
        st = "confirmed" if r.get("user_status") == "confirmed" else r["status"]
        doc = docs.get(r["doc_id"]) or {}
        c = db.effective(r)
        if st not in ("verified", "confirmed") or doc.get("status") == "duplicate" \
                or c.get("topic_relevance") == "none":
            continue
        b = doc.get("biblio") or {}
        c.update(_id=r["id"], _doc=r["doc_id"], _status=st, _src=source_key(r["doc_id"], b),
                 # пересказ чужой работы: дизайн этой статьи к нему неприменим
                 _design=None if c.get("secondary_citation") else b.get("study_design"),
                 _year=b.get("year"), _title=b.get("title") or doc.get("filename"),
                 _filename=doc.get("filename"))
        out[r["id"]] = c
    return out


def group_statement(row: dict) -> str:
    st = (row.get("override") or {}).get("statement") or row.get("statement") or row.get("topic")
    if st:
        return st
    for cid in row.get("card_ids") or []:
        r = db.q1("SELECT auto, override FROM cards WHERE id=?", [cid])
        if r:
            return db.effective(r).get("statement_ru") or ""
    return ""


def group_id(card_ids: list[str]) -> str:
    return "gr_" + hashlib.sha1("|".join(sorted(card_ids)).encode()).hexdigest()[:10]


def conflict_id(side_a: list[str], side_b: list[str]) -> str:
    return "cf_" + hashlib.sha1((",".join(side_a) + "|" + ",".join(side_b)).encode()).hexdigest()[:10]


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _age(c: dict) -> str:
    a = c.get("age_group")
    return a if a in ("children", "adults") else "other"


def _dir(c: dict) -> str:
    return "neg" if c.get("polarity") == "negative" else "pos"


def _violations(ids: list[str], facts: dict) -> tuple[bool, bool]:
    ages = {_age(facts[i]) for i in ids}
    return {"children", "adults"} <= ages, len({_dir(facts[i]) for i in ids}) > 1


def _first_statement(ids: list[str], facts: dict) -> str:
    return facts[ids[0]].get("statement_ru") or ""


def _guard(ids: list[str], statement: str, facts: dict) -> list[dict]:
    bad_age, bad_dir = _violations(ids, facts)
    if not (bad_age or bad_dir):
        return [dict(ids=ids, statement=statement or _first_statement(ids, facts))]
    parts: dict[tuple, list[str]] = {}
    for i in ids:
        key = (_age(facts[i]) if bad_age else "", _dir(facts[i]) if bad_dir else "")
        parts.setdefault(key, []).append(i)
    return [dict(ids=p, statement=_first_statement(p, facts)) for p in parts.values()]


def _conflict(cf: dict, allowed: set[str]) -> dict | None:
    a = [i for i in dict.fromkeys(cf.get("side_a") or []) if i in allowed]
    b = [i for i in dict.fromkeys(cf.get("side_b") or []) if i in allowed and i not in a]
    if not a or not b:
        return None
    kind = cf.get("kind") if cf.get("kind") in P.CONFLICT_KINDS else "other"
    return dict(side_a=a, side_b=b, kind=kind, description=(cf.get("description") or "").strip())


def _fact_item(i: str, c: dict) -> dict:
    d = dict(id=i, statement=c.get("statement_ru"), intervention=c.get("intervention"),
             population=c.get("population"), age=c.get("age_group"), polarity=c.get("polarity"),
             type=c.get("claim_type"), dose=c.get("dose"), timing=c.get("timing"), outcome=c.get("outcome"))
    return {k: v for k, v in d.items() if v}


async def _group_batch(section: str, part: list[str], facts: dict, ctx: CallCtx) -> tuple[list, list]:
    if len(part) == 1:
        return [dict(ids=part, statement=_first_statement(part, facts))], []
    items = [_fact_item(i, facts[i]) for i in part]
    prompt = (f"Раздел КР {section} «{P.SECTIONS.get(section, '')}». Факты:\n"
              + json.dumps(items, ensure_ascii=False, indent=0))
    res = await call_json("group", P.GROUP_SYSTEM, prompt, P.GROUP_SCHEMA, ctx)
    allowed, used, groups = set(part), set(), []
    for g in res.get("groups", []):
        requested = list(dict.fromkeys(g.get("card_ids") or []))
        ids = [i for i in requested if i in allowed and i not in used]
        if not ids:
            continue
        used.update(ids)
        # формулировка модели верна только для того набора, под который написана
        statement = (g.get("statement") or "").strip() if ids == requested else ""
        groups += _guard(ids, statement, facts)
    for i in part:  # потерянные моделью факты — отдельными группами
        if i not in used:
            groups.append(dict(ids=[i], statement=_first_statement([i], facts)))
    conflicts = [c for c in (_conflict(cf, allowed) for cf in res.get("conflicts", [])) if c]
    return groups, conflicts


async def _merge_groups(section: str, groups: list[dict], facts: dict, ctx: CallCtx) -> tuple[list, list]:
    for g in groups:
        g["id"] = group_id(g["ids"])
    by_id = {g["id"]: g for g in groups}
    orig = {g["id"]: list(g["ids"]) for g in groups}
    order = sorted(groups, key=lambda g: ((facts[g["ids"][0]].get("intervention") or "").lower(), g["id"]))
    chunks = _chunks(order, MERGE_BATCH)

    async def call(chunk):
        items = [dict(id=g["id"], statement=g["statement"][:300], n_facts=len(g["ids"]), part=g["part"] + 1)
                 for g in chunk]
        prompt = (f"Раздел КР {section} «{P.SECTIONS.get(section, '')}». Группы:\n"
                  + json.dumps(items, ensure_ascii=False, indent=0))
        return await call_json("group_merge", P.GROUP_MERGE_SYSTEM, prompt, P.GROUP_MERGE_SCHEMA, ctx)

    results = await asyncio.gather(*(call(c) for c in chunks))
    used, dropped, conflicts = set(), set(), []
    for chunk, res in zip(chunks, results):
        allowed = {g["id"] for g in chunk}
        for lst in res.get("merge", []):
            cand = [by_id[x] for x in dict.fromkeys(lst) if x in allowed and x not in used]
            if len(cand) < 2:
                continue
            used.update(g["id"] for g in cand)
            cand.sort(key=lambda g: -len(g["ids"]))
            head, parts = cand[0], {cand[0]["part"]}
            for g in cand[1:]:  # внутри одной части решение уже принято пачкой
                if g["part"] not in parts and not any(_violations(head["ids"] + g["ids"], facts)):
                    head["ids"] = head["ids"] + g["ids"]
                    parts.add(g["part"])
                    dropped.add(g["id"])
        for cf in res.get("conflicts", []):
            ga = [x for x in dict.fromkeys(cf.get("side_a") or []) if x in allowed]
            gb = [x for x in dict.fromkeys(cf.get("side_b") or []) if x in allowed and x not in ga]
            if len({by_id[x]["part"] for x in ga + gb}) < 2:
                continue  # внутри одной части противоречия уже найдены
            c = _conflict(dict(cf, side_a=[i for x in ga for i in orig[x]], side_b=[i for x in gb for i in orig[x]]),
                          set(facts))
            if c:
                conflicts.append(c)
    return [g for g in groups if g["id"] not in dropped], conflicts


def _section_conflicts(project_id: str, section: str, card_ids: set[str]) -> list[dict]:
    """Автоматические противоречия раздела без решения эксперта."""
    out = []
    for cf in db.q("SELECT id, section, card_ids, decided_by FROM conflicts WHERE project_id=? AND origin='auto'",
                   [project_id]):
        if cf.get("decided_by") == "expert":
            continue
        if cf.get("section") == section or (not cf.get("section") and card_ids & set(cf.get("card_ids") or [])):
            out.append(cf)
    return out


def _store(project_id: str, section: str, facts: dict, groups: list[dict], conflicts: list[dict],
           run_id: str | None) -> None:
    old = db.q("SELECT * FROM clusters WHERE project_id=? AND section=? AND origin='auto'", [project_id, section])
    rows = [dict(id=group_id(g["ids"]), project_id=project_id, section=section,
                 topic=facts[g["ids"][0]].get("intervention") or "", statement=g["statement"],
                 card_ids=g["ids"], duplicate_groups=None, override=None, excluded=0,
                 origin="auto", run_id=run_id) for g in groups]
    carried: dict[str, int] = {}
    for o in old:
        if not (o.get("override") or o.get("excluded")):
            continue
        oids = set(o.get("card_ids") or []) & set(facts)
        if not oids:
            continue
        best, n = max(((r, len(oids & set(r["card_ids"]))) for r in rows), key=lambda x: x[1], default=(None, 0))
        if best is None or n * 2 < len(oids) or carried.get(best["id"], 0) >= n:
            continue
        carried[best["id"]] = n
        best.update(override=o.get("override"), excluded=o.get("excluded") or 0)

    sec_ids = set(facts) | {i for o in old for i in o.get("card_ids") or []}
    stale = _section_conflicts(project_id, section, sec_ids)
    expert = []
    for c in db.q("SELECT card_ids, side_a, side_b FROM conflicts WHERE project_id=? AND decided_by='expert'",
                  [project_id]):
        x, y = set(c.get("side_a") or []), set(c.get("side_b") or [])
        expert.append((x, y) if x and y else (set(c.get("card_ids") or []),) * 2)  # старые записи без сторон

    def decided(a: list[str], b: list[str]) -> bool:
        # эксперт уже решил это противоречие, возможно с фактами, которых больше нет
        sa, sb = set(a), set(b)
        return any(sa <= x and sb <= y or sa <= y and sb <= x for x, y in expert)

    with db.tx():
        db.ex("DELETE FROM clusters WHERE project_id=? AND section=? AND origin='auto'", [project_id, section])
        for r in rows:
            db.upsert("clusters", r)
        for cf in stale:
            db.ex("DELETE FROM conflicts WHERE id=?", [cf["id"]])
        for cf in conflicts:
            a, b = sorted(cf["side_a"]), sorted(cf["side_b"])
            if a > b:
                a, b = b, a
            if decided(a, b):
                continue
            cid = conflict_id(a, b)
            prev = db.q1("SELECT decided_by, created_at FROM conflicts WHERE id=?", [cid]) or {}
            row = dict(id=cid, project_id=project_id, section=section, card_ids=a + b, side_a=a, side_b=b,
                       kind=cf["kind"], description=cf["description"], origin="auto", run_id=run_id,
                       created_at=prev.get("created_at") or time.time())
            if prev.get("decided_by") != "expert":
                row.update(decision=None, decided_by=None, reason="", status="open", resolution=None)
            db.upsert("conflicts", row)


async def _group_section(project_id: str, section: str, facts: dict, ctx: CallCtx) -> None:
    ids = sorted(facts, key=lambda i: ((facts[i].get("intervention") or "").lower(), i))
    parts = _chunks(ids, SETTINGS.cluster_batch)
    results = await asyncio.gather(*(_group_batch(section, p, facts, ctx) for p in parts), return_exceptions=True)
    for r in results:
        if isinstance(r, LLMError):
            return  # прежние группы и противоречия раздела сохраняются
        if isinstance(r, BaseException):
            raise r
    groups, conflicts = [], []
    for k, (gs, cfs) in enumerate(results):
        for g in gs:
            g["part"] = k
        groups += gs
        conflicts += cfs
    if len(parts) > 1 and len(groups) > 1:
        try:
            groups, more = await _merge_groups(section, groups, facts, ctx)
        except LLMError:
            return
        conflicts += more
    _store(project_id, section, facts, groups, conflicts, ctx.run_id)


async def group_project(project_id: str, ctx: CallCtx) -> None:
    facts = live_facts(project_id)
    by_sec: dict[str, dict[str, dict]] = {}
    for cid, c in facts.items():
        by_sec.setdefault(c.get("kr_section") or "7", {})[cid] = c
    await asyncio.gather(*(_group_section(project_id, s, fs, ctx) for s, fs in by_sec.items()))
    # разделы, где живых фактов больше нет
    for row in db.q("SELECT DISTINCT section FROM clusters WHERE project_id=? AND origin='auto'", [project_id]):
        if row["section"] not in by_sec:
            db.ex("DELETE FROM clusters WHERE project_id=? AND section=? AND origin='auto'", [project_id, row["section"]])
    for cf in db.q("SELECT id, section, card_ids, decided_by FROM conflicts WHERE project_id=? AND origin='auto'",
                   [project_id]):
        if cf.get("decided_by") == "expert":
            continue
        if (cf.get("section") and cf["section"] not in by_sec) or \
                (not cf.get("section") and not set(cf.get("card_ids") or []) & set(facts)):
            db.ex("DELETE FROM conflicts WHERE id=?", [cf["id"]])
