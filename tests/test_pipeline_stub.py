"""Сквозной тест конвейера и API с подменённой моделью: python tests/test_pipeline_stub.py"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = ROOT / "Статьи Разбор клинреков"
PDFS = [PDF_DIR / "311.pdf", PDF_DIR / "588.pdf"]
TMP = Path(tempfile.mkdtemp(prefix="krgen_test_"))
os.environ["KRGEN_DATA"] = str(TMP)  # до импорта krgen: config создаёт папки при импорте
sys.path.insert(0, str(ROOT))

from krgen import db, llm, prompts as P  # noqa: E402
from krgen.config import SETTINGS  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok, detail="") -> bool:
    RESULTS.append((bool(ok), name, str(detail)))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok or not detail else f" — {detail}"), flush=True)
    return bool(ok)


# ------------------------------------------------------------ заглушка модели ---

TITLES = {"A": ("Open fractures of long bones: antibiotic prophylaxis consensus", 2019, "rct"),
          "B": ("Fractures of the distal end of the radius treated by internal fixation", 1996, "cohort")}
# раздел, вмешательство, направление, возраст, тип, формулировка, вердикт рецензента
CLAIMS = {
    "A": [("3.2", "антибиотикопрофилактика", "positive", "adults", "source_recommendation",
           "Антибиотикопрофилактика снижает риск ранней инфекции у взрослых с открытыми переломами", "supported"),
          ("3.2", "антибиотикопрофилактика", "positive", "adults", "study_finding",
           "Раннее начало антибиотикопрофилактики уменьшает частоту инфекции у взрослых", "supported"),
          ("3.1", "гипсовая иммобилизация", "positive", "adults", "study_finding",
           "Гипсовая иммобилизация улучшает функциональный исход у взрослых", "supported"),
          ("3.1", "гипсовая иммобилизация", "negative", "adults", "study_finding",
           "Гипсовая иммобилизация не улучшает функциональный исход у взрослых", "supported"),
          ("1.3", "", "descriptive", "unspecified", "background_fact",
           "Открытые переломы длинных костей часто осложняются инфекцией", "supported"),
          ("3.2", "местные антибиотики", "positive", "adults", "study_finding",
           "Местные антибиотики дополняют системную профилактику у взрослых", "partial")],
    "B": [("3.2", "антибиотикопрофилактика", "negative", "adults", "study_finding",
           "Антибиотикопрофилактика не снижает риск инфекции у взрослых после остеосинтеза", "supported"),
          ("1.3", "", "descriptive", "unspecified", "background_fact",
           "Переломы дистального отдела лучевой кости часто встречаются у пожилых женщин", "supported"),
          ("2.4", "рентгенография", "positive", "adults", "source_recommendation",
           "Рентгенография позволяет оценить смещение отломков у взрослых", "supported"),
          ("2.4", "рентгенография", "positive", "adults", "study_finding",
           "Рентгенография выявляет внутрисуставной характер перелома у взрослых", "supported"),
          ("3.1", "гипсовая иммобилизация", "positive", "children", "study_finding",
           "Гипсовая иммобилизация эффективна у детей при переломах без смещения", "supported")],
}
SPEC = {c[5]: c for cs in CLAIMS.values() for c in cs}
FIRST: dict[str, str] = {}      # начало первой страницы → роль документа
GROUP_KEY: dict[str, tuple] = {}  # формулировка группы → (вмешательство, направление, возраст)
STATE = {"delay": 0.0, "calls": 0}
POL_RU = {"positive": "эффективно", "negative": "неэффективно", "descriptive": "сведения", "conditional": "условно"}


def _json_after(prompt: str, marker: str):
    return json.loads(prompt[prompt.index(marker) + len(marker):])


def _role_by_title(prompt: str) -> str | None:
    for role, (title, _, _) in TITLES.items():
        if title in prompt:
            return role
    return None


def a_keywords(prompt):
    return {"keywords": ["fracture", "radius", "antibiotic", "перелом", "лучевая кость", "иммобилизация"]}


def a_biblio(prompt):
    role = next((r for snip, r in FIRST.items() if snip in prompt), "A")
    title, year, design = TITLES[role]
    return dict(title=title, authors=["Author A", "Author B"], year=year, journal="Test Journal", doi="",
                pmid="", language="en", study_design=design, design_evidence="", sample_size=0,
                population="взрослые пациенты", is_clinical_guideline=False)


def a_extract(prompt):
    role = _role_by_title(prompt)
    text = re.search(r"<<<\n(.*)\n>>>", prompt, re.S).group(1)
    if role is None or not text.startswith("[[PAGE 1]]"):
        return {"has_clinical_content": False, "claims": []}
    parts = re.split(r"\[\[PAGE (\d+)\]\]\n", text)
    cands, seen = [], set()
    for page, ptext in zip(parts[1::2], parts[2::2]):
        for line in ptext.split("\n"):
            s = line.strip()
            if len(s) >= 45 and not re.search(r"\d", s) and s not in seen:
                seen.add(s)
                cands.append((int(page), s))
    claims = []
    for k, (sec, iv, pol, age, ctype, stmt, _) in enumerate(CLAIMS[role]):
        page, quote = cands[(2 * k) % len(cands)]
        claims.append(dict(
            quote=quote, page=page, statement_ru=stmt, claim_type=ctype, polarity=pol,
            population="дети" if age == "children" else "взрослые пациенты", age_group=age, intervention=iv,
            comparator="", outcome="", conditions="", dose="", timing="", thresholds="",
            intervention_type="diagnostic" if sec.startswith("2") else "descriptive" if sec.startswith("1")
            else "therapeutic", kr_section=sec, topic_relevance="direct", secondary_citation=False))
    return {"has_clinical_content": True, "claims": claims}


def a_judge(prompt):
    out = []
    for it in _json_after(prompt, "Проверь карточки:\n"):
        verdict = SPEC.get(it["statement_ru"], (None,) * 7)[6] or "supported"
        out.append(dict(card_id=it["card_id"], entailment=verdict, numbers_ok=True, negation_ok=True,
                        population_ok=True, claim_type_ok=True,
                        issues=[] if verdict == "supported" else ["Формулировка шире цитаты"]))
    return {"results": out}


def _statement(key: tuple) -> str:
    iv, pol, age = key
    st = f"{iv or 'Сведения о заболевании'}: {POL_RU.get(pol, pol)} ({age})"
    GROUP_KEY[st] = key
    return st


def _opposite(k1: tuple, k2: tuple) -> bool:
    return k1[0] == k2[0] and k1[2] == k2[2] and {k1[1], k2[1]} == {"positive", "negative"}


def a_group(prompt):
    buckets: dict[tuple, list[str]] = {}
    for it in _json_after(prompt, "Факты:\n"):
        buckets.setdefault((it.get("intervention", ""), it.get("polarity"), it.get("age")), []).append(it["id"])
    groups = [dict(statement=_statement(k), card_ids=ids) for k, ids in buckets.items()]
    conflicts = [dict(side_a=buckets[k1], side_b=buckets[k2], kind="direction",
                      description=f"Противоположные выводы: {k1[0]}")
                 for k1 in buckets for k2 in buckets if k1[1] == "positive" and _opposite(k1, k2)]
    return {"groups": groups, "conflicts": conflicts}


def a_group_merge(prompt):
    items = _json_after(prompt, "Группы:\n")
    # группа из одного факта собрана без модели: её формулировка — формулировка факта
    key = {it["id"]: GROUP_KEY.get(it["statement"]) or (lambda s: (s[1], s[2], s[3]))(SPEC[it["statement"]])
           for it in items}
    buckets: dict[tuple, list[dict]] = {}
    for it in items:
        buckets.setdefault(key[it["id"]], []).append(it)
    merge = [[x["id"] for x in b] for b in buckets.values() if len({x["part"] for x in b}) > 1]
    conflicts = [dict(side_a=[x["id"]], side_b=[y["id"]], kind="direction", description="Расхождение между частями")
                 for x in items for y in items
                 if x["part"] != y["part"] and key[x["id"]][1] == "positive" and _opposite(key[x["id"]], key[y["id"]])]
    return {"merge": merge, "conflicts": conflicts}


def a_synth(prompt):
    section = re.search(r"Раздел (\S+) «", prompt).group(1)
    theses = []
    for g in _json_after(prompt, "Группы:\n"):
        specs = [SPEC[f["statement"]] for f in g["facts"]]
        iv = specs[0][1] or "наблюдение"
        neg = all(s[2] == "negative" for s in specs)
        body = f"применение метода «{iv}» у пациентов с переломами с целью улучшения исхода"
        theses.append(dict(
            group_id=g["group_id"], insufficient=False, body=body, body_genitive="применения" + body[10:],
            direction="negative" if neg else "positive", subsection_title="", action=iv,
            target_population="пациенты с переломами", comparator="", outcome="улучшение исхода", mnn="",
            evidence_backed_core=body, expert_additions=[], comment="Данные одного исследования.",
            quality_criterion=f"Выполнена {iv}" if section.startswith("2") else ""))
    return {"theses": theses}


def a_narrative(prompt):
    section = re.search(r"Раздел (\S+) «", prompt).group(1)
    sec = section if section in P.NARRATIVE_SECTIONS else "7"
    paras = []
    for g in _json_after(prompt, "Группы фактов:\n"):
        ids = [f["id"] for f in g["facts"]]
        paras.append(dict(topic="Эпидемиология", insufficient=False, section=sec,
                          paragraph=f"{g['statement']} [{', '.join(ids)}].", fact_ids=ids))
    return {"paragraphs": paras}


ANSWERS = dict(keywords=a_keywords, biblio=a_biblio, extract=a_extract, judge=a_judge, group=a_group,
               group_merge=a_group_merge, synth=a_synth, narrative=a_narrative)


async def fake_call_json(purpose, system, prompt, schema, ctx, model=None):
    if STATE["delay"]:
        await asyncio.sleep(STATE["delay"])
    res = ANSWERS[purpose](prompt)
    err = llm._validate(res, schema)
    if err:
        raise AssertionError(f"заглушка {purpose}: {err}")
    STATE["calls"] += 1
    llm._log(ctx, purpose, model or SETTINGS.model, f"fake_{purpose}_{STATE['calls']}", False,
             {"input_tokens": len(prompt) // 4, "output_tokens": 50}, 0.001, 1, 1, None)
    return res


async def no_sdk(*a, **kw):
    raise RuntimeError("реальный вызов модели в тесте запрещён")


def patch_model():
    llm._sdk_call = no_sdk
    llm.call_json = fake_call_json
    import importlib
    for name in ("extract", "verify", "groups", "synth", "pipeline", "server"):
        importlib.import_module("krgen." + name)
    for name, mod in list(sys.modules.items()):
        if name.startswith("krgen.") and hasattr(mod, "call_json"):
            mod.call_json = fake_call_json


# ------------------------------------------------------------------- проверки ---

DISCLAIMERS = ("не предназначен", "черновик для экспертной", "для экспертной проверки", "предварительно, требует",
               "требует подтверждения экспертом", "требуется решение эксперта", "требует проверки",
               "должен быть проверен", "ассистент")


def part_migration() -> None:
    """База старого формата (без новых колонок) открывается и читается."""
    import sqlite3
    from krgen import resolve
    from krgen.config import DB_PATH
    from krgen.groups import group_statement

    assert db._CONN is None
    con = sqlite3.connect(DB_PATH)
    con.executescript(db.SCHEMA)
    j = lambda v: json.dumps(v, ensure_ascii=False)  # noqa: E731
    now = time.time()
    con.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,?,?)",
                ["prj_old", "Старый проект", "adults", j(["S52.5"]), "", None, now, j({})])
    con.execute("INSERT INTO documents(id, project_id, filename, path, n_pages, status, biblio, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", ["doc_old", "prj_old", "old.pdf", "nope.pdf", 1, "done", j({}), now])
    for cid, pol in (("ev_old1", "positive"), ("ev_old2", "negative")):
        con.execute("INSERT INTO cards(id, project_id, doc_id, auto, status, origin, created_at) VALUES(?,?,?,?,?,?,?)",
                    [cid, "prj_old", "doc_old", j(dict(statement_ru="Старый факт " + pol, polarity=pol, kr_section="3.1",
                                                       quote="old quote")), "verified", "auto", now])
    con.execute("INSERT INTO clusters(id, project_id, section, topic, card_ids, origin) VALUES(?,?,?,?,?,?)",
                ["cl_old", "prj_old", "3.1", "Старая тема", j(["ev_old1", "ev_old2"]), "auto"])
    con.execute("INSERT INTO conflicts(id, project_id, card_ids, description, kind, status, resolution, origin, "
                "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ["cf_old1", "prj_old", j(["ev_old1", "ev_old2"]), "старое", "direction", "resolved",
                 j({"text": "оба верны"}), "auto", now])
    con.execute("INSERT INTO conflicts(id, project_id, card_ids, description, kind, status, origin, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", ["cf_old2", "prj_old", j(["ev_old2", "ev_old1"]), "", "numbers", "open",
                                            "auto", now])
    con.commit()
    con.close()

    cf1 = db.q1("SELECT * FROM conflicts WHERE id='cf_old1'")
    check("миграция: решённое экспертом → оставить оба", (cf1["decision"], cf1["decided_by"], cf1["reason"])
          == ("keep_both", "expert", "оба верны"), cf1)
    cf2 = db.q1("SELECT * FROM conflicts WHERE id='cf_old2'")
    check("миграция: стороны старого противоречия по полярности", resolve.sides(cf2) == (["ev_old1"], ["ev_old2"]))
    check("миграция: формулировка старой группы", group_statement(db.q1("SELECT * FROM clusters")) == "Старая тема")
    resolve.resolve_project("prj_old")
    cf2 = db.q1("SELECT * FROM conflicts WHERE id='cf_old2'")
    check("миграция: старое противоречие нормировано", cf2["side_a"] == ["ev_old1"] and cf2["section"] == "3.1"
          and cf2["decision"] is None and cf2["status"] == "open", cf2)


def part_rules() -> None:
    from krgen import resolve
    f = lambda design: dict(age_group="adults", _design=design, _src=design, kr_section="3.1")  # noqa: E731
    facts = {"x": f("rct"), "y": f("cohort")}
    check("противоречие о разных группах пациентов → оставить оба",
          resolve.decide(["x"], ["y"], "3.1", facts, "population")[0] == "keep_both")
    check("разные оценки в описательном разделе → оставить оба",
          resolve.decide(["x"], ["y"], "1.3", facts, "numbers")[0] == "keep_both"
          and resolve.decide(["x"], ["y"], "3.1", facts, "numbers")[0] == "prefer_a")


def kr_items(model: dict) -> list[dict]:
    return [it for s in model["sections"] for it in s["items"] if it.get("id")]


def part_pipeline() -> str:
    from krgen import export, pipeline, projects, resolve, synth
    from krgen.groups import group_statement, live_facts
    from krgen.pdf_ingest import add_document

    SETTINGS.cluster_batch = 2  # несколько пачек в разделе — проверяется и проход group_merge
    p = projects.create_project("Переломы дистального отдела костей предплечья", "mixed",
                                ["S52.5", "S52.6"], "Тестовая организация")
    pid = p["id"]
    docs = [add_document(pid, f) for f in PDFS]
    for d, role in zip(docs, "AB"):
        first = db.q1("SELECT text FROM pages WHERE doc_id=? AND page_no=1", [d["id"]])["text"]
        FIRST[first[:150]] = role
    check("документы загружены", all(d["status"] == "queued" for d in docs), [d["status"] for d in docs])

    try:
        asyncio.run(pipeline.run_project(pid, stages=["extract", "bogus"]))
        check("неизвестный этап отклоняется", False, "нет ValueError")
    except ValueError:
        check("неизвестный этап отклоняется", True)

    rid = asyncio.run(pipeline.run_project(pid))
    run = db.q1("SELECT * FROM runs WHERE id=?", [rid])
    check("прогон завершён", run["status"] == "completed", f"{run['status']}: {run['error']}")
    check("журнал этапов", {e["stage"] for e in run["log"]} >= set(pipeline.STAGES), sorted({e["stage"] for e in run["log"]}))

    cards = db.q("SELECT * FROM cards WHERE project_id=?", [pid])
    by_status = {}
    for c in cards:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
    n_claims = sum(len(v) for v in CLAIMS.values())
    check("факты извлечены", len(cards) == n_claims, f"{len(cards)} из {n_claims}")
    not_ok = [(db.effective(c)["statement_ru"][:40], c["verification"]["issues"]) for c in cards
              if c["status"] != "verified"]
    check("факты подтверждены проверкой", by_status.get("verified") == n_claims - 1 and by_status.get("needs_review") == 1,
          (by_status, not_ok))
    live = live_facts(pid)
    check("живые факты: без needs_review", len(live) == n_claims - 1, len(live))
    stmt_id = {f["statement_ru"]: i for i, f in live.items()}

    groups = db.q("SELECT * FROM clusters WHERE project_id=?", [pid])
    check("группы созданы", len(groups) == 7, [(g["section"], len(g["card_ids"])) for g in groups])
    check("у групп есть формулировки", all(group_statement(g) for g in groups))
    check("есть группа из нескольких фактов", any(len(g["card_ids"]) > 1 for g in groups))
    check("группа не смешивает направления",
          all(len({live[i].get("polarity") for i in g["card_ids"] if i in live}) == 1 for g in groups))
    abx = {stmt_id[c[5]] for c in CLAIMS["A"][:2]}
    check("одинаковые факты из разных пачек слиты", any(set(g["card_ids"]) == abx for g in groups),
          [g["card_ids"] for g in groups if g["section"] == "3.2"])

    cfs = db.q("SELECT * FROM conflicts WHERE project_id=?", [pid])
    check("противоречия найдены", len(cfs) >= 2, len(cfs))
    check("у противоречий есть стороны", all(c["side_a"] and c["side_b"] for c in cfs))
    rule_ok = all((c["decision"], c["reason"]) == resolve.decide(c["side_a"], c["side_b"], c["section"], live, c["kind"])
                  for c in cfs if c["decided_by"] != "expert")
    check("решения по правилам", rule_ok, [(c["section"], c["decision"], c["reason"]) for c in cfs])
    b_neg = stmt_id[CLAIMS["B"][0][5]]
    cast = {stmt_id[CLAIMS["A"][2][5]], stmt_id[CLAIMS["A"][3][5]]}
    abx_cf = [c for c in cfs if b_neg in c["card_ids"]]
    cast_cf = [c for c in cfs if set(c["card_ids"]) == cast]
    check("УДД 2 против 3 → сильная сторона", abx_cf and all(
        c["decision"] == ("prefer_b" if b_neg in c["side_a"] else "prefer_a") for c in abx_cf),
        [(c["decision"], c["reason"]) for c in abx_cf])
    check("равные доказательства → решение не принято", len(cast_cf) == 1 and cast_cf[0]["decision"] is None
          and cast_cf[0]["status"] == "open", [(c["decision"], c["reason"]) for c in cast_cf])
    excl = resolve.excluded_fact_ids(pid)
    check("исключённые факты", excl == cast | {b_neg}, (len(excl), excl ^ (cast | {b_neg})))

    m = export.kr_model(pid)
    items = kr_items(m)
    theses = [i for i in items if i["type"] == "thesis"]
    texts = [i for i in items if i["type"] == "text"]
    check("КР: тезисы и абзацы", len(theses) == 3 and len(texts) == 1, (len(theses), len(texts)))
    check("КР: факты проигравших сторон не используются", not ({f for i in items for f in i["fact_ids"]} & excl))
    check("КР: тезис со ссылками и уровнями", all(t["refs"] and t["udd"] and t["uur"] and t["grade"] for t in theses),
          [(t["udd"], t["uur"], t["refs"]) for t in theses])
    check("КР: абзац со ссылками", texts and texts[0]["refs"] == [1, 2] and "[1, 2]" in texts[0]["text"]
          and "ev_" not in texts[0]["text"] and "ev_" not in texts[0]["body"], texts and texts[0]["text"])
    check("КР: литература", len(m["references"]) == 2, len(m["references"]))
    check("КР: критерий качества", len(m["criteria"]) == 1, m["criteria"])
    check("КР: свежая после сборки", not projects.kr_is_stale(pid))
    meta = (db.q1("SELECT meta FROM projects WHERE id=?", [pid])["meta"] or {})
    check("время сборки КР записано", meta.get("kr_built_at"))

    out = TMP / "export"
    files = export.export_all(pid, out)
    names = [f.name for f in files]
    check("экспорт: json, kr_recommendation json, md, docx",
          [n.rsplit(".", 1)[1] for n in names] == ["json", "json", "md", "docx"] and all(f.stat().st_size for f in files),
          names)
    krs_val = next(out.glob("*_kr_recommendation.validation.txt")).read_text(encoding="utf-8")
    own_val = [f for f in out.glob("*.validation.txt") if "kr_recommendation" not in f.name][0].read_text(encoding="utf-8")
    check("JSON по схеме kr_recommendation валиден", krs_val.startswith("OK"), krs_val[:500])
    check("полный JSON валиден по своей схеме", own_val.startswith("OK"), own_val[:500])
    md = files[2].read_text(encoding="utf-8")
    from docx import Document
    docx_text = "\n".join(p.text for p in Document(files[3]).paragraphs)
    check("в Markdown и Word нет дисклеймеров и флагов",
          not any(d in (md + docx_text).lower() for d in DISCLAIMERS) and "Числа не найдены" not in md + docx_text)
    check("Markdown: разделы и уровни", "## 3. Лечение" in md and "Уровень убедительности рекомендаций" in md
          and "Нет данных в загруженных источниках." in md and "## Список литературы" in md, md[:300])
    snap = TMP / "snapshots" / rid
    check("снимок прогона", all((snap / f).exists() for f in ("draft.docx", "draft.md", "export.json", "summary.json")))
    summ = json.loads((snap / "summary.json").read_text(encoding="utf-8"))
    check("сводка снимка", summ["n_facts"] == n_claims and summ["n_groups"] == 7 and summ["n_theses"] == 3
          and summ["calls"] > 0 and summ["conflicts"]["open"] == 1, {k: summ[k] for k in
                                                                       ("n_facts", "n_groups", "n_theses", "calls")})

    # решения эксперта и пересборка КР
    cid = cast_cf[0]["id"]
    projects.decide_conflict(cid, "keep_both", "оба верны")
    check("решение эксперта сохранено", db.q1("SELECT decision, decided_by, status FROM conflicts WHERE id=?", [cid])
          == dict(decision="keep_both", decided_by="expert", status="resolved"))
    check("КР устарела после решения", projects.kr_is_stale(pid))
    abx_group = next(g for g in groups if set(g["card_ids"]) == abx)
    projects.edit_group(abx_group["id"], statement="Антибиотикопрофилактика при открытых переломах у взрослых")
    child = next(g for g in groups if stmt_id[CLAIMS["B"][4][5]] in g["card_ids"])
    projects.edit_group(child["id"], excluded=True)
    g2 = db.q1("SELECT * FROM clusters WHERE id=?", [abx_group["id"]])
    check("правка формулировки группы", group_statement(g2).startswith("Антибиотикопрофилактика при")
          and g2["statement"] != group_statement(g2))
    check("решение меняет доказательства КР", cast <= {f["_id"] for fs in synth.kr_evidence(pid).values() for f in fs})
    rid2 = asyncio.run(pipeline.run_project(pid, stages=["resolve", "kr"]))
    run2 = db.q1("SELECT status, error FROM runs WHERE id=?", [rid2])
    check("пересборка КР завершена", run2["status"] == "completed", run2)
    check("КР свежая после пересборки", not projects.kr_is_stale(pid))
    m2 = export.kr_model(pid)
    items2 = kr_items(m2)
    th2 = [i for i in items2 if i["type"] == "thesis"]
    used2 = {f for i in items2 for f in i["fact_ids"]}
    check("КР после решений: оба факта противоречия вошли", cast <= used2 and len(th2) == 4, (len(th2), cast - used2))
    check("исключённая группа не вошла в КР", stmt_id[CLAIMS["B"][4][5]] not in used2)
    check("тезис с отрицательным выводом", any(t["phrase"].startswith("Не рекомендуется") for t in th2),
          [t["phrase"] for t in th2])
    # повторная группировка сохраняет правки эксперта
    asyncio.run(pipeline.run_project(pid, stages=["group", "resolve", "kr"]))
    g3 = db.q1("SELECT * FROM clusters WHERE id=?", [abx_group["id"]])
    ch3 = db.q1("SELECT excluded FROM clusters WHERE id=?", [child["id"]])
    cf3 = db.q1("SELECT decision, decided_by FROM conflicts WHERE id=?", [cid])
    check("перегруппировка: правки групп и решения сохранены",
          g3 and (g3.get("override") or {}).get("statement") and ch3 and ch3["excluded"] == 1
          and cf3 == dict(decision="keep_both", decided_by="expert"), (g3 and g3.get("override"), ch3, cf3))
    return pid


def part_api(pid: str) -> None:
    from fastapi.testclient import TestClient
    from krgen import projects
    from krgen.server import app

    with TestClient(app) as c:
        r = c.get("/")
        check("GET / отдаёт интерфейс без кэша", r.status_code == 200 and "app.js" in r.text
              and r.headers.get("cache-control") == "no-cache")
        check("GET /static/app.js", c.get("/static/app.js").status_code == 200)
        r = c.get("/api/projects")
        check("GET /api/projects", r.status_code == 200 and any(p["id"] == pid for p in r.json())
              and set(r.json()[0]) == {"id", "title", "created_at"}, r.text[:200])
        bad = [dict(title="Второй проект", icd_codes=["bad"]), dict(title="Второй проект", icd_codes=[]),
               dict(title="КР", icd_codes=["S52.5"]), dict(title="Второй проект", icd_codes=5)]
        codes = [c.post("/api/projects", json=dict(b, age_category="adults", developer_org="")).status_code
                 for b in bad]
        check("POST /api/projects: проверка темы и МКБ", codes == [400] * 4, codes)
        r = c.post("/api/projects", json=dict(title="Второй проект", age_category="adults",
                                              icd_codes=["S52.5"], developer_org="Орг"))
        check("POST /api/projects", r.status_code == 200 and r.json()["id"].startswith("prj_"), r.text[:200])
        pid2 = r.json()["id"]

        r = c.get(f"/api/projects/{pid}")
        d = r.json()
        check("GET /api/projects/{pid}", r.status_code == 200 and set(d) == {"project", "counts", "run", "running"}
              and set(d["counts"]) >= {"documents", "facts", "facts_verified", "facts_review", "facts_rejected",
                                       "groups", "conflicts", "conflicts_open", "theses"}
              and d["counts"]["documents"] == 2 and d["counts"]["conflicts_open"] == 0 and d["run"]["status"] == "completed",
              d.get("counts"))
        check("404 для несуществующего проекта", c.get("/api/projects/nope").status_code == 404)

        with open(PDFS[0], "rb") as f:
            r = c.post(f"/api/projects/{pid2}/upload", files=[("files", (PDFS[0].name, f, "application/pdf")),
                                                              ("files", ("notes.txt", b"x", "text/plain"))])
        up = r.json()
        check("POST upload", r.status_code == 200 and up[0]["id"] and up[0]["status"] == "queued"
              and up[1]["status"] == "error", up)

        r = c.get(f"/api/projects/{pid}/documents")
        docs = r.json()
        check("GET documents", r.status_code == 200 and len(docs) == 2 and all(
            set(x) >= {"id", "filename", "n_pages", "status", "status_label", "detail", "facts", "pages", "biblio"}
            for x in docs) and all(x["status_label"] == "обработан" for x in docs), [x["status"] for x in docs])
        doc_id = docs[0]["id"]
        r = c.get(f"/api/docs/{doc_id}/pdf")
        check("GET pdf", r.status_code == 200 and r.content[:4] == b"%PDF")
        check("нет файла PDF → 404", c.get("/api/docs/doc_old/pdf").status_code == 404
              and c.get("/api/docs/doc_old/page/1.png").status_code == 404)

        r = c.get(f"/api/projects/{pid}/facts")
        fl = r.json()
        check("GET facts", r.status_code == 200 and fl["total"] == 11 and set(fl["items"][0]) >= {
            "id", "statement", "section", "status", "user_status", "doc_id", "filename", "page"}, fl.get("total"))
        totals = {s: c.get(f"/api/projects/{pid}/facts", params={"status": s}).json()["total"]
                  for s in ("verified", "needs_review", "unverified", "confirmed", "rejected")}
        check("фильтр фактов по статусу", totals == dict(verified=10, needs_review=1, unverified=0, confirmed=0,
                                                          rejected=0), totals)
        check("фильтры: раздел, документ, поиск, страницы",
              c.get(f"/api/projects/{pid}/facts", params={"section": "3.1"}).json()["total"] == 3
              and c.get(f"/api/projects/{pid}/facts", params={"doc": doc_id}).json()["total"] in (5, 6)
              and c.get(f"/api/projects/{pid}/facts", params={"q": "рентгенография"}).json()["total"] == 2
              and len(c.get(f"/api/projects/{pid}/facts", params={"limit": 4, "offset": 8}).json()["items"]) == 3)
        check("неверный статус фильтра → 400", c.get(f"/api/projects/{pid}/facts", params={"status": "x"}).status_code == 400)

        fid = next(x["id"] for x in fl["items"] if x["section"] == "3.2" and x["status"] == "verified")
        r = c.get(f"/api/facts/{fid}")
        f = r.json()
        check("GET fact", r.status_code == 200 and set(f) >= {
            "id", "statement", "quote", "page", "section", "claim_type", "polarity", "population", "age_group",
            "intervention", "comparator", "outcome", "dose", "timing", "thresholds", "conditions",
            "secondary_citation", "topic_relevance", "status", "user_status", "source", "checks", "judge", "group_id"}
            and f["group_id"] and f["checks"] and all(set(x) == {"ok", "text"} for x in f["checks"])
            and set(f["source"]) >= {"doc_id", "filename", "title", "year", "design", "doi"}, f)
        page = f["page"]
        r = c.get(f"/api/docs/{f['doc_id']}/page/{page}.png", params={"card": fid})
        check("GET page png с подсветкой", r.status_code == 200 and r.content[:4] == b"\x89PNG")
        check("png: страница вне документа → 404", c.get(f"/api/docs/{f['doc_id']}/page/999.png").status_code == 404)
        r = c.patch(f"/api/facts/{fid}", json={"outcome": "снижение инфекций", "comment": "уточнение"})
        check("PATCH fact", r.status_code == 200 and r.json()["outcome"] == "снижение инфекций" and r.json()["edited"])
        before = db.q1("SELECT override FROM cards WHERE id=?", [fid])["override"]
        r = c.patch(f"/api/facts/{fid}", json={"page": "abc"})
        check("PATCH fact: неверный тип → 400 без записи", r.status_code == 400
              and db.q1("SELECT override FROM cards WHERE id=?", [fid])["override"] == before, r.text[:200])
        r = c.post(f"/api/facts/{fid}/status", json={"status": "confirmed"})
        check("POST fact status", r.status_code == 200 and r.json()["user_status"] == "confirmed")
        check("статус факта: проверка значения", c.post(f"/api/facts/{fid}/status", json={"status": "x"}).status_code == 400)
        c.post(f"/api/facts/{fid}/status", json={"status": None})
        check("404 для несуществующего факта", c.get("/api/facts/ev_nope").status_code == 404)

        r = c.get(f"/api/projects/{pid}/groups")
        gs = r.json()
        flat = [g for s in gs["sections"] for g in s["groups"]]
        check("GET groups", r.status_code == 200 and len(flat) == 7 and all(set(g) >= {
            "id", "statement", "auto_statement", "edited", "excluded", "n_facts", "n_sources", "in_conflict", "facts"}
            for g in flat) and all(set(s) >= {"section", "title", "groups"} for s in gs["sections"])
            and any(g["edited"] for g in flat) and any(g["excluded"] for g in flat), len(flat))
        gid = next(g["id"] for g in flat if g["n_facts"] == 2 and g["section"] == "2.4")
        r = c.patch(f"/api/groups/{gid}", json={"statement": "Рентгенография при подозрении на перелом"})
        check("PATCH group", r.status_code == 200 and r.json()["edited"] and r.json()["statement"].startswith("Рентгеногр"))
        r = c.patch(f"/api/groups/{gid}", json={"statement": ""})
        check("PATCH group: вернуть исходную", r.status_code == 200 and not r.json()["edited"])
        check("404 для несуществующей группы", c.patch("/api/groups/gr_nope", json={"excluded": True}).status_code == 404)

        r = c.get(f"/api/projects/{pid}/conflicts")
        cl = r.json()
        check("GET conflicts", r.status_code == 200 and len(cl) >= 2 and all(set(x) >= {
            "id", "section", "kind", "description", "decision", "decided_by", "reason", "side_a", "side_b"} for x in cl)
            and all(set(s) >= {"id", "statement", "udd", "source"} for x in cl for s in x["side_a"] + x["side_b"]),
            cl[:1])
        auto_cf = next(x for x in cl if x["decided_by"] == "auto")
        r = c.post(f"/api/conflicts/{auto_cf['id']}/decision", json={"decision": "exclude", "comment": "оба сомнительны"})
        check("POST conflict decision", r.status_code == 200 and r.json()["decision"] == "exclude"
              and r.json()["decided_by"] == "expert")
        check("решение: проверка значения", c.post(f"/api/conflicts/{auto_cf['id']}/decision",
                                                   json={"decision": "x"}).status_code == 400)
        check("старый проект читается через API", all(
            c.get(f"/api/projects/prj_old/{x}").status_code == 200 for x in ("groups", "conflicts", "kr", "facts")))
        check("404 для несуществующего противоречия",
              c.post("/api/conflicts/cf_nope/decision", json={"decision": "keep_both"}).status_code == 404)

        r = c.get(f"/api/projects/{pid}/kr")
        kr = r.json()
        items = [it for s in kr["sections"] for it in s["items"] if it.get("id")]
        check("GET kr", r.status_code == 200 and set(kr) >= {"title", "meta", "sections", "references", "criteria",
                                                             "stale", "pending_conflicts"}
              and kr["stale"] is True and kr["pending_conflicts"] == 0 and items
              and all(set(s) >= {"key", "title", "level", "items"} for s in kr["sections"]), kr.get("stale"))
        tid = next(it["id"] for it in items if it["type"] == "thesis")
        r = c.patch(f"/api/theses/{tid}", json={"body": "выполнение рентгенографии пациентам", "udd": 3, "uur": "В"})
        th = r.json()
        check("PATCH thesis", r.status_code == 200 and th["udd"] == 3 and th["uur"] == "B" and th["edited"], th)
        check("PATCH thesis: проверка УДД", c.patch(f"/api/theses/{tid}", json={"udd": 9}).status_code == 400)
        check("PATCH thesis: неверный тип → 400", c.patch(f"/api/theses/{tid}", json={"comment": 123}).status_code == 400
              and c.get(f"/api/projects/{pid}/kr").status_code == 200)
        r = c.post(f"/api/theses/{tid}/status", json={"status": "approved"})
        check("POST thesis status", r.status_code == 200 and r.json()["status"] == "approved")
        r = c.post(f"/api/projects/{pid}/theses", json={"section": "3.3", "phrase": "Рекомендуется",
                                                        "body": "обезболивание пациентам", "comment": "экспертное"})
        check("POST expert thesis", r.status_code == 200 and r.json()["id"].startswith("rec_expert_"), r.text[:200])
        r = c.post(f"/api/projects/{pid}/theses", json={"section": "1.1", "phrase": "", "body": "Абзац эксперта"})
        check("POST expert paragraph", r.status_code == 200 and r.json()["type"] == "text", r.text[:200])
        kr = c.get(f"/api/projects/{pid}/kr").json()
        items = [it for s in kr["sections"] for it in s["items"] if it.get("id")]
        check("правки тезиса и экспертные тезисы в КР", any(it["id"] == tid and it["udd"] == 3 for it in items)
              and sum(it.get("provenance") == "manual" for it in items) == 2)

        for fmt, kind in (("docx", "wordprocessingml"), ("md", "markdown"), ("json", "json"), ("kr_schema", "json")):
            r = c.get(f"/api/projects/{pid}/export/{fmt}")
            check(f"GET export/{fmt}", r.status_code == 200 and kind in r.headers.get("content-type", "")
                  and len(r.content) > 100, r.headers.get("content-type"))
        r = c.get(f"/api/projects/{pid}/export/kr_schema_check")
        check("export/kr_schema без ошибок схемы", r.json()["valid"], r.json())
        check("export: неизвестный формат → 404", c.get(f"/api/projects/{pid}/export/pdf").status_code == 404)

        # прогон через API: запуск, повторный запуск → 409, опрос до конца
        STATE["delay"] = 0.05
        r = c.post(f"/api/projects/{pid2}/run", json={"mode": "all"})
        run_id = r.json().get("run_id")
        check("POST run", r.status_code == 200 and run_id and run_id.startswith("run_"), r.text[:200])
        check("POST run во время прогона → 409", c.post(f"/api/projects/{pid2}/run", json={"mode": "kr"}).status_code == 409)
        check("неверный режим → 400", c.post(f"/api/projects/{pid}/run", json={"mode": "x"}).status_code == 400)
        t0 = time.time()
        while time.time() - t0 < 120:
            d = c.get(f"/api/projects/{pid2}").json()
            if not d["running"]:
                break
            time.sleep(0.1)
        STATE["delay"] = 0.0
        check("прогон через API завершён", not d["running"] and d["run"]["id"] == run_id
              and d["run"]["status"] == "completed" and d["counts"]["facts"] == 6, (d["run"], d["counts"]))
        r = c.post(f"/api/projects/{pid2}/upload",
                   files=[("files", ("download", PDFS[1].read_bytes(), "application/octet-stream"))])
        check("upload: PDF без расширения", r.status_code == 200 and r.json()[0]["status"] == "queued"
              and r.json()[0]["filename"] == "download.pdf", r.text[:200])

        r = c.post(f"/api/projects/{pid}/run", json={"mode": "kr"})
        t0 = time.time()
        while time.time() - t0 < 60 and c.get(f"/api/projects/{pid}").json()["running"]:
            time.sleep(0.05)
        kr = c.get(f"/api/projects/{pid}/kr").json()
        check("сборка КР через API снимает «устарела»", r.status_code == 200 and kr["stale"] is False)
        check("утверждённый тезис сохранён после пересборки",
              any(it.get("id") == tid and it["status"] == "approved" for s in kr["sections"] for it in s["items"]))

        r = c.get(f"/api/projects/{pid}/runs")
        runs = r.json()
        check("GET runs", r.status_code == 200 and len(runs) >= 4 and all(
            set(x) >= {"id", "status", "started_at", "finished_at", "summary"} for x in runs)
            and runs[0]["summary"] and runs[0]["summary"]["n_theses"] >= 4, len(runs))
        r = c.get(f"/api/runs/{runs[0]['id']}")
        rd = r.json()
        check("GET run", r.status_code == 200 and set(rd) >= {"run", "summary", "log"} and rd["log"]
              and set(rd.get("files") or []) == {"draft.docx", "draft.md", "export.json"})
        for name in ("draft.docx", "draft.md", "export.json"):
            r = c.get(f"/api/runs/{runs[0]['id']}/file/{name}")
            check(f"GET run file {name}", r.status_code == 200 and len(r.content) > 100)
        check("файл снимка: чужое имя → 404", c.get(f"/api/runs/{runs[0]['id']}/file/summary.json").status_code == 404)
        check("404 для несуществующего прогона", c.get("/api/runs/run_nope").status_code == 404)
    check("КР свежая после API-сборки", not projects.kr_is_stale(pid))


def part_regressions(pid: str) -> None:
    from fastapi.testclient import TestClient
    from krgen import export, pipeline, projects, server, synth
    from krgen.groups import live_facts

    def kr_facts() -> set[str]:
        return {f["_id"] for fs in synth.kr_evidence(pid).values() for f in fs}

    # подтверждённый после группировки факт попадает в КР по кнопке «Собрать КР»
    nr = db.q1("SELECT id FROM cards WHERE project_id=? AND status='needs_review'", [pid])["id"]
    projects.set_card_status(nr, "confirmed")
    check("подтверждённый факт вне групп → КР устарела", projects.kr_is_stale(pid))
    asyncio.run(pipeline.run_project(pid, stages=server.RUN_STAGES["kr"]))
    check("«Собрать КР» включает подтверждённый факт", nr in kr_facts() and not projects.kr_is_stale(pid))

    # у утверждённого тезиса обновляются доказательства после смены решения по противоречию
    stmt = {f["statement_ru"]: i for i, f in live_facts(pid).items()}
    abx = {stmt[x[5]] for x in CLAIMS["A"][:2]}
    b_neg = stmt[CLAIMS["B"][0][5]]
    g = next(g for g in db.q("SELECT * FROM clusters WHERE project_id=?", [pid]) if set(g["card_ids"]) == abx)
    for cf in db.q("SELECT id, card_ids FROM conflicts WHERE project_id=?", [pid]):
        if set(cf["card_ids"]) & (abx | {b_neg}):
            projects.decide_conflict(cf["id"], "keep_both")
    a1 = sorted(abx)[0]
    db.upsert("conflicts", dict(id="cf_test", project_id=pid, section="3.2", card_ids=[a1, b_neg], side_a=[a1],
                                side_b=[b_neg], kind="direction", description="", origin="auto", status="open",
                                created_at=time.time()))
    projects.decide_conflict("cf_test", "prefer_b")
    asyncio.run(pipeline.run_project(pid, stages=["resolve", "kr"]))
    tid = synth.thesis_id(g["id"])
    projects.set_thesis_status(tid, "approved")
    projects.decide_conflict("cf_test", "keep_both")
    asyncio.run(pipeline.run_project(pid, stages=["resolve", "kr"]))
    item = next((it for it in kr_items(export.kr_model(pid)) if it["id"] == tid), {})
    check("утверждённый тезис получает факт, возвращённый решением", a1 in item.get("fact_ids", []), item)

    with TestClient(server.app) as c:
        para = next(it for it in kr_items(export.kr_model(pid)) if it["type"] == "text" and it["provenance"] != "manual")
        r = c.patch(f"/api/theses/{para['id']}", json={"body": "Первое предложение [1]. Второе предложение [2]."})
        text = next(it["text"] for it in kr_items(export.kr_model(pid)) if it["id"] == para["id"])
        check("правка абзаца сохраняет ссылки по предложениям",
              r.status_code == 200 and text == "Первое предложение [1]. Второе предложение [2].", text)

        # отклонённый тезис без правок остаётся отклонённым, когда состав группы меняется
        t2 = next(it["id"] for it in kr_items(export.kr_model(pid)) if str(it["section"]).startswith("2"))
        db.update("theses", t2, override=None, pending_auto=None)
        projects.set_thesis_status(t2, "rejected")
        xray = sorted(i for i, f in live_facts(pid).items() if f.get("kr_section") == "2.4")
        projects.set_card_status(xray[0], "rejected")
        asyncio.run(pipeline.run_project(pid, stages=["group", "resolve", "kr"]))
        row = db.q1("SELECT status, orphan FROM theses WHERE id=?", [t2])
        check("отклонённый тезис переносится на новую группу", row == dict(status="rejected", orphan=0)
              and not any(str(it["section"]).startswith("2") for it in kr_items(export.kr_model(pid))), row)
        kr = c.get(f"/api/projects/{pid}/kr").json()
        shown = [it for s in kr["sections"] for it in s["items"] if it.get("id") == t2]
        check("отклонённый тезис виден в окне 4", shown and shown[0]["status"] == "rejected"
              and shown[0]["refs"] == [] and not kr["criteria"], shown)
        r = c.post(f"/api/theses/{t2}/status", json={"status": "draft"})
        check("отклонённый тезис можно вернуть", r.status_code == 200
              and any(it["id"] == t2 for it in kr_items(export.kr_model(pid))))


def no_disclaimers() -> None:
    files = list((ROOT / "krgen").rglob("*.py")) + list((ROOT / "krgen" / "static").glob("*.*"))
    files += [f for d in ("gold", "schemas", "experiments") for f in (ROOT / d).glob("*.json")]
    files.append(ROOT / "config.example.json")
    bad = []
    for f in files:
        t = f.read_text(encoding="utf-8").lower()
        bad += [f"{f.relative_to(ROOT)}: {d}" for d in DISCLAIMERS if d in t]
    check("нет дисклеймеров", not bad, bad)


def main() -> int:
    if not all(f.exists() for f in PDFS):
        print("Нет тестовых PDF:", [str(f) for f in PDFS if not f.exists()])
        return 2
    patch_model()
    try:
        part_migration()
        part_rules()
        pid = part_pipeline()
        part_api(pid)
        part_regressions(pid)
        no_disclaimers()
    except Exception:  # noqa: BLE001 — падение теста тоже результат
        import traceback
        traceback.print_exc()
        check("тест выполнен без исключений", False)
    finally:
        if db._CONN is not None:
            db._CONN.close()
        shutil.rmtree(TMP, ignore_errors=True)
    failed = [r for r in RESULTS if not r[0]]
    print(f"\nИтого: {len(RESULTS) - len(failed)} из {len(RESULTS)} проверок прошли")
    return 1 if failed else 0


def test_pipeline_stub():
    assert main() == 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
