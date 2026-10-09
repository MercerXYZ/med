"""Оркестрация конвейера и отчёт об обработке.

PDF → страницы → фрагменты → карточки → проверка → повторы/противоречия →
тезисы → черновик. Каждый этап идемпотентен и сохраняет результат сразу,
поэтому сбой API посреди обработки не теряет уже полученное, а повторный запуск
дообрабатывает только недостающее (кэш + статусы фрагментов).
"""
from __future__ import annotations

import asyncio
import re
import time
import traceback
import uuid

from . import db
from .cluster import cluster_project
from .config import SETTINGS
from .extract import ensure_keywords, extract_biblio, extract_document
from .llm import CallCtx, LLMError, reset_fault_counter
from .pdf_ingest import make_chunks, plan_pages, score_pages
from .merge import merge_project
from .synth import synth_project
from .verify import verify_cards

STAGES = ["keywords", "extract", "verify", "cluster", "synth", "merge"]
_running: dict[str, asyncio.Task] = {}


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
    """Дубликат по DOI или по названию (≥ 30 символов) среди загруженных раньше документов.
    Основным считается файл, загруженный первым, — так пара не может пометить друг друга."""
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
    total, done, err = sum(ch.values()), ch.get("done", 0), ch.get("error", 0)
    parts = [f"фрагментов {done}/{total}", f"карточек {n_cards}"]
    if pg.get("skipped"):
        parts.append(f"пропущено вне бюджета {pg['skipped']} стр.")
    if pg.get("no_text"):
        parts.append(f"без текстового слоя {pg['no_text']} стр.")
    if pg.get("references"):
        parts.append(f"список литературы {pg['references']} стр.")
    if err:
        e = db.q1("SELECT error FROM chunks WHERE doc_id=? AND status='error' LIMIT 1", [doc_id])
        parts.append(f"ОШИБКИ в {err} фрагм.: {e['error'][:160]}")
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


async def run_project(project_id: str, doc_ids: list[str] | None = None, only_failed: bool = False,
                      stages: list[str] | None = None, use_judge: bool = True) -> str:
    stages = stages or STAGES
    run_id = "run_" + time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
    db.upsert("runs", dict(id=run_id, project_id=project_id, started_at=time.time(), finished_at=None,
                           status="running", stage="start", progress={}, config=SETTINGS.to_json(),
                           log=[], error=None))
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
            docs = db.q("SELECT * FROM documents WHERE project_id=? AND status NOT IN "
                        "('duplicate','unsupported') AND NOT (status='error' AND n_pages=0)", [project_id])
            if doc_ids:
                docs = [d for d in docs if d["id"] in doc_ids]
            done = 0

            async def one(doc):
                nonlocal done
                db.update("documents", doc["id"], status="processing")
                try:
                    await extract_biblio(doc, ctx)
                except LLMError as e:
                    errors.append(f"{doc['filename']}: библиография — {e}")
                dup = find_duplicate_by_biblio(project_id, doc)
                if dup:  # та же статья в другой вёрстке: не тратим запросы на повторное извлечение
                    db.update("documents", doc["id"], status="duplicate", duplicate_of=dup["id"],
                              status_detail=f"Та же публикация, что {dup['filename']} (совпадают DOI/название) — "
                                            f"не обрабатывается повторно", updated_at=time.time())
                    done += 1
                    _progress(run_id, "extract", done=done, total=len(docs), msg=f"{doc['filename']}: дубликат")
                    return
                score_pages(doc["id"], project.get("keywords") or [])
                chunks = make_chunks(doc["id"], plan_pages(doc["id"]))
                await extract_document(project, doc, chunks, ctx, only_failed=only_failed)
                st, detail = doc_status(doc["id"])
                db.update("documents", doc["id"], status=st, status_detail=detail, updated_at=time.time())
                done += 1
                _progress(run_id, "extract", done=done, total=len(docs),
                          msg=f"{doc['filename']}: {st} — {detail}")

            _progress(run_id, "extract", done=0, total=len(docs), msg=f"Извлечение: {len(docs)} док.")
            async def safe(doc):
                try:
                    await one(doc)
                except Exception as e:  # noqa: BLE001 — сбой одного документа не роняет прогон
                    errors.append(f"{doc['filename']}: {type(e).__name__}: {e}")
                    db.update("documents", doc["id"], status="error", updated_at=time.time(),
                              status_detail=f"Обработка прервана ошибкой: {type(e).__name__}: {e}"[:500])
                    db.ex("UPDATE chunks SET status='error', error=? WHERE doc_id=? AND status='running'",
                          [f"{type(e).__name__}: {e}"[:300], doc["id"]])

            await asyncio.gather(*(safe(d) for d in docs))

        if "verify" in stages:
            _progress(run_id, "verify", msg="Проверка цитат, чисел, отрицаний и смысла")
            await verify_cards(project_id, ctx, use_judge=use_judge)
        if "cluster" in stages:
            _progress(run_id, "cluster", msg="Повторы и противоречия")
            await cluster_project(project_id, ctx)
        if "synth" in stages:
            _progress(run_id, "synth", msg="Синтез тезисов")
            await synth_project(project, ctx)
        if "merge" in stages:
            _progress(run_id, "merge", msg="Объединение похожих тезисов")
            await merge_project(project, ctx)

        # ошибкой считается только запрос, который так и не удался (успешный повтор — не ошибка)
        n_err = db.q1("SELECT COUNT(DISTINCT cache_key) n FROM llm_calls e WHERE run_id=? AND error IS NOT NULL "
                      "AND NOT EXISTS (SELECT 1 FROM llm_calls s WHERE s.run_id=e.run_id "
                      "AND s.cache_key=e.cache_key AND s.error IS NULL)", [run_id])["n"]
        if n_err:
            first = db.q1("SELECT purpose, error FROM llm_calls e WHERE run_id=? AND error IS NOT NULL "
                          "AND NOT EXISTS (SELECT 1 FROM llm_calls s WHERE s.run_id=e.run_id "
                          "AND s.cache_key=e.cache_key AND s.error IS NULL) ORDER BY ts LIMIT 1", [run_id])
            errors.insert(0, f"{n_err} запросов к модели завершились ошибкой (первая — этап {first['purpose']}: "
                             f"{first['error']}). Полученные результаты сохранены; повторите обработку "
                             f"с отметкой «только сбойные фрагменты».")
        status = "completed_with_errors" if (errors or n_err) else "completed"
        _progress(run_id, "snapshot", msg="Сохранение снимка результата")
        try:
            from .snapshots import take
            take(project_id, run_id)
        except Exception as e:  # noqa: BLE001 — снимок не должен ронять прогон
            errors.append(f"Снимок результата не сохранён: {e}")
        db.update("runs", run_id, status=status, finished_at=time.time(),
                  error="\n".join(errors) if errors else None)
        _progress(run_id, "done", msg=f"Готово: {status}")
    except Exception as e:  # noqa: BLE001
        db.update("runs", run_id, status="failed", finished_at=time.time(),
                  error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}")
        _progress(run_id, "failed", msg=f"Сбой: {e}")
    return run_id


