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

from . import db, export, pipeline, projects, prompts as P
from .config import DATA_DIR, UPLOADS_DIR
from .groups import group_statement, live_facts
from .pdf_ingest import add_document, render_page_png
from .resolve import DECISIONS, excluded_fact_ids, sides
from .snapshots import FILES as SNAP_FILES, SNAP_DIR, load_summary
from .synth import kr_evidence, source_key, udd_for

STATIC = Path(__file__).parent / "static"
RUN_STAGES = {"all": None, "kr": ["group", "resolve", "kr"]}


@asynccontextmanager
async def _lifespan(_app):
    pipeline.reap_dead_runs()
    yield


app = FastAPI(title="KRGen", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(projects.NotFound)
def _not_found(_req, e: projects.NotFound):
    return JSONResponse({"detail": f"Не найдено: {e}"}, status_code=404)


@app.middleware("http")
async def _no_cache_ui(request, call_next):
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _project(pid: str) -> dict:
    p = db.q1("SELECT * FROM projects WHERE id=?", [pid])
    if not p:
        raise projects.NotFound(pid)
    return p


def _row(table: str, id_: str) -> dict:
    r = db.q1(f"SELECT * FROM {table} WHERE id=?", [id_])
    if not r:
        raise projects.NotFound(id_)
    return r


def _docs(pid: str) -> dict[str, dict]:
    return {d["id"]: d for d in db.q("SELECT id, filename, biblio FROM documents WHERE project_id=?", [pid])}


def _source(doc_id: str, doc: dict) -> dict:
    b = doc.get("biblio") or {}
    design = b.get("study_design")
    return dict(doc_id=doc_id, filename=doc.get("filename"), title=b.get("title") or "", year=b.get("year") or None,
                design=design, design_label=export.DESIGN_RU.get(design, ""), doi=b.get("doi") or "")


def _page(r: dict, c: dict):
    return ((r.get("verification") or {}).get("quote") or {}).get("found_page") or c.get("page")


# ---------------------------------------------------------------- проекты ---

@app.get("/api/projects")
def list_projects():
    return db.q("SELECT id, title, created_at FROM projects WHERE id NOT LIKE 'exp_%' ORDER BY created_at DESC")


@app.post("/api/projects")
def create_project(body: dict = Body(...)):
    title, org = body.get("title") or "", body.get("developer_org") or ""
    codes = body.get("icd_codes") or []
    if isinstance(codes, str):
        codes = codes.replace(";", ",").split(",")
    if not (isinstance(title, str) and isinstance(org, str) and isinstance(codes, list)
            and all(isinstance(c, str) for c in codes)):
        raise HTTPException(400, "Тема, организация и коды МКБ-10 — строки")
    try:
        return projects.create_project(title, body.get("age_category") or "mixed", codes, org.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))


def _counts(pid: str) -> dict:
    n = lambda sql: db.q1(sql, [pid])["n"]
    return dict(
        documents=n("SELECT COUNT(*) n FROM documents WHERE project_id=?"),
        facts=n("SELECT COUNT(*) n FROM cards WHERE project_id=?"),
        facts_verified=n("SELECT COUNT(*) n FROM cards WHERE project_id=? AND (user_status='confirmed' "
                         "OR (status='verified' AND user_status IS NULL))"),
        facts_review=n("SELECT COUNT(*) n FROM cards WHERE project_id=? AND status='needs_review' "
                       "AND user_status IS NULL"),
        facts_rejected=n("SELECT COUNT(*) n FROM cards WHERE project_id=? AND user_status='rejected'"),
        groups=n("SELECT COUNT(*) n FROM clusters WHERE project_id=?"),
        conflicts=n("SELECT COUNT(*) n FROM conflicts WHERE project_id=?"),
        conflicts_open=n("SELECT COUNT(*) n FROM conflicts WHERE project_id=? AND decision IS NULL"),
        theses=n("SELECT COUNT(*) n FROM theses WHERE project_id=? AND status!='rejected' AND merged_into IS NULL "
                 "AND (orphan=0 OR status='approved' OR provenance='manual')"),
    )


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    p = _project(pid)
    run = db.q1("SELECT id, status, stage, progress, started_at, finished_at, error FROM runs WHERE project_id=? "
                "ORDER BY started_at DESC", [pid])
    return dict(project={k: p.get(k) for k in ("id", "title", "age_category", "icd_codes", "developer_org")},
                counts=_counts(pid), run=run, running=pipeline.is_running(pid))


