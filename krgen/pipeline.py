"""Конвейер обработки проекта и отчёт об обработке."""
from __future__ import annotations

import asyncio
import re
import time
import traceback
import uuid

from . import db
from .classify import classify_project
from .config import SETTINGS
from .extract import ensure_keywords, extract_biblio, extract_document
from .groups import group_project
from .llm import CallCtx, LLMError
from .pdf_ingest import make_chunks, plan_pages, score_pages
from .resolve import resolve_project
from .synth import build_kr
from .verify import verify_cards

STAGES = ["keywords", "extract", "verify", "classify", "group", "resolve", "kr"]
_tasks: set[asyncio.Task] = set()  # ссылки на фоновые прогоны, чтобы их не собрал сборщик мусора


def _progress(run_id: str, stage: str, msg: str | None = None, **kw) -> None:
    log = db.q1("SELECT log FROM runs WHERE id=?", [run_id])["log"] or []
    if msg:
        log.append({"ts": time.time(), "stage": stage, "msg": msg})
    db.update("runs", run_id, stage=stage, progress=dict(stage=stage, **kw), log=log[-300:])


def _count(sql: str, project_id: str) -> int:
    return db.q1(sql, [project_id])["n"]


def _norm_title(t: str | None) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", (t or "").lower())


def find_duplicate(project_id: str, doc: dict) -> dict | None:
    """Загруженный раньше документ с тем же DOI или названием."""
    b = doc["biblio"] or {}
    doi, title = (b.get("doi") or "").lower(), _norm_title(b.get("title"))
    for o in db.q("SELECT id, filename, biblio FROM documents WHERE project_id=? AND id!=? AND status!='duplicate' "
                  "AND created_at<=? ORDER BY created_at", [project_id, doc["id"], doc["created_at"]]):
        ob = o["biblio"] or {}
        if (doi and doi == (ob.get("doi") or "").lower()) or (len(title) >= 30 and title == _norm_title(ob.get("title"))):
            return o
    return None


def doc_status(doc_id: str) -> tuple[str, str]:
    ch = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM chunks WHERE doc_id=? GROUP BY status",
                                             [doc_id])}
    skipped = db.q1("SELECT COUNT(*) n FROM pages WHERE doc_id=? AND kind IN ('skipped', 'no_text')", [doc_id])["n"]
    n_cards = db.q1("SELECT COUNT(*) n FROM cards WHERE doc_id=?", [doc_id])["n"]
    total, err = sum(ch.values()), ch.get("error", 0)
    detail = f"фрагментов {ch.get('done', 0)}/{total}, фактов {n_cards}"
    if skipped:
        detail += f", пропущено {skipped} стр."
    if err:
        e = db.q1("SELECT error FROM chunks WHERE doc_id=? AND status='error'", [doc_id])["error"]
        detail += f", ошибки в {err} фрагм.: {e[:160]}"
    if total == 0:
        return "queued", "Ещё не обрабатывался"
    if err == total:
        return "error", detail
    if err or skipped:
        return "partial", detail
    return "done", detail


