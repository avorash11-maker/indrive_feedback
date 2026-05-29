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


def get_runtime_env_names() -> dict[str, tuple[str, ...]]:
    """Expose supported runtime env overrides in one place."""

    return {
        "config_path": ("COMPETITOR_TRACKER_CONFIG_PATH",),
        "output_dir": ("COMPETITOR_TRACKER_OUTPUT_DIR",),
        "db_path": ("COMPETITOR_TRACKER_DB_PATH",),
        "lookback_days": ("COMPETITOR_TRACKER_LOOKBACK_DAYS",),
        "min_score": ("COMPETITOR_TRACKER_MIN_SCORE",),
        "use_llm_alerts": ("COMPETITOR_TRACKER_USE_LLM_ALERTS",),
        "llm_top_n": ("COMPETITOR_TRACKER_LLM_TOP_N",),
        "telegram_top_n": ("COMPETITOR_TRACKER_TELEGRAM_TOP_N",),
        "article_context_max_chars": ("COMPETITOR_TRACKER_ARTICLE_CONTEXT_MAX_CHARS",),
        "enable_newsapi_full_run": ("COMPETITOR_TRACKER_ENABLE_NEWSAPI_FULL_RUN",),
        "newsapi_max_queries_per_run": ("COMPETITOR_TRACKER_NEWSAPI_MAX_QUERIES_PER_RUN",),
        "newsapi_daily_request_limit": ("COMPETITOR_TRACKER_NEWSAPI_DAILY_REQUEST_LIMIT",),
        "newsapi_cache_ttl_seconds": ("COMPETITOR_TRACKER_NEWSAPI_CACHE_TTL_SECONDS",),
        "newsapi_cooldown_seconds": ("COMPETITOR_TRACKER_NEWSAPI_COOLDOWN_SECONDS",),
        "newsapi_cache_path": ("COMPETITOR_TRACKER_NEWSAPI_CACHE_PATH",),
        "newsapi_budget_path": ("COMPETITOR_TRACKER_NEWSAPI_BUDGET_PATH",),
        "guardian_max_queries_per_run": ("COMPETITOR_TRACKER_GUARDIAN_MAX_QUERIES_PER_RUN",),
        "guardian_daily_request_limit": ("COMPETITOR_TRACKER_GUARDIAN_DAILY_REQUEST_LIMIT",),
        "guardian_cache_ttl_seconds": ("COMPETITOR_TRACKER_GUARDIAN_CACHE_TTL_SECONDS",),
        "guardian_cooldown_seconds": ("COMPETITOR_TRACKER_GUARDIAN_COOLDOWN_SECONDS",),
        "guardian_cache_path": ("COMPETITOR_TRACKER_GUARDIAN_CACHE_PATH",),
        "guardian_budget_path": ("COMPETITOR_TRACKER_GUARDIAN_BUDGET_PATH",),
        "historical_precision_half_life_days": ("COMPETITOR_TRACKER_HISTORICAL_PRECISION_HALF_LIFE_DAYS",),
    }


def build_env_preflight_report(
    *,
    telegram_delivery: bool = False,
    notion_sync: bool = False,
    require_openai: bool = False,
) -> dict[str, object]:
    """Build a safe readiness report without calling external APIs."""

    loaded_dotenv = bootstrap_env()
    env_names = get_optional_env_names()
    checks: list[dict[str, object]] = []
    required_missing: list[dict[str, str]] = []
    warnings: list[str] = []

    def _record_check(
        *,
        key: str,
        names: tuple[str, ...],
        required_for: str,
        required: bool,
    ) -> None:
        resolved_name = ""
        resolved_value = ""
        for name in names:
            value = get_env_value(name)
            if value:
                resolved_name = name
                resolved_value = value
                break
        status = "present" if resolved_value else ("missing" if required else "optional_missing")
        checks.append(
            {
                "key": key,
                "names": list(names),
                "required": required,
                "required_for": required_for,
                "status": status,
                "resolved_name": resolved_name,
                "configured": bool(resolved_value),
            }
        )
        if required and not resolved_value:
            required_missing.append(
                {
                    "key": key,
                    "required_for": required_for,
                    "expected_names": ", ".join(names),
                }
            )

    _record_check(
        key="telegram_bot_token",
        names=env_names["telegram_bot_token"],
        required_for="Telegram delivery",
        required=telegram_delivery,
    )
    _record_check(
        key="telegram_chat_id",
        names=env_names["telegram_chat_id"],
        required_for="Telegram delivery",
        required=telegram_delivery,
    )
    _record_check(
        key="notion_token",
        names=env_names["notion_token"],
        required_for="Notion mirror sync",
        required=notion_sync,
    )
    _record_check(
        key="notion_database_id",
        names=env_names["notion_database_id"],
        required_for="Notion mirror sync",
        required=notion_sync,
    )
    _record_check(
        key="openai_api_key",
        names=env_names["openai_api_key"],
        required_for="LLM alert enrichment",
        required=require_openai,
    )

    if get_env_value("NOTION_DATABASE_ID") and not get_env_value("COMPETITOR_TRACKER_NOTION_DATABASE_ID"):
        warnings.append(
            "Using legacy NOTION_DATABASE_ID fallback for competitor tracker Notion mirror. "
            "Prefer COMPETITOR_TRACKER_NOTION_DATABASE_ID to keep the new schema isolated."
        )

    if telegram_delivery:
        warnings.append(
            "Telegram delivery readiness only checks local env presence; it does not send a message or validate the bot remotely."
        )
    if notion_sync:
        warnings.append(
            "Notion readiness only checks local env presence; it does not call the Notion API."
        )

    return {
        "ok": not required_missing,
        "dotenv_loaded": loaded_dotenv,
        "required_missing": required_missing,
        "warnings": warnings,
        "checks": checks,
    }