@app.post("/api/projects/{pid}/upload")
async def upload(pid: str, files: list[UploadFile] = File(...)):
    _project(pid)
    out = []
    for f in files:
        data = await f.read()
        name = f.filename or "document.pdf"
        if not name.lower().endswith(".pdf"):
            if b"%PDF" not in data[:1024]:
                out.append(dict(id=None, filename=f.filename, status="error", detail="Поддерживаются только PDF"))
                continue
            name += ".pdf"
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=DATA_DIR) as tmp:
            tmp.write(data)
        try:
            d = add_document(pid, Path(tmp.name), name)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        out.append(dict(id=d["id"], filename=d["filename"], status=d["status"], detail=d.get("status_detail") or ""))
    return out


@app.post("/api/projects/{pid}/run")
async def run(pid: str, body: dict = Body(default={})):
    _project(pid)
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

DOC_LABELS = {"queued": "ожидает", "processing": "обрабатывается", "done": "обработан", "done_empty": "обработан",
              "partial": "частично", "error": "ошибка", "duplicate": "дубликат", "unsupported": "нет текста"}


@app.get("/api/projects/{pid}/documents")
def documents(pid: str):
    _project(pid)
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
        b = d["biblio"] or {}
        out.append(dict(
            id=d["id"], filename=d["filename"], n_pages=d["n_pages"] or 0, status=d["status"],
            status_label=DOC_LABELS.get(d["status"], d["status"]), detail=d["status_detail"] or "",
            facts=n_facts.get(d["id"], 0), duplicate_of=d["duplicate_of"],
            pages=pages.get(d["id"], dict(done=0, skipped=0, references=0, no_text=0, error=0)),
            biblio=dict(title=b.get("title") or "", year=b.get("year") or None, design=b.get("study_design"),
                        design_label=export.DESIGN_RU.get(b.get("study_design"), ""), doi=b.get("doi") or "")))
    return out


def _pdf_path(d: dict) -> str:
    # путь в базе абсолютный: после переноса папки данных файл ищется на штатном месте
    for p in (Path(d["path"] or ""), UPLOADS_DIR / d["project_id"] / f"{d['id']}.pdf"):
        if p.is_file():
            return str(p)
    raise HTTPException(404, "Файл PDF не найден")


@app.get("/api/docs/{doc_id}/pdf")
def pdf(doc_id: str):
    d = _row("documents", doc_id)
    return FileResponse(_pdf_path(d), media_type="application/pdf", filename=d["filename"],
                        content_disposition_type="inline")


@app.get("/api/docs/{doc_id}/page/{n}.png")
def page_png(doc_id: str, n: int, card: str | None = None):
    d = _row("documents", doc_id)
    if not 1 <= n <= (d["n_pages"] or 0):
        raise HTTPException(404)
    quote = None
    if card:
        r = db.q1("SELECT * FROM cards WHERE id=?", [card])
        if r:
            quote = db.effective(r).get("quote")
    path = _pdf_path(d)
    try:
        png = render_page_png(path, n, quote)
    except (RuntimeError, IndexError):  # pymupdf.FileDataError — битый или подменённый файл
        raise HTTPException(422, "Не удалось открыть PDF")
    return Response(png, media_type="image/png")


# ------------------------------------------------------------------ факты ---

