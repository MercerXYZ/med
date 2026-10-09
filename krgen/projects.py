"""Операции с проектом и правки эксперта (с журналом expert_edits)."""
from __future__ import annotations

import hashlib
import re
import time
import uuid

from . import db


class NotFound(LookupError):
    """Нет такой карточки / тезиса / противоречия (сервер отвечает 404)."""


def _get(table: str, id_: str) -> dict:
    row = db.q1(f"SELECT * FROM {table} WHERE id=?", [id_])
    if not row:
        raise NotFound(id_)
    return row

ICD_RE = re.compile(r"^[A-ZА-Я][0-9]{2}(\.[0-9]{1,2})?$")


def create_project(title: str, age_category: str, icd_codes: list[str], developer_org: str,
                   project_id: str | None = None) -> dict:
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


# ------------------------------------------------------------- карточки ---

CARD_EDITABLE = {"statement_ru", "claim_type", "polarity", "population", "age_group", "intervention",
                 "comparator", "outcome", "conditions", "dose", "timing", "thresholds",
                 "intervention_type", "kr_section", "topic_relevance", "page", "quote", "expert_comment",
                 "secondary_citation"}


def edit_card(card_id: str, fields: dict, comment: str = "") -> dict:
    row = _get("cards", card_id)
    fields = {k: v for k, v in fields.items() if k in CARD_EDITABLE}
    before = {k: db.effective(row).get(k) for k in fields}
    override = dict(row.get("override") or {})
    override.update(fields)
    db.update("cards", card_id, override=override, updated_at=time.time())
    db.log_edit(row["project_id"], "card", card_id, "edit", before, fields, comment)
    # сразу пересчитать детерминированные проверки (без LLM): правка числа или цитаты не должна
    # оставлять карточку «подтверждённой» до следующего прогона
    from .verify import recheck
    recheck(db.q1("SELECT * FROM cards WHERE id=?", [card_id]))
    return db.q1("SELECT * FROM cards WHERE id=?", [card_id])


def set_card_status(card_id: str, user_status: str | None, comment: str = "") -> None:
    """confirmed | rejected | None (сброс) — решение по содержанию;
    unmerged — разъединить повтор (подтверждение/отклонение при этом не меняется)."""
    row = _get("cards", card_id)
    if user_status == "unmerged":
        db.update("cards", card_id, merged_into=None, merge_lock="unmerged", updated_at=time.time())
        db.log_edit(row["project_id"], "card", card_id, "unmerge", row["merged_into"], None, comment)
        return
    db.update("cards", card_id, user_status=user_status, updated_at=time.time())
    db.log_edit(row["project_id"], "card", card_id, f"status:{user_status}", row["user_status"], user_status, comment)


def merge_cards(canonical: str, others: list[str], comment: str = "") -> None:
    row = _get("cards", canonical)
    for o in others:
        db.update("cards", o, merged_into=canonical, merge_lock="merged", updated_at=time.time())
    db.log_edit(row["project_id"], "card", canonical, "merge", None, others, comment)


def accept_pending(entity: str, id_: str, accept: bool) -> None:
    """Принять/отклонить новую автоверсию, пришедшую после правки эксперта."""
    table = {"card": "cards", "thesis": "theses"}[entity]
    row = db.q1(f"SELECT * FROM {table} WHERE id=?", [id_])
    if not row or not row.get("pending_auto"):
        return
    if accept:
        # ручные связи тезиса с карточками переживают принятие новой формулировки
        keep = {"evidence_set_by_expert": True} if (row.get("override") or {}).get("evidence_set_by_expert") else None
        new = dict(row["pending_auto"])
        new_ids = new.pop("_evidence_ids", None)  # тезис пересобран из другого набора карточек
        db.update(table, id_, auto=new, override=keep, pending_auto=None)
        if new_ids and not keep:
            db.update(table, id_, evidence_ids=new_ids)
        if entity == "card" and row.get("user_status") == "confirmed":
            # эксперт подтверждал прежнее содержимое; новое (другие числа/цитата) требует нового решения
            db.update(table, id_, user_status=None)
    else:
        db.update(table, id_, pending_auto=None)
    if entity == "card":  # новое содержимое карточки сразу проходит детерминированные проверки
        from .verify import recheck
        recheck(db.q1("SELECT * FROM cards WHERE id=?", [id_]))
    db.log_edit(row["project_id"], entity, id_, "pending:" + ("accept" if accept else "keep_mine"),
                row.get("override"), row["pending_auto"] if accept else None)


