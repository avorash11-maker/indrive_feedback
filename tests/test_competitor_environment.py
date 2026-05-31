import os

from competitor_tracker.environment import (
    bootstrap_env,
    build_env_preflight_report,
    get_env_value,
    get_optional_env_names,
    get_runtime_env_names,
)


def test_bootstrap_env_loads_nearby_dotenv_without_overriding_existing_values(
    tmp_path, monkeypatch
):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "NEWS_API_KEY=from-dotenv\nGUARDIAN_API_KEY=guardian-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEWS_API_KEY", raising=False)
    monkeypatch.setenv("GUARDIAN_API_KEY", "already-set")

    loaded_path = bootstrap_env()

    assert loaded_path.endswith(".env")
    assert os.getenv("NEWS_API_KEY") == "from-dotenv"
    assert os.getenv("GUARDIAN_API_KEY") == "already-set"


def test_get_env_value_returns_first_non_empty_name(monkeypatch):
    monkeypatch.delenv("COMPETITOR_TRACKER_NOTION_DATABASE_ID", raising=False)
    monkeypatch.setenv("NOTION_DATABASE_ID", "legacy-notion-db")

    assert (
        get_env_value("COMPETITOR_TRACKER_NOTION_DATABASE_ID", "NOTION_DATABASE_ID")
        == "legacy-notion-db"
    )


def test_get_optional_env_names_lists_canonical_keys_and_aliases():
    names = get_optional_env_names()

    assert names["telegram_bot_token"] == ("TELEGRAM_BOT_TOKEN",)
    assert names["notion_database_id"] == (
        "COMPETITOR_TRACKER_NOTION_DATABASE_ID",
        "NOTION_DATABASE_ID",
    )


def test_get_runtime_env_names_lists_supported_runtime_overrides():
    names = get_runtime_env_names()

    assert names["config_path"] == ("COMPETITOR_TRACKER_CONFIG_PATH",)
    assert names["gdelt_min_request_interval_seconds"] == (
        "COMPETITOR_TRACKER_GDELT_MIN_REQUEST_INTERVAL_SECONDS",
    )
    assert names["gdelt_cooldown_seconds"] == (
        "COMPETITOR_TRACKER_GDELT_COOLDOWN_SECONDS",
    )
    assert names["gdelt_rate_limit_state_path"] == (
        "COMPETITOR_TRACKER_GDELT_RATE_LIMIT_STATE_PATH",
    )
    assert names["newsapi_cache_path"] == ("COMPETITOR_TRACKER_NEWSAPI_CACHE_PATH",)
    assert names["guardian_budget_path"] == ("COMPETITOR_TRACKER_GUARDIAN_BUDGET_PATH",)


def test_build_env_preflight_report_requires_only_selected_delivery_env(monkeypatch):
    monkeypatch.setattr(
        "competitor_tracker.environment.bootstrap_env",
        lambda: "",
    )
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("COMPETITOR_TRACKER_NOTION_DATABASE_ID", raising=False)
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    report = build_env_preflight_report(telegram_delivery=True, notion_sync=False)

    assert report["ok"] is False
    assert report["required_missing"] == [
        {
            "key": "telegram_bot_token",
            "required_for": "Telegram delivery",
            "expected_names": "TELEGRAM_BOT_TOKEN",
        },
        {
            "key": "telegram_chat_id",
            "required_for": "Telegram delivery",
            "expected_names": "TELEGRAM_CHAT_ID",
        },
    ]


def test_build_env_preflight_report_accepts_legacy_notion_fallback_with_warning(monkeypatch):
    monkeypatch.setattr(
        "competitor_tracker.environment.bootstrap_env",
        lambda: "",
    )
    monkeypatch.setenv("NOTION_TOKEN", "token")
    monkeypatch.delenv("COMPETITOR_TRACKER_NOTION_DATABASE_ID", raising=False)
    monkeypatch.setenv("NOTION_DATABASE_ID", "legacy-db")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    report = build_env_preflight_report(notion_sync=True)

    assert report["ok"] is True
    assert any("legacy NOTION_DATABASE_ID fallback" in item for item in report["warnings"])
