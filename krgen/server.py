"""Веб-приложение KRGen (FastAPI + статический интерфейс). Запуск: python -m krgen.server → http://127.0.0.1:8765"""
from __future__ import annotations

import io
import json
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, export, ontology as O, pipeline, projects, prompts as P
from .config import DATA_DIR, UPLOADS_DIR
from .grading import udd_for
from .groups import group_statement, live_facts, source_key
from .pdf_ingest import add_document, render_page_png
from .projects import get
from .resolve import DECISIONS, excluded_fact_ids
from .snapshots import FILES as SNAP_FILES, SNAP_DIR, load_summary
from .synth import kr_evidence
from .verify import JUDGE_ISSUES

STATIC = Path(__file__).parent / "static"
RUN_STAGES = {"all": None, "kr": ["group", "resolve", "kr"]}


@asynccontextmanager
async def _lifespan(_app):
    pipeline.stop_stale_runs()
    yield


app = FastAPI(title="KRGen", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(projects.NotFound)
def _not_found(_req, e: projects.NotFound):
    return JSONResponse({"detail": f"Не найдено: {e}"}, status_code=404)


@app.exception_handler(ValueError)
def _bad_value(_req, e: ValueError):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.middleware("http")
async def _no_cache_ui(request, call_next):
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _docs(pid: str) -> dict[str, dict]:
    return {d["id"]: d for d in db.q("SELECT id, filename, biblio FROM documents WHERE project_id=?", [pid])}


def _source(doc: dict) -> dict:
    b = doc["biblio"] or {}
    return dict(doc_id=doc["id"], filename=doc["filename"], title=b.get("title") or "", year=b.get("year"),
                design=b.get("study_design"), design_label=export.DESIGN_RU.get(b.get("study_design"), ""),
                doi=b.get("doi") or "")


def _page(r: dict, c: dict):
    return ((r["verification"] or {}).get("quote") or {}).get("found_page") or c.get("page")


# ---------------------------------------------------------------- проекты ---

@app.get("/api/projects")
def list_projects():
    return db.q("SELECT id, title, created_at FROM projects WHERE id NOT LIKE 'exp_%' ORDER BY created_at DESC")


@app.post("/api/projects")
def create_project(body: dict = Body(...)):
    codes = body.get("icd_codes") or []
    if isinstance(codes, str):
        codes = codes.replace(";", ",").split(",")
    if not isinstance(body.get("title"), str) or not isinstance(codes, list):
        raise HTTPException(400, "Тема и коды МКБ-10 — строки")
    return projects.create_project(body["title"], body.get("age_category") or "mixed", [str(c) for c in codes],
                                   str(body.get("developer_org") or "").strip())


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    p = get("projects", pid)
    n = lambda sql: db.q1(sql, [pid])["n"]  # noqa: E731
    counts = dict(
        documents=n("SELECT COUNT(*) n FROM documents WHERE project_id=?"),
        facts=n("SELECT COUNT(*) n FROM cards WHERE project_id=?"),
        facts_verified=n("SELECT COUNT(*) n FROM cards WHERE project_id=? AND (user_status='confirmed' "
                         "OR (status='verified' AND user_status IS NULL))"),
        facts_review=n("SELECT COUNT(*) n FROM cards WHERE project_id=? AND status='needs_review' "
                       "AND user_status IS NULL"),
        facts_rejected=n("SELECT COUNT(*) n FROM cards WHERE project_id=? AND user_status='rejected'"),
        groups=n("SELECT COUNT(*) n FROM fact_groups WHERE project_id=?"),
        conflicts=n("SELECT COUNT(*) n FROM conflicts WHERE project_id=?"),
        conflicts_open=n("SELECT COUNT(*) n FROM conflicts WHERE project_id=? AND decision IS NULL"),
        theses=n("SELECT COUNT(*) n FROM theses WHERE project_id=? AND status!='rejected' "
                 "AND (orphan=0 OR status='approved' OR provenance='manual')"),
    )
    run = db.q1("SELECT id, status, stage, progress, started_at, finished_at, error FROM runs WHERE project_id=? "
                "ORDER BY started_at DESC", [pid])
    return dict(project={k: p[k] for k in ("id", "title", "age_category", "icd_codes", "developer_org")},
                counts=counts, run=run, running=pipeline.is_running(pid))


@app.post("/api/projects/{pid}/upload")
async def upload(pid: str, files: list[UploadFile] = File(...)):
    get("projects", pid)
    out = []
    for f in files:
        data = await f.read()
        name = f.filename or "document.pdf"
        if not name.lower().endswith(".pdf"):
            if b"%PDF" not in data[:1024]:
                out.append(dict(id=None, filename=name, status="error", detail="Поддерживаются только PDF"))
                continue
            name += ".pdf"
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=DATA_DIR) as tmp:
            tmp.write(data)
        try:
            d = add_document(pid, Path(tmp.name), name)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        out.append(dict(id=d["id"], filename=d["filename"], status=d["status"], detail=d["status_detail"] or ""))
    return out


