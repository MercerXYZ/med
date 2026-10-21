"""Конвейер обработки проекта и отчёт об обработке."""
from __future__ import annotations

import asyncio
import os
import re
import socket
import time
import traceback
import uuid

from . import db
from .config import SETTINGS
from .extract import ensure_keywords, extract_biblio, extract_document
from .groups import group_project
from .llm import CallCtx, LLMError, reset_fault_counter
from .pdf_ingest import make_chunks, plan_pages, score_pages
from .resolve import resolve_project
from .synth import build_kr
from .verify import verify_cards

STAGES = ["keywords", "extract", "verify", "group", "resolve", "kr"]
ALIVE_WINDOW = 300
_running: dict[str, asyncio.Task] = {}
_live: set[str] = set()  # run_id прогонов этого процесса


def _progress(run_id: str, stage: str, **kw) -> None:
    run = db.q1("SELECT log FROM runs WHERE id=?", [run_id]) or {}
    log = run.get("log") or []
    msg = kw.pop("msg", None)
    if msg:
        log.append({"ts": time.time(), "stage": stage, "msg": msg})
    db.update("runs", run_id, stage=stage, progress=dict(stage=stage, **kw), log=log[-300:])


def _norm_title(t: str | None) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", (t or "").lower())


def find_duplicate_by_biblio(project_id: str, doc: dict) -> dict | None:
    """Ранее загруженный документ с тем же DOI или названием (основным считается первый)."""
    me = db.q1("SELECT biblio, created_at FROM documents WHERE id=?", [doc["id"]])
    b = me["biblio"] or {}
    doi, title = (b.get("doi") or "").lower().strip(), _norm_title(b.get("title"))
    for o in db.q("SELECT id, filename, biblio FROM documents WHERE project_id=? AND id!=? "
                  "AND status!='duplicate' AND (created_at<? OR (created_at=? AND id<?)) ORDER BY created_at",
                  [project_id, doc["id"], me["created_at"], me["created_at"], doc["id"]]):
        ob = o["biblio"] or {}
        if (doi and doi == (ob.get("doi") or "").lower().strip()) or (
                len(title) >= 30 and title == _norm_title(ob.get("title"))):
            return o
    return None


def doc_status(doc_id: str) -> tuple[str, str]:
    d = db.q1("SELECT status, status_detail FROM documents WHERE id=?", [doc_id])
    if d["status"] in ("duplicate", "unsupported") or (d["status"] == "error" and "открыть" in (d["status_detail"] or "")):
        return d["status"], d["status_detail"]
    pg = {r["kind"]: r["n"] for r in db.q("SELECT kind, COUNT(*) n FROM pages WHERE doc_id=? GROUP BY kind", [doc_id])}
    ch = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM chunks WHERE doc_id=? GROUP BY status", [doc_id])}
    n_cards = db.q1("SELECT COUNT(*) n FROM cards WHERE doc_id=?", [doc_id])["n"]
    total, err = sum(ch.values()), ch.get("error", 0)
    parts = [f"фрагментов {ch.get('done', 0)}/{total}", f"фактов {n_cards}"]
    if pg.get("skipped"):
        parts.append(f"пропущено {pg['skipped']} стр.")
    if pg.get("no_text"):
        parts.append(f"без текста {pg['no_text']} стр.")
    if pg.get("references"):
        parts.append(f"список литературы {pg['references']} стр.")
    if err:
        e = db.q1("SELECT error FROM chunks WHERE doc_id=? AND status='error' LIMIT 1", [doc_id])
        parts.append(f"ошибки в {err} фрагм.: {e['error'][:160]}")
    if total == 0:
        return "queued", "Ещё не обрабатывался"
    if err == total:
        status = "error"
    elif err or ch.get("running") or ch.get("pending"):
        status = "partial"
    elif pg.get("skipped") or pg.get("no_text"):
        status = "partial" if n_cards else "done_empty"
    else:
        status = "done" if n_cards else "done_empty"
    return status, "; ".join(parts)


def _count(sql: str, params: list) -> int:
    return db.q1(sql, params)["n"]


