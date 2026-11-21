"""Проект и правки эксперта (каждая пишется в журнал expert_edits)."""
from __future__ import annotations

import hashlib
import re
import time
import uuid

from . import db, ontology as O, prompts as P
from .groups import group_statement, live_facts
from .resolve import DECISIONS, decide
from .verify import recheck


class NotFound(LookupError):
    pass


def get(table: str, id_: str) -> dict:
    row = db.q1(f"SELECT * FROM {table} WHERE id=?", [id_])
    if not row:
        raise NotFound(id_)
    return row


def create_project(title: str, age_category: str, icd_codes: list[str], developer_org: str,
                   project_id: str | None = None) -> dict:
    """Метаданные КР (kr_meta онтологии)."""
    title = title.strip()
    icd_codes = [c.strip().upper() for c in icd_codes if c.strip()]
    if len(title) < 5:
        raise ValueError("Тема — не короче 5 символов")
    if not icd_codes:
        raise ValueError("Укажите хотя бы один код МКБ-10")
    bad = [c for c in icd_codes if not re.match(O.ICD_PATTERN, c)]
    if bad:
        raise ValueError(f"Некорректные коды МКБ-10: {', '.join(bad)}")
    if age_category not in O.AGE_CATEGORIES:
        raise ValueError("Возрастная группа: " + " | ".join(O.AGE_CATEGORIES))
    row = dict(id=project_id or "prj_" + uuid.uuid4().hex[:8], title=title, age_category=age_category,
               icd_codes=icd_codes, developer_org=developer_org, keywords=None, created_at=time.time(), meta={})
    db.upsert("projects", row)
    return row


# ---------------------------------------------------------------- факты ---

CARD_EDITABLE = {*P.CLAIM_PROPS, "kr_section", "expert_comment"}


def _check_card(fields: dict) -> None:
    props = {**P.CLAIM_PROPS, "kr_section": {"type": "string", "enum": [*O.SECTIONS_ENUM, P.UNASSIGNED]},
             "expert_comment": {"type": "string"}}
    for k, v in fields.items():
        spec = props[k]
        if spec["type"] == "integer" and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
            raise ValueError("Страница: целое число от 1")
        if spec["type"] == "boolean" and not isinstance(v, bool):
            raise ValueError(f"{k}: true или false")
        if spec["type"] == "string" and not isinstance(v, str):
            raise ValueError(f"{k}: ожидается строка")
        if "enum" in spec and v not in spec["enum"] and not (k == "intervention_type" and v == ""):
            raise ValueError(f"{k}: " + " | ".join(spec["enum"]))
    if "statement_ru" in fields and not fields["statement_ru"].strip():
        raise ValueError("Формулировка не может быть пустой")


def edit_card(card_id: str, fields: dict, comment: str = "") -> None:
    row = get("cards", card_id)
    fields = {k: v for k, v in fields.items() if k in CARD_EDITABLE}
    _check_card(fields)
    before = {k: db.effective(row).get(k) for k in fields}
    db.update("cards", card_id, override={**(row["override"] or {}), **fields}, updated_at=time.time())
    db.log_edit(row["project_id"], "card", card_id, "edit", before, fields, comment)
    recheck(get("cards", card_id))  # правка числа или цитаты сразу пересчитывает проверки


def set_card_status(card_id: str, user_status: str | None, comment: str = "") -> None:
    if user_status not in ("confirmed", "rejected", None):
        raise ValueError("Статус: confirmed | rejected | null")
    row = get("cards", card_id)
    db.update("cards", card_id, user_status=user_status, updated_at=time.time())
    db.log_edit(row["project_id"], "card", card_id, f"status:{user_status}", row["user_status"], user_status, comment)


def accept_pending(entity: str, id_: str, accept: bool) -> None:
    """Принять или отклонить новую автоверсию, пришедшую после правки эксперта."""
    table = {"card": "cards", "thesis": "theses"}[entity]
    row = get(table, id_)
    if not row["pending_auto"]:
        return
    if accept:
        db.update(table, id_, auto=row["pending_auto"], override=None, pending_auto=None)
        if entity == "card":
            db.update(table, id_, user_status=None)  # подтверждалось прежнее содержимое
    else:
        db.update(table, id_, pending_auto=None)
    if entity == "card":
        recheck(get("cards", id_))
    db.log_edit(row["project_id"], entity, id_, "pending:" + ("accept" if accept else "keep_mine"),
                row["override"], row["pending_auto"] if accept else None)


# --------------------------------------------------- группы и противоречия ---

def edit_group(group_id: str, statement: str | None = None, excluded: bool | None = None) -> None:
    row = get("fact_groups", group_id)
    before = dict(statement=group_statement(row), excluded=bool(row["excluded"]))
    upd, after = {}, {}
    if statement is not None:
        s = statement.strip()
        # пустая строка или исходный текст — вернуть автоматическую формулировку
        upd["override"] = {"statement": s} if s and s != row["statement"] else None
        after["statement"] = s
    if excluded is not None:
        upd["excluded"] = int(excluded)
        after["excluded"] = excluded
    if upd:
        db.update("fact_groups", group_id, **upd)
        db.log_edit(row["project_id"], "group", group_id, "edit", before, after)


