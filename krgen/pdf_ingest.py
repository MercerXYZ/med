"""Загрузка PDF: текст по страницам, дубликаты, разметка страниц, подсветка цитаты."""
from __future__ import annotations

import hashlib
import re
import shutil
import time
from pathlib import Path

import pymupdf

from . import db
from .config import SETTINGS, UPLOADS_DIR
from .textnorm import norm_hard_with_map, norm_soft

REF_HEADING = re.compile(
    r"^\s*(references|reference list|bibliography|literature cited|"
    r"список литературы|литература|список использованной литературы)\s*:?\s*$",
    re.I | re.M)
REF_LINE = re.compile(
    r"(\b(19|20)\d{2}\s*[;,.:]\s*\d+|et al\.|doi:|https?://doi|\b(19|20)\d{2}\)\s*[;,.]?\s*\d+|"
    r"//\s*[А-ЯA-Z]|\bvol\.|\bpp?\.\s*\d+)", re.I)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def text_fingerprint(pages: list[str]) -> str:
    # одна статья из разных источников: байты разные, текст первых страниц тот же
    t = norm_soft(" ".join(pages[:2]))[:4000]
    return hashlib.sha1(re.sub(r"[^a-zа-я0-9]", "", t).encode()).hexdigest()


def ref_density(text: str) -> float:
    lines = [l for l in text.splitlines() if len(l.strip()) > 15]
    if len(lines) < 6:
        return 0.0
    return sum(bool(REF_LINE.search(l)) for l in lines) / len(lines)


def add_document(project_id: str, src: Path, original_name: str | None = None) -> dict:
    name = original_name or src.name
    sha = sha256_file(src)
    doc_id = "doc_" + sha[:12]
    owner = db.q1("SELECT project_id FROM documents WHERE id=?", [doc_id])
    if owner and owner["project_id"] != project_id:
        # тот же файл в другом проекте — свой id (от doc_id строятся id фактов)
        doc_id += "_" + hashlib.sha1(project_id.encode()).hexdigest()[:6]
    existing = db.q1("SELECT * FROM documents WHERE id=? AND project_id=?", [doc_id, project_id])
    if existing:
        return existing
    dest_dir = UPLOADS_DIR / project_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{doc_id}.pdf"
    if not dest.exists():
        shutil.copyfile(src, dest)

    now = time.time()
    row = dict(id=doc_id, project_id=project_id, sha256=sha, filename=name, path=str(dest),
               n_pages=0, status="queued", status_detail="", biblio={}, duplicate_of=None,
               created_at=now, updated_at=now)
    try:
        pdf = pymupdf.open(dest)
    except Exception as e:  # битый/зашифрованный файл
        row.update(status="error", status_detail=f"Не удалось открыть PDF: {e}")
        db.upsert("documents", row)
        return row

    # без sort=True: сортировка по координатам перемешивает строки двух колонок
    texts = [p.get_text("text") for p in pdf]
    row["n_pages"] = len(texts)

    fp = text_fingerprint(texts)
    for other in db.q("SELECT id, biblio FROM documents WHERE project_id=? AND duplicate_of IS NULL",
                      [project_id]):
        if (other.get("biblio") or {}).get("_fingerprint") == fp:
            row.update(status="duplicate", duplicate_of=other["id"],
                       status_detail=f"Совпадает по тексту с {other['id']} — не обрабатывается повторно")
    row["biblio"] = {"_fingerprint": fp}
    db.upsert("documents", row)

    in_refs = False
    for i, (page, text) in enumerate(zip(pdf, texts), start=1):
        chars = len(text.strip())
        kind, reason = "body", ""
        m = REF_HEADING.search(text)
        if chars < SETTINGS.min_page_chars:
            kind, reason = "no_text", "Нет текстового слоя (скан/рисунок) — OCR не выполнялся"
        elif (in_refs and ref_density(text) > 0.15) or ref_density(text) > 0.45:
            kind, reason = "references", "Список литературы — клинические утверждения не извлекаются"
        else:
            in_refs = False
        if m and kind == "body":
            # литература начинается посреди страницы: текст до заголовка — тело
            in_refs = True
            text = text[: m.start()]
        try:
            label = page.get_label() or ""
        except Exception:
            label = ""
        db.upsert("pages", dict(doc_id=doc_id, page_no=i, chars=chars, kind=kind, relevance=0.0,
                                printed_label=label, status="pending", reason=reason, text=text),
                  key=("doc_id", "page_no"))
    if row["status"] != "duplicate":
        body = db.q1("SELECT COUNT(*) n FROM pages WHERE doc_id=? AND kind='body'", [doc_id])["n"]
        if body == 0:
            row.update(status="unsupported",
                       status_detail="Нет страниц с текстовым слоем: документ не обработан (нужен OCR)")
            db.update("documents", doc_id, status=row["status"], status_detail=row["status_detail"])
    return row


