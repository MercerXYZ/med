"""Сравнительный эксперимент (раздел 3 ТЗ «Обязательное исследование»).

Каждый вариант конфигурации обрабатывает ОДИН и тот же набор документов в
отдельном проекте, затем оценивается по одному эталону. Ресурсы (запросы,
токены, стоимость, время) берутся из журнала вызовов проекта. Варианты фильтра
карточек (all / reviewable / verified) считаются внутри evaluate без новых
вызовов модели.

  python -m krgen.experiment experiments/thinking_vs_gate.json
Спецификация:
  {"name": "...", "question": "...", "gold": "gold/x.json", "docs": ["path.pdf", ...],
   "project": {"title": "...", "age": "mixed", "icd": ["S52.5"], "org": "..."},
   "repeats": 1, "use_cache": true,
   "variants": [{"id": "think0", "settings": {"thinking_budget": 0}}, ...]}
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

from . import db
from .config import DATA_DIR, SETTINGS
from .evaluate import evaluate, write_outputs
from .pdf_ingest import add_document
from .pipeline import run_project
from .projects import create_project


async def run_variant(spec: dict, variant: dict, rep: int) -> dict:
    saved = {k: getattr(SETTINGS, k) for k in variant.get("settings", {})}
    saved["use_cache"] = SETTINGS.use_cache
    try:
        for k, v in variant.get("settings", {}).items():
            setattr(SETTINGS, k, v)
        SETTINGS.use_cache = spec.get("use_cache", True)
        pid = f"exp_{spec['name']}_{variant['id']}_r{rep}"
        for t in ("cards", "chunks", "clusters", "conflicts", "theses", "llm_calls", "runs", "expert_edits"):
            col = "project_id" if t != "chunks" else "doc_id"
            if t == "chunks":
                db.ex("DELETE FROM chunks WHERE doc_id IN (SELECT id FROM documents WHERE project_id=?)", [pid])
            else:
                db.ex(f"DELETE FROM {t} WHERE {col}=?", [pid])
        db.ex("DELETE FROM pages WHERE doc_id IN (SELECT id FROM documents WHERE project_id=?)", [pid])
        db.ex("DELETE FROM documents WHERE project_id=?", [pid])
        pr = spec["project"]
        create_project(pr["title"], pr.get("age", "mixed"), pr["icd"], pr.get("org", ""), pid)
        for d in spec["docs"]:
            add_document(pid, Path(d))
        t0 = time.time()
        run_id = await run_project(pid, use_judge=variant.get("use_judge", True))
        wall = time.time() - t0
        res = await evaluate(pid, Path(spec["gold"]))
        write_outputs(res, DATA_DIR / "eval")
        u = db.q1("SELECT COUNT(*) calls, SUM(cache_hit) hits, SUM(input_tokens) inp, SUM(output_tokens) outp, "
                  "SUM(cache_read_tokens) cread, SUM(cost_usd) cost, SUM(duration_ms) dur, "
                  "SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) errors FROM llm_calls "
                  "WHERE project_id=? AND run_id=?", [pid, run_id])
        return dict(variant=variant["id"], rep=rep, project=pid, wall_s=round(wall, 1), usage=u,
                    gates={g: dict(n=v["n_system"], **v["overall"], quote_verifiability=v["quote_verifiability"],
                                   semantic_support=v["semantic_support"], duplicate_rate=v["duplicate_rate"])
                           for g, v in res["gates"].items()})
    finally:
        for k, v in saved.items():
            setattr(SETTINGS, k, v)


def _fmt(v, pct=True):
    if v is None:
        return "—"
    return f"{v * 100:.1f}%" if pct else f"{v:.3f}"


def report(spec: dict, results: list[dict]) -> str:
    L = [f"# Эксперимент: {spec['name']}", "", f"**Вопрос:** {spec.get('question', '')}", "",
         f"Эталон: `{spec['gold']}`; документов: {len(spec['docs'])}; модель: {SETTINGS.model}; "
         f"повторов: {spec.get('repeats', 1)}; кэш: {'вкл' if spec.get('use_cache', True) else 'выкл'}", "",
         "| Вариант | Фильтр | Карточек | P | R | F1 | Цитаты на стр. | Смысл (LLM) | Повторы | Запросы | Вых. токены | $ (экв.) | Время, с |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    by_var: dict[str, list[dict]] = {}
    for r in results:
        by_var.setdefault(r["variant"], []).append(r)
    for vid, rs in by_var.items():
        for g in ("all", "reviewable", "verified"):
            def agg(key):
                vals = [r["gates"][g][key] for r in rs if r["gates"][g][key] is not None]
                if not vals:
                    return "—"
                m = statistics.mean(vals)
                sd = f" ± {statistics.stdev(vals) * 100:.1f}" if len(vals) > 1 else ""
                return f"{m * 100:.1f}%{sd}"
            u = rs[0]["usage"]
            L.append(f"| {vid} | {g} | {rs[0]['gates'][g]['n']} | {agg('precision')} | {agg('recall')} | {agg('f1')} | "
                     f"{agg('quote_verifiability')} | {agg('semantic_support')} | {agg('duplicate_rate')} | "
                     f"{u['calls']} ({u['hits'] or 0} кэш) | {u['outp'] or 0:,} | {(u['cost'] or 0):.3f} | "
                     f"{rs[0]['wall_s']} |")
    L += ["", "Precision — строгая (лишние карточки, отсутствующие в эталоне, считаются ошибкой до экспертной "
              "проверки — см. *_extras_for_review.csv). Стоимость — эквивалент по прейскуранту API, фактически "
              "расходуются лимиты подписки.", ""]
    return "\n".join(L)


async def main_async(spec_path: Path):
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    results = []
    for rep in range(spec.get("repeats", 1)):
        for v in spec["variants"]:
            print(f"→ {v['id']} (повтор {rep + 1})", flush=True)
            results.append(await run_variant(spec, v, rep))
    out = DATA_DIR / "experiments"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (out / f"{spec['name']}_{stamp}.json").write_text(json.dumps(dict(spec=spec, settings=SETTINGS.to_json(),
                                                                      results=results), ensure_ascii=False,
                                                                 indent=1, default=str), encoding="utf-8")
    md = out / f"{spec['name']}_{stamp}.md"
    md.write_text(report(spec, results), encoding="utf-8")
    print(md)
    print(md.read_text(encoding="utf-8"))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main_async(Path(sys.argv[1])))


if __name__ == "__main__":
    main()
