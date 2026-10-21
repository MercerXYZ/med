"""Сборка КР из групп фактов: тезисы с УДД/УУР и описательные разделы."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

from . import db, prompts as P
from .groups import group_statement, live_facts, source_key  # noqa: F401 — source_key реэкспортируется
from .llm import CallCtx, LLMError, call_json
from .textnorm import numbers

# Приложение А2, табл. 1 (диагностика) и табл. 2 (лечение/профилактика/реабилитация)
UDD_DIAGNOSTIC = {
    # УДД 1 — только систематический обзор с метаанализом; обзор без метаанализа — 2
    "meta_analysis_diagnostic": 1, "meta_analysis_rct": 1, "systematic_review_rct": 2,
    "diagnostic_with_reference": 2, "rct": 2, "systematic_review_other": 2,
    "diagnostic_without_reference": 3, "cohort": 3, "case_control": 3,
    "non_comparative": 4, "case_series": 4, "case_report": 4,
    "expert_opinion": 5, "narrative_review": 5, "textbook": 5,
}
UDD_TREATMENT = {
    "meta_analysis_rct": 1, "systematic_review_rct": 2,
    "rct": 2, "systematic_review_other": 2,
    "cohort": 3,
    "case_control": 4, "non_comparative": 4, "case_series": 4, "case_report": 4,
    "expert_opinion": 5, "narrative_review": 5, "textbook": 5,
}
UDD_RATIONALE = {
    1: "систематический обзор РКИ / исследований с референсным методом с метаанализом",
    2: "отдельные РКИ / исследования с референсным методом или систематические обзоры иных исследований",
    3: "нерандомизированные сравнительные (в т.ч. когортные) исследования",
    4: "несравнительные исследования, серии/описания случаев, «случай-контроль»",
    5: "мнение экспертов / обоснование механизма действия",
}
SECTIONS = P.SECTIONS
SECTION_TYPE = {"2": "diagnostic", "3": "therapeutic", "4": "rehab", "5": "preventive"}

NARRATIVE_MAX_FACTS = 40  # фактов описательного раздела на один вызов
SYNTH_BATCH = 6           # групп (будущих тезисов) на один вызов
FACTS_PER_GROUP = 12      # фактов группы в промпте; доказательствами остаются все


def scale_for(section: str) -> str:
    return "udd_diagnostic" if section.startswith("2") else "udd_treatment"


def intervention_type_for(section: str) -> str:
    return SECTION_TYPE.get(section.split(".")[0], "therapeutic")


def udd_for(design: str | None, section: str) -> int | None:
    table = UDD_DIAGNOSTIC if scale_for(section) == "udd_diagnostic" else UDD_TREATMENT
    return table.get(design or "")


def preliminary_uur(udds: list[int], conflicting: bool) -> tuple[str | None, str]:
    """УУР по табл. 3 приложения А2; udds — по одному значению на независимый источник."""
    if not udds:
        return None, "УДД источников не определён (дизайн неизвестен или это руководство)"
    best = min(udds)
    if best <= 2 and not conflicting and max(udds) <= 2 and (best == 1 or len(udds) >= 2):
        return "A", "все источники УДД 1–2, выводы согласованы"
    if best <= 2 and not conflicting and len(udds) == 1:
        return "B", "единственное исследование высокого уровня"
    if best <= 2:
        return "B", ("есть противоречащие доказательства" if conflicting
                     else "не все источники высокого методологического качества")
    if best == 3 and not conflicting:
        return "B", "нерандомизированные сравнительные исследования, выводы согласованы"
    return "C", "доказательства низкого уровня или несогласованные"


def strength_phrase(direction: str, uur: str | None, explicit_rec: bool) -> str:
    strong = uur in ("A", "B") or explicit_rec
    if direction == "negative":
        return "Не рекомендуется" if strong else "Не рекомендуется рассматривать"
    return "Рекомендуется" if strong else "Рекомендуется рассмотреть возможность"


def topic_terms(project: dict) -> list[str]:
    """Основы слов темы КР для проверки подмены популяции: «предплеч», «лучев»…"""
    words = re.findall(r"[а-яё]{5,}", (project.get("title") or "").lower())
    stop = {"переломы", "перелом", "отдела", "дистального", "проксимального", "костей", "кости",
            "заболевания", "состояния"}
    stems = {w[:-2] if len(w) > 6 else w for w in words if w not in stop}
    stems |= set((project.get("meta") or {}).get("population_terms") or [])
    return sorted(stems)


def kr_evidence(project_id: str) -> dict[str, list[dict]]:
    from .resolve import excluded_fact_ids
    facts = live_facts(project_id)
    excluded = excluded_fact_ids(project_id)
    out = {}
    for g in db.q("SELECT id, card_ids FROM clusters WHERE project_id=? AND (excluded IS NULL OR excluded=0) "
                  "ORDER BY section, id", [project_id]):
        ev = [facts[i] for i in g.get("card_ids") or [] if i in facts and i not in excluded]
        if ev:
            out[g["id"]] = ev
    return out


def _hash_id(parts: list[str]) -> str:
    return "rec_" + hashlib.sha1("|".join(sorted(parts)).encode()).hexdigest()[:10]


def thesis_id(group_id: str) -> str:
    return "rec_" + hashlib.sha1(group_id.encode()).hexdigest()[:10]


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _pick(ev: list[dict], section: str, n: int = FACTS_PER_GROUP) -> list[dict]:
    """Факты для промпта: сначала по одному на источник, сильные и явные рекомендации вперёд."""
    order = sorted(ev, key=lambda c: (udd_for(c.get("_design"), section) or 9,
                                      c.get("claim_type") != "source_recommendation", c["_id"]))
    first, rest, seen = [], [], set()
    for c in order:
        (rest if c["_src"] in seen else first).append(c)
        seen.add(c["_src"])
    return (first + rest)[:n]


def _source(c: dict) -> str:
    s = c.get("_title") or ""
    if c.get("_year"):
        s += f" ({c['_year']})"
    if c.get("_design"):
        s += f", {c['_design']}"
    return s


def _save(project_id: str, rid: str, section: str, group_id: str | None, ids: list[str], auto: dict,
          flags: list[str], run_id: str | None) -> str:
    """Сохраняет автоверсию тезиса; возвращает id тезиса, в который она записана."""
    now = time.time()
    old = db.q1("SELECT * FROM theses WHERE id=?", [rid])
    relinked = False
    if old is None:
        # состав группы сдвинулся → новый id; решение эксперта переносим на новую версию
        old = _protected_match(project_id, group_id, ids, run_id, auto.get("kind"))
        if old:
            rid, relinked = old["id"], True
            if old.get("override") or old.get("status") == "approved":
                auto = {**auto, "_evidence_ids": ids}
    base = dict(project_id=project_id, section=section, cluster_id=group_id, evidence_ids=ids,
                review_flags=flags, orphan=0, run_id=run_id, updated_at=now)
    if old is None:
        db.upsert("theses", dict(id=rid, auto=auto, override=None, pending_auto=None, status="draft",
                                 provenance="auto", created_at=now, **base))
        return rid
    protected = bool(old.get("override")) or old.get("status") == "approved"
    if protected:
        base.pop("section")
        if relinked:
            base.pop("evidence_ids")  # новый набор фактов применится при принятии новой версии
    if protected and old.get("auto") != auto:
        db.update("theses", rid, pending_auto=auto, **base)
    else:
        db.update("theses", rid, auto=auto, **base)
    return rid


def _protected_match(project_id: str, group_id: str | None, ids: list[str], run_id: str | None,
                     kind: str | None) -> dict | None:
    """Правленый, утверждённый или отклонённый тезис-предшественник (ещё не обновлённый в этом прогоне)."""
    best, best_key = None, (False, 0)
    new = set(ids)
    for t in db.q("SELECT * FROM theses WHERE project_id=? AND provenance!='manual' AND "
                  "(override IS NOT NULL OR status IN ('approved', 'rejected'))", [project_id]):
        if (run_id and t.get("run_id") == run_id) or (t.get("auto") or {}).get("kind") != kind:
            continue
        same_group = kind is None and group_id is not None and t.get("cluster_id") == group_id
        old = set(t.get("evidence_ids") or [])
        n = len(old & new)
        # отклонение скрывает тезис из КР — переносим его только на группу почти с теми же фактами
        if (t["status"] == "rejected" and not t.get("override") and not same_group
                and (2 * n < len(old) or 2 * n < len(new))):
            continue
        if (same_group or n) and (same_group, n) > best_key:
            best, best_key = t, (same_group, n)
    return best


def _clean_refs(text: str, allowed: set[str]) -> str:
    """Убирает из ссылок [ev_…] id фактов, которых модели не показывали."""
    def fix(m):
        ids = re.findall(r"ev_[0-9a-f]+", m.group(1))
        if not ids:
            return m.group(0)
        ok = [i for i in ids if i in allowed]
        return " [" + ", ".join(ok) + "]" if ok else ""
    return re.sub(r"\s*\[([^\]]*)\]", fix, text).strip()


async def synth_narrative_section(project: dict, section: str, groups: list[tuple[dict, list[dict]]],
                                  ctx: CallCtx) -> set[str] | None:
    """Разделы 1.x и 7: абзацы со ссылками на факты, без УДД/УУР."""
    group_of = {c["_id"]: g["id"] for g, ev in groups for c in ev}
    facts_of = {g["id"]: ev for g, ev in groups}
    chunks, cur, n = [], [], 0
    for g, ev in groups:
        shown = _pick(ev, section)
        if cur and n + len(shown) > NARRATIVE_MAX_FACTS:
            chunks.append(cur)
            cur, n = [], 0
        cur.append((g, shown))
        n += len(shown)
    if cur:
        chunks.append(cur)
    produced: set[str] = set()
    for chunk in chunks:
        payload = [dict(statement=g["_statement"],
                        facts=[{k: v for k, v in dict(id=c["_id"], statement=c.get("statement_ru"),
                                                       age=c.get("age_group"),
                                                       secondary=bool(c.get("secondary_citation"))).items() if v}
                               for c in shown]) for g, shown in chunk]
        prompt = (f"КР: «{project['title']}». Раздел {section} «{P.SECTIONS.get(section, '')}».\n"
                  f"Группы фактов:\n{json.dumps(payload, ensure_ascii=False, indent=0)}")
        try:
            res = await call_json("narrative", P.NARRATIVE_SYSTEM, prompt, P.NARRATIVE_SCHEMA, ctx)
        except LLMError:
            return None
        shown_ids = {c["_id"] for _, shown in chunk for c in shown}
        for para in res.get("paragraphs", []):
            text = (para.get("paragraph") or "").strip()
            if not text or para.get("insufficient"):
                continue
            sec = para["section"] if para.get("section") in P.NARRATIVE_SECTIONS else section
            text = _clean_refs(text, shown_ids)
            cited = set(re.findall(r"ev_[0-9a-f]+", text))
            used = (set(para.get("fact_ids") or []) | cited) & shown_ids
            if not used:
                continue
            # остальные факты тех же групп утверждают то же — тоже доказательства
            ev = {c["_id"]: c for gid in {group_of[i] for i in used} for c in facts_of[gid]}
            ids = sorted(ev)
            ev_nums = set()
            for c in ev.values():
                ev_nums |= numbers(" ".join(c.get(k) or "" for k in ("statement_ru", "quote")))
            extra = sorted(numbers(re.sub(r"\[[^\]]*\]", "", text)) - ev_nums)
            flags = ["Числа не найдены в цитатах: " + ", ".join(extra)] if extra else []
            auto = dict(kind="narrative", kr_section=sec, subsection_title=para.get("topic", ""),
                        body=text, statement=text, strength_phrase=None, direction=None, udd=None, uur=None,
                        uur_preliminary=False, comment="", expert_additions=[], evidence_backed_core=text,
                        grade_rationale="Описательный раздел: УДД/УУР не присваиваются",
                        consensus=dict(agreement="single", conflicting_refs=[]),
                        quality_criterion=dict(is_criterion=False, criterion_text=None),
                        explicit_source_recommendation=False)
            rid = _hash_id(sorted(used) + [para.get("topic") or text[:60]])
            gid = group_of[sorted(used)[0]]
            produced.add(_save(project["id"], rid, sec, gid, ids, auto, flags, ctx.run_id))
    return produced


def _items(ev: list[dict], section: str) -> list[dict]:
    out = []
    for c in _pick(ev, section):
        d = dict(id=c["_id"], statement=c.get("statement_ru"), quote=(c.get("quote") or "")[:300],
                 type=c.get("claim_type"), polarity=c.get("polarity"), population=c.get("population"),
                 age=c.get("age_group"), dose=c.get("dose"), timing=c.get("timing"),
                 conditions=c.get("conditions"), source=_source(c))
        out.append({k: v for k, v in d.items() if v})
    return out


async def synth_batch(project: dict, section: str, batch: list[tuple[dict, list[dict]]],
                      ctx: CallCtx) -> set[str] | None:
    """Несколько групп одного раздела → по тезису на группу за один вызов."""
    payload = [dict(group_id=g["id"], statement=g["_statement"], facts=_items(ev, section)) for g, ev in batch]
    prompt = (f"КР: «{project['title']}», возрастная группа КР: {project.get('age_category')}.\n"
              f"Раздел {section} «{P.SECTIONS.get(section, '')}». Сформулируй по одному тезису на каждую группу.\n"
              f"Группы:\n{json.dumps(payload, ensure_ascii=False, indent=0)}")
    try:
        res = await call_json("synth", P.SYNTH_SYSTEM, prompt, P.SYNTH_BATCH_SCHEMA, ctx)
    except LLMError:
        return None
    by_group = {t.get("group_id"): t for t in res.get("theses", [])}
    produced = set()
    for g, ev in batch:
        t = by_group.get(g["id"])
        if t is None:
            produced |= _existing(project["id"], g["id"])  # модель пропустила группу: прежний тезис остаётся
            continue
        if t.get("insufficient") or len((t.get("body") or "").strip()) < 15:
            continue
        produced.add(_finalize(project, g, ev, t, ctx))
    return produced


def _existing(project_id: str, group_id: str) -> set[str]:
    return {r["id"] for r in db.q("SELECT id FROM theses WHERE project_id=? AND cluster_id=?",
                                  [project_id, group_id])}


def _finalize(project: dict, group: dict, ev: list[dict], res: dict, ctx: CallCtx) -> str:
    section = group["section"]
    ids = [c["_id"] for c in ev]
    flags = []
    per_src: dict[str, int] = {}
    graded = []  # факты, давшие УДД
    for c in ev:
        u = udd_for(c["_design"], section)
        if u:
            per_src[c["_src"]] = min(u, per_src.get(c["_src"], 9))
            graded.append(c)
    udds = list(per_src.values())
    udd = min(udds) if udds else None
    uur, uur_why = preliminary_uur(udds, False)
    explicit = any(c.get("claim_type") == "source_recommendation" for c in ev)
    if udd is None:
        flags.append("УДД не определён")

    # подтверждённым считается только число из дословной цитаты
    ev_nums = set()
    for c in ev:
        ev_nums |= numbers(c.get("quote") or "")
    comment = (res.get("comment") or "").strip()
    if numbers(comment) - ev_nums:
        comment = ""  # числа не из цитат — комментарий в КР не берём
    extra = sorted(numbers(res["body"] + " " + (res.get("body_genitive") or "")) - ev_nums)
    additions = [{"claim": a, "basis": "expert_consensus", "refs": []} for a in res.get("expert_additions", [])]
    if extra:
        flags.append("Числа не найдены в цитатах: " + ", ".join(extra))
        additions.append({"claim": "Числовые значения " + ", ".join(extra) + " в формулировке тезиса",
                          "basis": "expert_consensus", "refs": []})

    # уровень косвенности определяют факты, давшие УДД
    if all(c.get("topic_relevance") == "indirect" for c in (graded or ev)) and uur == "A":
        uur, uur_why = "B", uur_why + "; понижен до B: доказательства косвенные"
    terms = topic_terms(project)
    ev_text = " ".join(" ".join(c.get(k) or "" for k in ("statement_ru", "population", "quote")) for c in ev).lower()
    body_text = " ".join([res.get("body") or "", res.get("body_genitive") or "",
                          res.get("target_population") or ""]).lower()
    added = sorted({t for t in terms if t in body_text and t not in ev_text})
    if added and not any(t in ev_text for t in terms):
        flags.append("Возможно, расширена популяция: " + (res.get("target_population") or ", ".join(added[:3])))
        additions.append({"claim": "Применимость к пациентам по теме КР (" + ", ".join(added[:3]) + ")",
                          "basis": "expert_consensus", "refs": []})
        if uur == "A":
            uur, uur_why = "B", uur_why + "; понижен до B: популяция экстраполирована"

    # группы однонаправленные (groups._guard), так что направление фактов известно
    n_neg = sum(c.get("polarity") == "negative" for c in ev)
    if (res["direction"] == "negative" and not n_neg) or (res["direction"] == "positive" and n_neg == len(ev)):
        flags.append("Направление тезиса не совпадает с фактами")
    phrase = strength_phrase(res["direction"], uur, explicit)
    body = res["body"].strip().rstrip(".")
    if phrase == "Рекомендуется рассмотреть возможность" and res.get("body_genitive"):
        body = res["body_genitive"].strip().rstrip(".")
    if res["direction"] == "negative" and re.match(r"(не|нет)\s", body, re.I):
        flags.append("Двойное отрицание в тезисе")
    if body[:1].isupper() and not body[:3].isupper():
        body = body[0].lower() + body[1:]
    itype = intervention_type_for(section)
    qc_text = (res.get("quality_criterion") or "").strip()
    n_src = len({c["_src"] for c in ev})
    auto = dict(
        kr_section=section, subsection_title=res.get("subsection_title", ""),
        strength_phrase=phrase, body=body, statement=f"{phrase} {body}.",
        direction=res["direction"],
        intervention=dict(type=itype, scale_ref=scale_for(section), action=res.get("action", ""),
                          target_population=res.get("target_population", ""),
                          comparator=res.get("comparator") or None, outcome=res.get("outcome", ""),
                          mnn=res.get("mnn") or None, atc_code=None),
        evidence_backed_core=res.get("evidence_backed_core", ""), expert_additions=additions,
        comment=comment, udd=udd, uur=uur, uur_preliminary=True,
        grade_rationale=(f"УДД {udd}: {UDD_RATIONALE[udd]}; " if udd else "УДД не определён; ")
        + f"УУР {uur or '—'}: {uur_why}; источников: {n_src}",
        consensus=dict(agreement="consistent" if n_src > 1 else "single", conflicting_refs=[]),
        quality_criterion=dict(
            is_criterion=bool(qc_text), criterion_text=qc_text or None,
            applicable_stage={"diagnostic": "diagnosis", "therapeutic": "treatment", "rehab": "rehabilitation",
                              "preventive": "prevention"}[itype],
            measurable=bool(qc_text),
            auto_eligible=bool(qc_text) and uur in ("A", "B") and itype in ("diagnostic", "therapeutic")),
        explicit_source_recommendation=explicit,
    )
    return _save(project["id"], thesis_id(group["id"]), section, group["id"], ids, auto, flags, ctx.run_id)


async def build_kr(project: dict, ctx: CallCtx) -> None:
    pid = project["id"]
    started = time.time()  # правки эксперта позже этого момента в сборку не попали
    groups = {g["id"]: g for g in db.q("SELECT * FROM clusters WHERE project_id=?", [pid])}
    narrative: dict[str, list] = {}
    recs: dict[str, list] = {}
    for gid, ev in kr_evidence(pid).items():
        g = dict(groups[gid])
        g["_statement"] = group_statement(g) or ev[0].get("statement_ru") or ""
        if g["section"] in P.NARRATIVE_SECTIONS:
            narrative.setdefault(g["section"], []).append((g, ev))
        elif not all(c.get("claim_type") == "background_fact" for c in ev):
            recs.setdefault(g["section"], []).append((g, ev))  # группа без вмешательства тезиса не даёт
    tasks = [synth_narrative_section(project, s, gs, ctx) for s, gs in narrative.items()]
    tasks += [synth_batch(project, s, b, ctx) for s, gs in recs.items() for b in _chunks(gs, SYNTH_BATCH)]
    results = await asyncio.gather(*tasks)
    if any(r is None for r in results):
        return  # был сбой модели: прежние тезисы не трогаем
    db.ex("UPDATE theses SET merged_into=NULL WHERE project_id=? AND merged_into IS NOT NULL", [pid])
    produced = set().union(*results) if results else set()
    # тезисы прошлых запусков: без правок — удаляются, с правками — остаются «осиротевшими»
    for t in db.q("SELECT id, override, provenance, status FROM theses WHERE project_id=?", [pid]):
        if t["id"] in produced or t["provenance"] == "manual":
            continue
        if t["override"] or t["status"] in ("approved", "rejected"):
            db.update("theses", t["id"], orphan=1)
        else:
            db.ex("DELETE FROM theses WHERE id=?", [t["id"]])
    row = db.q1("SELECT meta FROM projects WHERE id=?", [pid]) or {}
    db.update("projects", pid, meta={**(row.get("meta") or {}), "kr_built_at": started})
