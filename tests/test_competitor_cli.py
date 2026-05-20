from pathlib import Path

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
