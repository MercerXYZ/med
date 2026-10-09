"""Синтез тезисов-рекомендаций и критериев качества.

LLM формулирует тело тезиса только из карточек кластера. Всё, что можно
вычислить детерминированно, вычисляется кодом, а не моделью:
* УДД — из дизайна исследования по шкалам приложения А2 (диагностика/лечение);
* УУР — предварительно, по правилам ниже, всегда «требует подтверждения»;
* вводная фраза («Рекомендуется»/«Не рекомендуется»…) — из (direction, УУР);
* числа тезиса сверяются с карточками: «лишнее» число уходит в expert_additions
  с флагом (механизм отлова фабрикованных привязок из схемы преподавателя).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import Counter

from . import db, prompts as P
from .llm import CallCtx, LLMError, call_json
from .textnorm import numbers

# Приложение А2, табл. 1 (диагностика) и табл. 2 (лечение/профилактика/реабилитация)
UDD_DIAGNOSTIC = {
    # приложение А2: УДД 1 — только систематический обзор С МЕТААНАЛИЗОМ; обзор без метаанализа — 2
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

SECTION_TYPE = {"2": "diagnostic", "3": "therapeutic", "4": "rehab", "5": "preventive"}


def scale_for(section: str) -> str:
    return "udd_diagnostic" if section.startswith("2") else "udd_treatment"


def intervention_type_for(section: str) -> str:
    return SECTION_TYPE.get(section.split(".")[0], "therapeutic")


def udd_for(design: str | None, section: str) -> int | None:
    table = UDD_DIAGNOSTIC if scale_for(section) == "udd_diagnostic" else UDD_TREATMENT
    return table.get(design or "")


def preliminary_uur(udds: list[int], conflicting: bool) -> tuple[str | None, str]:
    """Предварительный УУР (табл. 3 приложения А2), всегда требует решения эксперта.
    A — УДД 1 либо ≥ 2 независимых источника УДД 2; все источники УДД ≤ 2, согласованы;
    B — есть УДД 1–2, но единственный источник, не все высокого качества или есть
        противоречия; либо согласованные доказательства УДД 3;
    C — УДД 3 с противоречиями, УДД 4–5.
    udds — по одному значению на независимый источник (документ)."""
    if not udds:
        return None, "УДД источников не определён (дизайн неизвестен или это руководство) — УУР не предлагается"
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


def _conflict_cards(ev: list[dict]) -> list[str]:
    """Карточки-участники противоречий, затрагивающих доказательства тезиса."""
    out = set()
    for cf in {i for c in ev for i in c["_conflicts"]}:
        row = db.q1("SELECT card_ids FROM conflicts WHERE id=? AND status='open'", [cf])
        out.update((row or {}).get("card_ids") or [])
    return sorted(out)


def topic_terms(project: dict) -> list[str]:
    """Основы слов темы КР (рус.) для проверки подмены популяции: «предплеч», «лучев»…"""
    words = re.findall(r"[а-яё]{5,}", (project.get("title") or "").lower())
    stop = {"переломы", "перелом", "отдела", "дистального", "проксимального", "костей", "кости",
            "заболевания", "состояния"}
    stems = {w[:-2] if len(w) > 6 else w for w in words if w not in stop}
    # явный список из настроек проекта (meta.population_terms) — точнее автоматического
    stems |= set((project.get("meta") or {}).get("population_terms") or [])
    return sorted(stems)


def source_key(doc_id: str, biblio: dict) -> str:
    """Независимый источник = публикация, а не файл: DOI, иначе нормализованное название."""
    doi = (biblio.get("doi") or "").lower().strip()
    title = re.sub(r"[^a-zа-я0-9]", "", (biblio.get("title") or "").lower())
    return "doi:" + doi if doi else ("title:" + title if len(title) >= 30 else "doc:" + doc_id)


def _rec_id(card_ids: list[str]) -> str:
    return "rec_" + hashlib.sha1("|".join(sorted(card_ids)).encode()).hexdigest()[:10]


def _evidence(project_id: str, cluster: dict) -> list[dict]:
    """Карточки кластера, пригодные для подтверждённой части: verified или
    подтверждённые экспертом; дубликаты подтягиваются как доп. источники."""
    out = []
    for cid in cluster["card_ids"]:
        r = db.q1("SELECT * FROM cards WHERE id=?", [cid])
        if not r or r.get("user_status") == "rejected":
            continue
        ok = r["status"] == "verified" or r.get("user_status") == "confirmed"
        if not ok:
            continue
        c = db.effective(r)
        doc = db.q1("SELECT biblio, filename, status FROM documents WHERE id=?", [r["doc_id"]])
        if (doc or {}).get("status") == "duplicate":
            continue  # та же публикация в другой вёрстке: источник уже учтён по основному файлу
        b = (doc or {}).get("biblio") or {}
        c.update(_id=cid, _doc=r["doc_id"], _src=source_key(r["doc_id"], b), _design=b.get("study_design"), _year=b.get("year"),
                 _title=b.get("title") or (doc or {}).get("filename"),
                 _merged=r.get("merged_into"), _conflicts=db.open_conflicts_by_card(project_id).get(cid, []))
        if c.get("secondary_citation"):  # пересказ чужой работы: дизайн этой статьи к нему неприменим
            c["_design"] = None
        out.append(c)
    return out


def _save(project_id: str, rid: str, section: str, cluster_id: str, ids: list[str], auto: dict,
          flags: list[str], run_id: str | None, relink: bool = True) -> str:
    """Сохраняет автоверсию тезиса; возвращает id тезиса, в который она записана."""
    now = time.time()
    old = db.q1("SELECT * FROM theses WHERE id=?", [rid])
    if old is None and relink:
        # состав кластера сдвинулся → новый id; правку эксперта не бросаем «осиротевшей»,
        # а показываем ему новую версию рядом с его текстом
        prev = _protected_match(project_id, ids, run_id, auto.get("kind"))
        if prev:
            rid, old = prev["id"], prev
            auto = {**auto, "_evidence_ids": ids}
    base = dict(project_id=project_id, section=section, cluster_id=cluster_id, evidence_ids=ids,
                review_flags=flags, orphan=0, run_id=run_id, updated_at=now)
    if old is None:
        db.upsert("theses", dict(id=rid, auto=auto, override=None, pending_auto=None, status="draft",
                                 provenance="auto", created_at=now, **base))
        return rid
    # тезис защищён, если эксперт его правил ИЛИ утвердил: такой текст молча не перезаписываем
    protected = bool(old.get("override")) or old.get("status") == "approved"
    if protected:
        # раздел и связи с карточками, заданные экспертом, повторный прогон не трогает
        base.pop("section")
        base.pop("evidence_ids")
    # служебные поля (пометка объединения) не считаются изменением содержания
    strip = lambda a: {k: v for k, v in (a or {}).items() if k != "merged_from"}
    if protected and strip(old.get("auto")) != strip(auto):
        db.update("theses", rid, pending_auto=auto, **base)
    else:
        if (old.get("auto") or {}).get("merged_from") and not auto.get("merged_from"):
            auto = {**auto, "merged_from": old["auto"]["merged_from"]}
        db.update("theses", rid, auto=auto, **base)
    return rid


def _protected_match(project_id: str, ids: list[str], run_id: str | None, kind: str | None) -> dict | None:
    """Тезис с правкой или утверждённый, у которого больше всего общих карточек с новым набором
    (не ручной и ещё не обновлённый в этом прогоне)."""
    best, best_n = None, 0
    for t in db.q("SELECT * FROM theses WHERE project_id=? AND provenance!='manual' AND "
                  "(override IS NOT NULL OR status='approved')", [project_id]):
        if (run_id and t.get("run_id") == run_id) or (t.get("auto") or {}).get("kind") != kind:
            continue  # уже обновлён в этом прогоне / абзац описательного раздела ≠ тезис-рекомендация
        n = len(set(t.get("evidence_ids") or []) & set(ids))
        if n > best_n:
            best, best_n = t, n
    return best


NARRATIVE_MAX_CARDS = 40   # карточек описательного раздела на один вызов
SYNTH_BATCH = 6            # кластеров (будущих тезисов) на один вызов


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


async def synth_narrative_section(project: dict, section: str, groups: list[tuple[dict, list[dict]]],
                                  ctx: CallCtx) -> set[str] | None:
    """Разделы 1.x и 7 по 103н — повествовательный текст со ссылками, без УДД/УУР.
    Один вызов на раздел (или на 40 карточек), а не на каждый кластер."""
    cards = {c["_id"]: c for _, ev in groups for c in ev}
    items = [dict(id=c["_id"], statement=c.get("statement_ru"), age=c.get("age_group"),
                  secondary=c.get("secondary_citation")) for c in cards.values() if not c["_merged"]]
    produced: set[str] = set()
    for part in _chunks(items, NARRATIVE_MAX_CARDS):
        prompt = (f"КР: «{project['title']}». Раздел {section} «{P.SECTIONS.get(section, '')}».\n"
                  f"Карточки:\n{json.dumps(part, ensure_ascii=False, indent=0)}")
        try:
            res = await call_json("synth", P.NARRATIVE_SYSTEM, prompt, P.NARRATIVE_SCHEMA, ctx)
        except LLMError:
            return None  # прежние тексты раздела сохраняются
        part_ids = {i["id"] for i in part}
        for para in res.get("paragraphs", []):
            text = (para.get("paragraph") or "").strip()
            if not text or para.get("insufficient"):
                continue
            flags, sec = [], section
            if para.get("section") in P.NARRATIVE_SECTIONS and para["section"] != section:
                flags.append(f"Перенесено моделью из раздела {section} в {para['section']}")
                sec = para["section"]
            cited = set(re.findall(r"ev_[0-9a-f]+", text))
            if cited - part_ids:
                flags.append("В тексте есть ссылки на карточки вне раздела: " + ", ".join(sorted(cited - part_ids)))
                for bad in cited - part_ids:
                    text = text.replace(bad, "?")
            ids = sorted((set(para.get("used_card_ids") or []) | cited) & part_ids)
            if not ids:
                continue
            ids += sorted(k for k, c in cards.items() if c["_merged"] in ids)  # повторы — тоже источники
            ev_nums = set()
            for i in ids:
                c = cards.get(i) or {}
                ev_nums |= numbers(" ".join(c.get(k) or "" for k in ("statement_ru", "quote")))
            extra = sorted(numbers(re.sub(r"\[[^\]]*\]", "", text)) - ev_nums)
            if extra:
                flags.append("Числа текста не найдены в доказательствах: " + ", ".join(extra))
            auto = dict(kind="narrative", kr_section=sec, subsection_title=para.get("topic", ""),
                        body=text, statement=text, strength_phrase=None, direction=None, udd=None, uur=None,
                        uur_preliminary=False, comment="", expert_additions=[], evidence_backed_core=text,
                        grade_rationale="Описательный раздел: УДД/УУР не присваиваются",
                        consensus=dict(agreement="single", conflicting_refs=[]),
                        quality_criterion=dict(is_criterion=False, criterion_text=None),
                        explicit_source_recommendation=False)
            rid = _rec_id(ids + [para.get("topic") or text[:60]])  # абзацы с общими карточками не затирают друг друга
            produced.add(_save(project["id"], rid, sec, None, ids, auto, flags, ctx.run_id))
    return produced


def _items(ev: list[dict]) -> list[dict]:
    items = [dict(id=c["_id"], statement=c.get("statement_ru"), quote=(c.get("quote") or "")[:300],
                  type=c.get("claim_type"), polarity=c.get("polarity"), population=c.get("population"),
                  age=c.get("age_group"), dose=c.get("dose"), timing=c.get("timing"),
                  conditions=c.get("conditions"), source=f"{c['_title']} ({c['_year']}), {c['_design']}")
             for c in ev if not c["_merged"]]
    return items or [dict(id=ev[0]["_id"], statement=ev[0].get("statement_ru"), quote=ev[0].get("quote"))]


async def synth_batch(project: dict, section: str, batch: list[tuple[dict, list[dict]]],
                      ctx: CallCtx) -> set[str]:
    """Несколько кластеров одного раздела → несколько тезисов за один вызов."""
    payload = [dict(cluster_id=cl["id"], topic=cl.get("topic"), cards=_items(ev)) for cl, ev in batch]
    prompt = (f"КР: «{project['title']}», возрастная группа КР: {project.get('age_category')}.\n"
              f"Раздел {section} «{P.SECTIONS.get(section, '')}». Сформулируй по одному тезису на КАЖДЫЙ кластер.\n"
              f"Кластеры:\n{json.dumps(payload, ensure_ascii=False, indent=0)}")
    ids_of = {cl["id"]: [c["_id"] for c in ev] for cl, ev in batch}
    try:
        res = await call_json("synth", P.SYNTH_SYSTEM, prompt, P.SYNTH_BATCH_SCHEMA, ctx)
    except LLMError:
        # сбой не портит результат: прежние тезисы этих кластеров сохраняются, ошибка — в журнале
        return {_rec_id(i) for i in ids_of.values()} | _existing(project["id"], [cl["id"] for cl, _ in batch])
    by_cluster = {t.get("cluster_id"): t for t in res.get("theses", [])}
    produced = set()
    for cl, ev in batch:
        t = by_cluster.get(cl["id"])
        if t is None:
            # модель пропустила кластер — это не «доказательств нет»: прежний тезис остаётся
            produced |= _existing(project["id"], [cl["id"]])
            continue
        if t.get("insufficient") or len((t.get("body") or "").strip()) < 15:
            continue  # явный отказ модели сформулировать тезис по этим карточкам
        produced.add(_finalize(project, cl, ev, t, ctx))
    return produced


def _existing(project_id: str, cluster_ids: list[str]) -> set[str]:
    """Тезисы прошлых прогонов для этих кластеров (сохраняются при сбое или пропуске)."""
    if not cluster_ids:
        return set()
    marks = ",".join("?" * len(cluster_ids))
    return {r["id"] for r in db.q(f"SELECT id FROM theses WHERE project_id=? AND cluster_id IN ({marks})",
                                  [project_id, *cluster_ids])}


def _finalize(project: dict, cluster: dict, ev: list[dict], res: dict, ctx: CallCtx, relink: bool = True) -> str:
    section = cluster["section"]
    ids = [c["_id"] for c in ev]
    rid = _rec_id(ids)
    flags = []
    # --- УДД / УУР ---
    per_doc: dict[str, int] = {}
    graded = []  # карточки, которые дали УДД
    for c in ev:  # один УДД на независимый источник (публикацию, не файл)
        u = udd_for(c["_design"], section)
        if u:
            per_doc[c["_src"]] = min(u, per_doc.get(c["_src"], 9))
            graded.append(c)
    udds = list(per_doc.values())
    conflicting = any(c["_conflicts"] for c in ev)
    udd = min(udds) if udds else None
    uur, uur_why = preliminary_uur(udds, conflicting)
    explicit = any(c.get("claim_type") == "source_recommendation" for c in ev)
    if not explicit:
        flags.append("Тезис предложен системой на основании результатов исследований (явной "
                     "рекомендации авторов нет) — требуется решение эксперта")
    if any(c.get("secondary_citation") for c in ev):
        flags.append("Часть доказательств — вторичное цитирование (пересказ других работ): "
                     "УДД по ним не присваивается, нужен первоисточник")
    if udd is None:
        flags.append("УДД не определён: дизайн источника неизвестен или источник — руководство/обзор")
    if conflicting:
        flags.append("Среди доказательств есть противоречия — см. вкладку «Противоречия»")
    # --- направление ---
    pols = Counter(c.get("polarity") for c in ev)
    if res["direction"] == "negative" and not pols.get("negative"):
        flags.append("Отрицательный тезис, но ни одна карточка не имеет отрицательной полярности")
    if res["direction"] == "positive" and pols.get("negative") and not pols.get("positive"):
        flags.append("Положительный тезис при только отрицательных доказательствах")
    # --- числа: всё, чего нет в доказательствах, — экспертная добавка ---
    # подтверждённым считается только число из дословной цитаты: поля карточки (доза, срок)
    # заполняет модель, и число в них могло прийти «из окрестностей» цитаты
    ev_nums = set()
    for c in ev:
        ev_nums |= numbers(c.get("quote") or "")
    # проверяем оба варианта тела: в черновик может уйти body_genitive («…рассмотреть возможность»)
    extra = sorted(numbers(res["body"] + " " + (res.get("body_genitive") or "")) - ev_nums)
    additions = [{"claim": a, "basis": "expert_consensus", "refs": []} for a in res.get("expert_additions", [])]
    if extra:
        flags.append("Числа тезиса не найдены в доказательствах: " + ", ".join(extra))
        additions.append({"claim": "Числовые значения " + ", ".join(extra) + " в формулировке тезиса",
                          "basis": "expert_consensus", "refs": []})

    # --- популяция: косвенные доказательства и подмена популяции темой КР ---
    # уровень определяют карточки, давшие УДД: прямая карточка без УДД (руководство,
    # пересказ) не должна «прикрывать» косвенность исследований
    if all(c.get("topic_relevance") == "indirect" for c in (graded or ev)):
        if uur == "A":
            uur, uur_why = "B", uur_why + "; понижен до B: доказательства косвенные (другая популяция)"
        flags.append("Доказательства косвенные: получены не на пациентах с темой КР — применимость "
                     "к теме требует решения эксперта")
    terms = topic_terms(project)
    ev_text = " ".join(" ".join(c.get(k) or "" for k in ("statement_ru", "population", "quote")) for c in ev).lower()
    body_text = " ".join([res.get("body") or "", res.get("body_genitive") or "",
                          res.get("target_population") or ""]).lower()
    added = sorted({t for t in terms if t in body_text and t not in ev_text})
    if added and not any(t in ev_text for t in terms):
        flags.append("Популяция тезиса подменена темой КР: в доказательствах нет упоминания «"
                     + "», «".join(added[:3]) + "» — исправьте группу пациентов")
        additions.append({"claim": "Применимость к пациентам по теме КР (" + ", ".join(added[:3]) + ")",
                          "basis": "expert_consensus", "refs": []})
        if uur == "A":
            uur, uur_why = "B", uur_why + "; понижен до B: популяция экстраполирована"

    phrase = strength_phrase(res["direction"], uur, explicit)
    body = res["body"].strip().rstrip(".")
    if phrase == "Рекомендуется рассмотреть возможность" and res.get("body_genitive"):
        body = res["body_genitive"].strip().rstrip(".")
    if res["direction"] == "negative" and re.search(r"(^|\s)(не|нет)\s", body):
        flags.append("В тексте отрицательного тезиса есть отрицание — проверьте, нет ли двойного отрицания")
    if body[:1].isupper() and not body[:3].isupper():
        body = body[0].lower() + body[1:]
    itype = intervention_type_for(section)
    qc_text = (res.get("quality_criterion") or "").strip()
    auto = dict(
        kr_section=section, subsection_title=res.get("subsection_title", ""),
        strength_phrase=phrase, body=body, statement=f"{phrase} {body}.",
        direction=res["direction"],
        intervention=dict(type=itype, scale_ref=scale_for(section), action=res.get("action", ""),
                          target_population=res.get("target_population", ""),
                          comparator=res.get("comparator") or None, outcome=res.get("outcome", ""),
                          mnn=res.get("mnn") or None, atc_code=None),
        evidence_backed_core=res.get("evidence_backed_core", ""), expert_additions=additions,
        comment=res.get("comment", ""), udd=udd, uur=uur, uur_preliminary=True,
        grade_rationale=(f"УДД {udd}: {UDD_RATIONALE[udd]}; " if udd else "УДД не определён; ")
        + f"УУР {uur or '—'} (предварительно): {uur_why}; источников: {len({c['_src'] for c in ev})}",
        consensus=dict(agreement="conflicting" if conflicting else
                       ("consistent" if len({c['_src'] for c in ev}) > 1 else "single"),
                       conflicting_refs=_conflict_cards(ev)),
        quality_criterion=dict(
            is_criterion=bool(qc_text), criterion_text=qc_text or None,
            applicable_stage={"diagnostic": "diagnosis", "therapeutic": "treatment", "rehab": "rehabilitation",
                              "preventive": "prevention"}[itype],
            measurable=bool(qc_text),
            auto_eligible=bool(qc_text) and uur in ("A", "B") and itype in ("diagnostic", "therapeutic")),
        explicit_source_recommendation=explicit,
        _synth=res,  # исходный ответ модели — для пересчёта при объединении тезисов
    )
    return _save(project["id"], rid, section, cluster["id"], ids, auto, flags, ctx.run_id, relink)


async def synth_project(project: dict, ctx: CallCtx) -> None:
    clusters = db.q("SELECT * FROM clusters WHERE project_id=? ORDER BY section, id", [project["id"]])
    narrative: dict[str, list] = {}
    recs: dict[str, list] = {}
    for cl in clusters:
        ev = _evidence(project["id"], cl)
        if not ev:
            continue
        if cl["section"] in P.NARRATIVE_SECTIONS:
            narrative.setdefault(cl["section"], []).append((cl, ev))
        elif not all(c.get("claim_type") == "background_fact" for c in ev):
            recs.setdefault(cl["section"], []).append((cl, ev))  # факты без вмешательства тезиса не дают
    tasks = [synth_narrative_section(project, s, g, ctx) for s, g in narrative.items()]
    tasks += [synth_batch(project, s, b, ctx) for s, g in recs.items() for b in _chunks(g, SYNTH_BATCH)]
    results = await asyncio.gather(*tasks)
    if any(r is None for r in results):
        return  # сбой в описательном разделе: ничего не удаляем, прежние тексты сохраняются
    produced = set().union(*results) if results else set()
    # тезисы прошлых запусков: без правок — удаляются, с правками — сохраняются как «осиротевшие»
    for t in db.q("SELECT id, override, provenance, status FROM theses WHERE project_id=?", [project["id"]]):
        if t["id"] in produced or t["provenance"] == "manual":
            continue
        if t["override"] or t["status"] in ("approved", "rejected"):
            db.update("theses", t["id"], orphan=1)
        else:
            db.ex("DELETE FROM theses WHERE id=?", [t["id"]])
