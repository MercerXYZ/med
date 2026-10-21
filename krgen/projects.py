"""Операции с проектом и правки эксперта (с журналом expert_edits)."""
from __future__ import annotations

import hashlib
import re
import time
import uuid

from . import db, prompts as P
from .groups import group_statement, live_facts
from .resolve import DECISIONS, decide, sides
from .verify import recheck


class NotFound(LookupError):
    pass


def _get(table: str, id_: str) -> dict:
    row = db.q1(f"SELECT * FROM {table} WHERE id=?", [id_])
    if not row:
        raise NotFound(id_)
    return row


ICD_RE = re.compile(r"^[A-ZА-Я][0-9]{2}(\.[0-9]{1,2})?$")


def create_project(title: str, age_category: str, icd_codes: list[str], developer_org: str,
                   project_id: str | None = None) -> dict:
    title = title.strip()
    if len(title) < 5:  # minLength в kr_recommendation.schema.json
        raise ValueError("Тема — не короче 5 символов")
    icd_codes = [c.strip().upper() for c in icd_codes if c.strip()]
    if not icd_codes:
        raise ValueError("Укажите хотя бы один код МКБ-10")
    bad = [c for c in icd_codes if not ICD_RE.match(c)]
    if bad:
        raise ValueError(f"Некорректные коды МКБ-10: {', '.join(bad)}")
    if age_category not in ("adults", "children", "mixed"):
        raise ValueError("age_category: adults | children | mixed")
    pid = project_id or "prj_" + uuid.uuid4().hex[:8]
    row = dict(id=pid, title=title, age_category=age_category, icd_codes=icd_codes,
               developer_org=developer_org, keywords=None, created_at=time.time(), meta={})
    db.upsert("projects", row)
    return row


# ---------------------------------------------------------------- факты ---

CARD_EDITABLE = {"statement_ru", "claim_type", "polarity", "population", "age_group", "intervention",
                 "comparator", "outcome", "conditions", "dose", "timing", "thresholds",
                 "intervention_type", "kr_section", "topic_relevance", "page", "quote", "expert_comment",
                 "secondary_citation"}
CARD_STATUSES = ("confirmed", "rejected", None)


def _recheck(card_id: str) -> None:
    recheck(db.q1("SELECT * FROM cards WHERE id=?", [card_id]))


