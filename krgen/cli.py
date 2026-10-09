"""Консольный запуск (для пакетной обработки, экспериментов и воспроизводимости).

  python -m krgen.cli init --title "..." --age mixed --icd S52.5 S52.6 --org "АТОР" --id demo
  python -m krgen.cli add demo "Статьи Разбор клинреков"/*.pdf
  python -m krgen.cli run demo [--no-judge] [--only-failed] [--stages extract verify]
  python -m krgen.cli report demo
  python -m krgen.cli export demo out/
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import db
from .pdf_ingest import add_document
from .pipeline import processing_report, run_project
from .projects import create_project


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="krgen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--title", required=True)
    p.add_argument("--age", default="mixed")
    p.add_argument("--icd", nargs="+", required=True)
    p.add_argument("--org", default="")
    p.add_argument("--id")
    p = sub.add_parser("add")
    p.add_argument("project")
    p.add_argument("files", nargs="+")
    p = sub.add_parser("run")
    p.add_argument("project")
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--only-failed", action="store_true")
    p.add_argument("--stages", nargs="+")
    p.add_argument("--docs", nargs="+")
    p = sub.add_parser("report")
    p.add_argument("project")
    p = sub.add_parser("snapshot", help="сохранить снимок результата последнего (или указанного) прогона")
    p.add_argument("project")
    p.add_argument("--run")
    p = sub.add_parser("export")
    p.add_argument("project")
    p.add_argument("out")
    a = ap.parse_args(argv)

    if a.cmd == "init":
        r = create_project(a.title, a.age, a.icd, a.org, a.id)
        print(r["id"])
    elif a.cmd == "add":
        for f in a.files:
            r = add_document(a.project, Path(f))
            print(f"{r['id']}  {r['status']:<11} {r['n_pages']:>5} стр.  {r['filename']}  {r['status_detail']}")
    elif a.cmd == "run":
        rid = asyncio.run(run_project(a.project, doc_ids=a.docs, only_failed=a.only_failed,
                                      stages=a.stages, use_judge=not a.no_judge))
        run = db.q1("SELECT status, error FROM runs WHERE id=?", [rid])
        print(rid, run["status"], run["error"] or "")
    elif a.cmd == "report":
        print(json.dumps(processing_report(a.project), ensure_ascii=False, indent=1, default=str))
    elif a.cmd == "snapshot":
        from .snapshots import take
        rid = a.run or db.q1("SELECT id FROM runs WHERE project_id=? AND status LIKE 'completed%' "
                             "ORDER BY started_at DESC", [a.project])["id"]
        print(take(a.project, rid))
    elif a.cmd == "export":
        from .export import export_all
        for p in export_all(a.project, Path(a.out)):
            print(p)


if __name__ == "__main__":
    main()
