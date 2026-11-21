"""Экспорт КР: модель документа по приказу 103н, Markdown, Word и полный JSON проекта."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import IO

from . import db, prompts as P
from .config import SETTINGS
from .groups import group_statement
from .projects import get
from .resolve import excluded_fact_ids
from .synth import kr_evidence

AGE_RU = {"adults": "Взрослые", "children": "Дети", "mixed": "Взрослые, дети"}
DESIGN_RU = {
    "meta_analysis_rct": "метаанализ РКИ", "systematic_review_rct": "систематический обзор РКИ",
    "rct": "РКИ", "meta_analysis_diagnostic": "метаанализ диагностических исследований",
    "diagnostic_with_reference": "диагностическое исследование с референсным методом",
    "diagnostic_without_reference": "диагностическое исследование без референсного метода",
    "cohort": "когортное исследование", "case_control": "исследование «случай-контроль»",
    "non_comparative": "несравнительное исследование", "case_series": "серия случаев",
    "case_report": "описание случая", "expert_opinion": "мнение экспертов",
}
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
EMPTY = "Нет данных в загруженных источниках."
NORMATIVE_BASIS = "Приказ Минздрава России от 28.02.2019 № 103н (ред. от 28.09.2023)"
CRITERIA_TITLE = "Критерии оценки качества медицинской помощи"
REFERENCES_TITLE = "Список литературы"
A2_TITLE = "Приложение А2. Методология разработки клинических рекомендаций"
METHODOLOGY = [
    ("Шкала оценки уровней достоверности доказательств (УДД) для методов диагностики", "УДД", [
        ("1", "Систематические обзоры исследований с контролем референсным методом или систематический обзор "
              "рандомизированных клинических исследований с применением метаанализа"),
        ("2", "Отдельные исследования с контролем референсным методом или отдельные рандомизированные клинические "
              "исследования и систематические обзоры исследований любого дизайна, за исключением "
              "рандомизированных клинических исследований, с применением метаанализа"),
        ("3", "Исследования без последовательного контроля референсным методом или исследования с референсным "
              "методом, не являющимся независимым от исследуемого метода, или нерандомизированные "
              "сравнительные исследования, в том числе когортные исследования"),
        ("4", "Несравнительные исследования, описание клинического случая"),
        ("5", "Имеется лишь обоснование механизма действия или мнение экспертов")]),
    ("Шкала оценки уровней достоверности доказательств (УДД) для методов профилактики, лечения и "
     "реабилитации (профилактических, лечебных, реабилитационных вмешательств)", "УДД", [
        ("1", "Систематический обзор рандомизированных клинических исследований с применением метаанализа"),
        ("2", "Отдельные рандомизированные клинические исследования и систематические обзоры исследований "
              "любого дизайна, за исключением рандомизированных клинических исследований, с применением метаанализа"),
        ("3", "Нерандомизированные сравнительные исследования, в т.ч. когортные исследования"),
        ("4", "Несравнительные исследования, описание клинического случая или серии случаев, исследования "
              "«случай-контроль»"),
        ("5", "Имеется лишь обоснование механизма действия вмешательства (доклинические исследования) "
              "или мнение экспертов")]),
    ("Шкала оценки уровней убедительности рекомендаций (УУР) для методов профилактики, диагностики, "
     "лечения и реабилитации (профилактических, диагностических, лечебных, реабилитационных вмешательств)", "УУР", [
        ("A", "Сильная рекомендация (все рассматриваемые критерии эффективности (исходы) являются важными, "
              "все исследования имеют высокое или удовлетворительное методологическое качество, их выводы по "
              "интересующим исходам являются согласованными)"),
        ("B", "Условная рекомендация (не все рассматриваемые критерии эффективности (исходы) являются важными, "
              "не все исследования имеют высокое или удовлетворительное методологическое качество и/или их "
              "выводы по интересующим исходам не являются согласованными)"),
        ("C", "Слабая рекомендация (отсутствие доказательств надлежащего качества (все рассматриваемые "
              "критерии эффективности (исходы) являются неважными, все исследования имеют низкое "
              "методологическое качество и их выводы по интересующим исходам не являются согласованными))")]),
]


# ---------------------------------------------------------------- общее ---

def fact_status(row: dict) -> str:
    return row["user_status"] or row["status"]


def _grade(uur, udd) -> str:
    return f"Уровень убедительности рекомендаций {uur} (уровень достоверности доказательств – {udd})"


def _ref_text(doc: dict) -> str:
    b = doc["biblio"] or {}
    authors = b.get("authors") or []
    s = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
    s = f"{s.rstrip('.')}. " if s else ""
    s += b.get("title") or doc["filename"]
    if b.get("journal"):
        s += f" // {b['journal']}"
    if b.get("year"):
        s += f". {b['year']}"
    if b.get("doi"):
        s += f". DOI: {b['doi']}"
    return s


def with_refs(item: dict) -> str:
    """Текст тезиса со ссылками в конце: «Рекомендуется … [1, 2].»"""
    if item["type"] != "thesis" or not item["refs"]:
        return item["text"]
    return f"{item['text'].rstrip('.')} [{', '.join(map(str, item['refs']))}]."


# ------------------------------------------------------------ модель КР ---

def _in_kr(row: dict, facts: dict, include_rejected: bool) -> list[str] | None:
    """Доказательства тезиса, вошедшие в КР; None — тезис в КР не идёт."""
    manual = row["provenance"] == "manual"
    if row["status"] == "rejected" and not include_rejected:
        return None
    if row["orphan"] and row["status"] != "approved" and not manual:
        return None
    ids = [e for e in row["evidence_ids"] or [] if e in facts]
    return ids if ids or manual else None


def _narrative(text: str, ids: list[str], cite) -> tuple[str, list[int]]:
    """[ev_…] в абзаце → номера источников [1, 2]."""
    used: list[int] = []

    def repl(m):
        ns = cite(re.findall(r"ev_[0-9a-z_]+", m.group(0)))
        used.extend(n for n in ns if n not in used)
        return " [" + ", ".join(map(str, ns)) + "]" if ns else ""

    out = re.sub(r"\s*\[[^\]]*ev_[^\]]*\]", repl, text)
    out = re.sub(r"\.\s*(\[\d+(?:, \d+)*\])\.?", r" \1.", out)  # «текст. [1].» → «текст [1].»
    out = re.sub(r"\s+([.,;:])", r"\1", out).strip()
    if not used and ids:  # ссылки убраны правкой — ставим в конец
        used = cite(ids)
        if used:
            out = f"{out.rstrip('.')} [{', '.join(map(str, used))}]."
    return out, sorted(used)


def narrative_markers(project_id: str, thesis_id: str, text: str) -> str:
    """[n] из окна КР → [ev_…] доказательств этого абзаца из источника n."""
    m = kr_model(project_id)
    it = next((i for s in m["sections"] for i in s["items"] if i["id"] == thesis_id), None)
    if not it:
        return text
    refs = {r["n"]: r["fact_ids"] for r in m["references"]}

    def repl(mm):
        ids = [f for n in re.findall(r"\d+", mm.group(0)) for f in refs.get(int(n), []) if f in it["fact_ids"]]
        return " [" + ", ".join(dict.fromkeys(ids)) + "]" if ids else ""

    return re.sub(r"\s*\[\d+(?:\s*,\s*\d+)*\]", repl, text)


def kr_model(project_id: str, include_rejected: bool = False) -> dict:
    """КР как документ: разделы 103н с тезисами и абзацами, литература, критерии качества."""
    p = get("projects", project_id)
    evidence = kr_evidence(project_id)
    facts = {f["_id"]: f for fs in evidence.values() for f in fs}
    fact_group = {f["_id"]: gid for gid, fs in evidence.items() for f in fs}
    docs = {d["id"]: d for d in db.q("SELECT id, filename, biblio FROM documents WHERE project_id=?", [project_id])}

    by_sec: dict[str, list] = {}
    for r in db.q("SELECT * FROM theses WHERE project_id=? ORDER BY created_at, id", [project_id]):
        ids = _in_kr(r, facts, include_rejected)
        if ids is not None:
            by_sec.setdefault(r["section"], []).append((r, db.effective(r), ids))
    for items in by_sec.values():
        items.sort(key=lambda x: x[1].get("kind") != "narrative")  # абзацы раздела — перед тезисами

    lit: dict[str, int] = {}
    lit_facts: dict[str, list[str]] = {}

    def cite(ids: list[str]) -> list[int]:
        nums = set()
        for i in ids:
            if i not in facts:
                continue
            d = facts[i]["_doc"]
            if d not in lit:
                lit[d], lit_facts[d] = len(lit) + 1, []
            if i not in lit_facts[d]:
                lit_facts[d].append(i)
            nums.add(lit[d])
        return sorted(nums)

    def item(r: dict, t: dict, ids: list[str]) -> dict:
        rejected = r["status"] == "rejected"  # виден только в окне КР, без ссылок
        cit = (lambda _: []) if rejected else cite
        gids = [g for g in [r["group_id"], *(fact_group.get(i) for i in ids)] if g]
        common = dict(id=r["id"], section=r["section"], fact_ids=ids, group_ids=list(dict.fromkeys(gids)),
                      flags=r["review_flags"] or [], edited=bool(r["override"]), status=r["status"],
                      pending=bool(r["pending_auto"]), provenance=r["provenance"])
        if t.get("kind") == "narrative":
            text, refs = _narrative(t["body"], ids, cit)
            body = re.sub(r"\s*\[[^\]]*ev_[^\]]*\]", "", t["body"])
            return dict(type="text", text=text, body=re.sub(r"\s+([.,;:])", r"\1", body).strip(), refs=refs,
                        **common)
        qc = t.get("quality_criterion") or {}
        criterion = qc.get("criterion_text") if qc.get("is_criterion") and not rejected and (
            qc.get("auto_eligible") or r["status"] == "approved") else None
        # без УДД остаётся только мнение экспертов: УДД 5, УУР C
        udd, uur = t.get("udd") or 5, t.get("uur") or "C"
        return dict(type="thesis", text=t["statement"], phrase=t.get("strength_phrase") or "", body=t["body"],
                    udd=udd, uur=uur, grade=_grade(uur, udd), comment=t.get("comment") or "",
                    criterion=criterion, refs=cit(ids), **common)

    def empty() -> dict:
        return dict(type="text", id=None, section=None, text=EMPTY, refs=[], fact_ids=[], group_ids=[], flags=[],
                    edited=False, status=None, pending=False, provenance=None, empty=True)

    def sec(key: str, title: str, level: int, items: list[dict]) -> dict:
        return dict(key=key, title=title, heading=f"{key}. {title}" if level == 1 else f"{key} {title}",
                    level=level, items=items)

    sections = []
    for num, title, subs in CHAPTERS:
        if subs == [num]:
            sections.append(sec(num, title, 1, [item(*x) for x in by_sec.get(num, [])] or [empty()]))
            continue
        children = [sec(s, P.SECTIONS[s], 2, [item(*x) for x in by_sec[s]]) for s in subs if s in by_sec]
        sections.append(sec(num, title, 1, [] if children else [empty()]))
        sections += children

    theses = [it for s in sections for it in s["items"] if it["type"] == "thesis"]
    return dict(
        title=p["title"],
        meta=dict(icd_codes=p["icd_codes"], age_category=p["age_category"], age_label=AGE_RU[p["age_category"]],
                  developer_org=p["developer_org"] or "", year=int(time.strftime("%Y"))),
        sections=sections,
        references=[dict(n=n, text=_ref_text(docs[d]), doc_id=d, fact_ids=lit_facts[d])
                    for d, n in sorted(lit.items(), key=lambda x: x[1])],
        criteria=[dict(text=t["criterion"], thesis_id=t["id"], udd=t["udd"], uur=t["uur"])
                  for t in theses if t["criterion"]],
    )


# ------------------------------------------------------------- Markdown ---

def _md_cell(s: str) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def to_markdown(project_id: str, model: dict | None = None) -> str:
    m = model or kr_model(project_id)
    meta = m["meta"]
    L = [f"# {m['title']}", "", "Клинические рекомендации", "",
         f"Кодирование по МКБ-10: {', '.join(meta['icd_codes'])}  ",
         f"Возрастная группа: {meta['age_label']}  ",
         f"Год утверждения: {meta['year']}  "]
    if meta["developer_org"]:
        L.append(f"Разработчик клинической рекомендации: {meta['developer_org']}  ")
    L.append("")
    for s in m["sections"]:
        L += [("## " if s["level"] == 1 else "### ") + s["heading"], ""]
        for it in s["items"]:
            if it["type"] == "text":
                L += [it["text"], ""]
                continue
            L += [f"- {with_refs(it)}  ", f"  **{it['grade']}**  "]
            if it["comment"]:
                L.append(f"  Комментарий: {it['comment']}")
            L.append("")
    L += [f"## {CRITERIA_TITLE}", ""]
    if m["criteria"]:
        L += ["| № | Критерии качества | Оценка выполнения |", "|---|---|---|"]
        L += [f"| {i} | {_md_cell(c['text'])} | Да/Нет |" for i, c in enumerate(m["criteria"], 1)]
    else:
        L.append(EMPTY)
    L += ["", f"## {REFERENCES_TITLE}", ""]
    L += [f"{r['n']}. {r['text']}" for r in m["references"]] or [EMPTY]
    L += ["", f"## {A2_TITLE}", ""]
    for caption, head, rows in METHODOLOGY:
        L += [f"**{caption}**", "", f"| {head} | Расшифровка |", "|---|---|"]
        L += [f"| {a} | {_md_cell(b)} |" for a, b in rows] + [""]
    return "\n".join(L).rstrip() + "\n"


# ----------------------------------------------------------------- Word ---

_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def xml_safe(text) -> str:
    """Управляющие символы из PDF недопустимы в XML документа Word."""
    return _XML_BAD.sub(" ", str(text or ""))


def to_docx(project_id: str, path: Path | IO[bytes], model: dict | None = None) -> Path | IO[bytes]:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    m = model or kr_model(project_id)
    meta = m["meta"]
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(12)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    for name in ("Heading 1", "Heading 2"):
        doc.styles[name].font.name = "Times New Roman"
        doc.styles[name].font.color.rgb = RGBColor(0, 0, 0)

    def para(text="", bold=False, italic=False, size=None, align=None, style=None):
        par = doc.add_paragraph(style=style)
        run = par.add_run(xml_safe(text))
        run.bold, run.italic = bold, italic
        if size:
            run.font.size = Pt(size)
        if align is not None:
            par.alignment = align
        return par

    def table(header: tuple, rows: list[tuple]):
        tbl = doc.add_table(rows=1, cols=len(header))
        tbl.style = "Table Grid"
        for cell, h in zip(tbl.rows[0].cells, header):
            cell.text = h
        for row in rows:
            for cell, v in zip(tbl.add_row().cells, row):
                cell.text = xml_safe(v)

    center = WD_ALIGN_PARAGRAPH.CENTER
    para("Клинические рекомендации", bold=True, size=16, align=center)
    para(m["title"], bold=True, size=16, align=center)
    para()
    para("Кодирование по Международной статистической классификации болезней и проблем, "
         "связанных со здоровьем: " + ", ".join(meta["icd_codes"]))
    para(f"Возрастная группа: {meta['age_label']}")
    para(f"Год утверждения: {meta['year']}")
    if meta["developer_org"]:
        para(f"Разработчик клинической рекомендации: {meta['developer_org']}")
    doc.add_page_break()

    for s in m["sections"]:
        doc.add_heading(xml_safe(s["heading"]), level=s["level"])
        for it in s["items"]:
            if it["type"] == "text":
                para(it["text"])
                continue
            para(with_refs(it), style="List Bullet")
            para(it["grade"], bold=True)
            if it["comment"]:
                cp = doc.add_paragraph()
                cp.add_run("Комментарий: ").italic = True
                cp.add_run(xml_safe(it["comment"])).italic = True

    doc.add_heading(CRITERIA_TITLE, level=1)
    if m["criteria"]:
        table(("№", "Критерии качества", "Оценка выполнения"),
              [(str(i), c["text"], "Да/Нет") for i, c in enumerate(m["criteria"], 1)])
    else:
        para(EMPTY)
    doc.add_heading(REFERENCES_TITLE, level=1)
    for r in m["references"]:
        para(f"{r['n']}. {r['text']}")
    if not m["references"]:
        para(EMPTY)
    doc.add_heading(A2_TITLE, level=1)
    for caption, head, rows in METHODOLOGY:
        para(caption, bold=True)
        table((head, "Расшифровка"), rows)
        para()
    doc.save(path)
    return path


# ------------------------------------------------------------ полный JSON ---

def build_export(project_id: str, model: dict | None = None) -> dict:
    """Всё по проекту: документы, доказательства с проверками, группы, противоречия, тезисы, КР, журнал правок."""
    from .pipeline import processing_report

    p = get("projects", project_id)
    m = model or kr_model(project_id)
    in_kr = {i for s in m["sections"] for it in s["items"] for i in it["fact_ids"]}
    excluded = excluded_fact_ids(project_id)
    group_of = {i: g["id"] for g in db.q("SELECT id, card_ids FROM fact_groups WHERE project_id=?", [project_id])
                for i in g["card_ids"]}
    facts = []
    for r in db.q("SELECT * FROM cards WHERE project_id=? ORDER BY doc_id, id", [project_id]):
        facts.append(dict(id=r["id"], doc_id=r["doc_id"], **db.effective(r), status=fact_status(r),
                          verification=r["verification"], group_id=group_of.get(r["id"]),
                          excluded_by_conflict=r["id"] in excluded, in_kr=r["id"] in in_kr,
                          edited_by_expert=bool(r["override"])))
    theses = []
    for r in db.q("SELECT * FROM theses WHERE project_id=? ORDER BY section, created_at", [project_id]):
        theses.append(dict(id=r["id"], section=r["section"], group_id=r["group_id"], **db.effective(r),
                           evidence_refs=r["evidence_ids"], status=r["status"], provenance=r["provenance"],
                           review_flags=r["review_flags"], orphan=bool(r["orphan"]),
                           edited_by_expert=bool(r["override"])))
    docs = db.q("SELECT id, filename, sha256, n_pages, status, status_detail, duplicate_of, biblio FROM documents "
                "WHERE project_id=? ORDER BY filename", [project_id])
    groups = db.q("SELECT id, section, statement, override, card_ids, excluded FROM fact_groups "
                  "WHERE project_id=? ORDER BY section, id", [project_id])
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "settings": SETTINGS.to_json() | {"cli_path": None},
        "project": {k: p[k] for k in ("id", "title", "age_category", "icd_codes", "developer_org")},
        "documents": docs,
        "facts": facts,
        "groups": [dict(g, statement=group_statement(g)) for g in groups],
        "conflicts": db.q("SELECT id, section, kind, description, side_a, side_b, decision, decided_by, reason "
                          "FROM conflicts WHERE project_id=? ORDER BY created_at, id", [project_id]),
        "theses": theses,
        "kr": m,
        "expert_edits": db.q("SELECT entity, entity_id, action, before, after, comment, ts FROM expert_edits "
                             "WHERE project_id=? ORDER BY ts", [project_id]),
        "processing_report": processing_report(project_id),
    }


def export_all(project_id: str, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"{project_id}_{time.strftime('%Y%m%d_%H%M')}"
    m = kr_model(project_id)
    full = Path(f"{base}.json")
    full.write_text(json.dumps(build_export(project_id, m), ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")
    md = Path(f"{base}.md")
    md.write_text(to_markdown(project_id, m), encoding="utf-8")
    return [full, md, to_docx(project_id, Path(f"{base}.docx"), m)]