FACT_FILTERS = {
    "verified": lambda r: r.get("user_status") == "confirmed" or (r["status"] == "verified" and not r.get("user_status")),
    "needs_review": lambda r: r["status"] == "needs_review" and not r.get("user_status"),
    "unverified": lambda r: r["status"] == "unverified" and not r.get("user_status"),
    "confirmed": lambda r: r.get("user_status") == "confirmed",
    "rejected": lambda r: r.get("user_status") == "rejected",
}
FACT_FIELDS = ("claim_type", "polarity", "population", "age_group", "intervention", "comparator", "outcome",
               "dose", "timing", "thresholds", "conditions", "topic_relevance")
SEARCH_FIELDS = ("statement_ru", "quote", "intervention", "population", "outcome")

# (текст при успехе, текст при неудаче); None — берётся текст самой проверки
CHECK_TEXT = {
    "quote_found": (None, None),
    "numbers": ("Числа формулировки есть в цитате", "Часть чисел есть рядом с цитатой, но не в ней"),
    "numbers_absent": (None, "Числа формулировки не найдены в цитате"),
    "negation": (None, None),
    "recommendation_marker": ("В цитате есть рекомендательная формулировка",
                              "В цитате нет рекомендательной формулировки"),
    "quote_number_boundary": (None, "Цитата обрезает число"),
    "quote_sign": (None, "Знак минус в цитате и на странице не совпадает"),
    "quote_negation_vs_page": (None, "Отрицание в цитате не совпадает со страницей"),
    "quote_direction_vs_page": (None, "Слово в цитате заменено на противоположное по смыслу"),
    "quote_numbers_vs_page": (None, "Цифры цитаты не совпадают со страницей"),
    "pdf_text_layer": (None, "Цифры отличаются от текстового слоя PDF"),
    "quote_layout_vs_page": (None, "На месте цитаты на странице есть посторонние цифры"),
    "quote_units_vs_page": (None, "Единицы измерения в цитате и на странице не совпадают"),
}
JUDGE_VERDICT = {"supported": "подтверждает", "partial": "частично", "not_supported": "не подтверждает"}
JUDGE_TEXT = {"supported": "Рецензент: цитата подтверждает формулировку",
              "partial": "Рецензент: цитата подтверждает формулировку лишь частично",
              "not_supported": "Рецензент: цитата не подтверждает формулировку"}
JUDGE_FLAGS = {"numbers_ok": "числа", "negation_ok": "отрицание или направление вывода",
               "population_ok": "группа пациентов", "claim_type_ok": "тип утверждения"}


def _short(s: str, n: int = 100) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def fact_checks(v: dict) -> list[dict]:
    out = []
    for c in v.get("checks") or []:
        if c.get("severity") == "info":
            continue
        ok, detail = bool(c.get("ok")), c.get("detail") or ""
        ok_text, bad_text = CHECK_TEXT.get(c.get("name"), (None, None))
        text = (ok_text if ok else bad_text) or detail
        if not ok and bad_text and ": " in detail:
            text += ": " + _short(detail.split(": ", 1)[1])
        if not ok and c.get("resolved_by_judge"):
            ok, text = None, text + ". Рецензент проверил: ошибки нет"
        out.append(dict(ok=ok, text=text))
    j = v.get("judge")
    if not j:
        out.append(dict(ok=False, text="Смысловая проверка не выполнялась"))
    elif j.get("error"):
        out.append(dict(ok=False, text="Смысловая проверка не выполнена"))
    else:
        out.append(dict(ok={"supported": True, "partial": None}.get(j.get("entailment"), False),
                        text=JUDGE_TEXT.get(j.get("entailment"), "Рецензент не вынес вердикт")))
        out += [dict(ok=False, text=f"Рецензент: не совпадает {what}") for k, what in JUDGE_FLAGS.items()
                if j.get(k) is False]
    return out