def _check_card(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        spec = P.CLAIM_PROPS.get(k, {"type": "string"})  # expert_comment — строка
        if k == "page":
            if v in (None, ""):
                v = None
            elif isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise ValueError("Страница: целое число от 1")
        elif spec["type"] == "boolean":
            if not isinstance(v, bool):
                raise ValueError(f"{k}: true или false")
        elif not isinstance(v, str):
            raise ValueError(f"{k}: ожидается строка")
        elif "enum" in spec and v not in spec["enum"]:
            raise ValueError(f"{k}: " + " | ".join(spec["enum"]))
        elif k == "statement_ru" and not v.strip():
            raise ValueError("Формулировка не может быть пустой")
        out[k] = v
    return out


def edit_card(card_id: str, fields: dict, comment: str = "") -> dict:
    row = _get("cards", card_id)
    fields = _check_card({k: v for k, v in fields.items() if k in CARD_EDITABLE})
    before = {k: db.effective(row).get(k) for k in fields}
    override = dict(row.get("override") or {})
    override.update(fields)
    db.update("cards", card_id, override=override, updated_at=time.time())
    db.log_edit(row["project_id"], "card", card_id, "edit", before, fields, comment)
    _recheck(card_id)  # правка числа или цитаты сразу пересчитывает проверки
    return db.q1("SELECT * FROM cards WHERE id=?", [card_id])


def set_card_status(card_id: str, user_status: str | None, comment: str = "") -> None:
    if user_status not in CARD_STATUSES:
        raise ValueError("Статус: confirmed | rejected | null")
    row = _get("cards", card_id)
    db.update("cards", card_id, user_status=user_status, updated_at=time.time())
    db.log_edit(row["project_id"], "card", card_id, f"status:{user_status}", row["user_status"], user_status, comment)


def accept_pending(entity: str, id_: str, accept: bool) -> None:
    """Принять или отклонить новую автоверсию, пришедшую после правки эксперта."""
    table = {"card": "cards", "thesis": "theses"}[entity]
    row = _get(table, id_)
    if not row.get("pending_auto"):
        return
    if accept:
        new = dict(row["pending_auto"])
        new_ids = new.pop("_evidence_ids", None)  # тезис пересобран из другого набора фактов
        db.update(table, id_, auto=new, override=None, pending_auto=None)
        if new_ids:
            db.update(table, id_, evidence_ids=new_ids)
        if entity == "card" and row.get("user_status") == "confirmed":
            db.update(table, id_, user_status=None)  # подтверждалось прежнее содержимое
    else:
        db.update(table, id_, pending_auto=None)
    if entity == "card":
        _recheck(id_)
    db.log_edit(row["project_id"], entity, id_, "pending:" + ("accept" if accept else "keep_mine"),
                row.get("override"), row["pending_auto"] if accept else None)


# --------------------------------------------------- группы и противоречия ---

def edit_group(group_id: str, statement: str | None = None, excluded: bool | None = None) -> dict:
    row = _get("clusters", group_id)
    before = dict(statement=group_statement(row), excluded=bool(row.get("excluded")))
    upd, after = {}, {}
    if statement is not None:
        s = statement.strip()
        override = dict(row.get("override") or {})
        if s and s != (row.get("statement") or ""):
            override["statement"] = s
        else:
            override.pop("statement", None)  # пустая строка — вернуть автоматическую формулировку
        upd["override"] = override or None
        after["statement"] = s
    if excluded is not None:
        upd["excluded"] = int(bool(excluded))
        after["excluded"] = bool(excluded)
    if upd:
        db.update("clusters", group_id, **upd)
        db.log_edit(row["project_id"], "group", group_id, "edit", before, after)
    return db.q1("SELECT * FROM clusters WHERE id=?", [group_id])


def decide_conflict(conflict_id: str, decision: str | None, comment: str = "") -> dict:
    if decision is not None and decision not in DECISIONS:
        raise ValueError("Решение: " + " | ".join(DECISIONS))
    row = _get("conflicts", conflict_id)
    a, b = sides(row)
    if decision is None:  # сброс решения эксперта — снова по правилам
        d, reason = decide(a, b, row.get("section"), live_facts(row["project_id"]), row.get("kind"))
        upd = dict(decision=d, decided_by="auto" if d else None, reason=reason)
    else:
        upd = dict(decision=decision, decided_by="expert", reason=comment.strip())
    db.update("conflicts", conflict_id, side_a=a, side_b=b, status="resolved" if upd["decision"] else "open", **upd)
    db.log_edit(row["project_id"], "conflict", conflict_id, "decision",
                dict(decision=row.get("decision"), decided_by=row.get("decided_by")), decision, comment)
    return db.q1("SELECT * FROM conflicts WHERE id=?", [conflict_id])


def kr_is_stale(project_id: str) -> bool:
    p = _get("projects", project_id)
    built = (p.get("meta") or {}).get("kr_built_at")
    if built is None:  # КР собиралась до появления отметки времени
        built = db.q1("SELECT MAX(updated_at) t FROM theses WHERE project_id=? AND provenance!='manual'",
                      [project_id])["t"]
        if built is None:
            return False
    # живой факт вне группы своего раздела: подтверждён или изменён после группировки
    grouped = {i: g["section"] for g in db.q("SELECT section, card_ids FROM clusters WHERE project_id=?", [project_id])
               for i in g.get("card_ids") or []}
    if any(grouped.get(i) != (c.get("kr_section") or "7") for i, c in live_facts(project_id).items()):
        return True
    last = db.q1("SELECT MAX(ts) t FROM expert_edits WHERE project_id=? AND entity IN ('card', 'group', 'conflict')",
                 [project_id])["t"]
    return bool(last and last > built)


# --------------------------------------------------------------- тезисы ---

THESIS_EDITABLE = {"body", "strength_phrase", "direction", "kr_section", "subsection_title", "comment",
                   "udd", "uur", "grade_rationale", "quality_criterion", "expert_additions",
                   "evidence_backed_core"}
THESIS_STATUSES = ("draft", "approved", "rejected")


def edit_thesis(thesis_id: str, fields: dict, comment: str = "") -> None:
    row = _get("theses", thesis_id)
    fields = {k: v for k, v in fields.items() if k in THESIS_EDITABLE}
    eff = db.effective(row)
    before = {k: eff.get(k) for k in fields}
    override = dict(row.get("override") or {})
    override.update(fields)
    if "uur" in fields or "udd" in fields:
        override["uur_preliminary"] = False
        override["grade_set_by"] = "expert"
    phrase = override.get("strength_phrase", eff.get("strength_phrase"))
    body = override.get("body", eff.get("body")) or ""
    if eff.get("kind") == "narrative" or not phrase:
        override["statement"] = body  # описательный абзац без вводной фразы
    else:
        override["statement"] = f"{phrase} {body.rstrip('.')}."
    upd = dict(override=override, updated_at=time.time(),
               provenance="manual" if row["provenance"] == "manual" else "auto+expert_review")
    if "kr_section" in fields:
        upd["section"] = fields["kr_section"]
    db.update("theses", thesis_id, **upd)
    db.log_edit(row["project_id"], "thesis", thesis_id, "edit", before, fields, comment)


def set_thesis_status(thesis_id: str, status: str, comment: str = "") -> None:
    if status not in THESIS_STATUSES:
        raise ValueError("Статус: " + " | ".join(THESIS_STATUSES))
    row = _get("theses", thesis_id)
    db.update("theses", thesis_id, status=status, updated_at=time.time(),
              provenance="manual" if row["provenance"] == "manual" else "auto+expert_review")
    db.log_edit(row["project_id"], "thesis", thesis_id, f"status:{status}", row["status"], status, comment)


def add_expert_thesis(project_id: str, section: str, phrase: str, body: str, comment: str = "",
                      author: str = "", evidence_ids: list[str] | None = None, uur: str | None = None,
                      udd: int | None = None) -> str:
    _get("projects", project_id)
    tid = "rec_expert_" + hashlib.sha1(f"{time.time()}{body}".encode()).hexdigest()[:8]
    phrase = (phrase or "").strip()
    body = body.strip().rstrip(".")
    auto = dict(kr_section=section, subsection_title="", strength_phrase=phrase or None, body=body,
                statement=f"{phrase} {body}." if phrase else f"{body}.",
                direction="negative" if phrase.startswith("Не") else "positive",
                intervention=None, evidence_backed_core="",
                expert_additions=[{"claim": body, "basis": "expert_consensus", "refs": []}],
                comment=comment, udd=udd or (None if evidence_ids or not phrase else 5), uur=uur,
                uur_preliminary=False, grade_rationale="Экспертное дополнение" + (f": {author}" if author else ""),
                consensus=dict(agreement="single", conflicting_refs=[]),
                quality_criterion=dict(is_criterion=False, criterion_text=None),
                expert_author=author, explicit_source_recommendation=False)
    if not phrase:  # без вводной фразы — абзац описательного раздела
        auto["kind"] = "narrative"
    now = time.time()
    db.upsert("theses", dict(id=tid, project_id=project_id, section=section, cluster_id=None, auto=auto,
                             override=None, pending_auto=None, evidence_ids=evidence_ids or [],
                             status="approved", provenance="manual", review_flags=[],
                             orphan=0, run_id=None, created_at=now, updated_at=now))
    db.log_edit(project_id, "thesis", tid, "create_expert", None, auto, comment)
    return tid
