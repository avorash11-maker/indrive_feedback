"""Environment bootstrap helpers for competitor_tracker."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def bootstrap_env() -> str:
    """Load a nearby `.env` file once and return the resolved path if found."""

    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=False)
        return str(Path(dotenv_path))
    return ""


def get_env_value(*names: str, default: str = "") -> str:
    """Return the first non-empty env value from a canonical name list."""

    for name in names:
        raw_value = os.getenv(name)
        if raw_value is None:
            continue
        normalized = str(raw_value).strip()
        if normalized:
            return normalized
    return default


def get_optional_env_names() -> dict[str, tuple[str, ...]]:
    """Expose canonical env names and accepted fallbacks in one place."""

    return {
        "openai_api_key": ("OPENAI_API_KEY",),
        "openai_model": ("OPENAI_MODEL",),
        "news_api_key": ("NEWS_API_KEY",),
        "guardian_api_key": ("GUARDIAN_API_KEY",),
        "telegram_bot_token": ("TELEGRAM_BOT_TOKEN",),
        "telegram_chat_id": ("TELEGRAM_CHAT_ID",),
        "notion_token": ("NOTION_TOKEN",),
        "notion_database_id": (
            "COMPETITOR_TRACKER_NOTION_DATABASE_ID",
            "NOTION_DATABASE_ID",
        ),
    }