def _judge(j: dict | None) -> dict | None:
    if not j or j.get("error"):
        return None
    return dict(verdict=JUDGE_VERDICT.get(j.get("entailment"), ""), entailment=j.get("entailment"),
                issues=list(j.get("issues") or []))


def _fact_out(r: dict) -> dict:
    c = db.effective(r)
    v = r.get("verification") or {}
    doc = db.q1("SELECT id, filename, biblio FROM documents WHERE id=?", [r["doc_id"]]) or {}
    g = db.q1("SELECT id FROM clusters WHERE project_id=? AND instr(card_ids, ?) > 0", [r["project_id"], f'"{r["id"]}"'])
    pending = None
    if r.get("pending_auto"):
        auto = r.get("auto") or {}
        pending = {k: val for k, val in r["pending_auto"].items() if not k.startswith("_") and auto.get(k) != val}
    return dict(
        id=r["id"], statement=c.get("statement_ru") or "", quote=c.get("quote") or "", page=_page(r, c),
        section=c.get("kr_section") or "7", **{k: c.get(k) for k in FACT_FIELDS},
        secondary_citation=bool(c.get("secondary_citation")), expert_comment=c.get("expert_comment") or "",
        status=r["status"], user_status=r.get("user_status"), doc_id=r["doc_id"],
        source=_source(r["doc_id"], doc), checks=fact_checks(v), judge=_judge(v.get("judge")),
        group_id=g["id"] if g else None, edited=bool(r.get("override")), pending=pending or None)


@app.get("/api/projects/{pid}/facts")
def facts(pid: str, status: str | None = None, section: str | None = None, doc: str | None = None,
          q: str | None = None, limit: int = 50, offset: int = 0):
    _project(pid)
    if status and status not in FACT_FILTERS:
        raise HTTPException(400, "status: " + " | ".join(FACT_FILTERS))
    sql, args = "SELECT * FROM cards WHERE project_id=?", [pid]
    if doc:
        sql += " AND doc_id=?"
        args.append(doc)
    docs = _docs(pid)
    ql = (q or "").strip().lower()
    items = []
    for r in db.q(sql, args):
        if status and not FACT_FILTERS[status](r):
            continue
        c = db.effective(r)
        sec = c.get("kr_section") or "7"
        if section and sec != section:
            continue
        if ql and ql not in " ".join(str(c.get(k) or "") for k in SEARCH_FIELDS).lower():
            continue
        items.append(dict(id=r["id"], statement=c.get("statement_ru") or "", section=sec, status=r["status"],
                          user_status=r.get("user_status"), doc_id=r["doc_id"],
                          filename=(docs.get(r["doc_id"]) or {}).get("filename"), page=_page(r, c)))
    items.sort(key=lambda x: (x["filename"] or "", x["page"] if isinstance(x["page"], int) else 0, x["id"]))
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    return dict(total=len(items), items=items[offset:offset + limit])


@app.get("/api/facts/{fid}")
def fact(fid: str):
    return _fact_out(_row("cards", fid))