def decide_conflict(conflict_id: str, decision: str | None, comment: str = "") -> None:
    if decision is not None and decision not in DECISIONS:
        raise ValueError("Решение: " + " | ".join(DECISIONS))
    row = get("conflicts", conflict_id)
    if decision is None:  # сброс решения эксперта — снова по правилам
        d, reason = decide(row["side_a"], row["side_b"], row["section"], live_facts(row["project_id"]), row["kind"])
        upd = dict(decision=d, decided_by="auto" if d else None, reason=reason)
    else:
        upd = dict(decision=decision, decided_by="expert", reason=comment.strip())
    db.update("conflicts", conflict_id, **upd)
    db.log_edit(row["project_id"], "conflict", conflict_id, "decision",
                dict(decision=row["decision"], decided_by=row["decided_by"]), decision, comment)


def kr_is_stale(project_id: str) -> bool:
    """КР собрана раньше последних правок эксперта или подтверждённый факт ещё не в группе."""
    built = (get("projects", project_id)["meta"] or {}).get("kr_built_at")
    if built is None:
        return False
    grouped = {i: g["section"] for g in db.q("SELECT section, card_ids FROM fact_groups WHERE project_id=?",
                                             [project_id]) for i in g["card_ids"]}
    if any(grouped.get(i) != c["kr_section"] for i, c in live_facts(project_id).items()):
        return True
    last = db.q1("SELECT MAX(ts) t FROM expert_edits WHERE project_id=? AND entity IN ('card', 'group', 'conflict')",
                 [project_id])["t"]
    return bool(last and last > built)


# --------------------------------------------------------------- тезисы ---

THESIS_EDITABLE = {"body", "strength_phrase", "direction", "kr_section", "subsection_title", "comment", "udd", "uur",
                   "grade_rationale"}


def edit_thesis(thesis_id: str, fields: dict, comment: str = "") -> None:
    row = get("theses", thesis_id)
    fields = {k: v for k, v in fields.items() if k in THESIS_EDITABLE}
    eff = db.effective(row)
    override = {**(row["override"] or {}), **fields}
    phrase = override.get("strength_phrase", eff.get("strength_phrase"))
    body = (override.get("body", eff.get("body")) or "").rstrip(".")
    # у абзаца описательного раздела нет вводной фразы
    override["statement"] = f"{phrase} {body}." if phrase and eff.get("kind") != "narrative" else body
    upd = dict(override=override, updated_at=time.time())
    if row["provenance"] == "auto":
        upd["provenance"] = "auto+expert_review"
    if "kr_section" in fields:
        upd["section"] = fields["kr_section"]
    db.update("theses", thesis_id, **upd)
    db.log_edit(row["project_id"], "thesis", thesis_id, "edit", {k: eff.get(k) for k in fields}, fields, comment)


def set_thesis_status(thesis_id: str, status: str, comment: str = "") -> None:
    if status not in ("draft", "approved", "rejected"):
        raise ValueError("Статус: draft | approved | rejected")
    row = get("theses", thesis_id)
    upd = dict(status=status, updated_at=time.time())
    if row["provenance"] == "auto":
        upd["provenance"] = "auto+expert_review"
    db.update("theses", thesis_id, **upd)
    db.log_edit(row["project_id"], "thesis", thesis_id, f"status:{status}", row["status"], status, comment)


def add_expert_thesis(project_id: str, section: str, phrase: str, body: str, comment: str = "") -> str:
    """Тезис эксперта (provenance=manual); без вводной фразы — абзац описательного раздела."""
    get("projects", project_id)
    tid = "rec_expert_" + hashlib.sha1(f"{time.time()}{body}".encode()).hexdigest()[:8]
    body = body.strip().rstrip(".")
    if phrase:
        auto = dict(kr_section=section, strength_phrase=phrase, body=body, statement=f"{phrase} {body}.",
                    direction="negative" if phrase.startswith("Не") else "positive", comment=comment,
                    udd=5, uur="C", grade_rationale="Мнение экспертов",
                    expert_additions=[dict(claim=body, basis="expert_consensus", refs=[])])
    else:
        auto = dict(kind="narrative", kr_section=section, body=body, statement=f"{body}.")
    now = time.time()
    db.upsert("theses", dict(id=tid, project_id=project_id, section=section, group_id=None, auto=auto,
                             evidence_ids=[], status="approved", provenance="manual", review_flags=[], orphan=0,
                             created_at=now, updated_at=now))
    db.log_edit(project_id, "thesis", tid, "create_expert", None, auto, comment)
    return tid
