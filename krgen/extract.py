"""Ключевые слова темы, описание источника и извлечение доказательств из фрагментов документа."""
from __future__ import annotations

import asyncio
import hashlib
import re
import time

from . import db, prompts as P
from .llm import CallCtx, LLMError, call_json
from .textnorm import norm_hard, norm_soft

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>,;]+[^\s\"<>,;.)])", re.I)
PMID_RE = re.compile(r"PMID:?\s*(\d{6,9})", re.I)


async def ensure_keywords(project: dict, ctx: CallCtx) -> list[str]:
    if project.get("keywords"):
        return project["keywords"]
    res = await call_json("keywords", P.KEYWORDS_SYSTEM,
                          P.keywords_prompt(project["title"], project.get("icd_codes") or []), P.KEYWORDS_SCHEMA, ctx)
    kws = sorted({k.strip().lower() for k in res["keywords"] if len(k.strip()) > 2})
    db.update("projects", project["id"], keywords=kws)
    project["keywords"] = kws
    return kws


async def extract_biblio(doc: dict, ctx: CallCtx) -> dict:
    """Источник (source онтологии) по первым двум страницам."""
    biblio = doc.get("biblio") or {}
    if biblio.get("title"):
        return biblio
    pages = db.q("SELECT text FROM pages WHERE doc_id=? ORDER BY page_no LIMIT 2", [doc["id"]])
    text = "\n".join(p["text"] for p in pages)[:7000]
    res = await call_json("biblio", P.BIBLIO_SYSTEM, P.biblio_prompt(text), P.BIBLIO_SCHEMA, ctx)
    # DOI и PMID — только те, что есть в тексте
    if res["doi"] and norm_hard(res["doi"]) not in norm_hard(text):
        res["doi"] = ""
    m = DOI_RE.search(text)
    res["doi"] = res["doi"] or (m.group(1) if m else "")
    m = PMID_RE.search(text)
    res["pmid"] = m.group(1) if m else ""
    res["year"] = res["year"] or None
    res["sample_size"] = res["sample_size"] or None
    res["_fingerprint"] = biblio.get("_fingerprint")
    db.update("documents", doc["id"], biblio=res, updated_at=time.time())
    doc["biblio"] = res
    return res


def card_id(doc_id: str, quote: str, statement: str) -> str:
    h = hashlib.sha1(f"{doc_id}|{norm_hard(quote)[:400]}|{norm_soft(statement)}".encode()).hexdigest()
    return "ev_" + h[:12]


def _store_cards(project_id: str, doc_id: str, chunk_id: str, claims: list[dict], run_id: str) -> int:
    """Сохраняет ответ модели, не затирая правки эксперта."""
    now = time.time()
    seen = set()
    for c in claims:
        if not c["quote"].strip() or not c["statement_ru"].strip():
            continue
        cid = card_id(doc_id, c["quote"], c["statement_ru"])
        if cid in seen:
            continue
        seen.add(cid)
        old = db.q1("SELECT auto, override, user_status FROM cards WHERE id=?", [cid])
        if old is None:
            db.upsert("cards", dict(id=cid, project_id=project_id, doc_id=doc_id, chunk_id=chunk_id, auto=c,
                                    status="extracted", run_id=run_id, created_at=now, updated_at=now))
        elif old["override"] and old["auto"] != c:
            db.update("cards", cid, pending_auto=c, run_id=run_id, updated_at=now)
        else:
            upd = dict(auto=c, pending_auto=None, run_id=run_id, updated_at=now)
            if old["user_status"] == "confirmed" and old["auto"] != c:
                upd["user_status"] = None  # эксперт подтверждал прежнее содержимое
            db.update("cards", cid, **upd)
    # пропавшие доказательства фрагмента удаляются, если эксперт их не трогал
    db.ex("DELETE FROM cards WHERE chunk_id=? AND override IS NULL AND user_status IS NULL AND id NOT IN (%s)"
          % ",".join("?" * len(seen)), [chunk_id, *seen])
    return len(seen)


async def extract_chunk(project: dict, doc: dict, chunk: dict, ctx: CallCtx) -> None:
    db.upsert("chunks", dict(id=chunk["id"], doc_id=doc["id"], page_from=chunk["page_from"], page_to=chunk["page_to"],
                             status="running", error=None, n_claims=0, updated_at=time.time()))
    pages = (doc["id"], chunk["page_from"], chunk["page_to"])
    try:
        res = await call_json("extract", P.EXTRACT_SYSTEM,
                              P.extract_prompt(project["title"], doc.get("biblio") or {}, chunk["text"]),
                              P.EXTRACT_SCHEMA, ctx)
    except LLMError as e:
        db.update("chunks", chunk["id"], status="error", error=str(e), updated_at=time.time())
        db.ex("UPDATE pages SET status='error', reason=? WHERE doc_id=? AND page_no BETWEEN ? AND ?",
              [f"Ошибка обработки: {e}"[:300], *pages])
        return
    claims = res["claims"]
    for c in claims:  # номер страницы вне фрагмента поправит проверка цитаты
        if not chunk["page_from"] <= c["page"] <= chunk["page_to"]:
            c["page"] = chunk["page_from"]
    n = _store_cards(project["id"], doc["id"], chunk["id"], claims, ctx.run_id)
    db.update("chunks", chunk["id"], status="done", n_claims=n, updated_at=time.time())
    db.ex("UPDATE pages SET status='done', reason='' WHERE doc_id=? AND page_no BETWEEN ? AND ?", pages)


async def extract_document(project: dict, doc: dict, chunks: list[dict], ctx: CallCtx,
                           only_failed: bool = False) -> None:
    # фрагменты прежнего разбиения и их нетронутые экспертом факты удаляются
    current = {ch["id"] for ch in chunks}
    done = set()
    for old in db.q("SELECT id, status FROM chunks WHERE doc_id=?", [doc["id"]]):
        if old["id"] not in current:
            db.ex("DELETE FROM cards WHERE chunk_id=? AND override IS NULL AND user_status IS NULL", [old["id"]])
            db.ex("DELETE FROM chunks WHERE id=?", [old["id"]])
        elif old["status"] == "done":
            done.add(old["id"])
    todo = [ch for ch in chunks if not (only_failed and ch["id"] in done)]
    await asyncio.gather(*(extract_chunk(project, doc, ch, ctx) for ch in todo))