@app.patch("/api/facts/{fid}")
def edit_fact(fid: str, body: dict = Body(...)):
    _row("cards", fid)
    fields = {k: v for k, v in body.items() if k in projects.CARD_EDITABLE}
    if fields:
        try:
            projects.edit_card(fid, fields, str(body.get("comment") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
    return _fact_out(_row("cards", fid))


@app.post("/api/facts/{fid}/status")
def fact_status(fid: str, body: dict = Body(...)):
    st = body.get("status")
    if st not in ("confirmed", "rejected", None):
        raise HTTPException(400, "status: confirmed | rejected | null")
    projects.set_card_status(fid, st, body.get("comment") or "")
    return _fact_out(_row("cards", fid))


@app.post("/api/facts/{fid}/pending")
def fact_pending(fid: str, body: dict = Body(...)):
    _row("cards", fid)
    projects.accept_pending("card", fid, bool(body.get("accept")))
    return _fact_out(_row("cards", fid))


# ----------------------------------------------------------------- группы ---

def _conflict_ids(row: dict) -> set[str]:
    return {i for k in ("side_a", "side_b", "card_ids") for i in export.ids_of(row.get(k))}


def _group_ctx(pid: str) -> dict:
    return dict(
        cards={r["id"]: r for r in db.q("SELECT * FROM cards WHERE project_id=?", [pid])}, docs=_docs(pid),
        kr={export.fact_id(f) for fs in kr_evidence(pid).values() for f in fs}, excluded=excluded_fact_ids(pid),
        open={i for c in db.q("SELECT card_ids, side_a, side_b FROM conflicts WHERE project_id=? AND decision IS NULL",
                              [pid]) for i in _conflict_ids(c)})


def _group_out(row: dict, ctx: dict) -> dict:
    ids = export.ids_of(row.get("card_ids"))
    facts, sources = [], set()
    for i in ids:
        r = ctx["cards"].get(i)
        if not r:
            continue
        c = db.effective(r)
        d = ctx["docs"].get(r["doc_id"]) or {}
        sources.add(source_key(r["doc_id"], d.get("biblio") or {}))
        facts.append(dict(id=i, statement=c.get("statement_ru") or "", filename=d.get("filename"), page=_page(r, c),
                          status=export.fact_status(r), in_kr=i in ctx["kr"], excluded=i in ctx["excluded"]))
    ov = row.get("override") if isinstance(row.get("override"), dict) else {}
    return dict(id=row["id"], section=row.get("section") or "7", statement=group_statement(row),
                auto_statement=group_statement({**row, "override": None}), edited=bool(ov.get("statement")),
                excluded=bool(row.get("excluded")), n_facts=len(facts), n_sources=len(sources),
                n_in_kr=sum(f["in_kr"] for f in facts), in_conflict=bool(set(ids) & ctx["open"]), facts=facts)


def _section_order(s: str) -> tuple:
    keys = list(P.SECTIONS)
    return (keys.index(s) if s in keys else len(keys), s)


@app.get("/api/projects/{pid}/groups")
def groups(pid: str):
    _project(pid)
    ctx = _group_ctx(pid)
    by_sec: dict[str, list] = {}
    for row in db.q("SELECT * FROM clusters WHERE project_id=?", [pid]):
        g = _group_out(row, ctx)
        by_sec.setdefault(g["section"], []).append(g)
    return dict(sections=[dict(section=s, title=P.SECTIONS.get(s, s),
                               groups=sorted(gs, key=lambda g: (-g["n_facts"], g["id"])))
                          for s, gs in sorted(by_sec.items(), key=lambda x: _section_order(x[0]))])


@app.patch("/api/groups/{gid}")
def edit_group(gid: str, body: dict = Body(...)):
    row = _row("clusters", gid)
    statement = None
    if "statement" in body:
        statement = (body["statement"] or "").strip()  # пустая строка — вернуть автоматическую формулировку
    excluded = bool(body["excluded"]) if body.get("excluded") is not None else None
    if statement is not None or excluded is not None:
        projects.edit_group(gid, statement=statement, excluded=excluded)
    return _group_out(_row("clusters", gid), _group_ctx(row["project_id"]))


# ----------------------------------------------------------- противоречия ---

def _side_fact(i: str, cards: dict, docs: dict, live: set) -> dict:
    r = cards[i]
    c = db.effective(r)
    d = docs.get(r["doc_id"]) or {}
    design = (d.get("biblio") or {}).get("study_design")
    sec = c.get("kr_section") or "7"
    return dict(id=i, statement=c.get("statement_ru") or "", filename=d.get("filename"), page=_page(r, c),
                status=export.fact_status(r), live=i in live,
                udd=None if c.get("secondary_citation") else udd_for(design, sec), source=_source(r["doc_id"], d))


def _conflict_out(row: dict, cards: dict, docs: dict, live: set) -> dict:
    a, b = sides(row)
    first = next((cards[i] for i in a + b if i in cards), None)
    sec = row.get("section") or (db.effective(first).get("kr_section") if first else None) or "7"
    kind = row.get("kind") if row.get("kind") in P.CONFLICT_KINDS else "other"
    return dict(id=row["id"], section=sec, section_title=P.SECTIONS.get(sec, sec), kind=kind,
                description=row.get("description") or "", decision=row.get("decision"),
                decided_by=row.get("decided_by"), reason=row.get("reason") or "",
                side_a=[_side_fact(i, cards, docs, live) for i in a if i in cards],
                side_b=[_side_fact(i, cards, docs, live) for i in b if i in cards])


def _conflict_ctx(pid: str) -> tuple[dict, dict, set]:
    return ({r["id"]: r for r in db.q("SELECT * FROM cards WHERE project_id=?", [pid])}, _docs(pid),
            set(live_facts(pid)))


@app.get("/api/projects/{pid}/conflicts")
def conflicts(pid: str):
    _project(pid)
    ctx = _conflict_ctx(pid)
    rows = db.q("SELECT * FROM conflicts WHERE project_id=? ORDER BY decision IS NOT NULL, created_at, id", [pid])
    return [_conflict_out(r, *ctx) for r in rows]


@app.post("/api/conflicts/{cid}/decision")
def decide(cid: str, body: dict = Body(...)):
    row = _row("conflicts", cid)
    if body.get("decision") not in DECISIONS:
        raise HTTPException(400, "decision: " + " | ".join(DECISIONS))
    projects.decide_conflict(cid, body["decision"], body.get("comment") or "")
    return _conflict_out(_row("conflicts", cid), *_conflict_ctx(row["project_id"]))


# --------------------------------------------------------------------- КР ---

@app.get("/api/projects/{pid}/kr")
def kr(pid: str):
    m = export.kr_model(pid, include_rejected=True)
    m["stale"] = bool(projects.kr_is_stale(pid))
    m["pending_conflicts"] = db.q1("SELECT COUNT(*) n FROM conflicts WHERE project_id=? AND decision IS NULL",
                                   [pid])["n"]
    return m


THESIS_KEYS = {"body", "strength_phrase", "udd", "uur", "comment", "subsection_title", "direction",
               "kr_section", "grade_rationale"}


def _thesis_out(tid: str) -> dict:
    r = _row("theses", tid)
    t = db.effective(r)
    return dict(id=r["id"], section=r.get("section"), type="text" if t.get("kind") == "narrative" else "thesis",
                text=t.get("statement") or "", phrase=t.get("strength_phrase") or "", body=t.get("body") or "",
                udd=t.get("udd"), uur=t.get("uur"), comment=t.get("comment") or "", status=r["status"],
                edited=bool(r.get("override")), pending=bool(r.get("pending_auto")),
                flags=list(r.get("review_flags") or []), provenance=r.get("provenance"))


def _grade_fields(fields: dict) -> dict:
    bad = [k for k in ("body", "comment", "subsection_title", "grade_rationale")
           if k in fields and not isinstance(fields[k], str)]
    if bad:
        raise HTTPException(400, ", ".join(bad) + ": ожидается строка")
    if "strength_phrase" in fields:
        fields["strength_phrase"] = fields["strength_phrase"] or ""
        if fields["strength_phrase"] not in ("", *export.PHRASES):
            raise HTTPException(400, "Вводная фраза: " + " | ".join(export.PHRASES))
    if fields.get("direction", "positive") not in ("positive", "negative"):
        raise HTTPException(400, "direction: positive | negative")
    if fields.get("kr_section", "7") not in P.SECTIONS:
        raise HTTPException(400, "Неизвестный раздел КР")
    if "udd" in fields:
        u = fields["udd"]
        if u in ("", None):
            fields["udd"] = None
        elif str(u).strip() in ("1", "2", "3", "4", "5"):
            fields["udd"] = int(str(u).strip())
        else:
            raise HTTPException(400, "УДД: 1–5")
    if "uur" in fields:
        u = str(fields["uur"] or "").strip().upper().replace("А", "A").replace("В", "B").replace("С", "C")
        if u not in ("", "A", "B", "C"):
            raise HTTPException(400, "УУР: A, B или C")
        fields["uur"] = u or None
    return fields


@app.patch("/api/theses/{tid}")
def edit_thesis(tid: str, body: dict = Body(...)):
    row = _row("theses", tid)
    fields = {k: v for k, v in body.items() if k in THESIS_KEYS}
    if fields:
        fields = _grade_fields(fields)
        if "body" in fields and db.effective(row).get("kind") == "narrative":
            fields["body"] = export.narrative_markers(row["project_id"], tid, fields["body"])
        projects.edit_thesis(tid, fields)
    return _thesis_out(tid)


@app.post("/api/theses/{tid}/status")
def thesis_status(tid: str, body: dict = Body(...)):
    if body.get("status") not in ("approved", "rejected", "draft"):
        raise HTTPException(400, "status: approved | rejected | draft")
    projects.set_thesis_status(tid, body["status"], body.get("comment") or "")
    return _thesis_out(tid)


@app.post("/api/theses/{tid}/pending")
def thesis_pending(tid: str, body: dict = Body(...)):
    _row("theses", tid)
    projects.accept_pending("thesis", tid, bool(body.get("accept")))
    return _thesis_out(tid)


@app.post("/api/projects/{pid}/theses")
def add_thesis(pid: str, body: dict = Body(...)):
    _project(pid)
    section = str(body.get("section") or "")
    if section not in P.SECTIONS:
        raise HTTPException(400, "Неизвестный раздел КР")
    text = (body.get("body") or "").strip()
    if not text:
        raise HTTPException(400, "Пустой текст тезиса")
    phrase = str(body.get("phrase") or "").strip()
    tid = projects.add_expert_thesis(pid, section, phrase, text, (body.get("comment") or "").strip(),
                                     author="эксперт")
    return _thesis_out(tid)


# ---------------------------------------------------------------- экспорт ---

@app.get("/api/projects/{pid}/export/{fmt}")
def do_export(pid: str, fmt: str):
    _project(pid)
    stamp = time.strftime("%Y%m%d_%H%M")
    attach = lambda name: {"Content-Disposition": f'attachment; filename="{pid}_{stamp}{name}"'}
    if fmt == "json":
        data = json.dumps(export.build_export(pid), ensure_ascii=False, indent=1, default=str)
        return Response(data, media_type="application/json", headers=attach(".json"))
    if fmt == "kr_schema":
        data, errors = export.kr_schema_export(pid)
        return Response(json.dumps(data, ensure_ascii=False, indent=1), media_type="application/json",
                        headers=attach("_kr_recommendation.json"))
    if fmt == "kr_schema_check":
        _, errors = export.kr_schema_export(pid)
        return dict(valid=not errors, errors=errors[:100])
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
    _project(pid)
    return [dict(r, summary=load_summary(r["id"])) for r in
            db.q("SELECT id, status, stage, started_at, finished_at, error FROM runs WHERE project_id=? "
                 "ORDER BY started_at DESC LIMIT 50", [pid])]


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    r = _row("runs", run_id)
    log = r.pop("log", None) or []
    r.pop("config", None)
    return dict(run=r, summary=load_summary(run_id), log=log,
                files=[f for f in SNAP_FILES if (SNAP_DIR / run_id / f).exists()])


@app.get("/api/runs/{run_id}/file/{name}")
def run_file(run_id: str, name: str):
    _row("runs", run_id)
    f = SNAP_DIR / run_id / name
    if name not in SNAP_FILES or not f.exists():
        raise HTTPException(404, "Файл снимка не найден")
    return FileResponse(f, filename=f"{run_id}_{name}")


def main():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