# --------------------------------------------------------------- тезисы ---

THESIS_EDITABLE = {"body", "strength_phrase", "direction", "kr_section", "subsection_title", "comment",
                   "udd", "uur", "grade_rationale", "quality_criterion", "expert_additions",
                   "evidence_backed_core"}


def edit_thesis(thesis_id: str, fields: dict, comment: str = "") -> None:
    row = _get("theses", thesis_id)
    fields = {k: v for k, v in fields.items() if k in THESIS_EDITABLE}
    eff = db.effective(row)
    before = {k: eff.get(k) for k in fields}
    override = dict(row.get("override") or {})
    override.update(fields)
    if "uur" in fields or "udd" in fields:
        override["uur_preliminary"] = False  # решение эксперта
        override["grade_set_by"] = "expert"
    phrase = override.get("strength_phrase", eff.get("strength_phrase"))
    body = override.get("body", eff.get("body"))
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


def set_thesis_evidence(thesis_id: str, evidence_ids: list[str], comment: str = "") -> None:
    row = _get("theses", thesis_id)
    missing = [e for e in evidence_ids if not db.q1("SELECT 1 FROM cards WHERE id=? AND project_id=?",
                                                     [e, row["project_id"]])]
    if missing:
        raise ValueError("Карточки не найдены в проекте: " + ", ".join(missing))
    # отметка в override защищает ручные связи от перезаписи при повторной обработке
    override = {**(row.get("override") or {}), "evidence_set_by_expert": True}
    db.update("theses", thesis_id, evidence_ids=evidence_ids, override=override, updated_at=time.time(),
              provenance="manual" if row["provenance"] == "manual" else "auto+expert_review")
    db.log_edit(row["project_id"], "thesis", thesis_id, "evidence", row["evidence_ids"], evidence_ids, comment)


def set_thesis_status(thesis_id: str, status: str, comment: str = "") -> None:
    row = _get("theses", thesis_id)
    db.update("theses", thesis_id, status=status, updated_at=time.time(),
              provenance="manual" if row["provenance"] == "manual" else "auto+expert_review")
    db.log_edit(row["project_id"], "thesis", thesis_id, f"status:{status}", row["status"], status, comment)


def add_expert_thesis(project_id: str, section: str, phrase: str, body: str, comment: str,
                      author: str, evidence_ids: list[str] | None = None, uur: str | None = None,
                      udd: int | None = None) -> str:
    """Экспертный тезис: без вымышленной цитаты, с явной отметкой происхождения."""
    tid = "rec_expert_" + hashlib.sha1(f"{time.time()}{body}".encode()).hexdigest()[:8]
    auto = dict(kr_section=section, subsection_title="", strength_phrase=phrase, body=body.rstrip("."),
                statement=f"{phrase} {body.rstrip('.')}.",
                direction="negative" if phrase.startswith("Не") else "positive",
                intervention=None, evidence_backed_core="", expert_additions=[
                    {"claim": body, "basis": "expert_consensus", "refs": []}],
                comment=comment, udd=udd or (None if evidence_ids else 5), uur=uur, uur_preliminary=False,
                grade_rationale="Экспертное дополнение: " + (author or "эксперт"),
                consensus=dict(agreement="single", conflicting_refs=[]),
                quality_criterion=dict(is_criterion=False, criterion_text=None),
                expert_author=author, explicit_source_recommendation=False)
    now = time.time()
    db.upsert("theses", dict(id=tid, project_id=project_id, section=section, cluster_id=None, auto=auto,
                             override=None, pending_auto=None, evidence_ids=evidence_ids or [],
                             status="approved", provenance="manual",
                             review_flags=["Экспертное дополнение — не извлечено из статей"],
                             orphan=0, run_id=None, created_at=now, updated_at=now))
    db.log_edit(project_id, "thesis", tid, "create_expert", None, auto, comment)
    return tid


def resolve_conflict(conflict_id: str, resolution: str, comment: str = "") -> None:
    row = _get("conflicts", conflict_id)
    db.update("conflicts", conflict_id, status="resolved", resolution={"text": resolution, "ts": time.time()})
    db.log_edit(row["project_id"], "conflict", conflict_id, "resolve", None, resolution, comment)
