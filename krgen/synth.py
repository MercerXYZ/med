"""Сборка КР из групп доказательств: тезисы (recommendation_thesis онтологии) с УДД и УУР
и абзацы описательных разделов."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

from . import db, prompts as P
from .grading import (STAGE, UDD_RATIONALE, intervention_type_for, preliminary_uur, scale_for, strength_phrase,
                      udd_for)
from .groups import group_statement, live_facts
from .llm import CallCtx, LLMError, call_json
from .resolve import conflicting, excluded_fact_ids
from .textnorm import numbers

NARRATIVE_MAX_FACTS = 40  # доказательств описательного раздела на один запрос
SYNTH_BATCH = 6           # групп (будущих тезисов) на один запрос
FACTS_PER_GROUP = 12      # доказательств группы в промпте; в тезис идут все


def kr_evidence(project_id: str) -> dict[str, list[dict]]:
    """Группа → её доказательства, которые идут в КР (без исключённых групп и проигравших сторон)."""
    facts = live_facts(project_id)
    excluded = excluded_fact_ids(project_id)
    out = {}
    for g in db.q("SELECT id, card_ids FROM fact_groups WHERE project_id=? AND excluded=0 ORDER BY section, id",
                  [project_id]):
        ev = [facts[i] for i in g["card_ids"] if i in facts and i not in excluded]
        if ev:
            out[g["id"]] = ev
    return out


def thesis_id(key: str) -> str:
    return "rec_" + hashlib.sha1(key.encode()).hexdigest()[:10]


def _pick(ev: list[dict], section: str) -> list[dict]:
    """Доказательства для промпта: по одному на источник, сильные вперёд."""
    order = sorted(ev, key=lambda c: (udd_for(c["_design"], section) or 9, c["_id"]))
    first, rest, seen = [], [], set()
    for c in order:
        (rest if c["_src"] in seen else first).append(c)
        seen.add(c["_src"])
    return (first + rest)[:FACTS_PER_GROUP]


def _save(project_id: str, rid: str, section: str, group_id: str, ids: list[str], auto: dict,
          flags: list[str], run_id: str | None) -> str:
    """Записывает автоверсию тезиса; правленый или утверждённый тезис получает её в pending_auto."""
    now = time.time()
    old = db.q1("SELECT auto, override, status FROM theses WHERE id=?", [rid])
    base = dict(project_id=project_id, group_id=group_id, evidence_ids=ids, review_flags=flags, orphan=0,
                run_id=run_id, updated_at=now)
    if old is None:
        db.upsert("theses", dict(id=rid, section=section, auto=auto, status="draft", provenance="auto",
                                 created_at=now, **base))
    elif (old["override"] or old["status"] == "approved") and old["auto"] != auto:
        db.update("theses", rid, pending_auto=auto, **base)
    else:
        db.update("theses", rid, auto=auto, section=section, **base)
    return rid


def _quote_numbers(ev: list[dict]) -> set[str]:
    out = set()
    for c in ev:
        out |= numbers(c.get("quote") or "")
    return out


async def synth_narrative(project: dict, section: str, groups: list[tuple[dict, list[dict]]],
                          ctx: CallCtx) -> set[str] | None:
    """Разделы 1.x и 7: абзацы со ссылками на доказательства, без УДД и УУР."""
    group_of = {c["_id"]: g["id"] for g, ev in groups for c in ev}
    facts_of = {g["id"]: ev for g, ev in groups}
    batches, cur, n = [], [], 0
    for g, ev in groups:
        shown = _pick(ev, section)
        if cur and n + len(shown) > NARRATIVE_MAX_FACTS:
            batches.append(cur)
            cur, n = [], 0
        cur.append((g, shown))
        n += len(shown)
    if cur:
        batches.append(cur)
    produced = set()
    for batch in batches:
        payload = [dict(statement=g["_statement"],
                        facts=[dict(id=c["_id"], statement=c["statement_ru"], age=c.get("age_group")) for c in shown])
                   for g, shown in batch]
        prompt = (f"КР: «{project['title']}». Раздел {section} «{P.SECTIONS[section]}».\n"
                  f"Группы фактов:\n{json.dumps(payload, ensure_ascii=False, indent=0)}")
        try:
            res = await call_json("narrative", P.NARRATIVE_SYSTEM, prompt, P.NARRATIVE_SCHEMA, ctx)
        except LLMError:
            return None
        shown_ids = {c["_id"] for _, shown in batch for c in shown}
        for para in res["paragraphs"]:
            text = para["paragraph"].strip()
            if not text or para["insufficient"]:
                continue
            # ссылки только на показанные модели доказательства
            text = re.sub(r"\s*\[([^\]]*)\]", lambda m: (
                " [" + ", ".join(i for i in re.findall(r"ev_[0-9a-f]+", m.group(1)) if i in shown_ids) + "]"
                if set(re.findall(r"ev_[0-9a-f]+", m.group(1))) & shown_ids else ""), text).strip()
            used = (set(para["fact_ids"]) | set(re.findall(r"ev_[0-9a-f]+", text))) & shown_ids
            if not used:
                continue
            # остальные доказательства тех же групп утверждают то же самое
            ev = [c for gid in sorted({group_of[i] for i in used}) for c in facts_of[gid]]
            extra = sorted(numbers(re.sub(r"\[[^\]]*\]", "", text)) - _quote_numbers(ev)
                           - {n for c in ev for n in numbers(c["statement_ru"])})
            flags = ["Числа не найдены в цитатах: " + ", ".join(extra)] if extra else []
            auto = dict(kind="narrative", kr_section=para["section"], subsection_title=para["topic"], body=text,
                        statement=text)
            rid = thesis_id("|".join(sorted(used)) + para["topic"])
            produced.add(_save(project["id"], rid, para["section"], group_of[min(used)], sorted(c["_id"] for c in ev),
                               auto, flags, ctx.run_id))
    return produced


def _items(ev: list[dict], section: str) -> list[dict]:
    out = []
    for c in _pick(ev, section):
        source = f"{c['_title']} ({c['_year'] or 'год не указан'}), {c['_design'] or 'дизайн не определён'}"
        d = dict(id=c["_id"], statement=c["statement_ru"], quote=(c.get("quote") or "")[:300], negation=c.get("negation"),
                 population=c.get("population"), age=c.get("age_group"), dose=c.get("dose"), timing=c.get("timing"),
                 conditions=c.get("conditions"), source=source)
        out.append({k: v for k, v in d.items() if v not in (None, "")})
    return out


async def synth_batch(project: dict, section: str, batch: list[tuple[dict, list[dict]]],
                      conflicts: dict[str, list[str]], ctx: CallCtx) -> set[str] | None:
    """Несколько групп одного раздела → по тезису на группу за один запрос."""
    payload = [dict(group_id=g["id"], statement=g["_statement"], facts=_items(ev, section)) for g, ev in batch]
    prompt = (f"КР: «{project['title']}», возрастная группа КР: {project['age_category']}.\n"
              f"Раздел {section} «{P.SECTIONS[section]}». Сформулируй по одному тезису на каждую группу.\n"
              f"Группы:\n{json.dumps(payload, ensure_ascii=False, indent=0)}")
    try:
        res = await call_json("synth", P.SYNTH_SYSTEM, prompt, P.SYNTH_SCHEMA, ctx)
    except LLMError:
        return None
    by_group = {t["group_id"]: t for t in res["theses"]}
    produced = set()
    for g, ev in batch:
        t = by_group.get(g["id"])
        if t is None:  # модель пропустила группу: прежний тезис остаётся
            produced |= {r["id"] for r in db.q("SELECT id FROM theses WHERE group_id=?", [g["id"]])}
        elif not t["insufficient"] and len(t["body"].strip()) >= 15:
            produced.add(_thesis(project, g, ev, t, conflicts, ctx))
    return produced


def _thesis(project: dict, group: dict, ev: list[dict], res: dict, conflicts: dict[str, list[str]],
            ctx: CallCtx) -> str:
    section = group["section"]
    ids = [c["_id"] for c in ev]
    flags = []

    # УДД — лучший среди источников; у источника — лучший среди его доказательств
    per_src: dict[str, int] = {}
    graded = []
    for c in ev:
        u = udd_for(c["_design"], section)
        if u:
            per_src[c["_src"]] = min(u, per_src.get(c["_src"], 9))
            graded.append(c)
    udd = min(per_src.values()) if per_src else None
    if udd is None:
        flags.append("УДД не определён")
    others = sorted({o for i in ids for o in conflicts.get(i, []) if o not in ids})
    consensus = dict(agreement="conflicting" if others else "consistent", conflicting_refs=others)
    uur, why = preliminary_uur(list(per_src.values()), bool(others))
    if uur == "A" and graded and all(c.get("topic_relevance") == "indirect" for c in graded):
        uur, why = "B", why + "; понижен до B: доказательства косвенные"

    # число, которого нет в цитатах, — экспертное добавление, а не доказательство
    quoted = _quote_numbers(ev)
    additions = [dict(claim=a["claim"], basis=a["basis"], refs=[r for r in a["refs"] if r in ids])
                 for a in res["expert_additions"]]
    extra = sorted(numbers(res["body"] + " " + res["body_genitive"]) - quoted)
    if extra:
        flags.append("Числа не найдены в цитатах: " + ", ".join(extra))
        additions.append(dict(claim="Числовые значения " + ", ".join(extra) + " в формулировке тезиса",
                              basis="expert_consensus", refs=[]))
    comment = res["comment"].strip()
    if numbers(comment) - quoted:
        comment = ""

    direction = res["direction"]
    n_neg = sum(bool(c.get("negation")) for c in ev)
    if (direction == "negative" and not n_neg) or (direction == "positive" and n_neg == len(ev)):
        flags.append("Направление тезиса не совпадает с доказательствами")
    phrase = strength_phrase(direction, uur)
    body = (res["body_genitive"] if phrase == "Рекомендуется рассмотреть возможность" and res["body_genitive"]
            else res["body"]).strip().rstrip(".")
    if body[:1].isupper() and not body[:3].isupper():  # «КТ» оставляем как есть
        body = body[0].lower() + body[1:]
    if direction == "negative" and re.match(r"(не|нет)\s", body, re.I):
        flags.append("Двойное отрицание в тезисе")

    itype = intervention_type_for(section)
    iv = res["intervention"]
    qc = res["quality_criterion"]
    qc_text = qc["criterion_text"].strip() if qc["is_criterion"] else ""
    n_src = len({c["_src"] for c in ev})
    auto = dict(
        kr_section=section, subsection_title=res["subsection_title"], strength_phrase=phrase, body=body,
        statement=f"{phrase} {body}.", direction=direction,
        intervention=dict(type=itype, scale_ref=scale_for(itype), action=iv["action"],
                          target_population=iv["target_population"], comparator=iv["comparator"] or None,
                          outcome=iv["outcome"], mnn=iv["mnn"] or None, atc_code=None),
        evidence_backed_core=res["evidence_backed_core"], expert_additions=additions, comment=comment,
        udd=udd, uur=uur,
        grade_rationale=(f"УДД {udd}: {UDD_RATIONALE[udd]}; " if udd else "УДД не определён; ")
        + f"УУР {uur or '—'}: {why}; источников: {n_src}",
        consensus=consensus,
        quality_criterion=dict(
            is_criterion=bool(qc_text), criterion_text=qc_text or None, applicable_stage=STAGE[itype],
            measurable=qc["measurable"],
            auto_eligible=bool(qc_text) and qc["measurable"] and uur in ("A", "B")
            and itype in ("diagnostic", "therapeutic")),
    )
    return _save(project["id"], thesis_id(group["id"]), section, group["id"], ids, auto, flags, ctx.run_id)


async def build_kr(project: dict, ctx: CallCtx) -> None:
    pid = project["id"]
    started = time.time()  # правки эксперта позже этого момента в сборку не попали
    groups = {g["id"]: g for g in db.q("SELECT * FROM fact_groups WHERE project_id=?", [pid])}
    conflicts = conflicting(pid)
    narrative: dict[str, list] = {}
    recs: dict[str, list] = {}
    for gid, ev in kr_evidence(pid).items():
        g = dict(groups[gid], _statement=group_statement(groups[gid]))
        (narrative if g["section"] in P.NARRATIVE_SECTIONS else recs).setdefault(g["section"], []).append((g, ev))
    tasks = [synth_narrative(project, s, gs, ctx) for s, gs in narrative.items()]
    tasks += [synth_batch(project, s, gs[i:i + SYNTH_BATCH], conflicts, ctx)
              for s, gs in recs.items() for i in range(0, len(gs), SYNTH_BATCH)]
    results = await asyncio.gather(*tasks)
    if any(r is None for r in results):
        return  # был сбой модели: прежние тезисы не трогаем
    produced = set().union(*results)
    # тезисы прошлых сборок: без правок удаляются, правленые и утверждённые остаются вне КР
    for t in db.q("SELECT id, override, provenance, status FROM theses WHERE project_id=?", [pid]):
        if t["id"] in produced or t["provenance"] == "manual":
            continue
        if t["override"] or t["status"] in ("approved", "rejected"):
            db.update("theses", t["id"], orphan=1)
        else:
            db.ex("DELETE FROM theses WHERE id=?", [t["id"]])
    meta = db.q1("SELECT meta FROM projects WHERE id=?", [pid])["meta"] or {}
    db.update("projects", pid, meta={**meta, "kr_built_at": started})