@app.post("/api/projects/{pid}/run")
async def run(pid: str, body: dict = Body(default={})):
    get("projects", pid)
    mode = body.get("mode") or "all"
    if mode not in RUN_STAGES:
        raise HTTPException(400, "mode: all | kr")
    try:
        run_id = pipeline.start_background(pid, stages=RUN_STAGES[mode], use_judge=bool(body.get("use_judge", True)),
                                           only_failed=bool(body.get("only_failed", False)))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"run_id": run_id}


# -------------------------------------------------------------- документы ---

DOC_LABELS = {"queued": "ожидает", "processing": "обрабатывается", "done": "обработан", "partial": "частично",
              "error": "ошибка", "duplicate": "дубликат", "unsupported": "нет текста"}


@app.get("/api/projects/{pid}/documents")
def documents(pid: str):
    get("projects", pid)
    n_facts = {r["doc_id"]: r["n"] for r in db.q("SELECT doc_id, COUNT(*) n FROM cards WHERE project_id=? "
                                                  "GROUP BY doc_id", [pid])}
    pages: dict[str, dict] = {}
    for r in db.q("SELECT p.doc_id, p.kind, p.status, COUNT(*) n FROM pages p JOIN documents d ON d.id=p.doc_id "
                  "WHERE d.project_id=? GROUP BY p.doc_id, p.kind, p.status", [pid]):
        c = pages.setdefault(r["doc_id"], dict(done=0, skipped=0, references=0, no_text=0, error=0))
        if r["status"] in ("done", "error"):
            c[r["status"]] += r["n"]
        if r["kind"] in ("skipped", "references", "no_text"):
            c[r["kind"]] += r["n"]
    out = []
    for d in db.q("SELECT id, filename, n_pages, status, status_detail, biblio, duplicate_of FROM documents "
                  "WHERE project_id=? ORDER BY filename", [pid]):
        out.append(dict(id=d["id"], filename=d["filename"], n_pages=d["n_pages"], status=d["status"],
                        status_label=DOC_LABELS.get(d["status"], d["status"]), detail=d["status_detail"] or "",
                        facts=n_facts.get(d["id"], 0), duplicate_of=d["duplicate_of"],
                        pages=pages.get(d["id"], dict(done=0, skipped=0, references=0, no_text=0, error=0)),
                        biblio=_source(d)))
    return out


def _pdf_path(d: dict) -> str:
    p = UPLOADS_DIR / d["project_id"] / f"{d['id']}.pdf"
    if not p.is_file():
        raise HTTPException(404, "Файл PDF не найден")
    return str(p)


@app.get("/api/docs/{doc_id}/pdf")
def pdf(doc_id: str):
    d = get("documents", doc_id)
    return FileResponse(_pdf_path(d), media_type="application/pdf", filename=d["filename"],
                        content_disposition_type="inline")


@app.get("/api/docs/{doc_id}/page/{n}.png")
def page_png(doc_id: str, n: int, card: str | None = None):
    d = get("documents", doc_id)
    if not 1 <= n <= d["n_pages"]:
        raise HTTPException(404)
    quote = db.effective(get("cards", card)).get("quote") if card else None
    return Response(render_page_png(_pdf_path(d), n, quote), media_type="image/png")