async def _extract(project: dict, run_id: str, ctx: CallCtx, doc_ids, only_failed: bool, errors: list) -> None:
    # файлы, которые не открылись, имеют 0 страниц
    docs = db.q("SELECT * FROM documents WHERE project_id=? AND status NOT IN ('duplicate', 'unsupported') "
                "AND n_pages>0", [project["id"]])
    if doc_ids:
        docs = [d for d in docs if d["id"] in doc_ids]
    done = 0
    _progress(run_id, "extract", f"Извлечение фактов: {len(docs)} док.", done=0, total=len(docs))

    async def one(doc):
        nonlocal done
        db.update("documents", doc["id"], status="processing")
        try:
            try:
                doc["biblio"] = await extract_biblio(doc, ctx)
            except LLMError as e:
                errors.append(f"{doc['filename']}: описание источника — {e}")
            dup = find_duplicate(project["id"], doc)
            if dup:  # та же статья в другом файле: повторно не извлекаем
                db.update("documents", doc["id"], status="duplicate", duplicate_of=dup["id"],
                          status_detail=f"Та же публикация, что {dup['filename']}")
                msg = "дубликат"
            else:
                score_pages(doc["id"], project.get("keywords") or [])
                chunks = make_chunks(doc["id"], plan_pages(doc["id"]))
                await extract_document(project, doc, chunks, ctx, only_failed=only_failed)
                st, msg = doc_status(doc["id"])
                db.update("documents", doc["id"], status=st, status_detail=msg, updated_at=time.time())
        except Exception as e:  # noqa: BLE001 — сбой одного документа не роняет прогон
            msg = f"Ошибка обработки: {type(e).__name__}: {e}"[:500]
            errors.append(f"{doc['filename']}: {msg}")
            db.update("documents", doc["id"], status="error", status_detail=msg, updated_at=time.time())
        done += 1
        _progress(run_id, "extract", f"{doc['filename']}: {msg}", done=done, total=len(docs))

    await asyncio.gather(*(one(d) for d in docs))


def check_stages(stages: list[str] | None) -> list[str]:
    stages = stages or STAGES
    unknown = [s for s in stages if s not in STAGES]
    if unknown:
        raise ValueError(f"Неизвестные этапы: {', '.join(unknown)}. Есть: {', '.join(STAGES)}")
    return stages


def stop_stale_runs() -> None:
    """При запуске сервера: прогоны прошлого процесса уже не идут."""
    db.ex("UPDATE runs SET status='stopped', finished_at=?, error='Обработка прервана' WHERE status='running'",
          [time.time()])
    db.ex("UPDATE documents SET status='partial', status_detail='Обработка прервана' WHERE status='processing'")


def is_running(project_id: str) -> bool:
    return bool(db.q1("SELECT 1 x FROM runs WHERE project_id=? AND status='running'", [project_id]))


def _open_run(project_id: str) -> str:
    if is_running(project_id):
        raise RuntimeError("Обработка уже идёт")
    run_id = "run_" + time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
    db.upsert("runs", dict(id=run_id, project_id=project_id, started_at=time.time(), finished_at=None,
                           status="running", stage="start", progress={}, config=SETTINGS.to_json(), log=[],
                           error=None))
    return run_id


