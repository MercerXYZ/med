"""Хранилище проекта (SQLite). У фактов и тезисов два слоя: auto (результат модели) и
override (правки эксперта); новая автоверсия поверх правки ложится в pending_auto."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable

from .config import DB_PATH

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, title TEXT, age_category TEXT, icd_codes TEXT,
  developer_org TEXT, keywords TEXT, created_at REAL, meta TEXT);

CREATE TABLE IF NOT EXISTS documents(
  id TEXT PRIMARY KEY, project_id TEXT, sha256 TEXT, filename TEXT, path TEXT,
  n_pages INTEGER, status TEXT, status_detail TEXT, biblio TEXT,
  duplicate_of TEXT, created_at REAL, updated_at REAL);

CREATE TABLE IF NOT EXISTS pages(
  doc_id TEXT, page_no INTEGER, chars INTEGER, kind TEXT, relevance REAL,
  printed_label TEXT, status TEXT, reason TEXT, text TEXT,
  PRIMARY KEY(doc_id, page_no));

CREATE TABLE IF NOT EXISTS chunks(
  id TEXT PRIMARY KEY, doc_id TEXT, page_from INTEGER, page_to INTEGER,
  status TEXT, error TEXT, n_claims INTEGER, has_clinical INTEGER, updated_at REAL);

CREATE TABLE IF NOT EXISTS cards(
  id TEXT PRIMARY KEY, project_id TEXT, doc_id TEXT, chunk_id TEXT,
  auto TEXT, override TEXT, pending_auto TEXT,
  verification TEXT, status TEXT, user_status TEXT,
  merged_into TEXT, origin TEXT, run_id TEXT, created_at REAL, updated_at REAL);

CREATE TABLE IF NOT EXISTS clusters(
  id TEXT PRIMARY KEY, project_id TEXT, section TEXT, topic TEXT,
  card_ids TEXT, duplicate_groups TEXT, origin TEXT, run_id TEXT);

CREATE TABLE IF NOT EXISTS conflicts(
  id TEXT PRIMARY KEY, project_id TEXT, card_ids TEXT, description TEXT,
  kind TEXT, status TEXT, resolution TEXT, origin TEXT, run_id TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS theses(
  id TEXT PRIMARY KEY, project_id TEXT, section TEXT, cluster_id TEXT,
  auto TEXT, override TEXT, pending_auto TEXT, evidence_ids TEXT,
  status TEXT, provenance TEXT, review_flags TEXT, orphan INTEGER DEFAULT 0,
  run_id TEXT, created_at REAL, updated_at REAL);

CREATE TABLE IF NOT EXISTS expert_edits(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, entity TEXT,
  entity_id TEXT, action TEXT, before TEXT, after TEXT, comment TEXT, ts REAL);

CREATE TABLE IF NOT EXISTS llm_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, run_id TEXT,
  purpose TEXT, model TEXT, prompt_version TEXT, cache_key TEXT, cache_hit INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cache_write_tokens INTEGER, cost_usd REAL, duration_ms INTEGER,
  attempt INTEGER, error TEXT, ts REAL);

CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, project_id TEXT, started_at REAL, finished_at REAL,
  status TEXT, stage TEXT, progress TEXT, config TEXT, log TEXT, error TEXT);
"""