# ------------------------------------------------------------------ факты ---

FACT_FILTERS = {
    "verified": lambda r: r["user_status"] == "confirmed" or (r["status"] == "verified" and not r["user_status"]),
    "needs_review": lambda r: r["status"] == "needs_review" and not r["user_status"],
    "unverified": lambda r: r["status"] == "unverified" and not r["user_status"],
    "confirmed": lambda r: r["user_status"] == "confirmed",
    "rejected": lambda r: r["user_status"] == "rejected",
}
SEARCH_FIELDS = ("statement_ru", "quote", "subject", "object", "population")
JUDGE_TEXT = {"supported": "Рецензент: цитата подтверждает формулировку",
              "partial": "Рецензент: цитата подтверждает формулировку лишь частично",
              "not_supported": "Рецензент: цитата не подтверждает формулировку"}


def fact_checks(v: dict) -> list[dict]:
    """Проверки для окна факта: ok True / False / None (рецензент снял замечание)."""
    out = [dict(ok=None if c.get("cleared") else c["ok"],
                text=c["text"] + (". Рецензент проверил: ошибки нет" if c.get("cleared") else ""))
           for c in v.get("checks") or []]
    j = v.get("judge")
    if not j or j.get("error"):
        out.append(dict(ok=False, text="Смысловая проверка не выполнена" if j else "Смысловая проверка не выполнялась"))
    else:
        out.append(dict(ok={"supported": True, "partial": None}.get(j["entailment"], False),
                        text=JUDGE_TEXT[j["entailment"]]))
        out += [dict(ok=False, text=text) for k, text in JUDGE_ISSUES.items() if j.get(k) is False]
    return out


def _fact_out(r: dict) -> dict:
    c = db.effective(r)
    v = r["verification"] or {}
    doc = get("documents", r["doc_id"])
    g = db.q1("SELECT id FROM fact_groups WHERE project_id=? AND instr(card_ids, ?) > 0", [r["project_id"], f'"{r["id"]}"'])
    pending = None
    if r["pending_auto"]:
        pending = {k: val for k, val in r["pending_auto"].items() if (r["auto"] or {}).get(k) != val}
    judge = v.get("judge") or {}
    return dict(
        id=r["id"], statement=c.get("statement_ru") or "", quote=c.get("quote") or "", page=_page(r, c),
        section=c.get("kr_section"), **{k: c.get(k) for k in P.CLAIM_PROPS if k not in ("statement_ru", "quote", "page")},
        expert_comment=c.get("expert_comment") or "", status=r["status"], user_status=r["user_status"],
        doc_id=r["doc_id"], source=_source(doc), checks=fact_checks(v),
        judge=dict(issues=judge.get("issues") or []) if judge.get("entailment") else None,
        group_id=g["id"] if g else None, edited=bool(r["override"]), pending=pending)


@app.get("/api/projects/{pid}/facts")
def facts(pid: str, status: str | None = None, section: str | None = None, doc: str | None = None,
          q: str | None = None, limit: int = 50, offset: int = 0):
    get("projects", pid)
    if status and status not in FACT_FILTERS:
        raise HTTPException(400, "status: " + " | ".join(FACT_FILTERS))
    docs = _docs(pid)
    ql = (q or "").strip().lower()
    items = []
    for r in db.q("SELECT * FROM cards WHERE project_id=?", [pid]):
        c = db.effective(r)
        if (doc and r["doc_id"] != doc) or (status and not FACT_FILTERS[status](r)) \
                or (section and c.get("kr_section") != section) \
                or (ql and ql not in " ".join(str(c.get(k) or "") for k in SEARCH_FIELDS).lower()):
            continue
        items.append(dict(id=r["id"], statement=c.get("statement_ru") or "", section=c.get("kr_section"),
                          status=r["status"], user_status=r["user_status"], doc_id=r["doc_id"],
                          filename=docs[r["doc_id"]]["filename"], page=_page(r, c)))
    items.sort(key=lambda x: (x["filename"], x["page"] or 0, x["id"]))
    limit, offset = max(1, min(limit, 500)), max(0, offset)
    return dict(total=len(items), items=items[offset:offset + limit])