async def _extract(project: dict, run_id: str, ctx: CallCtx, doc_ids, only_failed: bool, errors: list) -> None:
    project_id = project["id"]
    docs = db.q("SELECT * FROM documents WHERE project_id=? AND status NOT IN "
                "('duplicate','unsupported') AND NOT (status='error' AND n_pages=0)", [project_id])
    if doc_ids:
        docs = [d for d in docs if d["id"] in doc_ids]
    done = 0
    _progress(run_id, "extract", done=0, total=len(docs), msg=f"Извлечение фактов: {len(docs)} док.")

    async def one(doc):
        nonlocal done
        db.update("documents", doc["id"], status="processing")
        try:
            await extract_biblio(doc, ctx)
        except LLMError as e:
            errors.append(f"{doc['filename']}: библиография — {e}")
        dup = find_duplicate_by_biblio(project_id, doc)
        if dup:  # та же статья в другой вёрстке: повторно не извлекаем
            db.update("documents", doc["id"], status="duplicate", duplicate_of=dup["id"],
                      status_detail=f"Та же публикация, что {dup['filename']}", updated_at=time.time())
            done += 1
            _progress(run_id, "extract", done=done, total=len(docs), msg=f"{doc['filename']}: дубликат")
            return
        score_pages(doc["id"], project.get("keywords") or [])
        chunks = make_chunks(doc["id"], plan_pages(doc["id"]))
        await extract_document(project, doc, chunks, ctx, only_failed=only_failed)
        st, detail = doc_status(doc["id"])
        db.update("documents", doc["id"], status=st, status_detail=detail, updated_at=time.time())
        done += 1
        _progress(run_id, "extract", done=done, total=len(docs), msg=f"{doc['filename']}: {detail}")

    async def safe(doc):
        try:
            await one(doc)
        except Exception as e:  # noqa: BLE001 — сбой одного документа не роняет прогон
            errors.append(f"{doc['filename']}: {type(e).__name__}: {e}")
            db.update("documents", doc["id"], status="error", updated_at=time.time(),
                      status_detail=f"Ошибка обработки: {type(e).__name__}: {e}"[:500])
            db.ex("UPDATE chunks SET status='error', error=? WHERE doc_id=? AND status='running'",
                  [f"{type(e).__name__}: {e}"[:300], doc["id"]])

    await asyncio.gather(*(safe(d) for d in docs))


def _check_stages(stages: list[str] | None) -> list[str]:
    stages = stages or STAGES
    unknown = [s for s in stages if s not in STAGES]
    if unknown:
        raise ValueError(f"Неизвестные этапы: {', '.join(unknown)}. Есть: {', '.join(STAGES)}")
    return stages


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill(pid, 0) в Windows шлёт Ctrl+C, а не проверяет процесс
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _last_activity(run: dict) -> float:
    last_log = max([e.get("ts", 0) for e in (run.get("log") or [])] + [run["started_at"] or 0])
    last_call = db.q1("SELECT MAX(ts) t FROM llm_calls WHERE run_id=?", [run["id"]])["t"] or 0
    return max(last_log, last_call)


def _run_alive(run: dict) -> bool:
    owner = (run.get("config") or {}).get("owner") or {}
    if owner.get("host") == socket.gethostname() and owner.get("pid"):
        if owner["pid"] == os.getpid():  # заодно PID, доставшийся после перезагрузки
            return run["id"] in _live
        return _pid_alive(owner["pid"])
    return time.time() - _last_activity(run) < ALIVE_WINDOW  # запись без владельца или с другой машины


def reap_dead_runs(project_id: str | None = None) -> None:
    sql, args = "SELECT id, project_id, started_at, log, config FROM runs WHERE status='running'", []
    if project_id:
        sql += " AND project_id=?"
        args.append(project_id)
    now = time.time()
    for run in db.q(sql, args):
        if _run_alive(run):
            continue
        db.ex("UPDATE runs SET status='stopped', finished_at=?, error=COALESCE(error || '\n', '') || ? "
              "WHERE id=? AND status='running'", [now, "Обработка прервана", run["id"]])
        db.ex("UPDATE documents SET status='partial', status_detail=? WHERE project_id=? AND status='processing'",
              ["Обработка прервана", run["project_id"]])
        db.ex("UPDATE chunks SET status='error', error='Обработка прервана' WHERE status='running' "
              "AND doc_id IN (SELECT id FROM documents WHERE project_id=?)", [run["project_id"]])


