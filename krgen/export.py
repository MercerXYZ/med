"""Экспорт: JSON (собственная версионированная схема + схема преподавателя),
читаемый текст (Markdown) и Word по структуре приказа № 103н."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import db, prompts as P
from .config import ROOT, SCHEMA_VERSION, SETTINGS
from .pipeline import processing_report
from .synth import UDD_DIAGNOSTIC, UDD_TREATMENT, intervention_type_for, scale_for

AGE_RU = {"adults": "Взрослые", "children": "Дети", "mixed": "Взрослые, дети"}
STATUS_RU = {"verified": "подтверждена", "needs_review": "требует проверки", "unverified": "не подтверждена",
             "extracted": "не проверялась"}

CHAPTERS = [
    ("1", "Краткая информация по заболеванию или состоянию (группе заболеваний или состояний)",
     ["1.1", "1.2", "1.3", "1.4", "1.5", "1.6"]),
    ("2", "Диагностика заболевания или состояния (группы заболеваний или состояний), медицинские "
          "показания и противопоказания к применению методов диагностики",
     ["2.1", "2.2", "2.3", "2.4", "2.5"]),
    ("3", "Лечение, включая медикаментозную и немедикаментозную терапии, диетотерапию, обезболивание, "
          "медицинские показания и противопоказания к применению методов лечения", ["3.1", "3.2", "3.3"]),
    ("4", "Медицинская реабилитация и санаторно-курортное лечение, медицинские показания и "
          "противопоказания к применению методов медицинской реабилитации", ["4"]),
    ("5", "Профилактика и диспансерное наблюдение, медицинские показания и противопоказания к "
          "применению методов профилактики", ["5"]),
    ("6", "Организация оказания медицинской помощи", ["6"]),
    ("7", "Дополнительная информация (в том числе факторы, влияющие на исход заболевания или состояния)", ["7"]),
]
EMPTY = "Недостаточно материалов: в обработанных статьях не найдено подтверждённых утверждений для этого раздела."


# ------------------------------------------------------------------ сборка ---

def _cards(project_id: str) -> list[dict]:
    out = []
    open_cf = db.open_conflicts_by_card(project_id)
    for r in db.q("SELECT * FROM cards WHERE project_id=? ORDER BY doc_id, id", [project_id]):
        c = db.effective(r)
        v = r.get("verification") or {}
        q = v.get("quote") or {}
        out.append(dict(
            id=r["id"], doc_id=r["doc_id"], statement_ru=c.get("statement_ru"),
            claim_type=c.get("claim_type"), polarity=c.get("polarity"),
            negation=c.get("polarity") == "negative", population=c.get("population") or None,
            age_group=c.get("age_group"), intervention=c.get("intervention") or None,
            comparator=c.get("comparator") or None, outcome=c.get("outcome") or None,
            conditions=c.get("conditions") or None, dose=c.get("dose") or None,
            timing=c.get("timing") or None, thresholds=c.get("thresholds") or None,
            intervention_type=c.get("intervention_type"), kr_section=c.get("kr_section"),
            topic_relevance=c.get("topic_relevance"), secondary_citation=bool(c.get("secondary_citation")),
            source_quote=c.get("quote"), pdf_page=q.get("found_page") or c.get("page"),
            pdf_page_note="порядковый номер страницы в файле PDF (с 1)",
            verification=dict(status=r["status"], user_status=r.get("user_status"),
                              quote_match=q.get("status"), quote_score=q.get("score"),
                              quote_found_page=q.get("found_page"),
                              checks=v.get("checks", []), judge=v.get("judge"),
                              issues=v.get("issues", []), conflicts=open_cf.get(r["id"], [])),
            merged_into=r.get("merged_into"), origin=r["origin"],
            edited_by_expert=bool(r.get("override")), auto_value=r.get("auto"),
            pending_auto_update=bool(r.get("pending_auto")), run_id=r.get("run_id")))
    return out


def _theses(project_id: str) -> list[dict]:
    out = []
    for r in db.q("SELECT * FROM theses WHERE project_id=? ORDER BY section, created_at", [project_id]):
        t = db.effective(r)
        out.append(dict(id=r["id"], section=r["section"], kind=t.get("kind", "recommendation"), **{
            k: t.get(k) for k in ("subsection_title", "statement", "strength_phrase", "body", "direction",
                                  "intervention", "evidence_backed_core", "expert_additions", "comment",
                                  "udd", "uur", "uur_preliminary", "grade_rationale", "consensus",
                                  "quality_criterion", "explicit_source_recommendation", "expert_author")},
            evidence_ids=r.get("evidence_ids") or [], status=r["status"], provenance=r["provenance"],
            merged_into=r.get("merged_into"), merged_from=t.get("merged_from") or [],
            review_flags=r.get("review_flags") or [], orphan=bool(r.get("orphan")),
            edited_by_expert=bool(r.get("override")), auto_value=r.get("auto"),
            pending_auto_update=bool(r.get("pending_auto"))))
    return out


def build_export(project_id: str) -> dict:
    p = db.q1("SELECT * FROM projects WHERE id=?", [project_id])
    docs = []
    for d in db.q("SELECT * FROM documents WHERE project_id=? ORDER BY filename", [project_id]):
        b = {k: v for k, v in (d.get("biblio") or {}).items() if not k.startswith("_")}
        docs.append(dict(id=d["id"], filename=d["filename"], sha256=d["sha256"], n_pages=d["n_pages"],
                         processing_status=d["status"], status_detail=d["status_detail"],
                         duplicate_of=d["duplicate_of"], bibliography=b))
    theses = _theses(project_id)
    qc = [dict(id="qc_" + t["id"][4:], thesis_id=t["id"], criterion_text=t["quality_criterion"]["criterion_text"],
               applicable_stage=t["quality_criterion"].get("applicable_stage"),
               auto_eligible=t["quality_criterion"].get("auto_eligible"),
               status="approved" if t["status"] == "approved" else "requires_expert_review")
          for t in theses if (t.get("quality_criterion") or {}).get("is_criterion") and t["status"] != "rejected"
          and not t.get("merged_into")]
    conflicts = db.q("SELECT id, card_ids, description, kind, status, resolution, origin FROM conflicts "
                     "WHERE project_id=?", [project_id])
    edits = db.q("SELECT entity, entity_id, action, before, after, comment, ts FROM expert_edits "
                 "WHERE project_id=? ORDER BY ts", [project_id])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "generator": {"model": SETTINGS.model, "judge_model": SETTINGS.judge_model,
                      "settings": SETTINGS.to_json() | {"cli_path": None}},
        "project": dict(id=p["id"], title=p["title"], age_category=p["age_category"],
                        icd_codes=p["icd_codes"], developer_org=p["developer_org"],
                        normative_basis="Приказ Минздрава России от 28.02.2019 № 103н (ред. от 28.09.2023)"),
        "documents": docs,
        "evidence_cards": _cards(project_id),
        "recommendations": theses,
        "conflicts": conflicts,
        "quality_criteria": qc,
        "expert_edits": edits,
        "processing_report": processing_report(project_id),
    }


# ------------------------------------------- схема преподавателя (kr_recommendation) ---

DESIGN_TO_TEACHER = {"systematic_review_other": "systematic_review_rct", "clinical_guideline": "expert_opinion",
                     "narrative_review": "expert_opinion", "textbook": "expert_opinion", "unknown": "expert_opinion"}


def teacher_export(project_id: str) -> tuple[dict, list[str]]:
    """Экспорт в kr_recommendation.schema.json. Возвращает (документ, ошибки валидации)."""
    data = build_export(project_id)
    p = data["project"]
    docs = {d["id"]: d for d in data["documents"]}
    used_ev = set()
    recs = []
    card_map = {c["id"]: c for c in data["evidence_cards"]}
    for t in data["recommendations"]:
        if t["kind"] == "narrative" or not t.get("statement"):
            continue
        include, extra_flags = draft_eligibility(t, card_map)
        if not include:
            continue  # те же правила, что и для черновика: только живые подтверждённые доказательства
        t = {**t, "review_flags": t["review_flags"] + extra_flags}
        section = t["section"]
        itype = (t.get("intervention") or {}).get("type") or intervention_type_for(section)
        refs = [e for e in t["evidence_ids"] if _card_ok(card_map.get(e))]  # только подтверждённые
        if not refs:
            if t["provenance"] != "manual":
                continue  # авто-тезис без доказательств не выдаём за «мнение экспертов»
            refs = [f"ev_expert_{t['id'][4:]}"]  # экспертный тезис без статей
        used_ev.update(refs)
        flags = list(t["review_flags"])
        if t.get("uur_preliminary"):
            flags.append("УУР предварительный — требует подтверждения экспертом")
        rec = dict(id=t["id"], kr_section=section, statement=t["statement"], direction=t["direction"] or "positive",
                   intervention=dict(type=itype, scale_ref=scale_for(section),
                                     **{k: v for k, v in (t.get("intervention") or {}).items()
                                        if k in ("action", "target_population", "comparator", "outcome", "mnn",
                                                 "atc_code") and v is not None}),
                   evidence_refs=refs, udd=t["udd"] or 5, uur=t["uur"] or "C",
                   grade_rationale=(t["grade_rationale"] or "—") + (
                       "" if t["udd"] else " [УДД не определён автоматически, указан 5 по умолчанию схемы]")
                   + ("" if t["uur"] else " [УУР не определён автоматически, указан C по умолчанию схемы]"),
                   provenance=t["provenance"], review_flags=flags,
                   consensus={"agreement": "mixed" if (t.get("consensus") or {}).get("agreement") == "single"
                              else (t.get("consensus") or {}).get("agreement", "consistent"),
                              "conflicting_refs": (t.get("consensus") or {}).get("conflicting_refs", [])},
                   icd_codes=p["icd_codes"])
        if (t.get("consensus") or {}).get("agreement") == "single":
            rec["consensus"]["agreement"] = "consistent"
        if t.get("strength_phrase"):
            rec["strength_phrase"] = t["strength_phrase"]
        if t.get("subsection_title"):
            rec["subsection_title"] = t["subsection_title"]
        if t.get("evidence_backed_core"):
            rec["evidence_backed_core"] = t["evidence_backed_core"]
        rec["expert_additions"] = [dict(claim=a["claim"], basis=a["basis"], refs=a.get("refs") or [])
                                   for a in t.get("expert_additions") or []]
        qc = t.get("quality_criterion") or {}
        if qc:
            rec["quality_criterion"] = {k: v for k, v in qc.items() if v is not None or k == "criterion_text"}
        if rec["consensus"]["agreement"] == "conflicting" and rec["uur"] == "A":
            rec["uur"] = "B"
        recs.append(rec)

    units = []
    cards = {c["id"]: c for c in data["evidence_cards"]}
    for eid in sorted(used_ev):
        if eid.startswith("ev_expert_"):
            units.append(dict(id=eid, subject="Экспертное мнение", predicate="обосновывает",
                              object="тезис без опоры на статьи", source=dict(title="Экспертное дополнение",
                                                                             study_design="expert_opinion"),
                              udd_source=5, verification_status="unverified",
                              verification_notes="Экспертное дополнение, цитаты нет"))
            continue
        c = cards[eid]
        d = docs.get(c["doc_id"], {})
        b = d.get("bibliography") or {}
        design = DESIGN_TO_TEACHER.get(b.get("study_design"), b.get("study_design") or "expert_opinion")
        section = c["kr_section"] or "7"
        table = UDD_DIAGNOSTIC if scale_for(section) == "udd_diagnostic" else UDD_TREATMENT
        src = dict(title=b.get("title") or d.get("filename", "?"), study_design=design)
        for k in ("doi", "journal"):
            if b.get(k):
                src[k] = b[k]
        if b.get("pmid"):
            src["pmid"] = b["pmid"]
        if b.get("authors"):
            src["authors"] = b["authors"]
        if b.get("year"):
            src["year"] = b["year"]
        if b.get("sample_size") is not None:
            src["sample_size"] = b["sample_size"]
        v = c["verification"]
        if v["user_status"] == "rejected":
            status = "unverified"  # отклонена экспертом — никогда не «verified»
        else:
            status = "conflicted" if v["conflicts"] else (
                "verified" if v["status"] == "verified" or v["user_status"] == "confirmed" else "unverified")
        units.append(dict(
            id=eid, subject=c["intervention"] or c["statement_ru"][:80], predicate=c["polarity"] or "descriptive",
            object=c["outcome"] or c["statement_ru"], negation=c["negation"], source=src,
            intervention_type=intervention_type_for(section),
            udd_source=None if c["secondary_citation"] else table.get(b.get("study_design") or ""),
            verification_status=status,
            verification_notes="; ".join(v["issues"] + (
                ["дизайн источника — систематический обзор без мета-анализа (в схеме нет отдельного "
                 "значения, указан systematic_review_rct)"] if b.get("study_design") == "systematic_review_other" else []))
            or "проверки пройдены",
            kr_section=section, statement_ru=c["statement_ru"], source_quote=c["source_quote"],
            pdf_page=c["pdf_page"]))
        if units[-1]["udd_source"] is None:
            units[-1]["udd_source"] = 5
            units[-1]["verification_notes"] += "; УДД источника не определён (указан 5 по умолчанию)"
    out = {"kr_meta": dict(title=p["title"], year=int(time.strftime("%Y")), icd_codes=p["icd_codes"],
                           age_category=p["age_category"], developer_org=p["developer_org"] or "",
                           schema_version="1.0"),
           "evidence_units": units, "recommendation_theses": recs}
    errors = []
    schema_path = ROOT / "kr_recommendation.schema.json"
    if schema_path.exists():
        import jsonschema
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        v = jsonschema.Draft202012Validator(schema)
        errors = [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in v.iter_errors(out)]
    return out, errors


# --------------------------------------------------------------- черновик ---

def _card_ok(c: dict | None) -> bool:
    v = (c or {}).get("verification") or {}
    return bool(c) and v.get("user_status") != "rejected" and (
        v.get("status") == "verified" or v.get("user_status") == "confirmed")


def draft_eligibility(t: dict, cards: dict) -> tuple[bool, list[str]]:
    """Можно ли выпускать тезис в подтверждённую часть черновика — по ТЕКУЩЕМУ статусу
    доказательств (карточку могли отклонить или изменить после синтеза).
    Возвращает (включать ли, дополнительные флаги)."""
    if t["status"] == "rejected" or t.get("merged_into"):
        return False, []
    if t["provenance"] == "manual":
        return True, []
    flags = []
    if t.get("orphan") and t["status"] != "approved":
        return False, ["Не воспроизведён при повторной обработке"]
    if t.get("orphan"):
        flags.append("Не воспроизведён при повторной обработке; оставлен, т.к. утверждён экспертом")
    ok = [e for e in t["evidence_ids"] if _card_ok(cards.get(e))]
    if not ok:
        return False, ["Ни одно доказательство больше не подтверждено"]
    if len(ok) < len(t["evidence_ids"]):
        flags.append(f"{len(t['evidence_ids']) - len(ok)} из {len(t['evidence_ids'])} доказательств утратили "
                     f"подтверждение (отклонены, изменены или удалены) — проверьте формулировку и УДД/УУР")
    return True, flags


def _draft_model(project_id: str) -> dict:
    """Общая модель черновика для Markdown и Word: разделы, тезисы, нумерация литературы."""
    data = build_export(project_id)
    docs = {d["id"]: d for d in data["documents"]}
    cards = {c["id"]: c for c in data["evidence_cards"]}
    lit: dict[str, int] = {}

    def cite(ev_ids):
        nums = []
        for e in ev_ids:
            c = cards.get(e)
            if not _card_ok(c):  # в литературу — только источники подтверждённых доказательств
                continue
            d = c["doc_id"]
            if d not in lit:
                lit[d] = len(lit) + 1
            nums.append(lit[d])
        return sorted(set(nums))

    sections, dropped = {}, []
    for t in data["recommendations"]:
        include, extra_flags = draft_eligibility(t, cards)
        if not include:
            if extra_flags:
                dropped.append({**t, "drop_reason": extra_flags[0]})
            continue
        t = dict(t)
        t["review_flags"] = list(t["review_flags"]) + extra_flags
        t["cites"] = cite(t["evidence_ids"])
        if t["kind"] == "narrative":
            # ссылки [ev_x] в тексте → номера литературы
            def repl(m):
                ns = cite(re.findall(r"ev_[0-9a-f]+", m.group(0)))
                return "[" + ", ".join(map(str, ns)) + "]" if ns else ""
            t["text"] = re.sub(r"\[[^\]]*ev_[^\]]*\]", repl, t["body"] or "")
        sections.setdefault(t["section"], []).append(t)
    refs = []
    for d, n in sorted(lit.items(), key=lambda x: x[1]):
        b = docs[d]["bibliography"] or {}
        authors = b.get("authors") or []
        a = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
        s = f"{a + '. ' if a else ''}{b.get('title') or docs[d]['filename']}"
        if b.get("journal"):
            s += f" // {b['journal']}"
        if b.get("year"):
            s += f". {b['year']}"
        if b.get("doi"):
            s += f". DOI: {b['doi']}"
        refs.append((n, s))
    problems = [c for c in data["evidence_cards"] if c["verification"]["status"] != "verified"
                and c["verification"]["user_status"] != "confirmed" and c["verification"]["user_status"] != "rejected"
                and c["topic_relevance"] != "none"]
    # в таблицу — только пригодные критерии (УУР A/B, диагностика/лечение) и утверждённые экспертом;
    # остальные предложения модели остаются в JSON (quality_criteria) со статусом «на проверку»
    qc_all = [t for ts in sections.values() for t in ts if (t.get("quality_criterion") or {}).get("is_criterion")]
    qc = [t for t in qc_all if t["quality_criterion"].get("auto_eligible") or t.get("status") == "approved"]
    return dict(data=data, sections=sections, refs=refs, problems=problems, qc=qc, qc_other=len(qc_all) - len(qc), dropped=dropped,
                conflicts=[c for c in data["conflicts"] if c["status"] != "resolved"])


def _grade_line(t: dict) -> str:
    if not t.get("uur") and not t.get("udd"):
        return "Уровень убедительности рекомендаций и уровень достоверности доказательств не определены автоматически — требуется оценка эксперта."
    pre = " (предварительно, требует подтверждения экспертом)" if t.get("uur_preliminary") else ""
    return (f"Уровень убедительности рекомендаций {t.get('uur') or '—'} "
            f"(уровень достоверности доказательств – {t.get('udd') or '—'}){pre}.")


def _origin_note(t: dict) -> str | None:
    if t["provenance"] == "manual":
        return f"Экспертное дополнение ({t.get('expert_author') or 'эксперт'}); не извлечено из статей."
    if t["status"] != "approved":
        return "Черновик: сформирован автоматически, экспертом не утверждён."
    return None


def to_markdown(project_id: str) -> str:
    m = _draft_model(project_id)
    p = m["data"]["project"]
    L = [f"# Клинические рекомендации (черновик)", f"## {p['title']}", "",
         f"**Кодирование по МКБ-10:** {', '.join(p['icd_codes'])}  ",
         f"**Возрастная группа:** {AGE_RU.get(p['age_category'], p['age_category'])}  ",
         f"**Разработчик:** {p['developer_org'] or '—'}  ",
         f"**Нормативная основа:** {p['normative_basis']}  ",
         f"*Черновик сформирован автоматически {m['data']['generated_at']} и требует экспертной проверки.*", ""]
    for num, title, secs in CHAPTERS:
        L += [f"## {num}. {title}", ""]
        for s in secs:
            ts = m["sections"].get(s, [])
            if len(secs) > 1:
                L += [f"### {s} {P.SECTIONS[s]}", ""]
            if not ts:
                L += [f"*{EMPTY}*", ""]
                continue
            for t in ts:
                if t["kind"] == "narrative":
                    L += [t["text"], ""]
                    continue
                cites = f" [{', '.join(map(str, t['cites']))}]" if t["cites"] else ""
                L += [f"- **{t['statement'].rstrip('.')}**{cites}.", f"  {_grade_line(t)}"]
                if t.get("comment"):
                    L.append(f"  *Комментарий:* {t['comment']}")
                note = _origin_note(t)
                if note:
                    L.append(f"  > {note}")
                for f in t["review_flags"]:
                    L.append(f"  > ⚠ {f}")
                L.append("")
    L += ["## Критерии оценки качества медицинской помощи", ""]
    if m["qc"]:
        L += ["| № | Критерий качества | Оценка выполнения |", "|---|---|---|"]
        for i, t in enumerate(m["qc"], 1):
            L.append(f"| {i} | {t['quality_criterion']['criterion_text']} | Да/Нет |")
        L += ["", "*Критерии предложены автоматически и требуют утверждения экспертом.*"
              + (f" *Ещё {m['qc_other']} предложений (УУР C или не диагностика/лечение) — в JSON-выгрузке.*"
                 if m["qc_other"] else ""), ""]
    else:
        L += [f"*{EMPTY}*", ""]
    L += ["## Список литературы", ""] + [f"{n}. {s}" for n, s in m["refs"]] + [""]
    L += ["## Приложение А2. Методология", "",
          "Уровни достоверности доказательств присвоены автоматически по дизайну исследования-источника "
          "согласно шкалам приложения А2 (для диагностических и для лечебных/профилактических/"
          "реабилитационных вмешательств). Уровни убедительности рекомендаций — предварительные, "
          "по правилам системы, и подлежат утверждению рабочей группой.", ""]
    L += ["## Служебное приложение. Требует решения эксперта", "",
          f"### Возможные противоречия ({len(m['conflicts'])})", ""]
    for c in m["conflicts"]:
        L.append(f"- {c['description']} — карточки: {', '.join(c['card_ids'])}")
    L += ["", f"### Тезисы, исключённые из черновика ({len(m['dropped'])})", ""]
    for t in m["dropped"]:
        L.append(f"- {t['statement']} — {t['drop_reason']} ({t['id']})")
    L += ["", f"### Неподтверждённые карточки ({len(m['problems'])})", ""]
    for c in m["problems"]:
        L.append(f"- [{STATUS_RU.get(c['verification']['status'], c['verification']['status'])}] "
                 f"{c['statement_ru']} (стр. {c['pdf_page']}, {c['id']}) — {'; '.join(c['verification']['issues'][:3])}")
    return "\n".join(L) + "\n"


_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def xml_safe(text) -> str:
    """Текст из PDF может содержать управляющие символы, недопустимые в XML (Word падает)."""
    return _XML_BAD.sub(" ", str(text or ""))


def to_docx(project_id: str, path: Path) -> Path:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    m = _draft_model(project_id)
    p = m["data"]["project"]
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(12)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    for name in ("Heading 1", "Heading 2", "Heading 3"):
        doc.styles[name].font.name = "Times New Roman"
        doc.styles[name].font.color.rgb = RGBColor(0, 0, 0)

    def para(text="", bold=False, italic=False, size=None, align=None, color=None):
        par = doc.add_paragraph()
        run = par.add_run(xml_safe(text))
        run.bold, run.italic = bold, italic
        if size:
            run.font.size = Pt(size)
        if color:
            run.font.color.rgb = RGBColor(*color)
        if align:
            par.alignment = align
        return par

    # титул
    para(f"Утверждено: {p['developer_org'] or '________'}", align=WD_ALIGN_PARAGRAPH.RIGHT)
    para()
    para("Клинические рекомендации", bold=True, size=16, align=WD_ALIGN_PARAGRAPH.CENTER)
    para(p["title"], bold=True, size=16, align=WD_ALIGN_PARAGRAPH.CENTER)
    para()
    para("Кодирование по Международной статистической классификации болезней и проблем, "
         "связанных со здоровьем: " + ", ".join(p["icd_codes"]))
    para(f"Возрастная группа: {AGE_RU.get(p['age_category'], p['age_category'])}")
    para(f"Год утверждения: {time.strftime('%Y')}")
    para(f"Разработчик клинической рекомендации: {p['developer_org'] or '—'}")
    para()
    para("ЧЕРНОВИК, сформированный автоматически из научных публикаций. Требует экспертной проверки. "
         "Не предназначен для лечения пациентов.", italic=True, color=(160, 0, 0),
         align=WD_ALIGN_PARAGRAPH.CENTER)
    doc.add_page_break()

    doc.add_heading("Оглавление", level=1)
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), 'TOC \\o "1-3" \\h \\z \\u')
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "Обновите поле (F9), чтобы построить оглавление."
    r.append(t)
    fld.append(r)
    doc.add_paragraph()._p.append(fld)
    doc.add_page_break()
    doc.add_heading("Список сокращений", level=1)
    para("Заполняется рабочей группой.", italic=True)
    doc.add_heading("Термины и определения", level=1)
    para("Заполняется рабочей группой.", italic=True)

    grey = (110, 110, 110)
    for num, title, secs in CHAPTERS:
        doc.add_heading(f"{num}. {title}", level=1)
        for s in secs:
            ts = m["sections"].get(s, [])
            if len(secs) > 1:
                doc.add_heading(f"{s} {P.SECTIONS[s]}", level=2)
            if not ts:
                para(EMPTY, italic=True, color=grey)
                continue
            for t in ts:
                if t["kind"] == "narrative":
                    para(t["text"])
                    continue
                par = doc.add_paragraph(style="List Bullet")
                par.add_run(xml_safe(t["statement"].rstrip("."))).bold = False
                if t["cites"]:
                    par.add_run(f" [{', '.join(map(str, t['cites']))}]")
                par.add_run(".")
                para(_grade_line(t), bold=True)
                if t.get("comment"):
                    cp = doc.add_paragraph()
                    cp.add_run("Комментарий: ").italic = True
                    cp.add_run(xml_safe(t["comment"])).italic = True
                note = _origin_note(t)
                if note:
                    para(note, italic=True, size=10, color=grey)
                for f in t["review_flags"]:
                    para("⚠ " + f, italic=True, size=10, color=(180, 90, 0))

    doc.add_heading("Критерии оценки качества медицинской помощи", level=1)
    if m["qc"]:
        tbl = doc.add_table(rows=1, cols=3)
        tbl.style = "Table Grid"
        for cell, h in zip(tbl.rows[0].cells, ("№", "Критерии качества", "Оценка выполнения (да/нет)")):
            cell.text = h
        for i, t in enumerate(m["qc"], 1):
            row = tbl.add_row().cells
            row[0].text, row[1].text, row[2].text = str(i), xml_safe(t["quality_criterion"]["criterion_text"]), "Да/Нет"
        para("Критерии предложены автоматически и требуют утверждения экспертом."
             + (f" Ещё {m['qc_other']} предложений (УУР C или не диагностика/лечение) — в JSON-выгрузке."
                if m["qc_other"] else ""), italic=True, size=10, color=grey)
    else:
        para(EMPTY, italic=True, color=grey)

    doc.add_heading("Список литературы", level=1)
    for n, s in m["refs"]:
        para(f"{n}. {s}")
    if not m["refs"]:
        para(EMPTY, italic=True, color=grey)

    doc.add_heading("Приложение А1. Состав рабочей группы по разработке и пересмотру клинических рекомендаций", level=1)
    para("Заполняется рабочей группой.", italic=True)
    doc.add_heading("Приложение А2. Методология разработки клинических рекомендаций", level=1)
    para("Черновик сформирован системой автоматического извлечения из научных публикаций: каждое утверждение "
         "связано с дословной цитатой и страницей PDF источника; цитаты, числа и отрицания проверены "
         "автоматически, смысловое соответствие — независимой языковой моделью-рецензентом. Уровни "
         "достоверности доказательств присвоены по дизайну исследования согласно шкалам ниже; уровни "
         "убедительности рекомендаций предварительные и подлежат утверждению рабочей группой.")
    for caption, rows in (
        ("Шкала оценки УДД для методов диагностики", [
            ("1", "Систематические обзоры исследований с контролем референсным методом или систематический обзор РКИ с применением метаанализа"),
            ("2", "Отдельные исследования с контролем референсным методом или отдельные РКИ и систематические обзоры исследований любого дизайна, за исключением РКИ, с применением метаанализа"),
            ("3", "Исследования без последовательного контроля референсным методом или с референсным методом, не являющимся независимым, или нерандомизированные сравнительные исследования, в том числе когортные"),
            ("4", "Несравнительные исследования, описание клинического случая"),
            ("5", "Имеется лишь обоснование механизма действия или мнение экспертов")]),
        ("Шкала оценки УДД для методов профилактики, лечения и реабилитации", [
            ("1", "Систематический обзор РКИ с применением метаанализа"),
            ("2", "Отдельные РКИ и систематические обзоры исследований любого дизайна, за исключением РКИ, с применением метаанализа"),
            ("3", "Нерандомизированные сравнительные исследования, в т.ч. когортные исследования"),
            ("4", "Несравнительные исследования, описание клинического случая или серии случаев, исследования «случай-контроль»"),
            ("5", "Имеется лишь обоснование механизма действия вмешательства (доклинические исследования) или мнение экспертов")]),
        ("Шкала оценки УУР", [
            ("A", "Сильная рекомендация (все критерии эффективности важны, все исследования высокого или удовлетворительного качества, выводы согласованы)"),
            ("B", "Условная рекомендация (не все критерии важны, не все исследования высокого качества и/или выводы не согласованы)"),
            ("C", "Слабая рекомендация (отсутствие доказательств надлежащего качества)")]),
    ):
        para(caption, bold=True)
        tbl = doc.add_table(rows=0, cols=2)
        tbl.style = "Table Grid"
        for a, b in rows:
            c = tbl.add_row().cells
            c[0].text, c[1].text = a, b
    for title in ("Приложение А3. Справочные материалы", "Приложение Б. Алгоритмы действий врача",
                  "Приложение В. Информация для пациента",
                  "Приложение Г1–ГN. Шкалы оценки, вопросники и другие оценочные инструменты"):
        doc.add_heading(title, level=1)
        para("Заполняется рабочей группой.", italic=True)

    doc.add_page_break()
    doc.add_heading("Служебное приложение. Требует решения эксперта", level=1)
    para("Не входит в текст клинической рекомендации.", italic=True, color=grey)
    doc.add_heading(f"Возможные противоречия ({len(m['conflicts'])})", level=2)
    for c in m["conflicts"]:
        para(f"• {c['description']} (карточки: {', '.join(c['card_ids'])})")
    doc.add_heading(f"Тезисы, исключённые из черновика ({len(m['dropped'])})", level=2)
    for t in m["dropped"]:
        para(f"• {t['statement']} — {t['drop_reason']} ({t['id']})", size=10)
    doc.add_heading(f"Неподтверждённые карточки ({len(m['problems'])})", level=2)
    for c in m["problems"]:
        para(f"• [{STATUS_RU.get(c['verification']['status'], '')}] {c['statement_ru']} "
             f"(стр. {c['pdf_page']}, {c['id']}): {'; '.join(c['verification']['issues'][:3])}", size=10)
    doc.save(path)
    return path


def export_all(project_id: str, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    paths = []
    full = out_dir / f"{project_id}_{stamp}.json"
    data = build_export(project_id)
    full.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    paths.append(full)
    import jsonschema
    own = json.loads((ROOT / "schemas" / "krgen_export.schema.json").read_text(encoding="utf-8"))
    own_err = [f"{'/'.join(map(str, e.absolute_path))}: {e.message}"
               for e in jsonschema.Draft202012Validator(own).iter_errors(json.loads(full.read_text(encoding="utf-8")))]
    (out_dir / f"{project_id}_{stamp}.validation.txt").write_text(
        "OK: соответствует schemas/krgen_export.schema.json\n" if not own_err else "\n".join(own_err[:200]),
        encoding="utf-8")
    t, errors = teacher_export(project_id)
    tp = out_dir / f"{project_id}_{stamp}_kr_recommendation.json"
    tp.write_text(json.dumps(t, ensure_ascii=False, indent=1), encoding="utf-8")
    paths.append(tp)
    (out_dir / f"{project_id}_{stamp}_kr_recommendation.validation.txt").write_text(
        "OK: соответствует kr_recommendation.schema.json\n" if not errors else "\n".join(errors), encoding="utf-8")
    md = out_dir / f"{project_id}_{stamp}.md"
    md.write_text(to_markdown(project_id), encoding="utf-8")
    paths.append(md)
    paths.append(to_docx(project_id, out_dir / f"{project_id}_{stamp}.docx"))
    return paths