@app.get("/api/facts/{fid}")
def fact(fid: str):
    return _fact_out(get("cards", fid))


@app.patch("/api/facts/{fid}")
def edit_fact(fid: str, body: dict = Body(...)):
    fields = {k: v for k, v in body.items() if k in projects.CARD_EDITABLE}
    if fields:
        projects.edit_card(fid, fields, str(body.get("comment") or ""))
    return _fact_out(get("cards", fid))


@app.post("/api/facts/{fid}/status")
def fact_status(fid: str, body: dict = Body(...)):
    projects.set_card_status(fid, body.get("status"), body.get("comment") or "")
    return _fact_out(get("cards", fid))


@app.post("/api/facts/{fid}/pending")
def fact_pending(fid: str, body: dict = Body(...)):
    projects.accept_pending("card", fid, bool(body.get("accept")))
    return _fact_out(get("cards", fid))


# ----------------------------------------------------------------- группы ---

def _group_ctx(pid: str) -> dict:
    return dict(cards={r["id"]: r for r in db.q("SELECT * FROM cards WHERE project_id=?", [pid])}, docs=_docs(pid),
                kr={f["_id"] for fs in kr_evidence(pid).values() for f in fs}, excluded=excluded_fact_ids(pid),
                open={i for c in db.q("SELECT side_a, side_b FROM conflicts WHERE project_id=? AND decision IS NULL",
                                      [pid]) for i in c["side_a"] + c["side_b"]})


def _fact_row(r: dict, ctx: dict) -> dict:
    c = db.effective(r)
    return dict(id=r["id"], statement=c.get("statement_ru") or "", filename=ctx["docs"][r["doc_id"]]["filename"],
                page=_page(r, c), status=export.fact_status(r))


def _group_out(row: dict, ctx: dict) -> dict:
    ids = [i for i in row["card_ids"] if i in ctx["cards"]]
    facts = [dict(_fact_row(ctx["cards"][i], ctx), in_kr=i in ctx["kr"], excluded=i in ctx["excluded"]) for i in ids]
    sources = {source_key(ctx["cards"][i]["doc_id"], ctx["docs"][ctx["cards"][i]["doc_id"]]["biblio"] or {})
               for i in ids}
    return dict(id=row["id"], section=row["section"], statement=group_statement(row), auto_statement=row["statement"],
                edited=bool(row["override"]), excluded=bool(row["excluded"]), n_facts=len(facts),
                n_sources=len(sources), n_in_kr=sum(f["in_kr"] for f in facts),
                in_conflict=bool(set(ids) & ctx["open"]), facts=facts)


@app.get("/api/projects/{pid}/groups")
def groups(pid: str):
    get("projects", pid)
    ctx = _group_ctx(pid)
    by_sec: dict[str, list] = {}
    for row in db.q("SELECT * FROM fact_groups WHERE project_id=?", [pid]):
        by_sec.setdefault(row["section"], []).append(_group_out(row, ctx))
    dup = {r["id"] for r in db.q("SELECT id FROM documents WHERE project_id=? AND status='duplicate'", [pid])}
    unassigned = [_fact_row(r, ctx) for r in ctx["cards"].values()
                  if db.effective(r).get("kr_section") == P.UNASSIGNED and r["user_status"] != "rejected"
                  and r["doc_id"] not in dup]
    unassigned.sort(key=lambda f: (f["filename"], f["page"] or 0, f["id"]))
    return dict(sections=[dict(section=s, title=P.SECTIONS[s], groups=sorted(by_sec[s], key=lambda g: -g["n_facts"]))
                          for s in O.SECTIONS_ENUM if s in by_sec],
                unassigned=unassigned, unassigned_title=P.UNASSIGNED_TITLE)


