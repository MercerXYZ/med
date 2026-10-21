"""Ключевые слова темы, библиография документа и извлечение фактов из фрагментов."""
from __future__ import annotations

import asyncio
import hashlib
import re
import time

from . import db, prompts as P
from .llm import CallCtx, LLMError, call_json
from .textnorm import norm_hard, norm_soft
from .verify import JUDGE_KEYS

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>,;]+[^\s\"<>,;.)])", re.I)
PMID_RE = re.compile(r"PMID:?\s*(\d{6,9})", re.I)


async def ensure_keywords(project: dict, ctx: CallCtx) -> list[str]:
    if project.get("keywords"):
        return project["keywords"]
    res = await call_json("keywords", P.KEYWORDS_SYSTEM,
                          P.keywords_prompt(project["title"], project.get("icd_codes") or []),
                          P.KEYWORDS_SCHEMA, ctx)
    kws = sorted({k.strip().lower() for k in res.get("keywords", []) if len(k.strip()) > 2})
    db.update("projects", project["id"], keywords=kws)
    project["keywords"] = kws
    return kws


async def extract_biblio(doc: dict, ctx: CallCtx) -> dict:
    biblio = doc.get("biblio") or {}
    if biblio.get("title"):
        return biblio
    pages = db.q("SELECT text FROM pages WHERE doc_id=? ORDER BY page_no LIMIT 2", [doc["id"]])
    text = "\n".join(p["text"] for p in pages)[:7000]
    res = await call_json("biblio", P.BIBLIO_SYSTEM, P.biblio_prompt(text), P.BIBLIO_SCHEMA, ctx)
    # DOI/PMID — только найденные в тексте, не «вспомненные» моделью
    m = DOI_RE.search(text)
    regex_doi = m.group(1) if m else ""
    if res.get("doi") and norm_hard(res["doi"]) not in norm_hard(text):
        res["doi_note"] = f"DOI от модели «{res['doi']}» не найден в тексте — отброшен"
        res["doi"] = ""
    res["doi"] = res.get("doi") or regex_doi
    pm = PMID_RE.search(text)
    res["pmid"] = pm.group(1) if pm else ""
    if res.get("year") == 0:
        res["year"] = None
    if not res.get("sample_size"):
        res["sample_size"] = None
    res["_fingerprint"] = biblio.get("_fingerprint")
    db.update("documents", doc["id"], biblio=res, updated_at=time.time())
    doc["biblio"] = res
    return res


def card_id(doc_id: str, quote: str, statement: str) -> str:
    h = hashlib.sha1(f"{doc_id}|{norm_hard(quote)[:400]}|{norm_soft(statement)}".encode()).hexdigest()
    return "ev_" + h[:12]


def _store_cards(project_id: str, doc_id: str, chunk_id: str, claims: list[dict], run_id: str) -> int:
    """Сохраняет результат модели, не затирая правки эксперта."""
    now = time.time()
    seen = set()
    for c in claims:
        if not c.get("quote", "").strip() or not c.get("statement_ru", "").strip():
            continue
        cid = card_id(doc_id, c["quote"], c["statement_ru"])
        if cid in seen:
            continue
        seen.add(cid)
        old = db.q1("SELECT * FROM cards WHERE id=?", [cid])
        if old is None:
            db.upsert("cards", dict(id=cid, project_id=project_id, doc_id=doc_id, chunk_id=chunk_id,
                                    auto=c, override=None, pending_auto=None, verification=None,
                                    status="extracted", user_status=None,
                                    origin="auto", run_id=run_id, created_at=now, updated_at=now))
        elif old.get("override") and old.get("auto") != c:
            db.update("cards", cid, pending_auto=c, run_id=run_id, updated_at=now)
        else:
            upd = dict(auto=c, run_id=run_id, updated_at=now, pending_auto=None)
            # изменилось то, что подтверждал эксперт, — подтверждение снимается
            if old.get("user_status") == "confirmed" and any(
                    (old.get("auto") or {}).get(k) != c.get(k) for k in JUDGE_KEYS + ("page",)):
                upd["user_status"] = None
                db.log_edit(project_id, "card", cid, "status:reset_auto_changed", "confirmed", None,
                            "содержимое карточки изменилось при повторном извлечении")
            db.update("cards", cid, **upd)
    # исчезнувшие факты фрагмента удаляются, если эксперт их не трогал
    for old in db.q("SELECT id, override, user_status FROM cards WHERE chunk_id=? AND origin='auto'",
                    [chunk_id]):
        if old["id"] not in seen and not old["override"] and not old["user_status"]:
            db.ex("DELETE FROM cards WHERE id=?", [old["id"]])
    return len(seen)


async def extract_chunk(project: dict, doc: dict, chunk: dict, ctx: CallCtx) -> None:
    db.upsert("chunks", dict(id=chunk["id"], doc_id=doc["id"], page_from=chunk["page_from"],
                             page_to=chunk["page_to"], status="running", error=None,
                             n_claims=0, has_clinical=None, updated_at=time.time()))
    try:
        res = await call_json("extract", P.EXTRACT_SYSTEM,
                              P.extract_prompt(project["title"], doc.get("biblio") or {}, chunk["text"]),
                              P.EXTRACT_SCHEMA, ctx)
    except LLMError as e:
        db.update("chunks", chunk["id"], status="error", error=str(e), updated_at=time.time())
        db.ex("UPDATE pages SET status='error', reason=? WHERE doc_id=? AND page_no BETWEEN ? AND ?",
              [f"Ошибка обработки: {e}"[:300], doc["id"], chunk["page_from"], chunk["page_to"]])
        return
    claims = res.get("claims") or []
    pages = set(range(chunk["page_from"], chunk["page_to"] + 1))
    for c in claims:  # страницу вне фрагмента поправит проверка цитаты
        if c.get("page") not in pages:
            c["page_note"] = f"модель указала стр. {c.get('page')}, вне фрагмента"
            c["page"] = chunk["page_from"]
    n = _store_cards(project["id"], doc["id"], chunk["id"], claims, ctx.run_id)
    db.update("chunks", chunk["id"], status="done", n_claims=n,
              has_clinical=int(bool(res.get("has_clinical_content"))), updated_at=time.time())
    db.ex("UPDATE pages SET status='done', reason='' WHERE doc_id=? AND page_no BETWEEN ? AND ?",
          [doc["id"], chunk["page_from"], chunk["page_to"]])


async def extract_document(project: dict, doc: dict, chunks: list[dict], ctx: CallCtx,
                           only_failed: bool = False) -> None:
    # фрагменты прошлых планов и их нетронутые экспертом факты удаляются
    current = {ch["id"] for ch in chunks}
    for old in db.q("SELECT id FROM chunks WHERE doc_id=?", [doc["id"]]):
        if old["id"] not in current:
            db.ex("DELETE FROM cards WHERE chunk_id=? AND override IS NULL AND user_status IS NULL", [old["id"]])
            db.ex("DELETE FROM chunks WHERE id=?", [old["id"]])
    todo = []
    for ch in chunks:
        prev = db.q1("SELECT status FROM chunks WHERE id=?", [ch["id"]])
        if only_failed and prev and prev["status"] == "done":
            continue
        todo.append(ch)
    await asyncio.gather(*(extract_chunk(project, doc, ch, ctx) for ch in todo))
