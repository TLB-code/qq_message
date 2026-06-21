from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    if not path.is_file():
        return

    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    database_path: Path
    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_model: str
    host: str
    port: int
    webhook_debug: bool


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        database_path=Path(os.getenv("QQ_SUMMARY_DB", BASE_DIR / "data" / "qq_summary.sqlite3")),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY"),
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        host=os.getenv("QQ_SUMMARY_HOST", "127.0.0.1"),
        port=int(os.getenv("QQ_SUMMARY_PORT", "8000")),
        webhook_debug=os.getenv("QQ_SUMMARY_WEBHOOK_DEBUG", "").lower() in {"1", "true", "yes", "on"},
    )
