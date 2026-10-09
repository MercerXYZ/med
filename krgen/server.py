"""Локальное веб-приложение: FastAPI + статический интерфейс.

Запуск:  python -m krgen.server   → http://127.0.0.1:8765
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import db, export, pipeline, projects
from .config import DATA_DIR, SETTINGS
from .pdf_ingest import add_document, render_page_png
from .prompts import SECTIONS

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="KRGen — черновик клинических рекомендаций из статей")


@app.exception_handler(projects.NotFound)
def _not_found(_req, e: projects.NotFound):
    return JSONResponse({"detail": f"Не найдено: {e}"}, status_code=404)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def _no_cache_ui(request, call_next):
    """Интерфейс всегда свежий: после обновления кода браузер не должен показывать старый app.js."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.on_event("startup")
def _recover_interrupted_runs() -> None:
    """Прогон, прерванный остановкой сервера, иначе навсегда оставался бы «идёт»."""
    now = time.time()
    for run in db.q("SELECT id, project_id, started_at, log FROM runs WHERE status='running'"):
        # живой прогон (CLI или второй экземпляр сервера) не трогаем: признак жизни — запись
        # в журнале или вызов модели за последние 5 минут
        last_log = max([e.get("ts", 0) for e in (run.get("log") or [])] + [run["started_at"] or 0])
        last_call = db.q1("SELECT MAX(ts) t FROM llm_calls WHERE run_id=?", [run["id"]])["t"] or 0
        if now - max(last_log, last_call) < 300:
            continue
        db.ex("UPDATE runs SET status='stopped', finished_at=?, error=COALESCE(error, '') || "
              "'Прерван остановкой сервера; полученные результаты сохранены' WHERE id=?", [now, run["id"]])
        db.ex("UPDATE documents SET status='partial', status_detail='Обработка прервана остановкой сервера — "
              "запустите обработку с отметкой «только сбойные фрагменты»' WHERE project_id=? AND status='processing'",
              [run["project_id"]])
        db.ex("UPDATE chunks SET status='error', error='Прервано остановкой сервера' WHERE status='running' "
              "AND doc_id IN (SELECT id FROM documents WHERE project_id=?)", [run["project_id"]])


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/meta")
def meta():
    return dict(sections=SECTIONS, settings=SETTINGS.to_json() | {"cli_path": bool(SETTINGS.cli_path)})


# ---------------------------------------------------------------- проекты ---

@app.get("/api/projects")
def list_projects():
    return db.q("SELECT id, title, age_category, icd_codes, developer_org, created_at, meta FROM projects "
                "ORDER BY created_at DESC")


@app.post("/api/projects")
def create_project(body: dict = Body(...)):
    try:
        return projects.create_project(body["title"], body.get("age_category", "mixed"),
                                       [c.strip() for c in body.get("icd_codes", []) if c.strip()],
                                       body.get("developer_org", ""))
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    p = db.q1("SELECT * FROM projects WHERE id=?", [pid])
    if not p:
        raise HTTPException(404)
    run = db.q1("SELECT * FROM runs WHERE project_id=? ORDER BY started_at DESC", [pid])
    counts = dict(
        documents=db.q1("SELECT COUNT(*) n FROM documents WHERE project_id=?", [pid])["n"],
        cards={r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM cards WHERE project_id=? "
                                                  "GROUP BY status", [pid])},
        theses=db.q1("SELECT COUNT(*) n FROM theses WHERE project_id=? AND merged_into IS NULL", [pid])["n"],
        conflicts=db.q1("SELECT COUNT(*) n FROM conflicts WHERE project_id=? AND status='open'", [pid])["n"],
        pending=db.q1("SELECT (SELECT COUNT(*) FROM cards WHERE project_id=? AND pending_auto IS NOT NULL) + "
                      "(SELECT COUNT(*) FROM theses WHERE project_id=? AND pending_auto IS NOT NULL) n",
                      [pid, pid])["n"])
    return dict(project=p, run=run, running=pipeline.is_running(pid), counts=counts)


@app.post("/api/projects/{pid}/upload")
async def upload(pid: str, files: list[UploadFile] = File(...)):
    out = []
    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            out.append(dict(filename=f.filename, status="error", status_detail="Поддерживаются только PDF"))
            continue
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=DATA_DIR) as tmp:
            tmp.write(await f.read())
        try:
            out.append(add_document(pid, Path(tmp.name), f.filename))
        finally:
            Path(tmp.name).unlink(missing_ok=True)
    return out


