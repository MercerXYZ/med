"""Конфигурация системы. Все параметры, влияющие на результат, собраны здесь и
пишутся в журнал каждого запуска (воспроизводимость, раздел 8 ТЗ)."""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("KRGEN_DATA", ROOT / "data"))
UPLOADS_DIR = DATA_DIR / "uploads"
CACHE_DIR = DATA_DIR / "llm_cache"
DB_PATH = DATA_DIR / "krgen.sqlite"
SCHEMA_VERSION = "krgen-export/1.0"

# Версии промптов входят в ключ кэша: изменение промпта = новый результат, а не
# молчаливое переиспользование старого ответа.
PROMPT_VERSIONS = {
    "biblio": "b3",
    "keywords": "k2",
    "extract": "e7",
    "judge": "j6",
    "cluster": "c4",
    "synth": "s9",
    "match": "m2",
    "merge": "g1",
}


def _default_cli() -> str | None:
    p = os.environ.get("KRGEN_CLAUDE_CLI") or shutil.which("claude")
    if p:
        return p
    home = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    return str(home) if home.exists() else None


@dataclass
class Settings:
    # --- LLM (Claude Agent SDK, авторизация подпиской Claude, не API-ключом) ---
    model: str = os.environ.get("KRGEN_MODEL", "claude-haiku-5-5")
    judge_model: str = os.environ.get("KRGEN_JUDGE_MODEL", "claude-haiku-5-5")
    cli_path: str | None = field(default_factory=_default_cli)
    force_subscription: bool = True  # удаляет ANTHROPIC_API_KEY из окружения CLI
    concurrency: int = int(os.environ.get("KRGEN_CONCURRENCY", "4"))
    max_retries: int = 2
    call_timeout_s: int = 240
    use_cache: bool = True
    # Бюджет размышлений (extended thinking). 0 — выключено. По умолчанию Haiku
    # «думает» на 15–25 тыс. токенов на фрагмент (2–4 мин); для извлечения по
    # жёсткой схеме это дорого. Влияние на качество — предмет эксперимента.
    thinking_budget: int = int(os.environ.get("KRGEN_THINKING", "0"))
    judge_thinking_budget: int = int(os.environ.get("KRGEN_JUDGE_THINKING", "0"))

    # --- Разбиение документов ---
    chunk_chars: int = 9000          # ~2 страницы статьи на один запрос
    chunk_max_pages: int = 3
    max_pages_per_doc: int = 60      # бюджет на длинные документы (книги, руководства)
    min_page_chars: int = 80         # меньше — страница без текстового слоя

    # --- Верификация ---
    quote_exact_threshold: float = 100.0
    quote_fuzzy_threshold: float = 90.0     # >= — цитата найдена (с нормализацией)
    quote_partial_threshold: float = 75.0   # >= — найдена частично, нужна проверка
    judge_batch: int = 15

    # --- Кластеризация ---
    cluster_batch: int = 45

    # --- Тестовый сбой (демонстрационный сценарий ТЗ) ---
    fail_after_calls: int | None = (
        int(os.environ["KRGEN_FAIL_AFTER"]) if os.environ.get("KRGEN_FAIL_AFTER") else None
    )

    def to_json(self) -> dict:
        d = asdict(self)
        d["prompt_versions"] = PROMPT_VERSIONS
        d["schema_version"] = SCHEMA_VERSION
        return d


SETTINGS = Settings()


def load_overrides(path: Path | None = None) -> None:
    """Подхватывает config.json рядом с проектом (если есть)."""
    path = path or ROOT / "config.json"
    if path.exists():
        for k, v in json.loads(path.read_text(encoding="utf-8")).items():
            if hasattr(SETTINGS, k):
                setattr(SETTINGS, k, v)


for d in (DATA_DIR, UPLOADS_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)
load_overrides()