@app.patch("/api/groups/{gid}")
def edit_group(gid: str, body: dict = Body(...)):
    row = get("fact_groups", gid)
    excluded = bool(body["excluded"]) if body.get("excluded") is not None else None
    projects.edit_group(gid, statement=body.get("statement"), excluded=excluded)
    return _group_out(get("fact_groups", gid), _group_ctx(row["project_id"]))


# ----------------------------------------------------------- противоречия ---

def _conflict_out(row: dict, cards: dict, docs: dict, live: set) -> dict:
    def side(ids):
        out = []
        for i in ids:
            r = cards[i]
            c = db.effective(r)
            d = docs[r["doc_id"]]
            out.append(dict(id=i, statement=c.get("statement_ru") or "", page=_page(r, c), status=export.fact_status(r),
                            live=i in live, source=_source(d),
                            udd=None if c.get("secondary_citation")
                            else udd_for((d["biblio"] or {}).get("study_design"), row["section"])))
        return out

    return dict(id=row["id"], section=row["section"], section_title=P.SECTIONS[row["section"]], kind=row["kind"],
                description=row["description"], decision=row["decision"], decided_by=row["decided_by"],
                reason=row["reason"] or "", side_a=side(row["side_a"]), side_b=side(row["side_b"]))


def _conflict_ctx(pid: str) -> tuple[dict, dict, set]:
    return {r["id"]: r for r in db.q("SELECT * FROM cards WHERE project_id=?", [pid])}, \
        {d["id"]: d for d in db.q("SELECT id, filename, biblio FROM documents WHERE project_id=?", [pid])}, \
        set(live_facts(pid))


@app.get("/api/projects/{pid}/conflicts")
def conflicts(pid: str):
    get("projects", pid)
    ctx = _conflict_ctx(pid)
    rows = db.q("SELECT * FROM conflicts WHERE project_id=? ORDER BY decision IS NOT NULL, created_at, id", [pid])
    return [_conflict_out(r, *ctx) for r in rows]


@app.post("/api/conflicts/{cid}/decision")
def decide(cid: str, body: dict = Body(...)):
    row = get("conflicts", cid)
    if body.get("decision") not in DECISIONS:
        raise HTTPException(400, "decision: " + " | ".join(DECISIONS))
    projects.decide_conflict(cid, body["decision"], body.get("comment") or "")
    return _conflict_out(get("conflicts", cid), *_conflict_ctx(row["project_id"]))


# --------------------------------------------------------------------- КР ---

@app.get("/api/projects/{pid}/kr")
def kr(pid: str):
    m = export.kr_model(pid, include_rejected=True)
    m["stale"] = projects.kr_is_stale(pid)
    m["pending_conflicts"] = db.q1("SELECT COUNT(*) n FROM conflicts WHERE project_id=? AND decision IS NULL",
                                   [pid])["n"]
    return m


def _thesis_out(tid: str) -> dict:
    r = get("theses", tid)
    t = db.effective(r)
    return dict(id=r["id"], section=r["section"], type="text" if t.get("kind") == "narrative" else "thesis",
                text=t["statement"], phrase=t.get("strength_phrase") or "", body=t["body"], udd=t.get("udd"),
                uur=t.get("uur"), comment=t.get("comment") or "", status=r["status"], edited=bool(r["override"]),
                pending=bool(r["pending_auto"]), flags=r["review_flags"] or [], provenance=r["provenance"])


def _thesis_fields(body: dict) -> dict:
    fields = {k: v for k, v in body.items() if k in projects.THESIS_EDITABLE}
    for k in ("body", "comment", "subsection_title", "grade_rationale"):
        if k in fields and not isinstance(fields[k], str):
            raise HTTPException(400, f"{k}: ожидается строка")
    if fields.get("strength_phrase", O.PHRASES[0]) not in O.PHRASES:
        raise HTTPException(400, "Вводная фраза: " + " | ".join(O.PHRASES))
    if fields.get("direction", "positive") not in ("positive", "negative"):
        raise HTTPException(400, "direction: positive | negative")
    if fields.get("kr_section", "7") not in P.SECTIONS:
        raise HTTPException(400, "Неизвестный раздел КР")
    if "udd" in fields and fields["udd"] not in (None, 1, 2, 3, 4, 5):
        raise HTTPException(400, "УДД: 1–5")
    if "uur" in fields:
        # кириллические А, В, С вместо латинских
        u = str(fields["uur"] or "").strip().upper().translate(str.maketrans("АВС", "ABC"))
        if u not in ("", "A", "B", "C"):
            raise HTTPException(400, "УУР: A, B или C")
        fields["uur"] = u or None
    return fields