@app.post("/api/projects/{pid}/run")
async def run(pid: str, body: dict = Body(default={})):
    try:
        pipeline.start_background(pid, only_failed=body.get("only_failed", False),
                                  doc_ids=body.get("doc_ids"), stages=body.get("stages"),
                                  use_judge=body.get("use_judge", True))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.get("/api/projects/{pid}/runs")
def runs(pid: str):
    return db.q("SELECT id, started_at, finished_at, status, stage, progress, log, error FROM runs "
                "WHERE project_id=? ORDER BY started_at DESC LIMIT 20", [pid])


# -------------------------------------------------------------- документы ---

@app.get("/api/projects/{pid}/documents")
def documents(pid: str):
    docs = db.q("SELECT id, filename, n_pages, status, status_detail, biblio, duplicate_of FROM documents "
                "WHERE project_id=? ORDER BY filename", [pid])
    for d in docs:
        d["biblio"] = {k: v for k, v in (d["biblio"] or {}).items() if not k.startswith("_")}
        d["pages"] = db.q("SELECT page_no, kind, status, reason, relevance FROM pages WHERE doc_id=? "
                          "ORDER BY page_no", [d["id"]])
        d["n_cards"] = db.q1("SELECT COUNT(*) n FROM cards WHERE doc_id=?", [d["id"]])["n"]
        d["chunks"] = db.q("SELECT id, page_from, page_to, status, error, n_claims FROM chunks WHERE doc_id=? "
                           "ORDER BY page_from", [d["id"]])
    return docs


@app.get("/api/docs/{doc_id}/pdf")
def pdf(doc_id: str):
    d = db.q1("SELECT path, filename FROM documents WHERE id=?", [doc_id])
    if not d:
        raise HTTPException(404)
    return FileResponse(d["path"], media_type="application/pdf")


@app.get("/api/docs/{doc_id}/page/{n}.png")
def page_png(doc_id: str, n: int, card: str | None = None):
    d = db.q1("SELECT path, n_pages FROM documents WHERE id=?", [doc_id])
    if not d or not 1 <= n <= d["n_pages"]:
        raise HTTPException(404)
    quote = None
    if card:
        r = db.q1("SELECT * FROM cards WHERE id=?", [card])
        if r:
            quote = db.effective(r).get("quote")
    return Response(render_page_png(d["path"], n, quote), media_type="image/png")


# --------------------------------------------------------------- карточки ---

def _slim_verification(v: dict | None) -> dict | None:
    """Без тяжёлых полей (контекст ±600 символов и фрагмент страницы): UI они не нужны."""
    if not v:
        return v
    q = {k: val for k, val in (v.get("quote") or {}).items() if k not in ("context", "page_span")}
    return {**v, "quote": q}


def _docs_map(pid: str) -> dict:
    return {d["id"]: d for d in db.q("SELECT id, filename, biblio FROM documents WHERE project_id=?", [pid])}


def _card_view(r: dict, docs: dict | None = None, dups: list | None = None) -> dict:
    c = db.effective(r)
    if docs is not None:
        doc = docs.get(r["doc_id"]) or {}
    else:
        doc = db.q1("SELECT filename, biblio FROM documents WHERE id=?", [r["doc_id"]]) or {}
    b = doc.get("biblio") or {}
    v = r.get("verification") or {}
    # страница, на которой цитата реально найдена (номер от модели мог быть неверным)
    c["page"] = (v.get("quote") or {}).get("found_page") or c.get("page")
    if dups is None:
        dups = db.q("SELECT id, doc_id FROM cards WHERE merged_into=?", [r["id"]])
    return dict(id=r["id"], doc_id=r["doc_id"], filename=doc.get("filename"),
                source=dict(title=b.get("title"), year=b.get("year"), design=b.get("study_design"),
                            authors=(b.get("authors") or [])[:3], journal=b.get("journal"), doi=b.get("doi")),
                data=c, override=r.get("override"), pending_auto=r.get("pending_auto"),
                verification=_slim_verification(v), status=r["status"], user_status=r.get("user_status"),
                merged_into=r.get("merged_into"), merge_lock=r.get("merge_lock"), origin=r["origin"], duplicates=dups)


