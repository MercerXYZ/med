"""Настройки. Всё, что влияет на результат, пишется в журнал каждого прогона."""
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
SCHEMA_VERSION = "krgen-export/1.1"

# версия промпта входит в ключ кэша
PROMPT_VERSIONS = {
    "biblio": "b3",
    "keywords": "k2",
    "extract": "e7",
    "judge": "j6",
    "group": "gr2",
    "group_merge": "gm2",
    "synth": "s11",
    "narrative": "n1",
    "match": "m2",
}


def _default_cli() -> str | None:
    p = os.environ.get("KRGEN_CLAUDE_CLI") or shutil.which("claude")
    if p:
        return p
    home = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    return str(home) if home.exists() else None


@dataclass
class Settings:
    # --- модель (Claude Agent SDK, вход по подписке, без API-ключа) ---
    model: str = os.environ.get("KRGEN_MODEL", "claude-haiku-5-5")
    judge_model: str = os.environ.get("KRGEN_JUDGE_MODEL", "claude-haiku-5-5")
    cli_path: str | None = field(default_factory=_default_cli)
    force_subscription: bool = True  # удаляет ANTHROPIC_API_KEY из окружения CLI
    concurrency: int = int(os.environ.get("KRGEN_CONCURRENCY", "4"))
    max_retries: int = 2
    call_timeout_s: int = 240
    use_cache: bool = True
    # уровень рассуждений: low | medium | high
    effort: str = os.environ.get("KRGEN_EFFORT", "low")
    judge_effort: str = os.environ.get("KRGEN_JUDGE_EFFORT", "low")

    # --- разбиение документов ---
    chunk_chars: int = 9000          # ~2 страницы статьи на один запрос
    chunk_max_pages: int = 3
    max_pages_per_doc: int = 60      # бюджет на длинные документы (книги, руководства)
    min_page_chars: int = 80         # меньше — страница без текстового слоя

    # --- проверка ---
    quote_exact_threshold: float = 100.0
    quote_fuzzy_threshold: float = 90.0     # >= — цитата найдена (с нормализацией)
    quote_partial_threshold: float = 75.0   # >= — найдена частично, нужна проверка
    judge_batch: int = 15

    # --- группировка ---
    cluster_batch: int = 45          # фактов раздела на один вызов

    # --- тестовый сбой после N запросов ---
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
    """Подхватывает config.json из корня проекта, если он есть."""
    path = path or ROOT / "config.json"
    if path.exists():
        for k, v in json.loads(path.read_text(encoding="utf-8")).items():
            if hasattr(SETTINGS, k):
                setattr(SETTINGS, k, v)


for d in (DATA_DIR, UPLOADS_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)
load_overrides()
