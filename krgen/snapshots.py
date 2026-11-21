"""Снимки прогонов: КР (Word, Markdown), полный JSON и сводка в data/snapshots/<run_id>/."""
from __future__ import annotations

import json
import time

from . import db
from .config import DATA_DIR

SNAP_DIR = DATA_DIR / "snapshots"
FILES = ("draft.docx", "draft.md", "export.json")


def summary(project_id: str, run_id: str, model: dict) -> dict:
    n = lambda sql: db.q1(sql, [project_id])["n"]  # noqa: E731
    items = [it for s in model["sections"] for it in s["items"] if it["id"]]
    usage = db.q1("SELECT COUNT(*) calls, COALESCE(SUM(cache_hit), 0) cache_hits, "
                  "COALESCE(SUM(output_tokens), 0) output_tokens, ROUND(COALESCE(SUM(cost_usd), 0), 3) cost_usd "
                  "FROM llm_calls WHERE run_id=?", [run_id])
    return dict(
        n_documents=n("SELECT COUNT(*) n FROM documents WHERE project_id=?"),
        n_facts=n("SELECT COUNT(*) n FROM cards WHERE project_id=?"),
        n_facts_verified=n("SELECT COUNT(*) n FROM cards WHERE project_id=? AND (user_status='confirmed' "
                           "OR (status='verified' AND user_status IS NULL))"),
        n_groups=n("SELECT COUNT(*) n FROM fact_groups WHERE project_id=?"),
        n_conflicts=n("SELECT COUNT(*) n FROM conflicts WHERE project_id=?"),
        n_conflicts_open=n("SELECT COUNT(*) n FROM conflicts WHERE project_id=? AND decision IS NULL"),
        n_theses=sum(it["type"] == "thesis" for it in items),
        n_texts=sum(it["type"] == "text" for it in items),
        n_references=len(model["references"]),
        n_criteria=len(model["criteria"]),
        **usage,
    )


def take(project_id: str, run_id: str) -> None:
    from .export import build_export, kr_model, to_docx, to_markdown

    d = SNAP_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    m = kr_model(project_id)
    (d / "export.json").write_text(json.dumps(build_export(project_id, m), ensure_ascii=False, indent=1,
                                              default=str), encoding="utf-8")
    (d / "draft.md").write_text(to_markdown(project_id, m), encoding="utf-8")
    to_docx(project_id, d / "draft.docx", m)
    (d / "summary.json").write_text(json.dumps(dict(run_id=run_id, taken_at=time.time(),
                                                    **summary(project_id, run_id, m)), ensure_ascii=False, indent=1),
                                    encoding="utf-8")


def load_summary(run_id: str) -> dict | None:
    f = SNAP_DIR / run_id / "summary.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