@app.get("/api/projects/{pid}/cards")
def cards(pid: str, status: str | None = None, section: str | None = None, doc: str | None = None,
          q: str | None = None, include_merged: bool = True):
    sql, args = "SELECT * FROM cards WHERE project_id=?", [pid]
    if status:
        sql += " AND (status=? OR user_status=?)"
        args += [status, status]
    if doc:
        sql += " AND doc_id=?"
        args.append(doc)
    if not include_merged:
        sql += " AND merged_into IS NULL"
    rows = db.q(sql + " ORDER BY doc_id, created_at", args)
    docs = _docs_map(pid)
    dup_map: dict[str, list] = {}
    for d in db.q("SELECT id, doc_id, merged_into FROM cards WHERE project_id=? AND merged_into IS NOT NULL", [pid]):
        dup_map.setdefault(d["merged_into"], []).append(dict(id=d["id"], doc_id=d["doc_id"]))
    out = []
    ql = (q or "").lower()
    for r in rows:
        v = _card_view(r, docs, dup_map.get(r["id"], []))
        if section and v["data"].get("kr_section") != section:
            continue
        if ql and ql not in json.dumps(v["data"], ensure_ascii=False).lower():
            continue
        out.append(_slim_card(v))
    return out


def _slim_card(v: dict) -> dict:
    """Строка списка: только то, что видно в списке; полная карточка — GET /api/cards/{id}."""
    d = v["data"]
    return dict(id=v["id"], doc_id=v["doc_id"], filename=v["filename"], source=dict(title=v["source"]["title"]),
                data={k: d.get(k) for k in ("statement_ru", "kr_section", "claim_type", "page")},
                status=v["status"], user_status=v["user_status"], merged_into=v["merged_into"],
                override={} if v["override"] else None, pending_auto=True if v["pending_auto"] else None,
                duplicates=v["duplicates"], slim=True)


@app.get("/api/cards/{cid}")
def card(cid: str):
    r = db.q1("SELECT * FROM cards WHERE id=?", [cid])
    if not r:
        raise HTTPException(404)
    return _card_view(r)


@app.patch("/api/cards/{cid}")
def edit_card(cid: str, body: dict = Body(...)):
    projects.edit_card(cid, body.get("fields", {}), body.get("comment", ""))
    return _card_view(db.q1("SELECT * FROM cards WHERE id=?", [cid]))


@app.post("/api/cards/{cid}/status")
def card_status(cid: str, body: dict = Body(...)):
    projects.set_card_status(cid, body.get("user_status"), body.get("comment", ""))
    return _card_view(db.q1("SELECT * FROM cards WHERE id=?", [cid]))


@app.post("/api/cards/merge")
def merge(body: dict = Body(...)):
    projects.merge_cards(body["canonical"], body["others"], body.get("comment", ""))
    return {"ok": True}


@app.post("/api/{entity}/{id_}/pending")
def pending(entity: str, id_: str, body: dict = Body(...)):
    projects.accept_pending({"cards": "card", "theses": "thesis"}[entity], id_, bool(body.get("accept")))
    return {"ok": True}


# ----------------------------------------------------- противоречия, тезисы ---

@app.get("/api/projects/{pid}/conflicts")
def conflicts(pid: str):
    out = db.q("SELECT * FROM conflicts WHERE project_id=? ORDER BY status, created_at", [pid])
    for c in out:
        c["cards"] = [_card_view(r) for r in
                      (db.q1("SELECT * FROM cards WHERE id=?", [i]) for i in c["card_ids"]) if r]
    return out


@app.post("/api/conflicts/{cid}/resolve")
def resolve(cid: str, body: dict = Body(...)):
    projects.resolve_conflict(cid, body.get("resolution", ""), body.get("comment", ""))
    return {"ok": True}


