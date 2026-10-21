"""Консольный запуск: python -m krgen.cli {init,add,run,report,snapshot,export} (справка: -h)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import db
from .pdf_ingest import add_document
from .pipeline import STAGES, processing_report, run_project
from .projects import create_project


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="krgen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="создать проект")
    p.add_argument("--title", required=True)
    p.add_argument("--age", default="mixed", choices=["adults", "children", "mixed"])
    p.add_argument("--icd", nargs="+", required=True)
    p.add_argument("--org", default="")
    p.add_argument("--id")
    p = sub.add_parser("add", help="загрузить PDF")
    p.add_argument("project")
    p.add_argument("files", nargs="+")
    p = sub.add_parser("run", help="обработать (по умолчанию все этапы)")
    p.add_argument("project")
    p.add_argument("--stages", nargs="+", choices=STAGES, help="например: --stages group resolve kr")
    p.add_argument("--docs", nargs="+")
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--only-failed", action="store_true")
    p = sub.add_parser("report", help="отчёт об обработке (JSON)")
    p.add_argument("project")
    p = sub.add_parser("snapshot", help="снимок результата последнего (или указанного) прогона")
    p.add_argument("project")
    p.add_argument("--run")
    p = sub.add_parser("export", help="выгрузить КР: JSON, Markdown, Word")
    p.add_argument("project")
    p.add_argument("out")
    a = ap.parse_args(argv)

    if a.cmd == "init":
        if a.id and db.q1("SELECT 1 x FROM projects WHERE id=?", [a.id]):
            sys.exit(f"Проект {a.id} уже есть")
        try:
            print(create_project(a.title, a.age, a.icd, a.org, a.id)["id"])
        except ValueError as e:
            sys.exit(str(e))
    elif a.cmd == "add":
        for f in a.files:
            r = add_document(a.project, Path(f))
            print(f"{r['id']}  {r['status']:<11} {r['n_pages']:>5} стр.  {r['filename']}  {r.get('status_detail') or ''}")
    elif a.cmd == "run":
        stages = [s for s in STAGES if s in a.stages] if a.stages else None
        try:
            rid = asyncio.run(run_project(a.project, doc_ids=a.docs, only_failed=a.only_failed,
                                          stages=stages, use_judge=not a.no_judge))
        except RuntimeError as e:
            sys.exit(str(e))
        run = db.q1("SELECT status, error FROM runs WHERE id=?", [rid])
        print(rid, run["status"], run["error"] or "")
    elif a.cmd == "report":
        print(json.dumps(processing_report(a.project), ensure_ascii=False, indent=1, default=str))
    elif a.cmd == "snapshot":
        from .snapshots import take
        rid = a.run
        if rid:
            run = db.q1("SELECT project_id FROM runs WHERE id=?", [rid])
            if not run or run["project_id"] != a.project:
                sys.exit(f"Прогон {rid} не найден в проекте {a.project}")
        else:
            last = db.q1("SELECT id FROM runs WHERE project_id=? AND status LIKE 'completed%' "
                         "ORDER BY started_at DESC", [a.project])
            if not last:
                sys.exit("Нет завершённых прогонов")
            rid = last["id"]
        print(take(a.project, rid))
    elif a.cmd == "export":
        from .export import export_all
        for p in export_all(a.project, Path(a.out)):
            print(p)


if __name__ == "__main__":
    main()
