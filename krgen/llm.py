"""Вызов модели через Claude Agent SDK (подписка, без API-ключа): ответ JSON по схеме, кэш и журнал запросов."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass

import jsonschema

from . import db
from .config import CACHE_DIR, SETTINGS


class LLMError(RuntimeError):
    pass


@dataclass
class CallCtx:
    project_id: str | None = None
    run_id: str | None = None


_sem: asyncio.Semaphore | None = None
_sem_loop = None


def _semaphore() -> asyncio.Semaphore:
    # у каждого asyncio.run свой цикл событий, семафор привязан к нему
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem_loop is not loop:
        _sem, _sem_loop = asyncio.Semaphore(SETTINGS.concurrency), loop
    return _sem


def _cache_key(purpose: str, system: str, prompt: str, schema: dict) -> str:
    raw = json.dumps([SETTINGS.model, SETTINGS.effort, purpose, system, prompt, schema], ensure_ascii=False,
                     sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _log(ctx: CallCtx, purpose: str, key: str, hit: bool, usage: dict | None = None, cost: float = 0.0,
         dur_ms: int = 0, attempt: int = 0, error: str | None = None) -> None:
    u = usage or {}
    db.ex("INSERT INTO llm_calls(project_id,run_id,purpose,model,cache_key,cache_hit,input_tokens,output_tokens,"
          "cache_read_tokens,cost_usd,duration_ms,attempt,error,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          [ctx.project_id, ctx.run_id, purpose, SETTINGS.model, key, int(hit), u.get("input_tokens", 0),
           u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0), cost or 0.0, dur_ms, attempt, error,
           time.time()])


def _validate(data, schema: dict) -> str | None:
    if not isinstance(data, dict):
        return "ответ не является JSON-объектом"
    err = next(iter(jsonschema.Draft202012Validator(schema).iter_errors(data)), None)
    return f"ответ не соответствует схеме: {err.message[:200]}" if err else None


async def _sdk_call(system: str, prompt: str, schema: dict):
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    # без ключа API: CLI работает по подписке
    os.environ.pop("ANTHROPIC_API_KEY", None)
    opts = ClaudeAgentOptions(model=SETTINGS.model, system_prompt=system, tools=[], setting_sources=[], max_turns=4,
                              cli_path=SETTINGS.cli_path, effort=SETTINGS.effort,
                              output_format={"type": "json_schema", "schema": schema})
    result = None
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, ResultMessage):
            result = msg
    if result is None:
        raise LLMError("Модель не вернула результат")
    if result.is_error:
        raise LLMError(f"Ошибка API ({result.api_error_status or result.subtype}): {(result.result or '')[:300]}")
    # в usage только последний ход, суммы по всем ходам — в model_usage
    mu = list((result.model_usage or {}).values())
    usage = dict(input_tokens=sum(m.get("inputTokens", 0) for m in mu),
                 output_tokens=sum(m.get("outputTokens", 0) for m in mu),
                 cache_read_input_tokens=sum(m.get("cacheReadInputTokens", 0) for m in mu))
    return result.structured_output, usage, result.total_cost_usd


async def call_json(purpose: str, system: str, prompt: str, schema: dict, ctx: CallCtx) -> dict:
    key = _cache_key(purpose, system, prompt, schema)
    cache_file = CACHE_DIR / f"{key}.json"
    if SETTINGS.use_cache and cache_file.exists():
        _log(ctx, purpose, key, True)
        return json.loads(cache_file.read_text(encoding="utf-8"))

    error = None
    async with _semaphore():
        for attempt in range(1, SETTINGS.max_retries + 2):
            t0 = time.time()
            try:
                data, usage, cost = await asyncio.wait_for(_sdk_call(system, prompt, schema),
                                                           timeout=SETTINGS.call_timeout_s)
                error = _validate(data, schema)
            except asyncio.TimeoutError:
                data, usage, cost, error = None, None, 0.0, f"Таймаут {SETTINGS.call_timeout_s} с"
            except Exception as e:  # noqa: BLE001 — ошибка CLI или сети
                data, usage, cost, error = None, None, 0.0, f"{type(e).__name__}: {e}"[:500]
            _log(ctx, purpose, key, False, usage, cost, int((time.time() - t0) * 1000), attempt, error)
            if not error:
                cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                return data
            if "authenticate" in error.lower():
                break  # без входа повтор не поможет
            await asyncio.sleep(2 * attempt)
    raise LLMError(error)