def _thesis_view(r: dict, cards: dict | None = None, docs: dict | None = None) -> dict:
    t = {k: v for k, v in db.effective(r).items() if k != "_synth"}  # сырой ответ модели UI не нужен
    if cards is None:
        ids = r.get("evidence_ids") or []
        cards = {c["id"]: c for c in (db.q1("SELECT * FROM cards WHERE id=?", [e]) for e in ids) if c}
    ev = []
    for e in r.get("evidence_ids") or []:
        c = cards.get(e)
        if c:
            v = _card_view(c, docs, [])
            ev.append(dict(id=e, statement=v["data"].get("statement_ru"), quote=v["data"].get("quote"),
                           page=v["data"].get("page"), doc_id=c["doc_id"], filename=v["filename"],
                           source=v["source"], status=c["status"], user_status=c.get("user_status")))
    return dict(id=r["id"], section=r["section"], data=t, override=r.get("override"),
                pending_auto=r.get("pending_auto"), evidence=ev, status=r["status"],
                provenance=r["provenance"], review_flags=r.get("review_flags") or [], orphan=bool(r.get("orphan")),
                merged_into=r.get("merged_into"), merged_from=t.get("merged_from") or [])


@app.get("/api/projects/{pid}/theses")
def theses(pid: str):
    cards = {c["id"]: c for c in db.q("SELECT * FROM cards WHERE project_id=?", [pid])}
    docs = _docs_map(pid)
    return [_thesis_view(r, cards, docs) for r in
            db.q("SELECT * FROM theses WHERE project_id=? ORDER BY section, created_at", [pid])]


@app.patch("/api/theses/{tid}")
def edit_thesis(tid: str, body: dict = Body(...)):
    projects.edit_thesis(tid, body.get("fields", {}), body.get("comment", ""))
    return _thesis_view(db.q1("SELECT * FROM theses WHERE id=?", [tid]))


@app.post("/api/theses/{tid}/status")
def thesis_status(tid: str, body: dict = Body(...)):
    projects.set_thesis_status(tid, body["status"], body.get("comment", ""))
    return _thesis_view(db.q1("SELECT * FROM theses WHERE id=?", [tid]))


