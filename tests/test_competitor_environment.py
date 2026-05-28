import os

from competitor_tracker.environment import bootstrap_env, get_env_value, get_optional_env_names


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
