"""Онтология предметной области — kr_recommendation.schema.json.
Каждый этап получает в промпт нужную часть схемы, а схемы ответов модели собираются из её полей."""
from __future__ import annotations

import json

from .config import ROOT

SCHEMA = json.loads((ROOT / "kr_recommendation.schema.json").read_text(encoding="utf-8"))
DEFS = SCHEMA["$defs"]


def resolve(node):
    """Подставляет $ref из $defs."""
    if isinstance(node, list):
        return [resolve(x) for x in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].split("/")[-1]
        return resolve({**DEFS[name], **{k: v for k, v in node.items() if k != "$ref"}})
    return {k: resolve(v) for k, v in node.items() if k != "$comment"}


def enum(name: str) -> list:
    return DEFS[name]["enum"]


def part(name: str, keys: list[str] | None = None) -> dict:
    """Определение из онтологии; keys — только эти поля."""
    d = resolve(DEFS[name])
    if keys:
        d["properties"] = {k: d["properties"][k] for k in keys}
        d["required"] = [k for k in d.get("required", []) if k in keys]
    return d


def prompt(**parts: dict) -> str:
    """Блок для системного промпта: части онтологии в виде JSON Schema."""
    return ("Онтология предметной области (фрагмент kr_recommendation.schema.json). "
            "Соблюдай типы, перечисления и ограничения полей:\n"
            + json.dumps(parts, ensure_ascii=False))


def _plain(p: dict) -> dict:
    # для схемы ответа: тип, перечисление и описание; null и шаблоны строк модели не нужны
    out = {k: v for k, v in p.items() if k in ("type", "enum", "description", "items", "properties", "required")}
    if isinstance(out.get("type"), list):
        out["type"] = next(t for t in out["type"] if t != "null")
    if "items" in out:
        out["items"] = _plain(out["items"])
    if "properties" in out:
        out["properties"] = {k: _plain(v) for k, v in out["properties"].items()}
    return out


def fields(name: str, keys: list[str]) -> dict:
    """Поля определения для схемы ответа модели."""
    props = resolve(DEFS[name])["properties"]
    return {k: _plain(props[k]) for k in keys}


SECTIONS_ENUM = enum("kr_section_enum")
PHRASES = DEFS["recommendation_thesis"]["properties"]["strength_phrase"]["enum"]
AGE_CATEGORIES = DEFS["kr_meta"]["properties"]["age_category"]["enum"]
ICD_PATTERN = DEFS["kr_meta"]["properties"]["icd_codes"]["items"]["pattern"]
