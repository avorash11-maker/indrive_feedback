from pathlib import Path

import pytest

from competitor_tracker import cli


def make_result():
    analysis = type("Analysis", (), {"candidates": [], "dropped_count": 0})()
    runtime = type("Runtime", (), {"database_path": Path("output/competitor_tracker/tracker.db")})()
    return {
        "analysis": analysis,
        "digest": None,
        "alert_schemas": [],
        "article_contexts": [],
        "query_count": 0,
        "candidates_path": Path("output/competitor_tracker/candidates.json"),
        "dropped_articles_path": Path("output/competitor_tracker/dropped_articles.json"),
        "digest_path": Path("output/competitor_tracker/digest.json"),
        "preview_path": Path("output/competitor_tracker/digest_preview.md"),
        "candidates_csv_path": None,
        "summary_path": Path("output/competitor_tracker/run_summary.json"),
        "runtime": runtime,
        "raw_articles_count": 0,
        "telegram_result": None,
        "notion_result": None,
    }


def test_cli_run_dispatches_requested_delivery_modes(monkeypatch, capsys):
    captured = {}

    def fake_run_pipeline(**kwargs):
        captured.update(kwargs)
        return make_result()

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    cli.main(["run", "--to-telegram", "--notion-dry-run", "--days", "14"])

    assert captured["days"] == 14
    assert captured["telegram_mode"] == "send"
    assert captured["notion_mode"] == "dry"
    assert "Competitor tracker completed." in capsys.readouterr().out


def test_cli_dry_run_enables_both_dry_modes(monkeypatch):
    captured = {}

    def fake_run_pipeline(**kwargs):
        captured.update(kwargs)
        return make_result()

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    cli.main(["dry-run"])

    assert captured["telegram_mode"] == "dry"
    assert captured["notion_mode"] == "dry"


def test_cli_send_digest_uses_telegram_mode(monkeypatch):
    captured = {}

    def fake_run_pipeline(**kwargs):
        captured.update(kwargs)
        return make_result()

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    cli.main(["send-digest", "--dry-run"])

    assert captured["telegram_mode"] == "dry"
    assert captured["notion_mode"] is None


def test_cli_sync_notion_uses_notion_mode(monkeypatch):
    captured = {}

    def fake_run_pipeline(**kwargs):
        captured.update(kwargs)
        return make_result()

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    cli.main(["sync-notion"])

    assert captured["telegram_mode"] is None
    assert captured["notion_mode"] == "sync"


def test_cli_backfill_avoids_external_delivery(monkeypatch):
    captured = {}

    def fake_run_pipeline(**kwargs):
        captured.update(kwargs)
        return make_result()

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    cli.main(["backfill", "--days", "30", "--region", "sea"])

    assert captured["days"] == 30
    assert captured["regions"] == ["sea"]
    assert captured["telegram_mode"] is None
    assert captured["notion_mode"] is None


def test_cli_defaults_to_run_for_backward_compatibility(monkeypatch):
    captured = {}

    def fake_run_pipeline(**kwargs):
        captured.update(kwargs)
        return make_result()

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    cli.main(["--days", "10"])

    assert captured["days"] == 10


def test_cli_passes_export_csv_flag(monkeypatch):
    captured = {}

    def fake_run_pipeline(**kwargs):
        captured.update(kwargs)
        return make_result()

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    cli.main(["run", "--export-csv"])

    assert captured["export_csv"] is True


def test_cli_test_provider_prints_structured_diagnostics(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "test_provider",
        lambda **kwargs: {
            "ok": False,
            "provider": kwargs["provider_name"],
            "query_count": len(kwargs["queries"]),
            "error": "network timeout",
            "diagnostics": {"provider": kwargs["provider_name"], "status": "error"},
        },
    )

    cli.main(
        [
            "test-provider",
            "--provider",
            "google_news_rss",
            "--query",
            "Grab launch Philippines",
        ]
    )

    output = capsys.readouterr().out
    assert '"provider": "google_news_rss"' in output
    assert '"status": "error"' in output


