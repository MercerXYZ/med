"""Хранилище проекта (SQLite). У фактов и тезисов два слоя: auto (ответ модели) и override (правки эксперта).
Если после правки модель выдала новую версию, она ждёт решения эксперта в pending_auto."""
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
  status TEXT, reason TEXT, text TEXT,
  PRIMARY KEY(doc_id, page_no));

CREATE TABLE IF NOT EXISTS chunks(
  id TEXT PRIMARY KEY, doc_id TEXT, page_from INTEGER, page_to INTEGER,
  status TEXT, error TEXT, n_claims INTEGER, updated_at REAL);

-- факт (evidence_unit онтологии); section — раздел КР от этапа classify
CREATE TABLE IF NOT EXISTS cards(
  id TEXT PRIMARY KEY, project_id TEXT, doc_id TEXT, chunk_id TEXT,
  auto TEXT, override TEXT, pending_auto TEXT, section TEXT,
  verification TEXT, status TEXT, user_status TEXT,
  run_id TEXT, created_at REAL, updated_at REAL);

CREATE TABLE IF NOT EXISTS fact_groups(
  id TEXT PRIMARY KEY, project_id TEXT, section TEXT, statement TEXT,
  card_ids TEXT, override TEXT, excluded INTEGER DEFAULT 0, run_id TEXT);

CREATE TABLE IF NOT EXISTS conflicts(
  id TEXT PRIMARY KEY, project_id TEXT, section TEXT, side_a TEXT, side_b TEXT,
  kind TEXT, description TEXT, decision TEXT, decided_by TEXT, reason TEXT,
  run_id TEXT, created_at REAL);

-- тезис (recommendation_thesis онтологии) или абзац описательного раздела
CREATE TABLE IF NOT EXISTS theses(
  id TEXT PRIMARY KEY, project_id TEXT, section TEXT, group_id TEXT,
  auto TEXT, override TEXT, pending_auto TEXT, evidence_ids TEXT,
  status TEXT, provenance TEXT, review_flags TEXT, orphan INTEGER DEFAULT 0,
  run_id TEXT, created_at REAL, updated_at REAL);

CREATE TABLE IF NOT EXISTS expert_edits(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, entity TEXT,
  entity_id TEXT, action TEXT, before TEXT, after TEXT, comment TEXT, ts REAL);

CREATE TABLE IF NOT EXISTS llm_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, run_id TEXT,
  purpose TEXT, model TEXT, cache_key TEXT, cache_hit INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cost_usd REAL, duration_ms INTEGER, attempt INTEGER, error TEXT, ts REAL);

CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, project_id TEXT, started_at REAL, finished_at REAL,
  status TEXT, stage TEXT, progress TEXT, config TEXT, log TEXT, error TEXT);

CREATE INDEX IF NOT EXISTS ix_cards_project ON cards(project_id);
CREATE INDEX IF NOT EXISTS ix_cards_chunk ON cards(chunk_id);
CREATE INDEX IF NOT EXISTS ix_llm_run ON llm_calls(run_id);
"""

JSON_COLS = {"icd_codes", "keywords", "meta", "biblio", "auto", "override", "pending_auto", "verification",
             "card_ids", "evidence_ids", "review_flags", "before", "after", "config", "progress", "log",
             "side_a", "side_b"}


def conn() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            _CONN = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.executescript(SCHEMA)
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


def _dec(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_COLS & d.keys():
        if isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass  # в журнале правок бывают простые строки
    return d


def _enc(v: Any) -> Any:
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def q(sql: str, params: Iterable = ()) -> list[dict]:
    with _LOCK:
        return [_dec(r) for r in conn().execute(sql, tuple(params)).fetchall()]


def q1(sql: str, params: Iterable = ()) -> dict | None:
    with _LOCK:
        return _dec(conn().execute(sql, tuple(params)).fetchone())


def ex(sql: str, params: Iterable = ()) -> None:
    with _LOCK:
        conn().execute(sql, tuple(_enc(p) for p in params))


def upsert(table: str, row: dict, key: tuple = ("id",)) -> None:
    cols = list(row)
    sets = ",".join(f"{c}=excluded.{c}" for c in cols if c not in key)
    ex(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
       f"ON CONFLICT({','.join(key)}) DO UPDATE SET {sets}", [row[c] for c in cols])


def update(table: str, id_: str, **fields) -> None:
    if fields:
        ex(f"UPDATE {table} SET {','.join(f'{k}=?' for k in fields)} WHERE id=?", [*fields.values(), id_])


def log_edit(project_id: str, entity: str, entity_id: str, action: str,
             before: Any = None, after: Any = None, comment: str = "") -> None:
    ex("INSERT INTO expert_edits(project_id,entity,entity_id,action,before,after,comment,ts) VALUES(?,?,?,?,?,?,?,?)",
       [project_id, entity, entity_id, action, before, after, comment, time.time()])


def effective(row: dict) -> dict:
    """Итоговое содержимое: ответ модели, раздел от классификации, правки эксперта."""
    out = dict(row.get("auto") or {})
    if row.get("section"):
        out["kr_section"] = row["section"]
    out.update(row.get("override") or {})
    return out
