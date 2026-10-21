"""Нормирование противоречий по правилам, без модели."""
from __future__ import annotations

from . import db, prompts as P
from .groups import live_facts
from .synth import udd_for

DECISIONS = ("keep_both", "prefer_a", "prefer_b", "exclude")


def sides(row: dict) -> tuple[list[str], list[str]]:
    a, b = list(row.get("side_a") or []), list(row.get("side_b") or [])
    if a and b:
        return a, b
    # старые записи без сторон: делим по полярности фактов
    ids = list(row.get("card_ids") or [])
    neg = []
    for i in ids:
        r = db.q1("SELECT auto, override FROM cards WHERE id=?", [i])
        if r and db.effective(r).get("polarity") == "negative":
            neg.append(i)
    pos = [i for i in ids if i not in neg]
    return (pos, neg) if pos and neg else (ids[:1], ids[1:])


def _ages(facts: list[dict]) -> set[str]:
    out = set()
    for c in facts:
        a = c.get("age_group")
        out |= {"children", "adults"} if a == "mixed" else ({a} if a in ("children", "adults") else set())
    return out


def side_strength(facts: list[dict], section: str) -> tuple[int | None, int]:
    """Лучший УДД стороны и число независимых источников."""
    udds = [u for u in (udd_for(c.get("_design"), section) for c in facts) if u]
    return (min(udds) if udds else None), len({c.get("_src") for c in facts})


def decide(a: list[str], b: list[str], section: str | None, facts: dict[str, dict],
           kind: str | None = None) -> tuple[str | None, str]:
    la, lb = [facts[i] for i in a if i in facts], [facts[i] for i in b if i in facts]
    if not la or not lb:
        return "keep_both", "одна из сторон больше не подтверждена"
    ages_a, ages_b = _ages(la), _ages(lb)
    if kind == "population" or (ages_a and ages_b and not ages_a & ages_b):
        return "keep_both", "разные группы пациентов"
    sec = section or la[0].get("kr_section") or "7"
    if kind == "numbers" and sec in P.NARRATIVE_SECTIONS:
        return "keep_both", "разные оценки в описательном разделе — приводятся обе"
    (ua, na), (ub, nb) = side_strength(la, sec), side_strength(lb, sec)
    if ua != ub:
        win = "prefer_a" if ub is None or (ua is not None and ua < ub) else "prefer_b"
        w, l = (ua, ub) if win == "prefer_a" else (ub, ua)
        if l is None:
            return win, f"сильнее доказательства: УДД {w}, у другой стороны УДД не определён"
        return win, f"сильнее доказательства: УДД {w} против {l}"
    if abs(na - nb) >= 2:
        return ("prefer_a" if na > nb else "prefer_b"), f"больше независимых источников: {max(na, nb)} против {min(na, nb)}"
    return None, "доказательства равной силы — нужно решение"


def resolve_project(project_id: str) -> None:
    facts = live_facts(project_id)
    for cf in db.q("SELECT * FROM conflicts WHERE project_id=?", [project_id]):
        a, b = sides(cf)
        upd = dict(side_a=a, side_b=b)
        if not cf.get("section"):
            first = next((facts[i] for i in a + b if i in facts), None)
            if first:
                upd["section"] = first.get("kr_section") or "7"
        if cf.get("decided_by") != "expert":
            decision, reason = decide(a, b, upd.get("section") or cf.get("section"), facts, cf.get("kind"))
            upd.update(decision=decision, decided_by="auto" if decision else None, reason=reason,
                       status="resolved" if decision else "open")
        db.update("conflicts", cf["id"], **upd)


def excluded_fact_ids(project_id: str) -> set[str]:
    out: set[str] = set()
    for cf in db.q("SELECT card_ids, side_a, side_b, decision FROM conflicts WHERE project_id=?", [project_id]):
        d = cf.get("decision")
        if d == "keep_both":
            continue
        a, b = sides(cf)
        if d == "prefer_a":
            out.update(b)
        elif d == "prefer_b":
            out.update(a)
        else:  # exclude или ещё не решено
            out.update(a + b)
    return out