def is_running(project_id: str) -> bool:
    reap_dead_runs(project_id)
    return bool(db.q1("SELECT 1 x FROM runs WHERE project_id=? AND status='running'", [project_id]))


def _open_run(project_id: str) -> str:
    if is_running(project_id):
        raise RuntimeError("Обработка уже идёт")
    run_id = "run_" + time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
    _live.add(run_id)  # до INSERT: опрос из другого потока не должен принять свой прогон за мёртвый
    db.upsert("runs", dict(id=run_id, project_id=project_id, started_at=time.time(), finished_at=None,
                           status="running", stage="start", progress={},
                           config=SETTINGS.to_json() | {"owner": {"host": socket.gethostname(), "pid": os.getpid()}},
                           log=[], error=None))
    return run_id


async def run_project(project_id: str, doc_ids: list[str] | None = None, only_failed: bool = False,
                      stages: list[str] | None = None, use_judge: bool = True, run_id: str | None = None) -> str:
    stages = _check_stages(stages)
    run_id = run_id or _open_run(project_id)
    reset_fault_counter()
    ctx = CallCtx(project_id=project_id, run_id=run_id)
    project = db.q1("SELECT * FROM projects WHERE id=?", [project_id])
    errors = []
    try:
        if "keywords" in stages:
            _progress(run_id, "keywords", msg="Ключевые слова темы")
            try:
                await ensure_keywords(project, ctx)
            except LLMError as e:
                errors.append(f"Ключевые слова: {e}")
                _progress(run_id, "keywords", msg=f"Ошибка: {e}")
        if "extract" in stages:
            await _extract(project, run_id, ctx, doc_ids, only_failed, errors)
        if "verify" in stages:
            _progress(run_id, "verify", msg="Проверка фактов")
            await verify_cards(project_id, ctx, use_judge=use_judge)
        if "group" in stages:
            _progress(run_id, "group", msg="Группировка фактов")
            await group_project(project_id, ctx)
            _progress(run_id, "group", msg="Групп: {}, противоречий: {}".format(
                _count("SELECT COUNT(*) n FROM clusters WHERE project_id=?", [project_id]),
                _count("SELECT COUNT(*) n FROM conflicts WHERE project_id=?", [project_id])))
        if "resolve" in stages:
            _progress(run_id, "resolve", msg="Нормирование противоречий")
            resolve_project(project_id)
            n_open = _count("SELECT COUNT(*) n FROM conflicts WHERE project_id=? AND decision IS NULL", [project_id])
            if n_open:
                _progress(run_id, "resolve", msg=f"Ждут решения эксперта: {n_open}")
        if "kr" in stages:
            _progress(run_id, "kr", msg="Сборка КР")
            await build_kr(db.q1("SELECT * FROM projects WHERE id=?", [project_id]), ctx)
            _progress(run_id, "kr", msg="Тезисов и абзацев: {}".format(
                _count("SELECT COUNT(*) n FROM theses WHERE project_id=? AND orphan=0", [project_id])))

        # ошибкой считается только запрос, который так и не удался
        failed_q = ("FROM llm_calls e WHERE run_id=? AND error IS NOT NULL AND NOT EXISTS (SELECT 1 FROM llm_calls s "
                    "WHERE s.run_id=e.run_id AND s.cache_key=e.cache_key AND s.error IS NULL)")
        n_err = db.q1("SELECT COUNT(DISTINCT cache_key) n " + failed_q, [run_id])["n"]
        if n_err:
            first = db.q1("SELECT purpose, error " + failed_q + " ORDER BY ts LIMIT 1", [run_id])
            errors.insert(0, f"{n_err} запросов к модели завершились ошибкой (первая — этап {first['purpose']}: "
                             f"{first['error']}). Повторите обработку: готовые ответы возьмутся из кэша.")
        status = "completed_with_errors" if errors else "completed"
        _progress(run_id, "snapshot", msg="Сохранение снимка")
        try:
            from .snapshots import take  # snapshots → export → pipeline
            take(project_id, run_id)
        except Exception as e:  # noqa: BLE001 — снимок не должен ронять прогон
            errors.append(f"Снимок не сохранён: {e}")
        db.update("runs", run_id, status=status, finished_at=time.time(),
                  error="\n".join(errors) if errors else None)
        _progress(run_id, "done", msg="Готово" if status == "completed" else "Готово, есть ошибки")
    except Exception as e:  # noqa: BLE001
        db.update("runs", run_id, status="failed", finished_at=time.time(),
                  error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}")
        _progress(run_id, "failed", msg=f"Сбой: {e}")
    finally:
        _live.discard(run_id)
    return run_id