@app.post("/api/theses/{tid}/evidence")
def thesis_evidence(tid: str, body: dict = Body(...)):
    try:
        projects.set_thesis_evidence(tid, body["evidence_ids"], body.get("comment", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _thesis_view(db.q1("SELECT * FROM theses WHERE id=?", [tid]))


@app.post("/api/projects/{pid}/theses")
def add_thesis(pid: str, body: dict = Body(...)):
    tid = projects.add_expert_thesis(pid, body["section"], body.get("strength_phrase", "Рекомендуется"),
                                     body["body"], body.get("comment", ""), body.get("author", "эксперт"),
                                     body.get("evidence_ids") or [], body.get("uur"), body.get("udd"))
    return _thesis_view(db.q1("SELECT * FROM theses WHERE id=?", [tid]))


# ------------------------------------------------- отчёт, журнал, экспорт ---

@app.get("/api/projects/{pid}/report")
def report(pid: str):
    return pipeline.processing_report(pid)


@app.get("/api/projects/{pid}/edits")
def edits(pid: str):
    return db.q("SELECT * FROM expert_edits WHERE project_id=? ORDER BY ts DESC LIMIT 500", [pid])


@app.get("/api/projects/{pid}/draft.md")
def draft_md(pid: str):
    return Response(export.to_markdown(pid), media_type="text/markdown; charset=utf-8")


@app.get("/api/projects/{pid}/export/{fmt}")
def do_export(pid: str, fmt: str):
    stamp = time.strftime("%Y%m%d_%H%M")
    if fmt == "json":
        data = json.dumps(export.build_export(pid), ensure_ascii=False, indent=1, default=str)
        return Response(data, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{pid}_{stamp}.json"'})
    if fmt == "kr_schema":
        data, errors = export.teacher_export(pid)
        return Response(json.dumps(data, ensure_ascii=False, indent=1), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{pid}_{stamp}_kr_recommendation.json"',
                                 "X-Validation-Errors": str(len(errors))})
    if fmt == "kr_schema_check":
        _, errors = export.teacher_export(pid)
        return dict(valid=not errors, errors=errors[:100])
    if fmt == "md":
        return Response(export.to_markdown(pid), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{pid}_{stamp}.md"'})
    if fmt == "docx":
        path = Path(tempfile.mkdtemp()) / f"{pid}_{stamp}.docx"  # временный файл, удаляется после отдачи
        export.to_docx(pid, path)
        return FileResponse(path, filename=path.name, background=BackgroundTask(path.unlink, missing_ok=True),
                            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    raise HTTPException(404)


# ------------------------------------------------------- история прогонов ---

@app.get("/api/runs")
def all_runs():
    from .snapshots import load_summary
    out = []
    for r in db.q("SELECT r.id, r.project_id, r.started_at, r.finished_at, r.status, r.stage, r.progress, "
                  "p.title, p.meta FROM runs r LEFT JOIN projects p ON p.id=r.project_id "
                  "WHERE r.project_id NOT LIKE 'exp_%' ORDER BY r.started_at DESC LIMIT 100"):
        u = db.q1("SELECT COUNT(*) calls, SUM(cache_hit) hits, ROUND(SUM(cost_usd),3) cost, "
                  "SUM(CASE WHEN error IS NOT NULL AND NOT EXISTS (SELECT 1 FROM llm_calls s2 WHERE s2.run_id=llm_calls.run_id AND s2.cache_key=llm_calls.cache_key AND s2.error IS NULL) THEN 1 ELSE 0 END) errors FROM llm_calls WHERE run_id=?", [r["id"]])
        r.update(usage=u, summary=load_summary(r["id"]),
                 running=pipeline.is_running(r["project_id"]) and r["status"] == "running")
        out.append(r)
    return out


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    from .snapshots import SNAP_DIR, load_summary
    r = db.q1("SELECT r.*, p.title FROM runs r LEFT JOIN projects p ON p.id=r.project_id WHERE r.id=?", [run_id])
    if not r:
        raise HTTPException(404)
    md = SNAP_DIR / run_id / "draft.md"
    r["summary"] = load_summary(run_id)
    r["draft_md"] = md.read_text(encoding="utf-8") if md.exists() else None
    r["usage_by_stage"] = db.q("SELECT purpose, COUNT(*) calls, SUM(cache_hit) hits, SUM(output_tokens) out_tokens, "
                               "ROUND(SUM(cost_usd),3) cost, SUM(duration_ms)/1000 seconds, "
                               "SUM(CASE WHEN error IS NOT NULL AND NOT EXISTS (SELECT 1 FROM llm_calls s2 WHERE s2.run_id=llm_calls.run_id AND s2.cache_key=llm_calls.cache_key AND s2.error IS NULL) THEN 1 ELSE 0 END) errors "
                               "FROM llm_calls WHERE run_id=? GROUP BY purpose", [run_id])
    return r


@app.get("/api/runs/{run_id}/file/{name}")
def run_file(run_id: str, name: str):
    from .snapshots import SNAP_DIR
    if name not in ("draft.docx", "draft.md", "export.json"):
        raise HTTPException(404)
    f = SNAP_DIR / run_id / name
    if not f.exists():
        raise HTTPException(404, "Снимок для этого прогона не сохранён")
    return FileResponse(f, filename=f"{run_id}_{name}")


@app.post("/api/projects/{pid}/fork")
async def fork_project(pid: str, body: dict = Body(...)):
    """Новый прогон в отдельной копии проекта на выбранных документах — результаты
    исходного проекта и его прогонов остаются нетронутыми (удобно для демонстрации)."""
    src = db.q1("SELECT * FROM projects WHERE id=?", [pid])
    doc_ids = body.get("doc_ids") or []
    if not src or not doc_ids:
        raise HTTPException(400, "Выберите хотя бы один документ")
    found = [d for d in (db.q1("SELECT id FROM documents WHERE id=? AND project_id=?", [i, pid]) for i in doc_ids) if d]
    if not found:
        raise HTTPException(400, "Выбранные документы не найдены в проекте")
    # тема (title) остаётся прежней: она входит в промпты, и так срабатывает кэш ответов модели;
    # подпись прогона хранится отдельно в meta.label
    new = projects.create_project(src["title"], src["age_category"], src["icd_codes"], src["developer_org"])
    label = body.get("title") or f"Прогон {time.strftime('%d.%m %H:%M')}"
    db.update("projects", new["id"], meta={**(src.get("meta") or {}), "label": label, "forked_from": pid},
              keywords=src.get("keywords"))
    for did in doc_ids:
        d = db.q1("SELECT path, filename FROM documents WHERE id=? AND project_id=?", [did, pid])
        if d:
            add_document(new["id"], Path(d["path"]), d["filename"])
    pipeline.start_background(new["id"], use_judge=body.get("use_judge", True))
    return {"project_id": new["id"]}


@app.get("/api/projects/{pid}/eval")
def eval_results(pid: str):
    path = DATA_DIR / "eval" / f"{pid}_latest.json"
    if not path.exists():
        return JSONResponse({"available": False})
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
