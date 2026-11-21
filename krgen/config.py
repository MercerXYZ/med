"""Настройки. Копия настроек пишется в журнал каждого прогона."""
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


def _default_cli() -> str | None:
    p = os.environ.get("KRGEN_CLAUDE_CLI") or shutil.which("claude")
    if p:
        return p
    home = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    return str(home) if home.exists() else None


@dataclass
class Settings:
    # модель через Claude Agent SDK, вход по подписке
    model: str = os.environ.get("KRGEN_MODEL", "claude-haiku-5-5")
    effort: str = os.environ.get("KRGEN_EFFORT", "low")  # low | medium | high
    cli_path: str | None = field(default_factory=_default_cli)
    concurrency: int = int(os.environ.get("KRGEN_CONCURRENCY", "4"))
    max_retries: int = 2
    call_timeout_s: int = 240
    use_cache: bool = True

    # разбиение документов
    chunk_chars: int = 9000      # примерно 2 страницы статьи на запрос
    chunk_max_pages: int = 3
    max_pages_per_doc: int = 60  # у длинных документов берутся самые релевантные страницы
    min_page_chars: int = 80     # меньше — страница без текстового слоя

    # проверка цитат: сходство в процентах
    quote_fuzzy_threshold: float = 90.0
    quote_partial_threshold: float = 75.0
    judge_batch: int = 15

    # фактов раздела на один запрос группировки
    cluster_batch: int = 45

    def to_json(self) -> dict:
        return asdict(self)


SETTINGS = Settings()

# config.json в корне проекта переопределяет настройки
_local = ROOT / "config.json"
if _local.exists():
    for k, v in json.loads(_local.read_text(encoding="utf-8")).items():
        if hasattr(SETTINGS, k):
            setattr(SETTINGS, k, v)

for d in (DATA_DIR, UPLOADS_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)