def start_background(project_id: str, **kw) -> str:
    kw["stages"] = _check_stages(kw.get("stages"))
    run_id = _open_run(project_id)
    _running[project_id] = asyncio.create_task(run_project(project_id, run_id=run_id, **kw))
    return run_id


# ------------------------------------------------------------------ отчёт ---

def processing_report(project_id: str) -> dict:
    docs = db.q("SELECT id, filename, n_pages, status, status_detail, duplicate_of FROM documents "
                "WHERE project_id=? ORDER BY filename", [project_id])
    out_docs = []
    for d in docs:
        pages = db.q("SELECT page_no, kind, status, reason FROM pages WHERE doc_id=? ORDER BY page_no", [d["id"]])
        processed = [p["page_no"] for p in pages if p["status"] == "done"]
        skipped = [dict(page=p["page_no"], kind=p["kind"], reason=p["reason"]) for p in pages
                   if p["status"] != "done"]
        out_docs.append(dict(
            doc_id=d["id"], filename=d["filename"], status=d["status"], detail=d["status_detail"],
            duplicate_of=d["duplicate_of"], n_pages=d["n_pages"], pages_processed=len(processed),
            coverage=round(len(processed) / d["n_pages"], 3) if d["n_pages"] else 0.0,
            skipped_pages=_compress(skipped)))
    calls = db.q("SELECT purpose, model, COUNT(*) calls, SUM(cache_hit) cache_hits, "
                 "SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) errors, "
                 "SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens, "
                 "SUM(cache_read_tokens) cache_read_tokens, SUM(cost_usd) cost_usd, "
                 "SUM(duration_ms) duration_ms FROM llm_calls WHERE project_id=? GROUP BY purpose, model",
                 [project_id])
    tot = {k: sum((c[k] or 0) for c in calls) for k in
           ("calls", "cache_hits", "errors", "input_tokens", "output_tokens", "cost_usd", "duration_ms")}
    runs = db.q("SELECT id, started_at, finished_at, status, error, config FROM runs WHERE project_id=? "
                "ORDER BY started_at DESC", [project_id])
    for r in runs:
        r["wall_s"] = round((r["finished_at"] or time.time()) - r["started_at"], 1)
        r["llm"] = db.q1("SELECT COUNT(*) calls, SUM(cache_hit) hits, SUM(input_tokens) inp, "
                         "SUM(output_tokens) outp, SUM(cost_usd) cost, "
                         "SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) errors "
                         "FROM llm_calls WHERE run_id=?", [r["id"]])
        r["model"] = (r.pop("config") or {}).get("model")
    cards = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM cards WHERE project_id=? "
                                                "GROUP BY status", [project_id])}
    errors = db.q("SELECT ts, purpose, error, run_id FROM llm_calls WHERE project_id=? AND error IS NOT NULL "
                  "ORDER BY ts DESC LIMIT 50", [project_id])
    return dict(documents=out_docs, llm_by_purpose=calls, llm_total=tot, runs=runs, card_status=cards,
                recent_errors=errors)


def _compress(skipped: list[dict]) -> list[dict]:
    """Подряд идущие пропущенные страницы с одной причиной — диапазоном."""
    out = []
    for s in skipped:
        if out and out[-1]["reason"] == s["reason"] and out[-1]["to"] == s["page"] - 1:
            out[-1]["to"] = s["page"]
        else:
            out.append(dict(**{"from": s["page"]}, to=s["page"], kind=s["kind"], reason=s["reason"] or s["kind"]))
    return out