def test_test_provider_marks_skipped_provider_as_not_ok_with_warning(monkeypatch):
    class FakeSkippedProvider:
        name = "guardian"

        def fetch_with_diagnostics(self, request):
            return [], {
                "provider": self.name,
                "status": "skipped",
                "queries": [
                    {
                        "provider": self.name,
                        "query": "guardian-content-api",
                        "request_url": "https://content.guardianapis.com/search",
                        "http_status": None,
                        "exception": "GUARDIAN_API_KEY is missing; Guardian provider skipped.",
                        "items_found": 0,
                        "items_after_filter": 0,
                        "status": "skipped",
                    }
                ],
                "items_found": 0,
                "items_after_filter": 0,
                "items_after_global_dedup": 0,
            }

    monkeypatch.setattr(cli, "build_providers", lambda names: [FakeSkippedProvider()])

    result = cli.test_provider(
        provider_name="guardian",
        queries=["inDrive"],
        days=7,
    )

    assert result["ok"] is False
    assert result["skipped"] is True
    assert result["warning"] == "GUARDIAN_API_KEY is missing; Guardian provider skipped."


def test_test_provider_marks_error_status_without_articles_as_not_ok(monkeypatch):
    class FakeErrorProvider:
        name = "gdelt"

        def fetch_with_diagnostics(self, request):
            return [], {
                "provider": self.name,
                "status": "error",
                "queries": [
                    {
                        "provider": self.name,
                        "query": request.queries[0],
                        "request_url": "https://api.gdeltproject.org/api/v2/doc/doc",
                        "http_status": 429,
                        "exception": "rate limit hit [429]",
                        "items_found": 0,
                        "items_after_filter": 0,
                        "status": "error",
                    }
                ],
                "items_found": 0,
                "items_after_filter": 0,
                "items_after_global_dedup": 0,
            }

    monkeypatch.setattr(cli, "build_providers", lambda names: [FakeErrorProvider()])

    result = cli.test_provider(
        provider_name="gdelt",
        queries=["Grab Indonesia"],
        days=1,
    )

    assert result["ok"] is False
    assert result["diagnostics"]["status"] == "error"
    assert result["error"] == "Provider returned no articles and reported an error status."


def test_cli_qa_feeds_prints_stored_feed_health_report(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "run_feed_qa",
        lambda **kwargs: {
            "days": kwargs["days"],
            "feed_count": 1,
            "highest_noise_feed": {"feed_name": "MercoPress"},
            "recommendations": [],
            "feeds": [],
        },
    )

    cli.main(["qa-feeds", "--days", "14", "--min-feed-items", "3", "--limit", "5"])

    output = capsys.readouterr().out
    assert '"days": 14' in output
    assert '"feed_name": "MercoPress"' in output


def test_cli_preflight_prints_readiness_report(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "run_preflight",
        lambda **kwargs: {
            "ok": True,
            "mode": kwargs["mode"],
            "required_missing": [],
            "warnings": [],
            "checks": [],
        },
    )

    cli.main(["preflight", "--mode", "local-dry-run"])

    output = capsys.readouterr().out
    assert '"ok": true' in output
    assert '"mode": "local-dry-run"' in output


def test_cli_preflight_exits_non_zero_when_env_is_not_ready(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "run_preflight",
        lambda **kwargs: {
            "ok": False,
            "mode": kwargs["mode"],
            "required_missing": [{"key": "telegram_bot_token"}],
            "warnings": [],
            "checks": [],
        },
    )

    with pytest.raises(SystemExit, match="1"):
        cli.main(["preflight", "--mode", "github-actions-production"])

    output = capsys.readouterr().out
    assert '"telegram_bot_token"' in output


def test_cli_fail_fast_for_real_telegram_delivery_when_env_is_missing(monkeypatch):
    monkeypatch.setattr(
        cli,
        "build_env_preflight_report",
        lambda **kwargs: {
            "ok": False,
            "required_missing": [
                {
                    "expected_names": "TELEGRAM_BOT_TOKEN",
                    "required_for": "Telegram delivery",
                }
            ],
        },
    )

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN required for Telegram delivery"):
        cli.main(["send-digest"])