def score_pages(doc_id: str, keywords: list[str]) -> None:
    """Релевантность страницы — число вхождений ключевых слов."""
    kws = [norm_soft(k) for k in keywords if k.strip()]
    for p in db.q("SELECT page_no, text FROM pages WHERE doc_id=? AND kind IN ('body','skipped')", [doc_id]):
        t = norm_soft(p["text"])
        s = sum(t.count(k) for k in kws)
        db.ex("UPDATE pages SET relevance=? WHERE doc_id=? AND page_no=?", [s, doc_id, p["page_no"]])


def plan_pages(doc_id: str) -> list[int]:
    """Страницы в пределах бюджета: короткий документ целиком, у длинного — самые релевантные."""
    db.ex("UPDATE pages SET kind='body', status=CASE WHEN status='skipped' THEN 'pending' ELSE status END, "
          "reason='' WHERE doc_id=? AND kind='skipped'", [doc_id])
    pages = db.q("SELECT page_no, relevance FROM pages WHERE doc_id=? AND kind='body' ORDER BY page_no",
                 [doc_id])
    budget = SETTINGS.max_pages_per_doc
    if len(pages) <= budget:
        return [p["page_no"] for p in pages]
    ranked = sorted((p for p in pages if p["relevance"] > 0), key=lambda p: -p["relevance"])
    no_keywords = not ranked
    if no_keywords:
        # нет ключевых слов (сбой этапа) — первые N страниц
        ranked = pages[:budget]
    chosen = sorted(p["page_no"] for p in ranked[:budget])
    for p in pages:
        if p["page_no"] in chosen:
            continue
        reason = (f"Ключевые слова темы недоступны — взяты первые {budget} стр., эта вне бюджета" if no_keywords
                  else "Нет ключевых слов темы — вне бюджета длинного документа" if p["relevance"] == 0
                  else f"Вне бюджета {budget} стр. на документ (релевантность {p['relevance']:.0f})")
        db.ex("UPDATE pages SET kind='skipped', status='skipped', reason=? WHERE doc_id=? AND page_no=?",
              [reason, doc_id, p["page_no"]])
    return chosen


def make_chunks(doc_id: str, page_nos: list[int]) -> list[dict]:
    """Соседние страницы → фрагменты с маркерами [[PAGE N]]."""
    texts = {p["page_no"]: p["text"] for p in
             db.q("SELECT page_no, text FROM pages WHERE doc_id=?", [doc_id])}
    chunks, cur, size = [], [], 0
    for n in page_nos:
        t = texts[n]
        contiguous = not cur or cur[-1] == n - 1
        if cur and (not contiguous or size + len(t) > SETTINGS.chunk_chars
                    or len(cur) >= SETTINGS.chunk_max_pages):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(n)
        size += len(t)
    if cur:
        chunks.append(cur)
    out = []
    for pg in chunks:
        cid = f"{doc_id}_p{pg[0]}-{pg[-1]}"
        text = "\n".join(f"[[PAGE {n}]]\n{texts[n]}" for n in pg)
        out.append(dict(id=cid, doc_id=doc_id, page_from=pg[0], page_to=pg[-1], text=text))
    return out


def locate_quote_rects(page: pymupdf.Page, quote: str) -> list[pymupdf.Rect]:
    """Прямоугольники слов, покрывающих цитату (нормализация как при проверке)."""
    from rapidfuzz import fuzz

    words = page.get_text("words")
    if not words or not quote:
        return []
    joined, starts = "", []
    for w in words:
        starts.append(len(joined))
        joined += w[4] + " "
    hay, hmap = norm_hard_with_map(joined)
    needle, _ = norm_hard_with_map(quote)
    if not needle or not hay:
        return []
    pos = hay.find(needle)
    if pos >= 0:
        s, e = pos, pos + len(needle)
    else:
        al = fuzz.partial_ratio_alignment(needle, hay)
        if al is None or al.score < SETTINGS.quote_partial_threshold:
            return []
        s, e = al.dest_start, al.dest_end
    a, b = hmap[s], hmap[max(s, e - 1)]
    rects = []
    for i, st in enumerate(starts):
        en = st + len(words[i][4])
        if en > a and st <= b:
            rects.append(pymupdf.Rect(words[i][:4]))
    return rects


def render_page_png(pdf_path: str, page_no: int, quote: str | None = None, zoom: float = 1.6) -> bytes:
    pdf = pymupdf.open(pdf_path)
    page = pdf[page_no - 1]
    if quote:
        for r in locate_quote_rects(page, quote):
            annot = page.add_highlight_annot(r)
            annot.set_colors(stroke=(1, 0.85, 0.1))
            annot.update()
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
