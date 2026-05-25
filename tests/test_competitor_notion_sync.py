from datetime import date

import competitor_tracker.notion_sync as notion_sync
from competitor_tracker.analyzer import CompetitorAlertAnalyzer
from competitor_tracker.models import CandidateArticle, RawArticle
from competitor_tracker.notion_sync import CompetitorNotionMirrorSync


def build_alert_and_schema():
    candidate = CandidateArticle(
        raw_article=RawArticle(
            title="Grab launches driver support campaign in the Philippines",
            url="https://example.com/grab-ph",
            provider="google_news_rss",
            source="Example News",
            published_at="2026-05-19",
            snippet="Driver support programs become part of public messaging.",
        ),
        competitor="Grab / Move It",
        topic_group="marketing + policy narrative",
        score=8,
        region="sea",
        country_hint="Philippines",
        language_hint="en",
        summary="Driver support is becoming part of brand communication.",
    )
    alert = candidate.to_alert(priority="MEDIUM", confidence=0.82)
    schema = CompetitorAlertAnalyzer(use_llm=False).analyze_candidate(candidate)
    return alert, schema


def test_notion_sync_maps_alert_to_properties():
    alert, schema = build_alert_and_schema()
    sync = CompetitorNotionMirrorSync(token="token", database_id="db")

    properties = sync.map_alert_to_properties(alert, schema)

    assert properties["Alert"]["title"][0]["text"]["content"] == alert.headline
    assert properties["Digest Key"]["rich_text"][0]["text"]["content"] == alert.digest_key
    assert properties["Competitor"]["rich_text"][0]["text"]["content"] == "Grab / Move It"
    assert properties["Priority"]["select"]["name"] == "MEDIUM"
    assert properties["Confidence"]["number"] == 0.82
    assert properties["Source URL"]["url"] == "https://example.com/grab-ph"
    assert properties["Status"]["select"]["name"] == "NEW"


def test_notion_sync_uses_resolved_publication_date_from_schema_without_rerunning_resolver(monkeypatch):
    alert, schema = build_alert_and_schema()
    sync = CompetitorNotionMirrorSync(token="token", database_id="db")
    schema["resolved_publication_date"] = date(2026, 5, 18)
    schema["resolved_publication_date_source"] = "html_scraped"

    def fail_if_called(*args, **kwargs):
        raise AssertionError("resolver should not be called when resolved date is already present")

    monkeypatch.setattr(notion_sync, "resolve_final_publication_date", fail_if_called)

    properties = sync.map_alert_to_properties(alert, schema)

    assert properties["Published Date"]["date"]["start"] == "2026-05-18"


def test_notion_sync_falls_back_to_resolver_when_resolved_date_missing(monkeypatch):
    alert, schema = build_alert_and_schema()
    sync = CompetitorNotionMirrorSync(token="token", database_id="db")
    schema.pop("resolved_publication_date", None)
    schema.pop("resolved_publication_date_source", None)

    monkeypatch.setattr(
        notion_sync,
        "resolve_final_publication_date",
        lambda alert_schema, candidate_raw: (date(2026, 5, 17), "provider"),
    )

    properties = sync.map_alert_to_properties(alert, schema)

    assert properties["Published Date"]["date"]["start"] == "2026-05-17"


def test_notion_sync_skips_cleanly_when_env_missing(caplog, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)
    monkeypatch.delenv("COMPETITOR_TRACKER_NOTION_DATABASE_ID", raising=False)
    alert, schema = build_alert_and_schema()
    sync = CompetitorNotionMirrorSync(token="", database_id="")

    stats = sync.sync_alerts([(alert, schema)], dry_run=False)

    assert stats == {"created": 0, "updated": 0, "skipped": 1, "would_create": 0, "would_update": 0}
    assert "Notion mirror skipped" in caplog.text


def test_notion_sync_dry_run_updates_existing_or_creates_new(monkeypatch):
    alert, schema = build_alert_and_schema()
    sync = CompetitorNotionMirrorSync(token="token", database_id="db")

    monkeypatch.setattr(sync, "find_page_by_digest_key", lambda digest_key: "page-1")
    stats = sync.sync_alerts([(alert, schema)], dry_run=True)
    assert stats["would_update"] == 1

    monkeypatch.setattr(sync, "find_page_by_digest_key", lambda digest_key: None)
    stats = sync.sync_alerts([(alert, schema)], dry_run=True)
    assert stats["would_create"] == 1


def test_notion_sync_prefers_competitor_tracker_database_id(monkeypatch):
    monkeypatch.setenv("COMPETITOR_TRACKER_NOTION_DATABASE_ID", "competitor-db")
    monkeypatch.setenv("NOTION_DATABASE_ID", "legacy-db")

    sync = CompetitorNotionMirrorSync(token="token")

    assert sync.database_id == "competitor-db"


def test_notion_sync_warns_when_falling_back_to_legacy_database_id(caplog, monkeypatch):
    monkeypatch.delenv("COMPETITOR_TRACKER_NOTION_DATABASE_ID", raising=False)
    monkeypatch.setenv("NOTION_DATABASE_ID", "legacy-db")

    sync = CompetitorNotionMirrorSync(token="token")

    assert sync.database_id == "legacy-db"
    assert "using legacy NOTION_DATABASE_ID as fallback" in caplog.text
