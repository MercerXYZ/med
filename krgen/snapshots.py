"""Снимки прогонов: КР (Word, Markdown), полный JSON и сводка в data/snapshots/<run_id>/."""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import db
from .config import DATA_DIR

SNAP_DIR = DATA_DIR / "snapshots"
FILES = ("draft.docx", "draft.md", "export.json")

# ошибкой считается только запрос, который так и не удался (успешный повтор — не ошибка)
_USAGE = ("SELECT COUNT(*) calls, COALESCE(SUM(cache_hit), 0) cache_hits, "
          "COALESCE(SUM(input_tokens), 0) input_tokens, COALESCE(SUM(output_tokens), 0) output_tokens, "
          "ROUND(COALESCE(SUM(cost_usd), 0), 3) cost_usd, "
          "COALESCE(SUM(CASE WHEN error IS NOT NULL AND NOT EXISTS (SELECT 1 FROM llm_calls s2 "
          "WHERE s2.run_id=llm_calls.run_id AND s2.cache_key=llm_calls.cache_key AND s2.error IS NULL) "
          "THEN 1 ELSE 0 END), 0) errors FROM llm_calls WHERE ")


def summary(project_id: str, run_id: str | None = None, model: dict | None = None) -> dict:
    pid = project_id
    docs = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM documents WHERE project_id=? "
                                               "GROUP BY status", [pid])}
    facts = dict(total=0, verified=0, needs_review=0, unverified=0, confirmed=0, rejected=0)
    for r in db.q("SELECT status, user_status, COUNT(*) n FROM cards WHERE project_id=? "
                  "GROUP BY status, user_status", [pid]):
        key = r["user_status"] if r["user_status"] in ("confirmed", "rejected") else r["status"]
        facts["total"] += r["n"]
        facts[key] = facts.get(key, 0) + r["n"]
    groups = db.q1("SELECT COUNT(*) total, COALESCE(SUM(CASE WHEN excluded THEN 1 ELSE 0 END), 0) excluded "
                   "FROM clusters WHERE project_id=?", [pid])
    conflicts = db.q1(
        "SELECT COUNT(*) total, "
        "COALESCE(SUM(CASE WHEN decision IS NOT NULL AND decided_by='expert' THEN 1 ELSE 0 END), 0) expert, "
        "COALESCE(SUM(CASE WHEN decision IS NOT NULL AND COALESCE(decided_by, 'auto')!='expert' THEN 1 ELSE 0 END), 0) auto, "
        "COALESCE(SUM(CASE WHEN decision IS NULL THEN 1 ELSE 0 END), 0) open "
        "FROM conflicts WHERE project_id=?", [pid])
    if model is None:
        from .export import kr_model
        model = kr_model(pid)
    items = [it for s in model["sections"] for it in s["items"] if it.get("id")]
    theses = dict(in_kr=sum(it["type"] == "thesis" for it in items),
                  texts=sum(it["type"] == "text" for it in items),
                  approved=sum(it["type"] == "thesis" and it["status"] == "approved" for it in items),
                  references=len(model["references"]), criteria=len(model["criteria"]))
    usage = db.q1(_USAGE + "run_id=?", [run_id]) if run_id else None
    by_stage = db.q("SELECT purpose, COUNT(*) calls, COALESCE(SUM(cache_hit), 0) cache_hits, "
                    "COALESCE(SUM(output_tokens), 0) output_tokens, ROUND(COALESCE(SUM(cost_usd), 0), 3) cost_usd, "
                    "COALESCE(SUM(duration_ms), 0) / 1000 seconds FROM llm_calls WHERE run_id=? "
                    "GROUP BY purpose ORDER BY MIN(ts)", [run_id]) if run_id else []
    from .pipeline import processing_report
    coverage = [{k: d.get(k) for k in ("filename", "status", "n_pages", "pages_processed", "coverage",
                                       "skipped_pages")}
                for d in processing_report(pid).get("documents") or []]
    return dict(
        n_documents=sum(docs.values()), n_facts=facts["total"], n_groups=groups["total"],
        n_conflicts=conflicts["total"], n_conflicts_open=conflicts["open"], n_theses=theses["in_kr"],
        calls=(usage or {}).get("calls", 0), cost_usd=(usage or {}).get("cost_usd", 0),
        documents=docs, facts=facts, groups=groups, conflicts=conflicts, theses=theses,
        run_usage=usage, usage_by_stage=by_stage, project_usage=db.q1(_USAGE + "project_id=?", [pid]),
        coverage=coverage,
    )


def take(project_id: str, run_id: str) -> Path:
    from .export import build_export, kr_model, to_docx, to_markdown

    d = SNAP_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    m = kr_model(project_id)
    (d / "export.json").write_text(json.dumps(build_export(project_id, m), ensure_ascii=False, indent=1,
                                              default=str), encoding="utf-8")
    (d / "draft.md").write_text(to_markdown(project_id, m), encoding="utf-8")
    to_docx(project_id, d / "draft.docx", m)
    p = db.q1("SELECT title FROM projects WHERE id=?", [project_id]) or {}
    (d / "summary.json").write_text(json.dumps(dict(project_id=project_id, project_title=p.get("title"),
                                                    run_id=run_id, taken_at=time.time(),
                                                    **summary(project_id, run_id, m)),
                                               ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return d


def load_summary(run_id: str) -> dict | None:
    f = SNAP_DIR / run_id / "summary.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except (OSError, json.JSONDecodeError):
        return None
