"""Снимки результатов прогонов.

В конце каждого прогона сохраняется неизменяемый снимок: полный JSON, черновик
(Markdown и Word) и сводка. Его можно открыть и скачать, даже если проект уже
перерабатывается новым прогоном или изменён экспертом. Это же — «неисправленные
автоматически полученные результаты итогового запуска» из раздела 10 ТЗ.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import db
from .config import DATA_DIR

SNAP_DIR = DATA_DIR / "snapshots"


def summary(project_id: str, run_id: str | None = None) -> dict:
    pid = project_id
    docs = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM documents WHERE project_id=? "
                                               "GROUP BY status", [pid])}
    cards = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM cards WHERE project_id=? "
                                                "GROUP BY status", [pid])}
    th = db.q("SELECT auto, merged_into FROM theses WHERE project_id=?", [pid])
    live = [t for t in th if not t.get("merged_into")]
    usage_q = ("SELECT COUNT(*) calls, SUM(cache_hit) cache_hits, SUM(input_tokens) input_tokens, "
               "SUM(output_tokens) output_tokens, ROUND(SUM(cost_usd), 3) cost_usd, "
               "SUM(CASE WHEN error IS NOT NULL AND NOT EXISTS (SELECT 1 FROM llm_calls s2 WHERE s2.run_id=llm_calls.run_id AND s2.cache_key=llm_calls.cache_key AND s2.error IS NULL) THEN 1 ELSE 0 END) errors FROM llm_calls WHERE ")
    pages = db.q1("SELECT COUNT(*) total, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done FROM pages "
                  "WHERE doc_id IN (SELECT id FROM documents WHERE project_id=?)", [pid])
    return dict(
        documents=docs, n_documents=sum(docs.values()), pages=pages,
        cards=cards, n_cards=sum(cards.values()),
        theses=len(live), theses_narrative=sum(1 for t in live if (t["auto"] or {}).get("kind") == "narrative"),
        theses_merged_away=len(th) - len(live),
        conflicts=db.q1("SELECT COUNT(*) n FROM conflicts WHERE project_id=?", [pid])["n"],
        duplicates=db.q1("SELECT COUNT(*) n FROM cards WHERE project_id=? AND merged_into IS NOT NULL", [pid])["n"],
        run_usage=db.q1(usage_q + "run_id=?", [run_id]) if run_id else None,
        project_usage=db.q1(usage_q + "project_id=?", [pid]),
    )


def take(project_id: str, run_id: str) -> Path:
    from .export import _draft_model, build_export, to_docx, to_markdown

    d = SNAP_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "export.json").write_text(json.dumps(build_export(project_id), ensure_ascii=False, indent=1,
                                              default=str), encoding="utf-8")
    (d / "draft.md").write_text(to_markdown(project_id), encoding="utf-8")
    to_docx(project_id, d / "draft.docx")
    p = db.q1("SELECT title FROM projects WHERE id=?", [project_id])
    (d / "summary.json").write_text(json.dumps(dict(project_id=project_id, project_title=p["title"],
                                                    run_id=run_id, taken_at=time.time(),
                                                    theses_in_draft=sum(len(v) for v in
                                                                        _draft_model(project_id)["sections"].values()),
                                                    **summary(project_id, run_id)),
                                               ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return d


def load_summary(run_id: str) -> dict | None:
    f = SNAP_DIR / run_id / "summary.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
