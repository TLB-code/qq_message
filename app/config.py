from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
TRUE_VALUES = {"1", "true", "yes", "on"}


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
    deepseek_timeout: int
    deepseek_request_retries: int
    deepseek_max_concurrency: int
    host: str
    port: int
    webhook_debug: bool
    webhook_token: str | None
    web_password: str | None
    auto_summary_enabled: bool
    auto_summary_threshold: int
    special_member_user_id: str | None
    special_member_display_name: str
    voice_archive_enabled: bool
    voice_media_path: Path
    voice_source_root: Path
    voice_ffmpeg_path: str
    napcat_onebot_api_url: str | None
    napcat_onebot_access_token: str | None


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def load_settings() -> Settings:
    load_dotenv()
    database_path = Path(os.getenv("QQ_SUMMARY_DB", BASE_DIR / "data" / "qq_summary.sqlite3"))
    return Settings(
        database_path=database_path,
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY"),
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        deepseek_timeout=max(env_int("DEEPSEEK_TIMEOUT", 180), 30),
        deepseek_request_retries=max(env_int("DEEPSEEK_REQUEST_RETRIES", 2), 0),
        deepseek_max_concurrency=max(min(env_int("DEEPSEEK_MAX_CONCURRENCY", 2), 16), 1),
        host=os.getenv("QQ_SUMMARY_HOST", "127.0.0.1"),
        port=env_int("QQ_SUMMARY_PORT", 8000),
        webhook_debug=env_bool("QQ_SUMMARY_WEBHOOK_DEBUG"),
        webhook_token=os.getenv("QQ_SUMMARY_WEBHOOK_TOKEN") or None,
        web_password=os.getenv("QQ_SUMMARY_WEB_PASSWORD") or None,
        auto_summary_enabled=env_bool("QQ_SUMMARY_AUTO_SUMMARY_ENABLED", True),
        auto_summary_threshold=max(env_int("QQ_SUMMARY_AUTO_SUMMARY_THRESHOLD", 500), 1),
        special_member_user_id=os.getenv("QQ_SUMMARY_SPECIAL_MEMBER_USER_ID") or None,
        special_member_display_name=os.getenv(
            "QQ_SUMMARY_SPECIAL_MEMBER_DISPLAY_NAME",
            "魔女公主♪",
        ),
        voice_archive_enabled=env_bool("QQ_SUMMARY_VOICE_ARCHIVE_ENABLED", True),
        voice_media_path=Path(
            os.getenv(
                "QQ_SUMMARY_VOICE_MEDIA_DIR",
                database_path.parent / "media" / "voice",
            )
        ).expanduser(),
        voice_source_root=Path(
            os.getenv(
                "QQ_SUMMARY_VOICE_SOURCE_ROOT",
                Path.home() / ".config" / "QQ",
            )
        ).expanduser(),
        voice_ffmpeg_path=os.getenv("QQ_SUMMARY_FFMPEG_PATH", "ffmpeg"),
        napcat_onebot_api_url=os.getenv("NAPCAT_ONEBOT_API_URL") or None,
        napcat_onebot_access_token=os.getenv("NAPCAT_ONEBOT_ACCESS_TOKEN") or None,
    )