MIGRATIONS = [
    "ALTER TABLE theses ADD COLUMN merged_into TEXT",
    "ALTER TABLE cards ADD COLUMN merge_lock TEXT",
    "UPDATE cards SET merge_lock=user_status, user_status=NULL WHERE user_status IN ('merged','unmerged')",
    "CREATE INDEX IF NOT EXISTS ix_cards_project ON cards(project_id)",
    "CREATE INDEX IF NOT EXISTS ix_cards_doc ON cards(doc_id)",
    "CREATE INDEX IF NOT EXISTS ix_cards_chunk ON cards(chunk_id)",
    "CREATE INDEX IF NOT EXISTS ix_cards_merged ON cards(merged_into)",
    "CREATE INDEX IF NOT EXISTS ix_theses_project ON theses(project_id)",
    "CREATE INDEX IF NOT EXISTS ix_documents_project ON documents(project_id)",
    "CREATE INDEX IF NOT EXISTS ix_chunks_doc ON chunks(doc_id)",
    "CREATE INDEX IF NOT EXISTS ix_clusters_project ON clusters(project_id)",
    "CREATE INDEX IF NOT EXISTS ix_conflicts_project ON conflicts(project_id)",
    "CREATE INDEX IF NOT EXISTS ix_llm_run ON llm_calls(run_id)",
    "CREATE INDEX IF NOT EXISTS ix_llm_project ON llm_calls(project_id)",
    "ALTER TABLE clusters ADD COLUMN statement TEXT",
    "ALTER TABLE clusters ADD COLUMN override TEXT",
    "ALTER TABLE clusters ADD COLUMN excluded INTEGER DEFAULT 0",
    "ALTER TABLE conflicts ADD COLUMN side_a TEXT",
    "ALTER TABLE conflicts ADD COLUMN side_b TEXT",
    "ALTER TABLE conflicts ADD COLUMN decision TEXT",
    "ALTER TABLE conflicts ADD COLUMN decided_by TEXT",
    "ALTER TABLE conflicts ADD COLUMN reason TEXT",
    "ALTER TABLE conflicts ADD COLUMN section TEXT",
    # прежнее «разрешено экспертом» без выбора стороны = оставить оба
    "UPDATE conflicts SET reason=json_extract(resolution, '$.text') "
    "WHERE status='resolved' AND decided_by IS NULL AND reason IS NULL",
    "UPDATE conflicts SET decision='keep_both', decided_by='expert' WHERE status='resolved' AND decided_by IS NULL",
]


def conn() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            _CONN = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.executescript(SCHEMA)
            for ddl in MIGRATIONS:  # уже применённые падают с OperationalError
                try:
                    _CONN.execute(ddl)
                except sqlite3.OperationalError:
                    pass
        return _CONN


@contextmanager
def tx():
    with _LOCK:
        c = conn()
        c.execute("BEGIN")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


JSON_COLS = {
    "icd_codes", "keywords", "meta", "biblio", "auto", "override", "pending_auto",
    "verification", "card_ids", "duplicate_groups", "evidence_ids", "review_flags",
    "before", "after", "config", "progress", "log", "resolution", "side_a", "side_b",
}


def _dec(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k, v in d.items():
        if k in JSON_COLS and isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except json.JSONDecodeError:
                pass
    return d


def _enc(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def q(sql: str, params: Iterable = ()) -> list[dict]:
    with _LOCK:
        return [_dec(r) for r in conn().execute(sql, tuple(params)).fetchall()]


def q1(sql: str, params: Iterable = ()) -> dict | None:
    with _LOCK:
        return _dec(conn().execute(sql, tuple(params)).fetchone())


def ex(sql: str, params: Iterable = ()) -> None:
    with _LOCK:
        conn().execute(sql, tuple(_enc(p) for p in params))


def upsert(table: str, row: dict, key: str | tuple = "id") -> None:
    cols = list(row)
    keys = (key,) if isinstance(key, str) else key
    sets = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
    sql = (f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
           f"ON CONFLICT({','.join(keys)}) DO " + (f"UPDATE SET {sets}" if sets else "NOTHING"))
    ex(sql, [row[c] for c in cols])


def update(table: str, id_: str, **fields) -> None:
    if not fields:
        return
    sets = ",".join(f"{k}=?" for k in fields)
    ex(f"UPDATE {table} SET {sets} WHERE id=?", [*fields.values(), id_])


def log_edit(project_id: str, entity: str, entity_id: str, action: str,
             before: Any = None, after: Any = None, comment: str = "") -> None:
    ex("INSERT INTO expert_edits(project_id,entity,entity_id,action,before,after,comment,ts) "
       "VALUES(?,?,?,?,?,?,?,?)",
       [project_id, entity, entity_id, action, before, after, comment, time.time()])


def effective(row: dict) -> dict:
    out = dict(row.get("auto") or {})
    out.update(row.get("override") or {})
    return out