async def run_project(project_id: str, doc_ids: list[str] | None = None, only_failed: bool = False,
                      stages: list[str] | None = None, use_judge: bool = True, run_id: str | None = None) -> str:
    stages = check_stages(stages)
    project = db.q1("SELECT * FROM projects WHERE id=?", [project_id])
    if not project:
        raise ValueError(f"Проект не найден: {project_id}")
    run_id = run_id or _open_run(project_id)
    ctx = CallCtx(project_id=project_id, run_id=run_id)
    errors = []
    try:
        if "keywords" in stages:
            _progress(run_id, "keywords", "Ключевые слова темы")
            try:
                await ensure_keywords(project, ctx)
            except LLMError as e:
                errors.append(f"Ключевые слова: {e}")
        if "extract" in stages:
            await _extract(project, run_id, ctx, doc_ids, only_failed, errors)
        if "verify" in stages:
            _progress(run_id, "verify", "Проверка фактов")
            await verify_cards(project_id, ctx, use_judge=use_judge)
        if "classify" in stages:
            _progress(run_id, "classify", "Определение разделов КР")
            await classify_project(project, ctx)
        if "group" in stages:
            _progress(run_id, "group", "Группировка фактов")
            await group_project(project_id, ctx)
            _progress(run_id, "group", "Групп: {}, противоречий: {}".format(
                _count("SELECT COUNT(*) n FROM fact_groups WHERE project_id=?", project_id),
                _count("SELECT COUNT(*) n FROM conflicts WHERE project_id=?", project_id)))
        if "resolve" in stages:
            _progress(run_id, "resolve", "Нормирование противоречий")
            resolve_project(project_id)
            n_open = _count("SELECT COUNT(*) n FROM conflicts WHERE project_id=? AND decision IS NULL", project_id)
            if n_open:
                _progress(run_id, "resolve", f"Ждут решения эксперта: {n_open}")
        if "kr" in stages:
            _progress(run_id, "kr", "Сборка КР")
            await build_kr(project, ctx)
            _progress(run_id, "kr", "Тезисов и абзацев: {}".format(
                _count("SELECT COUNT(*) n FROM theses WHERE project_id=? AND orphan=0", project_id)))

        # ошибка — запрос, который так и не удался после повторов
        failed = db.q("SELECT purpose, error FROM llm_calls e WHERE run_id=? AND error IS NOT NULL AND NOT EXISTS "
                      "(SELECT 1 FROM llm_calls s WHERE s.cache_key=e.cache_key AND s.error IS NULL) "
                      "GROUP BY cache_key", [run_id])
        if failed:
            errors.insert(0, f"{len(failed)} запросов к модели завершились ошибкой (первая — этап "
                             f"{failed[0]['purpose']}: {failed[0]['error']}). Повторите обработку: готовые ответы "
                             "возьмутся из кэша.")
        _progress(run_id, "snapshot", "Сохранение снимка")
        from .snapshots import take
        try:
            take(project_id, run_id)
        except Exception as e:  # noqa: BLE001 — без снимка результат всё равно в базе
            errors.append(f"Снимок не сохранён: {type(e).__name__}: {e}")
        status = "completed_with_errors" if errors else "completed"
        db.update("runs", run_id, status=status, finished_at=time.time(), error="\n".join(errors) or None)
        _progress(run_id, "done", "Готово" if status == "completed" else "Готово, есть ошибки")
    except Exception as e:  # noqa: BLE001
        db.update("runs", run_id, status="failed", finished_at=time.time(),
                  error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}")
        _progress(run_id, "failed", f"Сбой: {e}")
    return run_id


def start_background(project_id: str, **kw) -> str:
    kw["stages"] = check_stages(kw.get("stages"))
    run_id = _open_run(project_id)
    task = asyncio.create_task(run_project(project_id, run_id=run_id, **kw))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return run_id


# ------------------------------------------------------------------ отчёт ---

def processing_report(project_id: str) -> dict:
    """Покрытие документов по страницам и расход запросов к модели."""
    docs = []
    for d in db.q("SELECT id, filename, n_pages, status, status_detail FROM documents WHERE project_id=? "
                  "ORDER BY filename", [project_id]):
        pages = db.q("SELECT page_no, kind, status, reason FROM pages WHERE doc_id=? ORDER BY page_no", [d["id"]])
        done = sum(p["status"] == "done" for p in pages)
        skipped = []  # подряд идущие пропущенные страницы с одной причиной — диапазоном
        for p in pages:
            if p["status"] == "done":
                continue
            if skipped and skipped[-1]["reason"] == p["reason"] and skipped[-1]["to"] == p["page_no"] - 1:
                skipped[-1]["to"] = p["page_no"]
            else:
                skipped.append({"from": p["page_no"], "to": p["page_no"], "reason": p["reason"] or p["kind"]})
        docs.append(dict(doc_id=d["id"], filename=d["filename"], status=d["status"], detail=d["status_detail"],
                         n_pages=d["n_pages"], pages_processed=done,
                         coverage=round(done / d["n_pages"], 3) if d["n_pages"] else 0.0, skipped_pages=skipped))
    calls = db.q("SELECT purpose, COUNT(*) calls, SUM(cache_hit) cache_hits, SUM(error IS NOT NULL) errors, "
                 "SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens, SUM(cost_usd) cost_usd, "
                 "SUM(duration_ms) duration_ms FROM llm_calls WHERE project_id=? GROUP BY purpose", [project_id])
    return dict(documents=docs, llm_by_purpose=calls)