def start_background(project_id: str, **kw) -> None:
    t = _running.get(project_id)
    if t and not t.done():
        raise RuntimeError("Обработка уже идёт")
    _running[project_id] = asyncio.create_task(run_project(project_id, **kw))


def is_running(project_id: str) -> bool:
    t = _running.get(project_id)
    return bool(t and not t.done())


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
        c = db.q1("SELECT COUNT(*) calls, SUM(cache_hit) hits, SUM(input_tokens) inp, SUM(output_tokens) outp, "
                  "SUM(cost_usd) cost, SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) errors "
                  "FROM llm_calls WHERE run_id=?", [r["id"]])
        r.update(llm=c)
        r["model"] = (r.pop("config") or {}).get("model")
    cards = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM cards WHERE project_id=? "
                                                "GROUP BY status", [project_id])}
    dup = db.q1("SELECT COUNT(*) n FROM cards WHERE project_id=? AND merged_into IS NOT NULL", [project_id])["n"]
    errors = db.q("SELECT ts, purpose, error, run_id FROM llm_calls WHERE project_id=? AND error IS NOT NULL "
                  "ORDER BY ts DESC LIMIT 50", [project_id])
    return dict(documents=out_docs, llm_by_purpose=calls, llm_total=tot, runs=runs, card_status=cards,
                duplicates_merged=dup, recent_errors=errors,
                cost_note="cost_usd — эквивалент по прейскуранту API, который сообщает Claude Agent SDK; "
                          "фактически расходуются лимиты подписки Claude. Кэш-попадания стоят 0.")


def _compress(skipped: list[dict]) -> list[dict]:
    """Сжимает подряд идущие пропущенные страницы с одной причиной в диапазоны."""
    out = []
    for s in skipped:
        if out and out[-1]["reason"] == s["reason"] and out[-1]["to"] == s["page"] - 1:
            out[-1]["to"] = s["page"]
        else:
            out.append(dict(**{"from": s["page"]}, to=s["page"], kind=s["kind"], reason=s["reason"] or s["kind"]))
    return out
