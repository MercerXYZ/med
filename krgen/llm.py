"""Вызов языковой модели через Claude Agent SDK.

* Авторизация — подписка Claude (OAuth-логин Claude Code CLI), не API-ключ:
  ANTHROPIC_API_KEY принудительно убирается из окружения дочернего процесса.
* Модель работает без инструментов (tools=[]) и без пользовательских настроек
  (setting_sources=[]): только текст на входе и JSON по схеме на выходе.
* Каждый вызов журналируется (токены, стоимость, время, ошибки) — отчёт ТЗ.
* Кэш по ключу (модель, версия промпта, текст запроса, схема): повторная
  обработка не тратит лимиты и даёт воспроизводимый результат.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass

from . import db
from .config import CACHE_DIR, PROMPT_VERSIONS, SETTINGS


class LLMError(RuntimeError):
    """Ошибка модели/API. Никогда не превращается в «утверждений не найдено»."""


@dataclass
class CallCtx:
    project_id: str | None = None
    run_id: str | None = None


_sem: asyncio.Semaphore | None = None
_sem_loop = None
_calls_made = 0


def _semaphore() -> asyncio.Semaphore:
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem, _sem_loop = asyncio.Semaphore(SETTINGS.concurrency), loop
    return _sem


def reset_fault_counter() -> None:
    global _calls_made
    _calls_made = 0


def _thinking(budget: int) -> dict:
    return {"type": "enabled", "budget_tokens": max(1024, budget)} if budget else {"type": "disabled"}


def _cache_key(model: str, purpose: str, system: str, prompt: str, schema: dict, budget: int) -> str:
    h = hashlib.sha256()
    for part in (model, purpose, f"thinking={budget}", PROMPT_VERSIONS.get(purpose, "?"), system, prompt,
                 json.dumps(schema, sort_keys=True)):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _log(ctx: CallCtx, purpose: str, model: str, key: str, hit: bool, usage: dict | None,
         cost: float | None, dur_ms: int, attempt: int, error: str | None) -> None:
    u = usage or {}
    db.ex("INSERT INTO llm_calls(project_id,run_id,purpose,model,prompt_version,cache_key,cache_hit,"
          "input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,cost_usd,duration_ms,"
          "attempt,error,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          [ctx.project_id, ctx.run_id, purpose, model, PROMPT_VERSIONS.get(purpose), key, int(hit),
           u.get("input_tokens", 0), u.get("output_tokens", 0),
           u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0),
           cost or 0.0, dur_ms, attempt, error, time.time()])


def _parse_json(text: str | None):
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        try:
            return json.JSONDecoder().raw_decode(text[start:])[0]
        except json.JSONDecodeError:
            return None


async def _sdk_call(model: str, system: str, prompt: str, schema: dict, budget: int):
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    if SETTINGS.force_subscription:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    opts = ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        tools=[],
        setting_sources=[],
        max_turns=4,
        cli_path=SETTINGS.cli_path,
        thinking=_thinking(budget),
        output_format={"type": "json_schema", "schema": schema},
    )
    result = None
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, ResultMessage):
            result = msg
    if result is None:
        raise LLMError("Модель не вернула результат")
    if result.is_error:
        raise LLMError(f"Ошибка API ({result.api_error_status or result.subtype}): "
                       f"{(result.result or '')[:300]}")
    data = result.structured_output
    if data is None:
        data = _parse_json(result.result)
    if data is None:
        raise LLMError("Ответ модели не соответствует JSON-схеме")
    # result.usage описывает только последний ход; полные суммы по всем ходам — в model_usage
    usage = dict(result.usage or {})
    if result.model_usage:
        mu = list(result.model_usage.values())
        usage = dict(input_tokens=sum(m.get("inputTokens", 0) for m in mu),
                     output_tokens=sum(m.get("outputTokens", 0) for m in mu),
                     cache_read_input_tokens=sum(m.get("cacheReadInputTokens", 0) for m in mu),
                     cache_creation_input_tokens=sum(m.get("cacheCreationInputTokens", 0) for m in mu))
    return data, usage, result.total_cost_usd


def _validate(data, schema: dict) -> str | None:
    """Ответ модели должен соответствовать схеме: иначе пустой/обрезанный ответ превратился бы
    в «утверждений не найдено» (запрещено ТЗ) или уронил бы этап на KeyError."""
    import jsonschema
    if not isinstance(data, dict):
        return "ответ не является JSON-объектом"
    err = next(iter(jsonschema.Draft202012Validator(schema).iter_errors(data)), None)
    return f"ответ не соответствует схеме: {err.message[:200]}" if err else None


def _read_cache(cache_file, schema: dict):
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # битый файл кэша — считаем промахом
    return data if _validate(data, schema) is None else None


def _write_cache(cache_file, data) -> None:
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, cache_file)  # атомарно: прерванная запись не оставит битый файл


async def call_json(purpose: str, system: str, prompt: str, schema: dict, ctx: CallCtx,
                    model: str | None = None) -> dict:
    """Структурированный вызов модели с кэшем, проверкой схемы, повторами и журналом."""
    global _calls_made
    model = model or SETTINGS.model
    budget = SETTINGS.judge_thinking_budget if purpose == "judge" else SETTINGS.thinking_budget
    key = _cache_key(model, purpose, system, prompt, schema, budget)
    cache_file = CACHE_DIR / f"{key}.json"
    if SETTINGS.use_cache and cache_file.exists():
        data = _read_cache(cache_file, schema)
        if data is not None:
            _log(ctx, purpose, model, key, True, None, 0.0, 0, 0, None)
            return data

    last_err = None
    async with _semaphore():
        # повторная проверка кэша внутри семафора: одинаковый параллельный запрос мог уже
        # получить ответ, пока этот ждал очереди
        if SETTINGS.use_cache and cache_file.exists():
            data = _read_cache(cache_file, schema)
            if data is not None:
                _log(ctx, purpose, model, key, True, None, 0.0, 0, 0, None)
                return data
        for attempt in range(1, SETTINGS.max_retries + 2):
            t0 = time.time()
            try:
                _calls_made += 1
                if SETTINGS.fail_after_calls is not None and _calls_made > SETTINGS.fail_after_calls:
                    raise LLMError(f"Тестовый сбой API: лимит {SETTINGS.fail_after_calls} запросов "
                                   f"исчерпан (KRGEN_FAIL_AFTER)")
                data, usage, cost = await asyncio.wait_for(
                    _sdk_call(model, system, prompt, schema, budget), timeout=SETTINGS.call_timeout_s)
                bad = _validate(data, schema)
                if bad:
                    _log(ctx, purpose, model, key, False, usage, cost, int((time.time() - t0) * 1000),
                         attempt, bad)
                    last_err = bad
                    continue  # не кэшируем и повторяем
                _log(ctx, purpose, model, key, False, usage, cost, int((time.time() - t0) * 1000),
                     attempt, None)
                _write_cache(cache_file, data)
                return data
            except Exception as e:  # noqa: BLE001 — любые сбои логируются и всплывают
                msg = f"{type(e).__name__}: {e}"
                if isinstance(e, asyncio.TimeoutError):
                    msg = f"Таймаут {SETTINGS.call_timeout_s} с"
                _log(ctx, purpose, model, key, False, None, 0.0, int((time.time() - t0) * 1000),
                     attempt, msg[:500])
                last_err = msg
                if "Тестовый сбой" in msg or "authenticate" in msg.lower():
                    break  # повтор не поможет
                await asyncio.sleep(2 * attempt)
    raise LLMError(last_err or "неизвестная ошибка")
