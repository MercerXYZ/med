"""Нормирование противоречий по правилам, без модели."""
from __future__ import annotations

from . import db, prompts as P
from .groups import live_facts
from .grading import udd_for

DECISIONS = ("keep_both", "prefer_a", "prefer_b", "exclude")


def _ages(facts: list[dict]) -> set[str]:
    out = set()
    for c in facts:
        a = c.get("age_group")
        out |= {"adults", "children"} if a == "mixed" else ({a} if a in ("adults", "children") else set())
    return out


def side_strength(facts: list[dict], section: str) -> tuple[int | None, int]:
    """Лучший УДД стороны и число независимых источников."""
    udds = [u for u in (udd_for(c["_design"], section) for c in facts) if u]
    return (min(udds) if udds else None), len({c["_src"] for c in facts})


def decide(a: list[str], b: list[str], section: str, facts: dict[str, dict],
           kind: str | None = None) -> tuple[str | None, str]:
    """Решение по правилам; None — нужно решение эксперта."""
    la, lb = [facts[i] for i in a if i in facts], [facts[i] for i in b if i in facts]
    if not la or not lb:
        return "keep_both", "одна из сторон больше не подтверждена"
    ages_a, ages_b = _ages(la), _ages(lb)
    if ages_a and ages_b and not ages_a & ages_b:
        return "keep_both", "разные группы пациентов"
    if kind == "numbers" and section in P.NARRATIVE_SECTIONS:
        return "keep_both", "разные оценки в описательном разделе — приводятся обе"
    (ua, na), (ub, nb) = side_strength(la, section), side_strength(lb, section)
    if ua != ub:
        if ub is None or (ua is not None and ua < ub):
            return "prefer_a", f"сильнее доказательства: УДД {ua} против {ub or 'не определён'}"
        return "prefer_b", f"сильнее доказательства: УДД {ub} против {ua or 'не определён'}"
    if abs(na - nb) >= 2:
        return ("prefer_a" if na > nb else "prefer_b"), f"больше независимых источников: {max(na, nb)} против {min(na, nb)}"
    return None, "доказательства равной силы — нужно решение"


def resolve_project(project_id: str) -> None:
    facts = live_facts(project_id)
    for cf in db.q("SELECT * FROM conflicts WHERE project_id=? AND COALESCE(decided_by, '')!='expert'", [project_id]):
        decision, reason = decide(cf["side_a"], cf["side_b"], cf["section"], facts, cf["kind"])
        db.update("conflicts", cf["id"], decision=decision, decided_by="auto" if decision else None, reason=reason)


def excluded_fact_ids(project_id: str) -> set[str]:
    """Доказательства, которые не идут в КР: проигравшая сторона, обе при exclude и пока нет решения."""
    out: set[str] = set()
    for cf in db.q("SELECT side_a, side_b, decision FROM conflicts WHERE project_id=?", [project_id]):
        d = cf["decision"]
        if d != "prefer_a" and d != "keep_both":
            out.update(cf["side_a"])
        if d != "prefer_b" and d != "keep_both":
            out.update(cf["side_b"])
    return out


def conflicting(project_id: str) -> dict[str, list[str]]:
    """Для consensus онтологии: доказательство → противоречащие ему, если эксперт оставил обе стороны.
    Автоматическое «оставить оба» значит разные группы пациентов — это не противоречие."""
    out: dict[str, list[str]] = {}
    for cf in db.q("SELECT side_a, side_b FROM conflicts WHERE project_id=? AND decision='keep_both' "
                   "AND decided_by='expert'", [project_id]):
        for i in cf["side_a"]:
            out.setdefault(i, []).extend(cf["side_b"])
        for i in cf["side_b"]:
            out.setdefault(i, []).extend(cf["side_a"])
    return out