@app.patch("/api/theses/{tid}")
def edit_thesis(tid: str, body: dict = Body(...)):
    row = get("theses", tid)
    fields = _thesis_fields(body)
    if "body" in fields and db.effective(row).get("kind") == "narrative":
        fields["body"] = export.narrative_markers(row["project_id"], tid, fields["body"])
    if fields:
        projects.edit_thesis(tid, fields)
    return _thesis_out(tid)


@app.post("/api/theses/{tid}/status")
def thesis_status(tid: str, body: dict = Body(...)):
    projects.set_thesis_status(tid, body.get("status"), body.get("comment") or "")
    return _thesis_out(tid)


@app.post("/api/theses/{tid}/pending")
def thesis_pending(tid: str, body: dict = Body(...)):
    projects.accept_pending("thesis", tid, bool(body.get("accept")))
    return _thesis_out(tid)


@app.post("/api/projects/{pid}/theses")
def add_thesis(pid: str, body: dict = Body(...)):
    section = str(body.get("section") or "")
    text = str(body.get("body") or "").strip()
    phrase = str(body.get("phrase") or "").strip()
    if section not in P.SECTIONS:
        raise HTTPException(400, "Неизвестный раздел КР")
    if not text:
        raise HTTPException(400, "Пустой текст тезиса")
    if phrase and phrase not in O.PHRASES:
        raise HTTPException(400, "Вводная фраза: " + " | ".join(O.PHRASES))
    return _thesis_out(projects.add_expert_thesis(pid, section, phrase, text, str(body.get("comment") or "").strip()))


# ---------------------------------------------------------------- экспорт ---

@app.get("/api/projects/{pid}/export/{fmt}")
def do_export(pid: str, fmt: str):
    get("projects", pid)
    name = f"{pid}_{time.strftime('%Y%m%d_%H%M')}"
    attach = lambda ext: {"Content-Disposition": f'attachment; filename="{name}{ext}"'}  # noqa: E731
    if fmt == "json":
        data = json.dumps(export.build_export(pid), ensure_ascii=False, indent=1, default=str)
        return Response(data, media_type="application/json", headers=attach(".json"))
    if fmt == "md":
        return Response(export.to_markdown(pid), media_type="text/markdown; charset=utf-8", headers=attach(".md"))
    if fmt == "docx":
        buf = io.BytesIO()
        export.to_docx(pid, buf)
        return Response(buf.getvalue(), headers=attach(".docx"),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    raise HTTPException(404)


# ------------------------------------------------------- история прогонов ---

@app.get("/api/projects/{pid}/runs")
def runs(pid: str):
    get("projects", pid)
    return [dict(r, summary=load_summary(r["id"])) for r in
            db.q("SELECT id, status, stage, started_at, finished_at, error FROM runs WHERE project_id=? "
                 "ORDER BY started_at DESC LIMIT 50", [pid])]


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    r = get("runs", run_id)
    log = r.pop("log") or []
    r.pop("config")
    return dict(run=r, summary=load_summary(run_id), log=log,
                files=[f for f in SNAP_FILES if (SNAP_DIR / run_id / f).exists()])


@app.get("/api/runs/{run_id}/file/{name}")
def run_file(run_id: str, name: str):
    get("runs", run_id)
    f = SNAP_DIR / run_id / name
    if name not in SNAP_FILES or not f.exists():
        raise HTTPException(404, "Файл снимка не найден")
    return FileResponse(f, filename=f"{run_id}_{name}")


def main():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
