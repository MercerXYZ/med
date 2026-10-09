"""Оценка качества извлечения по ручному эталону (раздел 8 ТЗ).

Сопоставление: для каждого эталонного утверждения LLM-судья выбирает среди
кандидатов (карточки того же документа, отобранные по лексическому сходству)
то, что выражает ТО ЖЕ утверждение (действие/факт, группа пациентов,
направление, существенные условия). Расхождение в дозе, пороге, возрасте или
отрицании — не совпадение (near miss, считается ошибкой). Затем строится
сопоставление «один к одному» (венгерский алгоритм), поэтому перефразы не
увеличивают число найденных эталонных утверждений.

Варианты (gate) оцениваются на ОДНОМ наборе сопоставлений без новых вызовов:
  all        — все извлечённые карточки (базовый вариант без верификации);
  reviewable — verified + needs_review;
  verified   — только то, что попадает в подтверждённую часть черновика.

Запуск:
  python -m krgen.evaluate <project_id> gold/<file>.json [--extras-review extras.csv]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from pathlib import Path

import numpy as np
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

from . import db, prompts as P
from .config import DATA_DIR
from .llm import CallCtx, LLMError, call_json

SECTION_CAT = {"1": "background", "2": "diagnosis", "3": "treatment", "4": "rehabilitation",
               "5": "prevention", "6": "organization", "7": "other"}
CAT_RU = {"background": "Краткая информация", "diagnosis": "Диагностика", "treatment": "Лечение",
          "rehabilitation": "Реабилитация", "prevention": "Профилактика", "organization": "Организация",
          "other": "Прочее", "negative": "Отрицательные утверждения", "conditional": "Условные утверждения"}
GATES = {
    "all": lambda c: True,
    "reviewable": lambda c: c["status"] in ("verified", "needs_review") or c["user_status"] == "confirmed",
    "verified": lambda c: c["status"] == "verified" or c["user_status"] == "confirmed",
    # абляция: та же верификация БЕЗ LLM-рецензента — только детерминированные проверки
    "deterministic_only": lambda c: c["det_ok"],
    "quote_only": lambda c: c["quote_found"],
}


def _cat(section: str | None) -> str:
    return SECTION_CAT.get((section or "7").split(".")[0], "other")


def load_gold(path: Path) -> dict:
    g = json.loads(path.read_text(encoding="utf-8"))
    for d in g["documents"]:
        for i, c in enumerate(d["claims"]):
            c.setdefault("id", f"{Path(d['filename']).stem[:20]}_{i + 1}")
            c.setdefault("category", _cat(c.get("kr_section")))
    return g


def _system_cards(project_id: str, doc_id: str, pages: list[int] | None) -> list[dict]:
    out = []
    for r in db.q("SELECT * FROM cards WHERE project_id=? AND doc_id=?", [project_id, doc_id]):
        c = db.effective(r)
        v = r.get("verification") or {}
        q = v.get("quote") or {}
        # in_scope — карточка со страниц, размеченных в эталоне. Кандидатами для полноты служат
        # все карточки документа: утверждение аннотации часто извлекается из раздела «Результаты».
        out.append(dict(id=r["id"], in_scope=not pages or c.get("page") in pages,
                        statement=c.get("statement_ru"), page=c.get("page"),
                        section=c.get("kr_section"), polarity=c.get("polarity"),
                        age=c.get("age_group"), dose=c.get("dose"), timing=c.get("timing"),
                        status=r["status"], user_status=r.get("user_status"), merged_into=r.get("merged_into"),
                        quote_ok=q.get("status") in ("exact", "fuzzy"),  # страница в выгрузке = найденная страница
                        quote_found=q.get("status") in ("exact", "fuzzy"),
                        det_ok=all(k["ok"] or k["severity"] == "info" for k in v.get("checks") or []) and
                        bool(v.get("checks")),
                        judge=(v.get("judge") or {}).get("entailment")))
    return out


async def _match_one(gold: dict, cands: list[dict], ctx: CallCtx) -> dict:
    if not cands:
        return dict(match_id="", near_miss_id="", reason="нет кандидатов")
    items = [dict(id=c["id"], statement=c["statement"], page=c["page"], age=c["age"], polarity=c["polarity"],
                  dose=c["dose"], timing=c["timing"]) for c in cands]
    prompt = (f"ЭТАЛОН: {json.dumps({k: gold.get(k) for k in ('statement_ru', 'page', 'polarity', 'age_group', 'key_params')}, ensure_ascii=False)}\n"
              f"КАНДИДАТЫ:\n{json.dumps(items, ensure_ascii=False, indent=0)}")
    try:
        return await call_json("match", P.MATCH_SYSTEM, prompt, P.MATCH_SCHEMA, ctx)
    except LLMError as e:
        return dict(match_id="", near_miss_id="", reason=f"ошибка сопоставления: {e}", error=True)


def _assign(gold_ids: list[str], card_ids: list[str], pairs: set[tuple[str, str]]):
    if not gold_ids or not card_ids or not pairs:
        return {}
    gi = {g: i for i, g in enumerate(gold_ids)}
    ci = {c: j for j, c in enumerate(card_ids)}
    m = np.zeros((len(gold_ids), len(card_ids)))
    for g, c in pairs:
        if g in gi and c in ci:
            m[gi[g], ci[c]] = 1
    rows, cols = linear_sum_assignment(-m)
    return {gold_ids[r]: card_ids[c] for r, c in zip(rows, cols) if m[r, c] > 0}


def _prf(tp_p, n_sys, tp_r, n_gold):
    p = tp_p / n_sys if n_sys else None
    r = tp_r / n_gold if n_gold else None
    f = 2 * p * r / (p + r) if p is not None and r is not None and (p + r) else (0.0 if p is not None and r is not None else None)
    return dict(precision=p, recall=r, f1=f)


async def evaluate(project_id: str, gold_path: Path, extras_review: Path | None = None, top_k: int = 8) -> dict:
    gold = load_gold(gold_path)
    ctx = CallCtx(project_id=project_id, run_id="eval_" + time.strftime("%Y%m%d_%H%M%S"))
    docs = {d["filename"]: d for d in db.q("SELECT id, filename FROM documents WHERE project_id=?", [project_id])}
    all_gold, all_cards, raw_pairs, near, match_log = [], [], set(), {}, []
    tasks = []
    for gd in gold["documents"]:
        doc = docs.get(gd["filename"])
        if not doc:
            raise SystemExit(f"Документ эталона не найден в проекте: {gd['filename']}")
        pages = gd.get("pages")
        if isinstance(pages, list) and len(pages) == 2 and pages[0] <= pages[1] and gd.get("pages_is_range", True):
            pages = list(range(pages[0], pages[1] + 1))
        cards = _system_cards(project_id, doc["id"], pages if isinstance(pages, list) else None)
        all_cards += cards
        for g in gd["claims"]:
            g["doc"] = gd["filename"]
            all_gold.append(g)
            ranked = sorted(cards, key=lambda c: -(fuzz.token_set_ratio(g["statement_ru"], c["statement"] or "")
                                                   - 3 * abs((c["page"] or 0) - (g.get("page") or 0))))
            tasks.append((g, ranked[:top_k]))
    results = await asyncio.gather(*(_match_one(g, cands, ctx) for g, cands in tasks))
    for (g, cands), res in zip(tasks, results):
        ids = {c["id"] for c in cands}
        if res.get("match_id") in ids:
            raw_pairs.add((g["id"], res["match_id"]))
        if res.get("near_miss_id") in ids:
            near[g["id"]] = res["near_miss_id"]
        match_log.append(dict(gold_id=g["id"], gold=g["statement_ru"], match=res.get("match_id"),
                              near_miss=res.get("near_miss_id"), reason=res.get("reason")))

    reviewed = {}
    if extras_review and extras_review.exists():
        with open(extras_review, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("expert_valid", "").strip().lower() in ("1", "да", "yes", "true", "+"):
                    reviewed[row["card_id"]] = True
                elif row.get("expert_valid", "").strip():
                    reviewed[row["card_id"]] = False

    card_by_id = {c["id"]: c for c in all_cards}
    out = dict(project_id=project_id, gold_file=gold_path.name, n_gold=len(all_gold),
               evaluated_at=time.strftime("%Y-%m-%dT%H:%M:%S"), gates={}, match_log=match_log)
    for gname, gate in GATES.items():
        gated = [c for c in all_cards if gate(c)]
        cids = [c["id"] for c in gated]
        assign = _assign([g["id"] for g in all_gold], cids, raw_pairs)
        matched_cards = set(assign.values())
        # повторы: карточка совпала с эталоном, уже занятым другой карточкой
        dup_cards = {c for g, c in raw_pairs if c in cids and c not in matched_cards and assign.get(g)}
        # знаменатель точности: карточки размеченных страниц + совпавшие/повторы с других страниц
        cards = [c for c in gated if c["in_scope"] or c["id"] in matched_cards or c["id"] in dup_cards]
        extras = [c for c in cards if c["id"] not in matched_cards and c["id"] not in dup_cards]
        valid_extra = sum(1 for c in extras if reviewed.get(c["id"]) is True)
        unreviewed = sum(1 for c in extras if c["id"] not in reviewed)
        overall = _prf(len(matched_cards), len(cards), len(assign), len(all_gold))
        overall["precision_with_reviewed_extras"] = (
            (len(matched_cards) + valid_extra) / len(cards) if cards else None)
        overall["unreviewed_extras"] = unreviewed
        by_cat = {}
        cats = sorted({g["category"] for g in all_gold} | {_cat(c["section"]) for c in cards})
        for cat in cats + ["negative", "conditional"]:
            if cat in ("negative", "conditional"):
                gsel = [g for g in all_gold if g.get("polarity") == cat]
                csel = [c for c in cards if c["polarity"] == cat]
            else:
                gsel = [g for g in all_gold if g["category"] == cat]
                csel = [c for c in cards if _cat(c["section"]) == cat]
            tp_r = sum(1 for g in gsel if g["id"] in assign)
            tp_p = sum(1 for c in csel if c["id"] in matched_cards)
            m = _prf(tp_p, len(csel), tp_r, len(gsel))
            if not gsel:
                m.update(recall=None, f1=None)
            by_cat[CAT_RU.get(cat, cat)] = dict(n_gold=len(gsel), n_system=len(csel), **m)
        out["gates"][gname] = dict(
            n_system=len(cards), n_matched=len(assign), overall=overall, by_category=by_cat,
            quote_verifiability=(sum(c["quote_ok"] for c in cards) / len(cards)) if cards else None,
            semantic_support=(sum(c["judge"] == "supported" for c in cards if c["judge"]) /
                              max(1, sum(1 for c in cards if c["judge"]))) if cards else None,
            duplicate_rate=(len(dup_cards) + sum(1 for c in cards if c["merged_into"])) / len(cards) if cards else None,
            false_negatives=[dict(id=g["id"], doc=g["doc"], page=g.get("page"), statement=g["statement_ru"],
                                  near_miss=near.get(g["id"]),
                                  near_miss_statement=(card_by_id.get(near.get(g["id"])) or {}).get("statement"))
                             for g in all_gold if g["id"] not in assign],
            extras=[dict(id=c["id"], page=c["page"], status=c["status"], statement=c["statement"],
                         expert_valid=reviewed.get(c["id"])) for c in extras])
    # для интерфейса — основной вариант = verified
    main = out["gates"]["verified"]
    out.update(overall=main["overall"], by_category=main["by_category"],
               quote_verifiability=main["quote_verifiability"], duplicate_rate=main["duplicate_rate"])
    return out


def write_outputs(res: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    pid = res["project_id"]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    j = out_dir / f"{pid}_{stamp}.json"
    j.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / f"{pid}_latest.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    # лишние карточки — на экспертную проверку (не считаются ошибкой автоматически)
    extras_csv = out_dir / f"{pid}_extras_for_review.csv"
    with open(extras_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["card_id", "page", "status", "statement", "expert_valid"])
        for e in res["gates"]["all"]["extras"]:
            w.writerow([e["id"], e["page"], e["status"], e["statement"],
                        "" if e["expert_valid"] is None else ("да" if e["expert_valid"] else "нет")])
    md = out_dir / f"{pid}_{stamp}.md"
    pct = lambda v: "—" if v is None else f"{v * 100:.1f}%"
    L = [f"# Оценка качества: {pid}", "", f"Эталон: `{res['gold_file']}`, эталонных утверждений: **{res['n_gold']}**", "",
         "| Вариант | Карточек | Precision (строгая) | Precision (с проверкой лишних) | Recall | F1 | Проверяемость цитат | Подтверждение смысла | Доля повторов |",
         "|---|---|---|---|---|---|---|---|---|"]
    for g, v in res["gates"].items():
        o = v["overall"]
        L.append(f"| {g} | {v['n_system']} | {pct(o['precision'])} | {pct(o['precision_with_reviewed_extras'])}"
                 f"{' (не проверено ' + str(o['unreviewed_extras']) + ')' if o['unreviewed_extras'] else ''} | "
                 f"{pct(o['recall'])} | {pct(o['f1'])} | {pct(v['quote_verifiability'])} | "
                 f"{pct(v['semantic_support'])} | {pct(v['duplicate_rate'])} |")
    for g, v in res["gates"].items():
        L += ["", f"## По категориям — вариант `{g}`", "", "| Категория | n эталона | n системы | P | R | F1 |", "|---|---|---|---|---|---|"]
        for k, m in v["by_category"].items():
            L.append(f"| {k} | {m['n_gold']} | {m['n_system']} | {pct(m['precision'])} | {pct(m['recall'])} | {pct(m['f1'])} |")
    L += ["", "## Пропущенные эталонные утверждения (вариант verified)", ""]
    for fn in res["gates"]["verified"]["false_negatives"]:
        L.append(f"- [{fn['id']}, стр. {fn['page']}] {fn['statement']}" +
                 (f"  \n  ↳ близкая карточка с ошибкой параметров {fn['near_miss']}: {fn['near_miss_statement']}" if fn['near_miss'] else ""))
    md.write_text("\n".join(L) + "\n", encoding="utf-8")
    return [j, md, extras_csv]


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("gold")
    ap.add_argument("--extras-review", help="CSV с колонкой expert_valid для лишних карточек")
    ap.add_argument("--out", default=str(DATA_DIR / "eval"))
    a = ap.parse_args(argv)
    res = asyncio.run(evaluate(a.project, Path(a.gold), Path(a.extras_review) if a.extras_review else None))
    for p in write_outputs(res, Path(a.out)):
        print(p)
    for g, v in res["gates"].items():
        o = v["overall"]
        print(f"{g:<11} n={v['n_system']:<4} P={o['precision']} R={o['recall']} F1={o['f1']}")


if __name__ == "__main__":
    main()
